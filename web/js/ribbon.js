// The vertical map of the recording: time runs top to bottom, one lane per
// speaker. Shows who spoke when, the playhead, the part of the transcript that
// is on screen, search hits; click to seek, hover to peek at what was said.

import { esc } from './dom.js';
import { initials, pos as fmtPos } from './format.js';
import { axisStep } from './timeline.js';

const AXIS = 46; // px reserved for time labels
const NS = 'http://www.w3.org/2000/svg';
let clipCounter = 0;

export class Ribbon {
  /** @param {HTMLElement} root @param {{onSeek:(t:number)=>void, describe:(t:number)=>object|null}} hooks */
  constructor(root, { onSeek, describe }) {
    this.root = root;
    this.onSeek = onSeek;
    this.describe = describe;
    this.data = null;
    this.time = 0;
    this.hits = [];
    this.hitIndex = -1;
    this.clipId = `ribbon-clip-${clipCounter++}`;
    root.innerHTML = `
      <div class="lanehd"></div>
      <div class="rb" role="slider" aria-label="Карта записи: нажмите, чтобы перейти к моменту" tabindex="-1">
        <svg class="rbsvg" aria-hidden="true"></svg>
        <div class="vp"></div><div class="hv"></div>
        <div class="ph"><b></b></div>
      </div>
      <div class="rbempty" hidden></div>
      <div class="tip" hidden></div>`;
    this.head = root.querySelector('.lanehd');
    this.rb = root.querySelector('.rb');
    this.svg = root.querySelector('.rbsvg');
    this.vp = root.querySelector('.vp');
    this.hv = root.querySelector('.hv');
    this.ph = root.querySelector('.ph');
    this.phLabel = this.ph.querySelector('b');
    this.empty = root.querySelector('.rbempty');
    this.tip = root.querySelector('.tip');
    this.height = 0;
    this.width = 0;

    this.rb.addEventListener('click', (e) => this.onSeek(this._timeAt(e.clientY)));
    this.rb.addEventListener('mousemove', (e) => this._hover(e.clientY));
    this.rb.addEventListener('mouseleave', () => this._unhover());
    this.resize = new ResizeObserver(() => this.draw());
    this.resize.observe(this.rb);
  }

  /** `lanes` come from timeline.lanes(); `message` replaces the map when there is no audio. */
  setData({ duration, lanes, message = '' }) {
    this.data = { duration, lanes };
    this.message = message;
    const none = !!message || !duration;
    this.rb.hidden = none;
    this.head.hidden = none;
    this.empty.hidden = !none;
    this.empty.textContent = message;
    this.head.style.setProperty('--n', lanes.length);
    this.head.innerHTML = '<span></span>' + lanes.map(({ speaker }) => {
      const label = lanes.length > 5 ? initials(speaker.name, speaker.id) : speaker.label;
      return `<span title="${esc(speaker.label)}" style="color:${speaker.color}">${esc(label)}</span>`;
    }).join('');
    this.draw();
  }

  setTime(t) {
    this.time = t;
    this._placeHead();
    this.clipRect?.setAttribute('height', String(this._y(t)));
  }

  /** Part of the audio the transcript currently shows. */
  setViewport(from, to) {
    if (!this.height || !this.data?.duration || from == null || to == null) { this.vp.style.display = 'none'; return; }
    this.vp.style.display = '';
    this.vp.style.top = `${this._y(from)}px`;
    this.vp.style.height = `${Math.max(8, this._y(to) - this._y(from))}px`;
  }

  setHits(times, current = -1) {
    this.hits = times;
    this.hitIndex = current;
    this._drawHits();
  }

  _y(t) {
    const d = this.data?.duration;
    return d ? Math.min(1, Math.max(0, t / d)) * this.height : 0;
  }

  _timeAt(clientY) {
    const r = this.rb.getBoundingClientRect();
    return Math.min(1, Math.max(0, (clientY - r.top) / r.height)) * (this.data?.duration || 0);
  }

  _placeHead() {
    this.ph.style.top = `${this._y(this.time)}px`;
    this.phLabel.textContent = fmtPos(this.time, (this.data?.duration || 0) >= 3600);
  }

