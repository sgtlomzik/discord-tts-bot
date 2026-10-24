// Tiny shared UI bits.

let timer = 0;

/** A short message at the bottom of the screen. */
export function toast(text, { error = false, ms = 4500 } = {}) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = text;
  el.classList.toggle('err', error);
  el.hidden = false;
  clearTimeout(timer);
  timer = setTimeout(() => { el.hidden = true; }, ms);
}

/** Human text for an ApiError (or anything else that went wrong). */
export function explain(error) {
  switch (error?.code) {
    case 'network': return 'Нет связи с сервером.';
    case 'session_not_found': return 'Такой записи нет: возможно, она уже удалена.';
    case 'unauthorized': return 'Нужен вход: обновите страницу.';
    case 'busy': return 'Сервер занят, попробуйте через минуту.';
    default: return error?.message || 'Что-то пошло не так.';
  }
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement('textarea');
    area.value = text;
    area.style.cssText = 'position:fixed;opacity:0';
    document.body.append(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { /* ignored */ }
    area.remove();
    return ok;
  }
}
