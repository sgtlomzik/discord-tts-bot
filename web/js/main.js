// Entry point: loads the settings and the list, opens a recording by URL
// (/sessions/<guild>/<session>?t=seconds), keeps the address bar in step.

import { api } from './api.js';
import { esc } from './dom.js';
import { SessionList } from './sessions.js';
import { explain, toast } from './ui.js';
import { Viewer } from './viewer.js';

const ROUTE = /^\/sessions\/(\d+\/\d{8}-\d{6}Z-\d+)\/?$/;
const $ = (id) => document.getElementById(id);

function parseRoute() {
  const m = location.pathname.match(ROUTE);
  if (!m) return null;
  const t = Number.parseFloat(new URLSearchParams(location.search).get('t'));
  return { id: m[1], t: Number.isFinite(t) && t > 0 ? t : 0 };
}

function fatal(error) {
  $('scroll').innerHTML = `<div class="empty">${esc(explain(error))}<br><button class="retry" id="retry">Повторить</button></div>`;
  $('title').textContent = 'Эхо недоступно';
  $('retry')?.addEventListener('click', () => location.reload());
}

async function boot() {
  let info;
  try {
    info = await api.info();
  } catch (error) {
    fatal(error);
    return;
  }
  const tz = info.timezone || 'UTC';
  const keepSeconds = (info.retention?.audio_keep_days || 0) * 86400;

  const viewer = new Viewer({
    tz, keepSeconds,
    onTitle: (doc, channel, date) => {
      $('crumbs').innerHTML = `<span>/</span>${esc(doc.guild?.name || 'сервер')}<span>/</span>${esc(channel)}<span>/</span><b>${date.day.replace(/^0/, '')} ${esc(date.mon)} · ${esc(date.time)}</b>`;
    },
  });

  const list = new SessionList($('list'), {
    tz, keepSeconds,
    onOpen: (id) => go(id),
    onError: (error) => toast(explain(error), { error: true }),
    onChange: (items) => {
      const live = items.filter((s) => s.status === 'recording').length;
      $('live').hidden = !live;
      $('live').textContent = live ? `идёт запись · ${live}` : '';
    },
  });

  function show(id, t = 0) {
    list.select(id);
    document.body.classList.remove('drawer');
    viewer.open(id, { t });
  }

  function go(id) {
    if (id === viewer.id) { document.body.classList.remove('drawer'); return; }
    history.pushState({}, '', `/sessions/${id}`);
    show(id);
  }

  window.addEventListener('popstate', () => {
    const route = parseRoute();
    if (route) show(route.id, route.t);
  });
  document.addEventListener('keydown', (e) => { if (viewer.onKey(e)) e.preventDefault(); });
  // Space on a focused button would also "click" it on key-up: pause only once.
  document.addEventListener('keyup', (e) => {
    if (e.key === ' ' && !['INPUT', 'TEXTAREA'].includes(e.target.tagName)) e.preventDefault();
  });
  $('menu').addEventListener('click', () => document.body.classList.toggle('drawer'));
  $('scrim').addEventListener('click', () => document.body.classList.remove('drawer'));

  let items;
  try {
    items = await list.load();
  } catch (error) {
    fatal(error);
    return;
  }
  const route = parseRoute();
  if (route) {
    show(route.id, route.t);
  } else if (items.length) {
    history.replaceState({}, '', `/sessions/${items[0].id}`);
    show(items[0].id);
  } else {
    $('title').textContent = 'Записей пока нет';
    $('sub').textContent = 'Они появятся здесь, когда бот запишет голосовой канал.';
  }
}

boot();
