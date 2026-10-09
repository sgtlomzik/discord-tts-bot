// One opened recording: wires the card (date, retention, people), the player,
// the ribbon and the transcript together.

import { api, waitForMp3 } from './api.js';
import { esc } from './dom.js';
import { dateParts, left as timeLeft, pos as fmtPos, posTenths, split, urgency } from './format.js';
import { Player } from './player.js';
import { Ribbon } from './ribbon.js';
import { Transcript } from './transcript.js';
import { activeAt, buildModel, lanes } from './timeline.js';
import { copyText, explain, toast } from './ui.js';

const POLL_MS = 30000;
const RETRY_MS = 60000;
const pad = (n) => String(n).padStart(2, '0');
const $ = (id) => document.getElementById(id);

export class Viewer {
  /** @param {{tz:string, keepSeconds:number, onTitle:(doc:object)=>void}} opts */
  constructor({ tz, keepSeconds, onTitle }) {
    this.tz = tz;
    this.keepSeconds = keepSeconds;
    this.onTitle = onTitle;
    this.doc = null;
    this.model = null;
    this.id = null;
    this.startAt = 0;
    this.pending = null;
    this.query = '';
    this.kind = 'all';
    this.hiddenSpeakers = new Set();

    this.player = new Player($('audio-el'));
    this.ribbon = new Ribbon($('ribbon'), {
      onSeek: (t) => this.seek(t),
      describe: (t) => this._describe(t),
    });
    this.transcript = new Transcript($('scroll'), $('jump'), {
      onSeek: (t) => this.seek(t),
      onFollow: (on) => this._followState(on),
      onViewport: (from, to) => this.ribbon.setViewport(from, to),
    });

    this.player.addEventListener('tick', () => this._tick());
    this.player.addEventListener('state', () => this._state());
    $('play').addEventListener('click', () => this.player.toggle());
    $('b10').addEventListener('click', () => this.seek(this.player.time - 10));
    $('f10').addEventListener('click', () => this.seek(this.player.time + 10));
    $('rate').addEventListener('click', () => this.player.cycleRate());
    $('pill').addEventListener('click', () => this.transcript.setFollow(!this.transcript.following));
    $('mp3').addEventListener('click', () => this._downloadMp3());
    $('link').addEventListener('click', () => this._copyLink());

    const q = $('q');
    q.addEventListener('input', () => this._search(q.value));
    $('hn').addEventListener('click', () => this._hit(1));
    $('hp').addEventListener('click', () => this._hit(-1));
    $('chips').addEventListener('click', (e) => this._chip(e));

    setInterval(() => this._countdown(), 1000);
    this._setupMediaSession();
  }

  // -- opening ------------------------------------------------------------

  async open(id, { t = 0 } = {}) {
    this.abort?.abort();
    clearTimeout(this.pollTimer);
    const controller = new AbortController();
    this.abort = controller;
    this.id = id;
    this.startAt = t;
    this.pending = null;
    document.body.classList.add('loading');
    try {
      const doc = await api.session(id, { signal: controller.signal });
      if (this.id !== id) return;
      this._apply(doc, { startAt: t });
      this._schedulePoll();
    } catch (error) {
      if (error.name === 'AbortError') return;
      this._failed(error);
    } finally {
      if (this.id === id) document.body.classList.remove('loading');
    }
  }

  _failed(error) {
    this.doc = null;
    this.model = null;
    this.player.load(null);
    $('title').textContent = 'Не удалось открыть запись';
    $('sub').textContent = explain(error);
    $('scroll').innerHTML = `<div class="empty">${esc(explain(error))}</div>`;
    this.ribbon.setData({ duration: 0, lanes: [], message: '' });
    toast(explain(error), { error: true });
  }

  _schedulePoll() {
    clearTimeout(this.pollTimer);
    const doc = this.doc;
    if (!doc) return;
    const live = doc.status === 'recording' || doc.status === 'transcribing';
    const broken = doc.audio?.status === 'error';
    if (!live && !broken) return;
    this.pollTimer = setTimeout(() => this._refresh(), broken && !live ? RETRY_MS : POLL_MS);
  }

  /** Re-read a session that is still changing; never interrupts playback. */
  async _refresh() {
    const id = this.id;
    try {
      const doc = await api.session(id);
      if (this.id !== id) return;
      if (this.player.playing) this.pending = doc;
      else this._apply(doc, { keepTime: true });
    } catch { /* the next poll will try again */ }
    this._schedulePoll();
  }

  // -- rendering ----------------------------------------------------------

