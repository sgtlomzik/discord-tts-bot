// The only module that talks HTTP. Everything else gets plain objects.
// Basic auth is handled by the browser: it asks once for the page and then
// sends the credentials with every same-origin request, <audio> included.

const BASE = '/api/v1';

export class ApiError extends Error {
  constructor(status, code, message, retryAfter = 0) {
    super(message || code);
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

async function request(path, { signal, method = 'GET' } = {}) {
  let response;
  try {
    response = await fetch(BASE + path, { method, signal, headers: { Accept: 'application/json' } });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ApiError(0, 'network', 'Нет связи с сервером');
  }
  if (!response.ok) {
    let code = `http_${response.status}`;
    let message = response.statusText;
    try {
      const body = await response.json();
      code = body.error.code;
      message = body.error.message;
    } catch { /* not a JSON error */ }
    throw new ApiError(response.status, code, message, Number(response.headers.get('Retry-After')) || 0);
  }
  return method === 'HEAD' ? response : response.json();
}

const query = (params) => {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') q.set(k, v);
  const text = q.toString();
  return text ? `?${text}` : '';
};

export const api = {
  info: (opts) => request('/info', opts),
  guilds: (opts) => request('/guilds', opts),
  sessions: ({ guild, user, limit = 50, offset = 0, signal } = {}) => request(`/sessions${query({ guild, user, limit, offset })}`, { signal }),
  session: (id, opts) => request(`/sessions/${id}`, opts),
};

const sleep = (ms, signal) => new Promise((resolve, reject) => {
  const timer = setTimeout(resolve, ms);
  signal?.addEventListener('abort', () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); }, { once: true });
});

/**
 * The MP3 is transcoded on first request (202 until ready). Poll with HEAD
 * until it is ready, then the caller can download it for real.
 */
export async function waitForMp3(url, { onWaiting, signal } = {}) {
  for (let attempt = 0; attempt < 60; attempt++) {
    let response;
    try {
      response = await fetch(url, { method: 'HEAD', signal });
    } catch (error) {
      if (error.name === 'AbortError') throw error;
      throw new ApiError(0, 'network', 'Нет связи с сервером');
    }
    if (response.status === 200) return;
    if (response.status === 202) {
      onWaiting?.(attempt);
      await sleep(Math.max(2, Number(response.headers.get('Retry-After')) || 5) * 1000, signal);
      continue;
    }
    if (response.status === 409) throw new ApiError(409, 'session_recording', 'MP3 будет доступен, когда запись закончится');
    if (response.status === 503) throw new ApiError(503, 'busy', 'Сервер занят подготовкой других файлов, попробуйте позже');
    throw new ApiError(response.status, `http_${response.status}`, 'Не удалось подготовить MP3');
  }
  throw new ApiError(0, 'timeout', 'MP3 не успел подготовиться, попробуйте позже');
}