  draw() {
    if (!this.data || this.rb.hidden) return;
    const r = this.rb.getBoundingClientRect();
    this.height = r.height;
    this.width = r.width;
    if (!r.height || !r.width) return;
    const { duration, lanes } = this.data;
    const n = Math.max(1, lanes.length);
    const laneW = (r.width - AXIS) / n;
    const blockW = Math.max(4, Math.min(laneW * 0.55, 26));
    const long = duration >= 3600;
    const step = axisStep(duration, Math.max(3, Math.floor(r.height / 64)));

    const make = (tag, attrs) => {
      const el = document.createElementNS(NS, tag);
      for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
      return el;
    };
    const frag = document.createDocumentFragment();
    for (let t = 0; t <= duration + 0.001; t += step) {
      const y = this._y(t);
      frag.append(make('line', { x1: AXIS - 6, x2: r.width, y1: y, y2: y, stroke: '#2a2d34', 'stroke-width': 1 }));
      const label = make('text', { x: 0, y: Math.min(r.height - 2, y + 10), fill: '#5a5f6a', 'font-size': 10, 'font-family': 'var(--mono)' });
      label.textContent = fmtPos(t, long);
      frag.append(label);
    }
    for (let i = 1; i < n; i++) {
      frag.append(make('line', { x1: AXIS + i * laneW, x2: AXIS + i * laneW, y1: 0, y2: r.height, stroke: '#202328', 'stroke-width': 1 }));
    }

    const blocks = (color, opacity, extra = {}) => {
      const g = make('g', { opacity, ...extra });
      lanes.forEach((lane, i) => {
        const x = AXIS + i * laneW + (laneW - blockW) / 2;
        for (const [a, b] of lane.blocks) {
          g.append(make('rect', { x, y: this._y(a), width: blockW, height: Math.max(1.5, this._y(b) - this._y(a)), rx: 1.5, fill: lane.speaker.color }));
        }
        for (const t of lane.ticks) {
          g.append(make('rect', { x: x - 3, y: this._y(t) - 1, width: blockW + 6, height: 2, fill: 'none', stroke: lane.speaker.color, 'stroke-width': 1 }));
        }
      });
      return g;
    };
    const clip = make('clipPath', { id: this.clipId });
    this.clipRect = make('rect', { x: 0, y: 0, width: r.width, height: this._y(this.time) });
    clip.append(this.clipRect);
    frag.append(clip, blocks(null, 0.36), blocks(null, 1, { 'clip-path': `url(#${this.clipId})` }));
    this.hitsLayer = make('g', {});
    frag.append(this.hitsLayer);

    this.svg.setAttribute('viewBox', `0 0 ${r.width} ${r.height}`);
    this.svg.replaceChildren(frag);
    this.ph.style.left = `${AXIS - 8}px`;
    this.vp.style.left = `${AXIS - 4}px`;
    this.hv.style.left = `${AXIS - 4}px`;
    this._placeHead();
    this._drawHits();
  }

  _drawHits() {
    if (!this.hitsLayer) return;
    const frag = document.createDocumentFragment();
    this.hits.forEach((t, i) => {
      const rect = document.createElementNS(NS, 'rect');
      const current = i === this.hitIndex;
      rect.setAttribute('x', String(this.width - 9));
      rect.setAttribute('y', String(this._y(t) - (current ? 3 : 2)));
      rect.setAttribute('width', '9');
      rect.setAttribute('height', current ? '6' : '4');
      rect.setAttribute('rx', '1');
      rect.setAttribute('fill', current ? '#ffffff' : '#f2c66d');
      frag.append(rect);
    });
    this.hitsLayer.replaceChildren(frag);
  }

  _hover(clientY) {
    const t = this._timeAt(clientY);
    const info = this.describe(t);
    const y = this._y(t);
    this.hv.style.display = 'block';
    this.hv.style.top = `${y}px`;
    if (!info) { this.tip.hidden = true; return; }
    this.tip.innerHTML = `<b>${esc(fmtPos(info.at ?? t, (this.data?.duration || 0) >= 3600))}</b><em style="color:${info.color}">${esc(info.name)}</em><p>${esc(info.text)}</p>`;
    this.tip.hidden = false;
    const base = this.rb.offsetTop + y;
    this.tip.style.top = `${Math.max(4, Math.min(this.root.clientHeight - this.tip.offsetHeight - 4, base - this.tip.offsetHeight - 10))}px`;
  }

  _unhover() {
    this.tip.hidden = true;
    this.hv.style.display = 'none';
  }
}