  _apply(doc, { startAt = 0, keepTime = false } = {}) {
    const fresh = !keepTime;
    this.doc = doc;
    const model = buildModel(doc);
    this.model = model;
    this.pending = null;
    if (fresh) this.hiddenSpeakers = new Set();

    const date = dateParts(doc.started_at, this.tz);
    const channel = doc.channel?.name || `канал …${String(doc.channel?.id || '').slice(-4)}`;
    $('title').textContent = `${date.day.replace(/^0/, '')} ${date.monLong}, ${date.wdLong} · ${date.time}`;
    const problems = (doc.problems || []).length;
    $('sub').innerHTML = `${esc(channel)}${doc.guild?.name ? ` · ${esc(doc.guild.name)}` : ''} · ${model.lines.length} реплик · ${esc(this._lengthText(model, doc))}` +
      (problems ? ` · <span class="warn" title="${esc((doc.problems || []).join('\n'))}">⚠ ${problems} проблем с файлами</span>` : '');
    this.onTitle(doc, channel, date);
    this.updateMediaMetadata(doc, channel, date);

    this._renderPeople();
    this._renderChips();
    this._countdown();

    const audio = doc.audio || {};
    let message = '';
    if (audio.status === 'none') message = doc.retention?.audio?.state === 'deleted' ? 'Аудио удалено по сроку хранения. Текст сохранён.' : 'В этой записи нет аудио.';
    else if (audio.status === 'error') message = 'Не удалось подготовить аудио. Попробуем ещё раз через минуту.';
    this.ribbon.setData({ duration: model.duration, lanes: lanes(model, 0.2), message });
    this.transcript.setModel(model, {
      tz: this.tz, startedAt: doc.started_at,
      emptyText: doc.status === 'recording' ? 'Идёт запись: текст появится, когда закончится расшифровка первых фрагментов.'
        : doc.status === 'transcribing' ? 'Расшифровка ещё идёт, текст скоро появится.' : 'В этой записи нет текста.',
    });
    this.transcript.setHidden(this.hiddenSpeakers, this.kind);
    this._search(this.query, { silent: true });

    const playable = audio.status === 'ready' && !!audio.url;
    const previous = this.loadedUrl;
    if (!keepTime || previous !== audio.url) {
      this.player.load(playable ? audio.url : null, { keepTime, startAt });
      this.loadedUrl = playable ? audio.url : null;
    }
    document.querySelector('.col2').classList.toggle('noaudio', !playable);
    $('dur').textContent = model.duration ? `из ${fmtPos(model.duration)}` : '';
    $('mp3').disabled = !doc.links?.audio_mp3;
    $('mp3').title = doc.links?.audio_mp3 ? 'Скачать склеенную запись в MP3' : 'MP3 будет доступен, когда запись закончится';
    $('md').href = doc.links?.transcript_markdown || '#';
    $('md').toggleAttribute('aria-disabled', !doc.links?.transcript_markdown);
    document.title = `${date.day} ${date.mon} · ${channel} — Эхо`;
    this._state();
    this._tick(true);
  }

  _lengthText(model, doc) {
    const speech = doc.speech_seconds || 0;
    const live = doc.status === 'recording';
    return `${speech ? `${fmtPos(speech)} речи` : 'без речи'}${live ? ' · идёт запись' : ''}`;
  }

  _renderPeople() {
    const { speakers } = this.model;
    const total = speakers.reduce((a, s) => a + s.speech, 0) || 1;
    $('ppl').innerHTML = speakers.map((s) =>
      `<div class="p" data-s="${s.idx}" style="--c:${s.color}"><i class="sq" style="background:${s.color}"></i>` +
      `<div class="nm">${esc(s.label)}<span class="eq"><i></i><i></i><i></i><i></i></span></div><span>${fmtPos(s.speech)}</span></div>`).join('');
    $('shr').innerHTML = speakers.map((s) => {
      const pc = (s.speech / total) * 100;
      return `<i title="${esc(s.label)} · ${Math.round(pc)}%" style="width:${pc}%;background:${s.color}">${pc > 9 ? `${Math.round(pc)}%` : ''}</i>`;
    }).join('');
  }

  _renderChips() {
    const kinds = [['all', 'Всё'], ['voice', 'Голос'], ['tts', 'TTS']];
    $('chips').innerHTML = kinds.map(([k, label]) => `<button class="f${this.kind === k ? ' on' : ''}" data-kind="${k}">${label}</button>`).join('') +
      '<span class="sp"></span>' +
      this.model.speakers.map((s) => `<button class="f${this.hiddenSpeakers.has(s.idx) ? ' off' : ' on'}" data-spk="${s.idx}"><i class="d" style="background:${s.color}"></i>${esc(s.label)}</button>`).join('');
  }

