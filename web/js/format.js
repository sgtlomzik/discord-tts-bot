// Pure formatting helpers (no DOM): dates in the server's timezone,
// durations, retention countdowns, initials. Covered by tests/web.

const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
const MONTHS_GEN = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
const WEEKDAYS = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
const WEEKDAYS_LONG = ['воскресенье', 'понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота'];
const EN_WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

const formatters = new Map();

function formatter(tz) {
  let f = formatters.get(tz);
  if (!f) {
    const options = {
      year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric',
      second: 'numeric', hourCycle: 'h23', weekday: 'short',
    };
    try {
      f = new Intl.DateTimeFormat('en-GB', { ...options, timeZone: tz || 'UTC' });
    } catch {
      f = new Intl.DateTimeFormat('en-GB', { ...options, timeZone: 'UTC' });
    }
    formatters.set(tz, f);
  }
  return f;
}

const pad = (n) => String(n).padStart(2, '0');

/** Calendar fields of an epoch-seconds moment in the given timezone. */
export function zoned(epoch, tz) {
  const out = {};
  for (const p of formatter(tz).formatToParts(new Date(epoch * 1000))) out[p.type] = p.value;
  return {
    year: +out.year, month: +out.month, day: +out.day,
    hour: +out.hour % 24, minute: +out.minute, second: +out.second,
    weekday: Math.max(0, EN_WEEKDAYS.indexOf(out.weekday)),
  };
}

/** 21:15:50 (or 21:15 without seconds). */
export function clock(epoch, tz, seconds = true) {
  if (epoch == null) return '';
  const z = zoned(epoch, tz);
  return seconds ? `${pad(z.hour)}:${pad(z.minute)}:${pad(z.second)}` : `${pad(z.hour)}:${pad(z.minute)}`;
}

/** Pieces for a session card: day "04", month "окт", weekday "вс", time "21:14". */
export function dateParts(epoch, tz) {
  const z = zoned(epoch, tz);
  return {
    day: pad(z.day), mon: MONTHS[z.month - 1], monLong: MONTHS_GEN[z.month - 1], year: z.year,
    wd: WEEKDAYS[z.weekday], wdLong: WEEKDAYS_LONG[z.weekday], time: `${pad(z.hour)}:${pad(z.minute)}`,
  };
}

/** Position in the audio: 07:30, or 1:07:30 when long (or forced). */
export function pos(seconds, long = false) {
  const s = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor(s / 60) % 60;
  return h || long ? `${h}:${pad(m)}:${pad(s % 60)}` : `${pad(m)}:${pad(s % 60)}`;
}

/** Same with a tenth of a second: 07:30.3 */
export function posTenths(seconds, long = false) {
  const t = Math.max(0, seconds || 0);
  return `${pos(t, long)}.${Math.floor((t % 1) * 10)}`;
}

/** Plural form for Russian: plural(5, 'день', 'дня', 'дней'). */
export function plural(n, one, few, many) {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  return b === 1 ? one : many;
}

/** Days / hours / minutes / seconds of a non-negative duration. */
export function split(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0));
  return { d: Math.floor(s / 86400), h: Math.floor(s / 3600) % 24, m: Math.floor(s / 60) % 60, s: s % 60 };
}

/** Short "time left": 89 д 23 ч / 5 ч 12 мин / 12 мин / <1 мин. */
export function left(seconds) {
  const p = split(seconds);
  if (p.d > 0) return `${p.d} д ${p.h} ч`;
  if (p.h > 0) return `${p.h} ч ${p.m} мин`;
  if (p.m > 0) return `${p.m} мин`;
  return '<1 мин';
}

/** How worried to be about the time left. */
export function urgency(seconds) {
  if (seconds == null) return 'ok';
  if (seconds < 3 * 86400) return 'crit';
  if (seconds < 14 * 86400) return 'warn';
  return 'ok';
}

/** Two letters for an avatar square. */
export function initials(name, id = '') {
  const words = String(name || '').trim().split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  if (words.length === 1) return [...words[0]].slice(0, 2).join('').toUpperCase();
  return id ? String(id).slice(-2) : '?';
}

/** Name to show: unknown people (null name) are shown by the tail of their id. */
export function displayName(name, id = '') {
  return name || `#${String(id).slice(-4)}`;
}
