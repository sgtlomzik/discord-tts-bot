// The session document -> what the viewer needs (no DOM, covered by tests/web).
//
// Positions are seconds into the session's audio mix. Lines are in time
// order, which is not always mix order (overlapping speech is played one
// utterance after another), so "what is playing" is found by position over
// a separate index, never by walking the lines.

import { assignColors } from './palette.js';
import { displayName } from './format.js';

/** How long a TTS line (which has no audio of its own) stays "current". */
export const TTS_HOLD = 2.5;

const finite = (v) => typeof v === 'number' && Number.isFinite(v);

/**
 * Normalise a session document.
 * @returns {{
 *   speakers: {id:string,name:string|null,label:string,idx:number,color:string,speech:number,lines:number}[],
 *   lines: object[], hasPositions: boolean, duration: number, timeline: object[]}}
 */
export function buildModel(doc) {
  const hasPositions = !!doc.audio && doc.audio.status === 'ready';
  const participants = doc.participants || [];
  const lines = doc.lines || [];

  const ids = [...participants.map((p) => p.user_id), ...lines.map((l) => l.user_id)];
  const colors = assignColors(ids);
  const speakers = [];
  const index = new Map();
  const speaker = (id, name) => {
    if (!index.has(id)) {
      index.set(id, speakers.length);
      speakers.push({ id, name: name ?? null, label: displayName(name, id), idx: speakers.length,
                      color: colors.get(id), speech: 0, lines: 0 });
    } else if (name && !speakers[index.get(id)].name) {
      const s = speakers[index.get(id)];
      s.name = name;
      s.label = displayName(name, id);
    }
    return speakers[index.get(id)];
  };
  for (const p of participants) {
    const s = speaker(p.user_id, p.name);
    s.speech = p.speech_seconds || 0;
    s.lines = (p.voice_lines || 0) + (p.tts_lines || 0);
  }

  const out = lines.map((l, i) => {
    const sp = speaker(l.user_id, l.name);
    const raw = l.segments && l.segments.length ? l.segments
      : [{ at: l.at, until: l.until, text: l.text, position: l.position, position_end: l.position_end }];
    const segs = raw.map((s) => {
      const start = hasPositions && finite(s.position) ? s.position : null;
      let end = start;
      if (start !== null) end = finite(s.position_end) && s.position_end > start ? s.position_end : start + (l.kind === 'tts' ? 0 : 1);
      return { text: s.text ?? '', at: s.at ?? l.at, pos: start, end };
    });
    const timed = segs.filter((s) => s.pos !== null);
    return {
      index: i, speaker: sp.idx, kind: l.kind === 'tts' ? 'tts' : 'voice', at: l.at, until: l.until ?? l.at,
      text: l.text ?? segs.map((s) => s.text).join(' '), segs, exact: l.exact !== false,
      pos: timed.length ? timed[0].pos : null, end: timed.length ? timed[timed.length - 1].end : null,
    };
  });

  // Voice lines first on equal positions: a TTS line spoken "at" the start of
  // an utterance does not steal it.
  const timeline = [];
  for (const line of out) {
    line.segs.forEach((seg, k) => {
      if (seg.pos !== null) timeline.push({ line: line.index, seg: k, kind: line.kind, pos: seg.pos, end: seg.end });
    });
  }
  timeline.sort((a, b) => a.pos - b.pos || (a.kind === 'tts' ? -1 : 1) - (b.kind === 'tts' ? -1 : 1));

  const duration = hasPositions && finite(doc.audio.duration) ? doc.audio.duration
    : timeline.reduce((m, e) => Math.max(m, e.end), 0);
  return { speakers, lines: out, hasPositions, duration, timeline };
}

/**
 * What is playing at time `t`: the last segment that has started; during the
 * 0.5 s pause between utterances the previous one stays current. A TTS line
 * is current for TTS_HOLD seconds, then the voice line it interrupted again.
 * @returns {{line:number, seg:number, frac:number, lineFrac:number, kind:string}|null}
 */
export function activeAt(model, t) {
  const tl = model.timeline;
  let lo = 0;
  let hi = tl.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (tl[mid].pos <= t) lo = mid + 1; else hi = mid;
  }
  let i = lo - 1;
  if (i < 0) return null;
  if (tl[i].kind === 'tts' && t - tl[i].pos >= TTS_HOLD) {
    while (i >= 0 && tl[i].kind === 'tts') i--;
    if (i < 0) return null;
  }
  const e = tl[i];
  const span = e.end - e.pos;
  const frac = e.kind === 'voice' && span > 0 ? Math.min(1, Math.max(0, (t - e.pos) / span)) : 0;

  // progress through the whole line: finished segments + the current one
  const segs = model.lines[e.line].segs;
  let total = 0;
  let done = 0;
  segs.forEach((s, k) => {
    const d = s.pos === null ? 0 : Math.max(0, s.end - s.pos);
    total += d;
    if (k < e.seg) done += d;
    else if (k === e.seg) done += d * frac;
  });
  return { line: e.line, seg: e.seg, frac, lineFrac: total > 0 ? Math.min(1, done / total) : 0, kind: e.kind };
}

/** Per speaker: merged [start, end] blocks of speech and TTS tick positions, for the ribbon. */
export function lanes(model, mergeGap = 0.75) {
  const result = model.speakers.map((sp) => ({ speaker: sp, blocks: [], ticks: [] }));
  for (const e of model.timeline) {
    const lane = result[model.lines[e.line].speaker];
    if (e.kind === 'tts') { lane.ticks.push(e.pos); continue; }
    const last = lane.blocks[lane.blocks.length - 1];
    if (last && e.pos - last[1] <= mergeGap) last[1] = Math.max(last[1], e.end);
    else lane.blocks.push([e.pos, e.end]);
  }
  return result;
}

/**
 * Group lines into consecutive sections of `step` audio seconds (or, without
 * audio, of wall-clock time since `startedAt`). Sections never repeat: a line
 * that is out of mix order stays in the section it follows.
 */
export function sections(model, { step = 300, startedAt = 0 } = {}) {
  const result = [];
  let current = null;
  let key = -1;
  for (const line of model.lines) {
    const base = line.pos !== null ? line.pos : Math.max(0, (line.at ?? startedAt) - startedAt);
    const k = Math.floor(base / step);
    if (!current || k > key) {
      key = Math.max(key, k);
      current = { key, start: key * step, end: (key + 1) * step, lines: [], share: model.speakers.map(() => 0), firstAt: line.at };
      result.push(current);
    }
    current.lines.push(line.index);
    if (line.kind === 'voice') {
      for (const s of line.segs) if (s.pos !== null) current.share[line.speaker] += Math.max(0, s.end - s.pos);
    }
  }
  if (result.length && model.duration > 0) {
    const last = result[result.length - 1];
    last.end = Math.min(last.end, Math.max(last.start + 1, model.duration));
  }
  return result;
}

/** Indices of lines whose text contains the query. */
export function findHits(model, query) {
  const q = (query || '').trim().toLowerCase();
  if (!q) return [];
  return model.lines.filter((l) => l.text.toLowerCase().includes(q)).map((l) => l.index);
}

/** Nice axis step (seconds) for a ribbon of `duration` seconds: at most ~`maxTicks` labels. */
export function axisStep(duration, maxTicks = 8) {
  const steps = [15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400];
  return steps.find((s) => duration / s <= maxTicks) ?? 14400;
}
