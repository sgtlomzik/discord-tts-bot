// Escaping and search highlighting. Everything that reaches innerHTML goes
// through esc(): names and transcript text come from other people.

const ENTITIES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ENTITIES[c]);

const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** Escaped HTML of `text` with every case-insensitive match of `query` in <mark>. */
export function markup(text, query) {
  const q = (query || '').trim();
  if (!q) return esc(text);
  const parts = String(text ?? '').split(new RegExp(`(${escapeRegExp(q)})`, 'gi'));
  return parts.map((part, i) => (i % 2 ? `<mark>${esc(part)}</mark>` : esc(part))).join('');
}

/** True when the text contains the query (case-insensitive). */
export function matches(text, query) {
  const q = (query || '').trim().toLowerCase();
  return !!q && String(text ?? '').toLowerCase().includes(q);
}

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
