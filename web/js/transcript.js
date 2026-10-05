// The continuous transcript: one scrolling column of lines grouped into
// five-minute sections. The line being played is highlighted word by word and
// the column scrolls along (unless the reader scrolls away); a click on a time
// or a sentence seeks the audio there.

import { esc, markup } from './dom.js';
import { clock, pos as fmtPos, plural } from './format.js';
import { sections as makeSections, findHits } from './timeline.js';

const FOLLOW_AT = 0.34; // where in the viewport the current line is kept

export class Transcript {
  /**
   * @param {HTMLElement} scroll the scrolling element
   * @param {HTMLElement} jump the "back to the current line" button
   * @param {{onSeek:(t:number)=>void, onFollow:(on:boolean)=>void, onViewport:(from:number|null,to:number|null)=>void}} hooks
   */
  constructor(scroll, jump, { onSeek, onFollow, onViewport }) {
    this.scroll = scroll;
    this.jump = jump;
    this.onSeek = onSeek;
    this.onFollow = onFollow;
    this.onViewport = onViewport;
    this.model = null;
    this.rows = [];
    this.following = true;
    this.query = '';
    this.hits = [];
    this.hitIndex = -1;
    this.active = null;
    this.hidden = new Set();
    this.kind = 'all';
    this._frame = 0;

    const stopFollowing = () => { if (this.following) this.setFollow(false); };
    scroll.addEventListener('wheel', stopFollowing, { passive: true });
    scroll.addEventListener('touchmove', stopFollowing, { passive: true });
    scroll.addEventListener('pointerdown', (e) => { if (e.target === scroll) stopFollowing(); }); // the scrollbar
    scroll.addEventListener('scroll', () => this._schedule());
    scroll.addEventListener('click', (e) => this._click(e));
    jump.addEventListener('click', () => this.setFollow(true));
  }

  /** Build the DOM for a session. `ctx`: {tz, startedAt, colorOf}. */
  setModel(model, ctx) {
    this.model = model;
    this.ctx = ctx;
    this.active = null;
    this.hits = [];
    this.hitIndex = -1;
    this.rows = [];
    const frag = document.createDocumentFragment();

    if (!model.lines.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = ctx.emptyText || 'В этой записи пока нет текста.';
      this.scroll.replaceChildren(empty);
      this._schedule();
      return;
    }

    const long = model.duration >= 3600;
    this.sectionEls = [];
    for (const sec of makeSections(model, { step: 300, startedAt: ctx.startedAt })) {
      const total = sec.share.reduce((a, b) => a + b, 0) || 1;
      const head = document.createElement('div');
      head.className = 'sec';
      const label = model.hasPositions ? `${fmtPos(sec.start, long)} — ${fmtPos(sec.end, long)}` : clock(sec.firstAt, ctx.tz, false);
      head.innerHTML = `<b>${esc(label)}</b><small>${sec.lines.length} ${plural(sec.lines.length, 'реплика', 'реплики', 'реплик')}</small>` +
        `<div class="sh">${sec.share.map((v, i) => (v > 0 ? `<i style="width:${(v / total) * 100}%;background:${model.speakers[i].color}"></i>` : '')).join('')}</div>`;
      frag.append(head);
      this.sectionEls.push({ el: head, lines: sec.lines });
      for (const index of sec.lines) {
        const row = this._buildRow(model.lines[index], long);
        this.rows[index] = row;
        frag.append(row);
      }
    }
    this.scroll.replaceChildren(frag);
    this._applyHidden();
    this._schedule();
  }

  _buildRow(line, long) {
    const { model, ctx } = this;
    const sp = model.speakers[line.speaker];
    const row = document.createElement('div');
    row.className = `ln ${line.kind}`;
    row.dataset.i = line.index;
    row.style.setProperty('--c', sp.color);
    const wall = clock(line.at, ctx.tz);
    const seekable = line.pos !== null;
    const stamp = seekable ? fmtPos(line.pos, long) : clock(line.at, ctx.tz, false);
    const dur = seekable && line.end > line.pos ? Math.max(14, Math.min(100, ((line.end - line.pos) / 16) * 100)) : 0;
    const segs = line.segs.map((s, k) => `<span class="sg" data-k="${k}"></span>`).join(' ');
    row.innerHTML =
      `<div class="l"><button class="ts" ${seekable ? `data-seek="${line.pos}"` : 'disabled'} title="${esc(wall)}${seekable ? '' : ' · аудио недоступно'}">${esc(stamp)}</button>` +
      `${dur ? `<div class="du" style="width:${dur}%"></div>` : ''}</div>` +
      `<div><span class="nm">${esc(sp.label)}</span><span class="wall">${esc(wall)}</span><span class="tx">${segs}</span></div>`;
    this._paintRow(row, line, null);
    return row;
  }