  _chip(e) {
    const kind = e.target.closest('[data-kind]');
    const spk = e.target.closest('[data-spk]');
    if (kind) this.kind = kind.dataset.kind;
    if (spk) {
      const i = Number(spk.dataset.spk);
      if (this.hiddenSpeakers.has(i)) this.hiddenSpeakers.delete(i); else this.hiddenSpeakers.add(i);
    }
    if (!kind && !spk) return;
    this._renderChips();
    this.transcript.setHidden(this.hiddenSpeakers, this.kind);
  }

  // -- retention ------------------------------------------------------------

  _countdown() {
    const doc = this.doc;
    if (!doc) return;
    const audio = doc.retention?.audio || {};
    const transcript = doc.retention?.transcript || {};
    const big = $('keep');
    const card = $('ret');
    let note = '';
    let level = 'ok';
    let width = 100;
    let until = '';

    if (audio.state === 'deleted') {
      big.textContent = 'аудио удалено';
      level = 'mute';
      width = 0;
    } else if (audio.state === 'none' || !audio.state) {
      big.textContent = 'нет аудио';
      level = 'mute';
      width = 0;
    } else if (audio.expires_at == null) {
      big.textContent = 'бессрочно';
    } else {
      const secs = Math.max(0, audio.expires_at - Date.now() / 1000);
      const p = split(secs);
      level = urgency(secs);
      big.innerHTML = p.d > 0 ? `${p.d}<em>д</em> ${pad(p.h)}:${pad(p.m)}:${pad(p.s)}` : `${pad(p.h)}:${pad(p.m)}:${pad(p.s)}`;
      width = this.keepSeconds > 0 ? Math.min(100, (secs / this.keepSeconds) * 100) : 100;
      const d = dateParts(audio.expires_at, this.tz);
      until = `${d.day.replace(/^0/, '')} ${d.mon} ${d.year}`;
      if (audio.state === 'partial') note = 'часть аудио уже удалена. ';
      if (audio.kept_until_transcribed) note += 'часть хранится, пока не расшифрована. ';
    }
    if (transcript.expires_at == null) note += 'Расшифровка хранится бессрочно.';
    else note += `Расшифровка — ещё ${timeLeft(Math.max(0, transcript.expires_at - Date.now() / 1000))}${transcript.held ? ' (отложено, пока идёт расшифровка)' : ''}.`;

    card.className = `blk ret ${level}`;
    $('keepUntil').textContent = until;
    $('keepBar').style.width = `${width}%`;
    $('keepNote').textContent = note.charAt(0).toUpperCase() + note.slice(1);
  }

  // -- playback ---------------------------------------------------------------

  seek(t) {
    this.startAt = 0;
    if (!this.player.available) { toast('Аудио для этой записи недоступно.'); return; }
    this.transcript.setFollow(true);
    this.player.seek(t);
  }

  _time() {
    // before the audio's metadata arrives the element may not report a deep link's start
    return this.player.audio.readyState > 0 ? this.player.time : this.startAt || this.player.time;
  }

  _tick(force = false) {
    const model = this.model;
    if (!model) return;
    const t = this._time();
    $('cur').textContent = posTenths(t, model.duration >= 3600);
    this.ribbon.setTime(t);
    const act = model.hasPositions ? activeAt(model, t) : null;
    this.transcript.setActive(act);
    const now = act && model.lines[act.line].kind === 'voice' ? model.lines[act.line].speaker : -1;
    if (force || now !== this._now) {
      this._now = now;
      for (const el of document.querySelectorAll('#ppl .p')) el.classList.toggle('now', Number(el.dataset.s) === now && this.player.playing);
    }
    if ('mediaSession' in navigator && this.player.duration && this._lastPos !== Math.floor(t)) {
      this._lastPos = Math.floor(t);
      try { navigator.mediaSession.setPositionState({ duration: this.player.duration, position: Math.min(t, this.player.duration), playbackRate: this.player.rate }); } catch { /* not supported */ }
    }
  }

  _state() {
    const p = this.player;
    $('pi').setAttribute('d', p.playing ? 'M5 3.5h5v17H5zM14 3.5h5v17h-5z' : 'M6 3.5v17l15-8.5z');
    $('play').setAttribute('aria-label', p.playing ? 'Пауза' : 'Играть');
    $('play').disabled = !p.available;
    $('rate').textContent = `${p.rate}×`;
    document.body.classList.toggle('playing', p.playing);
    if (p.error) toast('Браузер не смог воспроизвести аудио. Попробуйте скачать MP3.', { error: true });
    if (!p.playing && this.pending) { const doc = this.pending; this.pending = null; this._apply(doc, { keepTime: true }); }
  }

  _followState(on) {
    $('pill').classList.toggle('on', on);
    $('pilltx').textContent = on ? 'Слежу за звуком' : 'Слежение выключено';
  }

