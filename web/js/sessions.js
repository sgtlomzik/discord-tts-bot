// The list of recordings (left column): date, channel, who took part, how
// much was said, and how long the audio will still be kept.

import { api } from './api.js';
import { assignColors } from './palette.js';
import { esc } from './dom.js';
import { dateParts, initials, left as timeLeft, pos as fmtPos, plural, urgency } from './format.js';

const PAGE = 50;
const POLL_MS = 30000;
const EXPIRING = 14 * 86400;

export class SessionList {
  /** @param {HTMLElement} root @param {{tz:string, keepSeconds:number, onOpen:(id:string)=>void, onError:(e:Error)=>void, onChange?:(items:object[])=>void}} opts */
  constructor(root, { tz, keepSeconds, onOpen, onError, onChange = () => {} }) {
    this.root = root;
    this.tz = tz;
    this.keepSeconds = keepSeconds;
    this.onOpen = onOpen;
    this.onError = onError;
    this.onChange = onChange;
    this.items = [];
    this.total = 0;
    this.filter = { channel: null, expiring: false };
    this.selected = null;
    this.rowsEl = root.querySelector('.rows');
    this.filtersEl = root.querySelector('.flt');
    this.countEl = root.querySelector('.count');
    this.rowsEl.addEventListener('click', (e) => {
      const more = e.target.closest('[data-more]');
      if (more) { this.loadMore(); return; }
      const item = e.target.closest('[data-id]');
      if (item) this.onOpen(item.dataset.id);
    });
    this.filtersEl.addEventListener('click', (e) => {
      const chip = e.target.closest('[data-f]');
      if (!chip) return;
      const [kind, value] = [chip.dataset.f, chip.dataset.v];
      if (kind === 'all') this.filter = { channel: null, expiring: false };
      else if (kind === 'expiring') this.filter = { channel: null, expiring: !this.filter.expiring };
      else this.filter = { channel: this.filter.channel === value ? null : value, expiring: false };
      this.render();
    });
    this._timer = setInterval(() => this.refresh().catch(() => {}), POLL_MS);
  }

  async load() {
    const page = await api.sessions({ limit: PAGE });
    this.items = page.items;
    this.total = page.total;
    this.render();
    return this.items;
  }

  /** Re-read what is already shown (new sessions appear on top, statuses change). */
  async refresh() {
    const page = await api.sessions({ limit: Math.min(200, Math.max(PAGE, this.items.length)) });
    this.items = page.items;
    this.total = page.total;
    this.render();
  }

  async loadMore() {
    try {
      const page = await api.sessions({ limit: PAGE, offset: this.items.length });
      const seen = new Set(this.items.map((s) => s.id));
      this.items = this.items.concat(page.items.filter((s) => !seen.has(s.id)));
      this.total = page.total;
      this.render();
    } catch (error) {
      this.onError(error);
    }
  }

  select(id) {
    this.selected = id;
    for (const el of this.rowsEl.querySelectorAll('[data-id]')) el.classList.toggle('sel', el.dataset.id === id);
    this.rowsEl.querySelector('.sel')?.scrollIntoView({ block: 'nearest' });
  }

  /** The session with this id as last listed (for the title bar). */
  find(id) { return this.items.find((s) => s.id === id) || null; }

  destroy() { clearInterval(this._timer); }

  _channelName(s) { return s.channel?.name || `канал …${String(s.channel?.id || '').slice(-4)}`; }

  _visible() {
    return this.items.filter((s) => {
      if (this.filter.channel && this._channelName(s) !== this.filter.channel) return false;
      if (this.filter.expiring) {
        const left = this._secondsLeft(s);
        return left !== null && left < EXPIRING;
      }
      return true;
    });
  }

  /** Seconds until the audio starts to be deleted (null: kept forever or nothing to keep). */
  _secondsLeft(s) {
    const audio = s.retention?.audio;
    if (!audio || !['kept', 'partial'].includes(audio.state) || audio.expires_at == null) return null;
    return Math.max(0, audio.expires_at - Date.now() / 1000);
  }