  /** Paint the sentences of a row; `act` = {seg, frac} when this is the line being played. */
  _paintRow(row, line, act) {
    const spans = row.querySelectorAll('.sg');
    line.segs.forEach((seg, k) => {
      const el = spans[k];
      if (!el) return;
      if (seg.pos !== null) el.dataset.seek = seg.pos; else delete el.dataset.seek;
      if (!act) { el.className = 'sg'; el.innerHTML = markup(seg.text, this.query); return; }
      if (k < act.seg) { el.className = 'sg done'; el.innerHTML = markup(seg.text, this.query); }
      else if (k > act.seg) { el.className = 'sg rest'; el.innerHTML = markup(seg.text, this.query); }
      else {
        const cut = Math.round(seg.text.length * act.frac);
        el.className = 'sg cur';
        el.innerHTML = `<span class="done">${markup(seg.text.slice(0, cut), this.query)}</span><span class="rest">${markup(seg.text.slice(cut), this.query)}</span>`;
      }
    });
  }

  /** `act` from timeline.activeAt (or null). Called on every tick. */
  setActive(act) {
    const prev = this.active;
    const sameSeg = prev && act && prev.line === act.line && prev.seg === act.seg;
    const sameLine = prev && act && prev.line === act.line;
    if (prev && !sameLine) {
      const row = this.rows[prev.line];
      row?.classList.remove('act');
      row?.style.removeProperty('--p');
      if (row) this._paintRow(row, this.model.lines[prev.line], null);
    }
    this.active = act;
    if (!act) { this._schedule(); return; }
    const row = this.rows[act.line];
    if (!row) return;
    if (!sameLine) row.classList.add('act');
    row.style.setProperty('--p', `${act.lineFrac * 100}%`);
    // repaint the sentence only when it changed or its progress moved a character
    const seg = this.model.lines[act.line].segs[act.seg];
    const cut = Math.round(seg.text.length * act.frac);
    if (!sameSeg || cut !== this._cut) { this._paintRow(row, this.model.lines[act.line], act); this._cut = cut; }
    if (!sameLine && this.following) this.scrollToLine(act.line, !prev);
    if (!sameLine) this._schedule();
  }

  scrollToLine(index, instant = false) {
    const row = this.rows[index];
    if (!row) return;
    const top = Math.max(0, row.offsetTop - this.scroll.clientHeight * FOLLOW_AT);
    this.scroll.scrollTo({ top, behavior: instant ? 'auto' : 'smooth' });
  }

  setFollow(on) {
    this.following = on;
    this.onFollow(on);
    if (on && this.active) this.scrollToLine(this.active.line);
    this._schedule();
  }

  setQuery(query) {
    this.query = (query || '').trim();
    this.hits = this.model ? findHits(this.model, this.query) : [];
    this.hitIndex = -1;
    if (this.model) {
      this.model.lines.forEach((line) => {
        const row = this.rows[line.index];
        if (!row) return;
        const act = this.active && this.active.line === line.index ? this.active : null;
        this._paintRow(row, line, act);
      });
    }
    return this.hits.length;
  }

  gotoHit(delta) {
    if (!this.hits.length) return -1;
    this.hitIndex = (this.hitIndex + delta + this.hits.length) % this.hits.length;
    if (this.following) this.setFollow(false);
    const row = this.rows[this.hits[this.hitIndex]];
    this.scroll.scrollTo({ top: Math.max(0, row.offsetTop - this.scroll.clientHeight * 0.3), behavior: 'smooth' });
    return this.hitIndex;
  }

  /** Hide the lines of some speakers and/or all but one kind ('voice' | 'tts'). */
  setHidden(speakers, kind = 'all') {
    this.hidden = new Set(speakers);
    this.kind = kind;
    this._applyHidden();
    this._schedule();
  }

  _applyHidden() {
    if (!this.model) return;
    const off = (line) => this.hidden.has(line.speaker) || (this.kind && this.kind !== 'all' && line.kind !== this.kind);
    for (const line of this.model.lines) {
      const row = this.rows[line.index];
      if (row) row.hidden = off(line);
    }
    for (const { el, lines } of this.sectionEls || []) el.hidden = lines.every((i) => off(this.model.lines[i]));
  }

  _click(e) {
    if (String(window.getSelection?.() || '')) return; // selecting text to copy is not a seek
    const target = e.target.closest('[data-seek]');
    if (!target) return;
    const t = Number(target.dataset.seek);
    if (!Number.isFinite(t)) return;
    this.onSeek(t + 0.01);
  }

  _schedule() {
    if (this._frame) return;
    this._frame = requestAnimationFrame(() => { this._frame = 0; this._measure(); });
  }

  /** Which lines are on screen -> viewport bracket on the ribbon; is the current line out of view -> jump button. */
  _measure() {
    if (!this.model || !this.model.lines.length) { this.onViewport(null, null); this.jump.hidden = true; return; }
    const top = this.scroll.scrollTop;
    const bottom = top + this.scroll.clientHeight;
    const rows = this.rows;
    let first = -1;
    let last = -1;
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      if (!r || r.hidden) continue;
      const y = r.offsetTop;
      if (y + r.offsetHeight > top + 34 && first < 0) first = i;
      if (y < bottom) last = i; else break;
    }
    const lines = this.model.lines;
    const from = first >= 0 ? lines[first].pos : null;
    const to = last >= 0 ? lines[last].end : null;
    this.onViewport(from, to);
    const act = this.active && rows[this.active.line];
    this.jump.hidden = !act || !(act.offsetTop + act.offsetHeight < top + 34 || act.offsetTop > bottom - 10) || this.following;
  }
}