  _describe(t) {
    const model = this.model;
    if (!model) return null;
    const act = activeAt(model, t);
    if (!act) return null;
    const line = model.lines[act.line];
    const sp = model.speakers[line.speaker];
    const text = line.segs[act.seg]?.text || line.text;
    return { at: line.pos ?? t, name: sp.label, color: sp.color, text: text.length > 120 ? `${text.slice(0, 118)}…` : text };
  }

  // -- search -------------------------------------------------------------------

  _search(value, { silent = false } = {}) {
    this.query = (value || '').trim();
    if ($('q').value !== value && !silent) $('q').value = value;
    const count = this.transcript.setQuery(this.query);
    $('sbox').classList.toggle('has', !!this.query);
    $('scnt').textContent = this.query ? (count ? String(count) : 'нет') : '';
    this.ribbon.setHits(this._hitTimes(), -1);
  }

  _hitTimes() {
    if (!this.model) return [];
    return this.transcript.hits.map((i) => this.model.lines[i].pos).filter((t) => t !== null);
  }

  _hit(delta) {
    const i = this.transcript.gotoHit(delta);
    if (i < 0) return;
    $('scnt').textContent = `${i + 1} из ${this.transcript.hits.length}`;
    const times = this.transcript.hits.map((l) => this.model.lines[l].pos);
    this.ribbon.setHits(times.filter((t) => t !== null), times[i] === null ? -1 : times.slice(0, i).filter((t) => t !== null).length);
  }

  // -- actions ------------------------------------------------------------------

  async _downloadMp3() {
    const url = this.doc?.links?.audio_mp3;
    if (!url) return;
    const button = $('mp3');
    button.disabled = true;
    button.textContent = 'Готовлю MP3…';
    try {
      await waitForMp3(url, { onWaiting: () => { button.textContent = 'Готовлю MP3…'; } });
      const a = document.createElement('a');
      a.href = `${url}?download=1`;
      a.download = '';
      document.body.append(a);
      a.click();
      a.remove();
    } catch (error) {
      toast(explain(error), { error: true });
    } finally {
      button.disabled = !this.doc?.links?.audio_mp3;
      button.textContent = '↓ mp3';
    }
  }

  async _copyLink() {
    const url = new URL(location.href);
    url.search = '';
    const t = Math.floor(this._time());
    if (t > 0) url.searchParams.set('t', String(t));
    const ok = await copyText(url.toString());
    toast(ok ? `Ссылка скопирована${t > 0 ? ` (с ${fmtPos(t)})` : ''}` : 'Не удалось скопировать ссылку', { error: !ok });
  }

  // -- keyboard ------------------------------------------------------------------

  onKey(e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return false;
    const typing = ['INPUT', 'TEXTAREA'].includes(e.target.tagName);
    if (typing) {
      if (e.key === 'Escape') { e.target.blur(); return true; }
      if (e.key === 'Enter') { this._hit(e.shiftKey ? -1 : 1); return true; }
      return false;
    }
    switch (e.key) {
      case ' ': this.player.toggle(); return true;
      case 'ArrowRight': this.seek(this.player.time + 5); return true;
      case 'ArrowLeft': this.seek(this.player.time - 5); return true;
      case 'j': case 'k': this._stepLine(e.key === 'j' ? 1 : -1); return true;
      case 'f': this.transcript.setFollow(!this.transcript.following); return true;
      case '/': $('q').focus(); return true;
      default: return false;
    }
  }

  _stepLine(delta) {
    const model = this.model;
    if (!model?.hasPositions) return;
    const act = activeAt(model, this._time());
    const timed = model.lines.filter((l) => l.pos !== null && l.kind === 'voice');
    if (!timed.length) return;
    let i = act ? timed.findIndex((l) => l.index === act.line) : -1;
    if (i < 0) i = delta > 0 ? -1 : timed.length;
    const next = timed[Math.min(timed.length - 1, Math.max(0, i + delta))];
    this.seek(next.pos + 0.01);
  }

  _setupMediaSession() {
    if (!('mediaSession' in navigator)) return;
    const set = (name, fn) => { try { navigator.mediaSession.setActionHandler(name, fn); } catch { /* unsupported */ } };
    set('play', () => this.player.play());
    set('pause', () => this.player.pause());
    set('seekbackward', () => this.seek(this.player.time - 10));
    set('seekforward', () => this.seek(this.player.time + 10));
    set('seekto', (d) => this.seek(d.seekTime));
  }

  updateMediaMetadata(doc, channel, date) {
    if (!('mediaSession' in navigator) || typeof MediaMetadata === 'undefined') return;
    navigator.mediaSession.metadata = new MediaMetadata({ title: `${channel} · ${date.day}.${date.mon} ${date.time}`, artist: doc.guild?.name || 'Эхо', album: 'Голосовые записи' });
  }
}