  render() {
    this._renderFilters();
    const shown = this._visible();
    this.countEl.textContent = String(this.total);
    const rows = shown.map((s) => this._row(s)).join('');
    const more = this.items.length < this.total
      ? `<button class="more" data-more>Показать ещё (${this.total - this.items.length})</button>` : '';
    const scrolled = this.rowsEl.scrollTop;
    this.rowsEl.innerHTML = (rows || `<div class="empty">${this.items.length ? 'Под фильтр ничего не подходит.' : 'Записей пока нет.'}</div>`) + more;
    this.rowsEl.scrollTop = scrolled;
    this.onChange(this.items);
    for (const el of this.rowsEl.querySelectorAll('[data-id]')) el.classList.toggle('sel', el.dataset.id === this.selected);
  }

  _renderFilters() {
    const channels = [...new Set(this.items.map((s) => this._channelName(s)))].slice(0, 4);
    const expiring = this.items.filter((s) => { const l = this._secondsLeft(s); return l !== null && l < EXPIRING; }).length;
    const chip = (attrs, text, on) => `<button class="${on ? 'on' : ''}" ${attrs}>${esc(text)}</button>`;
    this.filtersEl.innerHTML = chip('data-f="all"', 'Все', !this.filter.channel && !this.filter.expiring) +
      channels.map((c) => chip(`data-f="channel" data-v="${esc(c)}"`, c, this.filter.channel === c)).join('') +
      (expiring ? chip('data-f="expiring"', `Истекают · ${expiring}`, this.filter.expiring) : '');
  }

  _row(s) {
    const date = dateParts(s.started_at, this.tz);
    const people = s.participants || [];
    const colors = assignColors(people.map((p) => p.user_id));
    const speech = people.reduce((a, p) => a + (p.speech_seconds || 0), 0);
    const live = s.status === 'recording';

    const status = live ? '<span class="tagl">● идёт запись</span>'
      : s.status === 'transcribing' ? '<span class="tagt">расшифровка…</span>'
      : s.status === 'partial' ? '<span class="tagt">текст неполный</span>'
      : `${s.line_count} ${plural(s.line_count, 'реплика', 'реплики', 'реплик')}`;

    const avatars = people.slice(0, 5).map((p) => `<i style="background:${colors.get(p.user_id)}" title="${esc(p.name || p.user_id)}">${esc(initials(p.name, p.user_id))}</i>`).join('')
      + (people.length > 5 ? `<i class="more-p">+${people.length - 5}</i>` : '');
    const share = speech > 0
      ? people.map((p) => `<i style="width:${((p.speech_seconds || 0) / speech) * 100}%;background:${colors.get(p.user_id)}"></i>`).join('')
      : '';

    return `<button class="it${s.id === this.selected ? ' sel' : ''}" data-id="${esc(s.id)}">
      <div class="r1"><b>${date.day} ${date.mon}</b><span>${date.wd} · ${date.time}</span><span class="dur" title="речи в записи">${speech ? fmtPos(speech) : ''}</span></div>
      <div class="r2"><span class="chn">${esc(this._channelName(s))}</span><span>· ${status}</span><span class="ini">${avatars}</span></div>
      <div class="r3"><div class="shr mini">${share}</div>${this._keep(s)}</div>
    </button>`;
  }

  _keep(s) {
    const audio = s.retention?.audio;
    if (!audio || audio.state === 'none') return '<div class="keep mute">нет аудио</div>';
    if (audio.state === 'deleted') return '<div class="keep mute">аудио удалено</div>';
    const left = this._secondsLeft(s);
    if (left === null) return '<div class="keep ok">∞<div class="t"><i style="width:100%"></i></div></div>';
    const u = urgency(left);
    const width = this.keepSeconds > 0 ? Math.min(100, (left / this.keepSeconds) * 100) : 100;
    return `<div class="keep ${u}">${esc(timeLeft(left))}<div class="t"><i style="width:${width}%"></i></div></div>`;
  }
}
