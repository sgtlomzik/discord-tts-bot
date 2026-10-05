// The <audio> element behind a small interface: a clock that ticks every
// frame while playing (timeupdate alone is too coarse for following the
// words), plus play/pause/seek/rate. Emits 'tick' and 'state'.

const SPEEDS = [1, 1.25, 1.5, 2, 0.75];

export class Player extends EventTarget {
  constructor(audio) {
    super();
    this.audio = audio;
    this.available = false;
    this.error = null;
    this._raf = 0;
    audio.preload = 'metadata';
    const tick = () => this._emit('tick');
    audio.addEventListener('timeupdate', tick); // also covers background tabs, where rAF stops
    audio.addEventListener('seeked', tick);
    audio.addEventListener('loadedmetadata', () => { this._emit('state'); tick(); });
    audio.addEventListener('play', () => { this._loop(); this._emit('state'); });
    audio.addEventListener('pause', () => { this._stop(); this._emit('state'); tick(); });
    audio.addEventListener('ended', () => { this._stop(); this._emit('state'); });
    audio.addEventListener('ratechange', () => this._emit('state'));
    audio.addEventListener('error', () => {
      if (!this.available) return;
      this.error = audio.error ? audio.error.code : 'unknown';
      this._emit('state');
    });
  }

  _emit(type) { this.dispatchEvent(new Event(type)); }

  _loop() {
    cancelAnimationFrame(this._raf);
    const frame = () => { this._emit('tick'); this._raf = requestAnimationFrame(frame); };
    this._raf = requestAnimationFrame(frame);
  }

  _stop() { cancelAnimationFrame(this._raf); this._raf = 0; }

  /** Point the player at a (versioned) audio URL; keeps the current time when asked. */
  load(url, { keepTime = false, startAt = 0 } = {}) {
    const time = keepTime ? this.time : startAt;
    const wasPlaying = keepTime && this.playing;
    const rate = this.rate;
    this.error = null;
    this.available = !!url;
    this._stop();
    if (!url) {
      this.audio.removeAttribute('src');
      this.audio.load();
      this._emit('state');
      return;
    }
    this.audio.src = url;
    this.audio.playbackRate = rate;
    if (time > 0) this.audio.currentTime = time;
    if (wasPlaying) this.audio.play().catch(() => {});
    this._emit('state');
  }

  get time() { return this.audio.currentTime || 0; }
  get duration() { return Number.isFinite(this.audio.duration) ? this.audio.duration : 0; }
  get playing() { return !this.audio.paused && !this.audio.ended; }
  get rate() { return this.audio.playbackRate || 1; }

  seek(seconds) {
    if (!this.available) return;
    const limit = this.duration || Infinity;
    this.audio.currentTime = Math.min(Math.max(0, seconds), limit);
    this._emit('tick');
  }

  async play() {
    if (!this.available) return;
    try { await this.audio.play(); } catch { /* blocked or interrupted: the state events tell the UI */ }
  }

  pause() { this.audio.pause(); }
  toggle() { return this.playing ? this.pause() : this.play(); }

  cycleRate() {
    const i = SPEEDS.indexOf(this.rate);
    this.audio.playbackRate = SPEEDS[(i + 1) % SPEEDS.length];
  }
}
