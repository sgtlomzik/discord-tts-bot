# Discord TTS bot: disconnect audit

Дата ревью: `2026-05-17`

## Что я посмотрел

Я собрал два слоя фактов:

1. `docker logs discord_tts_bot` — доступный runtime-log текущего контейнера.
2. Экспорт сообщений пользователя `bussshy` за последний период в `exports/`, чтобы понимать, в какие моменты бот получал последовательности реплик.

Важно: текущий контейнер `discord_tts_bot` был поднят недавно, поэтому доступный Docker-log на этой машине покрывает не полный календарный месяц, а только историю этого инстанса. Ниже я честно разделяю:

- что реально видно в логах бота;
- что можно вывести из кода;
- что пока не подтверждено историей на этом хосте.

## Краткий вывод

В текущей реализации бот отключается только по нескольким причинам:

1. `idle disconnect` после истечения таймера без активного воспроизведения;
2. явное отключение через `/voicebot off` или команду `stop`;
3. аварийная очистка при ошибке подготовки TTS или playback;
4. автоматическое планирование idle-disconnect при смене голосового состояния, если в канале не осталось разрешённых пользователей.

Из доступного runtime-лога я увидел:

- `Generating Piper TTS`: `11`
- `Queued TTS guild=`: `10`
- `Playback finished guild=`: `10`
- `Scheduled idle disconnect guild=`: `10`
- `Scheduled continuous stream idle stop guild=`: `10`
- `Continuous stream idle stop executed guild=`: `1`
- `Idle disconnect executed guild=`: `1`
- `Voice disconnected guild=`: `0`
- `Voice connect cooldown set`: `0`

То есть в доступной истории есть только **один фактический выход из voice-соединения**. Остальные случаи — это постановка таймера на будущий idle-disconnect после завершения воспроизведения.

## Что реально произошло в логах

Ниже — последовательность из runtime-лога, которая и показывает механизм отключения:

```text
2026-05-17 20:43:04,083 INFO [tts_bot] Scheduled idle disconnect guild=1030870431181312060 timeout=900s
2026-05-17 20:43:04,083 INFO [tts_bot] Scheduled continuous stream idle stop guild=1030870431181312060 timeout=900s

2026-05-17 20:58:04,084 INFO [tts_bot] Continuous stream idle stop executed guild=1030870431181312060
2026-05-17 20:58:04,084 INFO [discord.voice_state] The voice handshake is being terminated for Channel ID 1030870431181312064 (Guild ID 1030870431181312060)
2026-05-17 20:58:04,409 INFO [tts_bot] Auto-connect suppressed guild=1030870431181312060 seconds=30 reason=idle_disconnect
2026-05-17 20:58:04,409 INFO [tts_bot] Idle disconnect executed guild=1030870431181312060
```

Интерпретация:

- после завершения playback бот ставит два таймера;
- через `900s` срабатывает `continuous stream idle stop`;
- почти сразу после этого срабатывает `idle disconnect`;
- после disconnect выставляется suppress-auto-connect на `30s`, чтобы бот не зациклился на повторном автоподключении.

## Когда бот отключается

### 1. Idle disconnect после простоя

Это основной путь автоматического отключения.

Механика:

- после каждого успешного playback вызывается `schedule_idle_disconnect(...)`;
- task спит `IDLE_DISCONNECT_SECONDS`;
- когда sleep заканчивается, код проверяет, есть ли voice client и не играет ли он сейчас;
- если всё тихо, бот делает `vc.disconnect(force=True)`.

Это не “про текст сообщения”, а именно про состояние voice после завершения воспроизведения.

### 2. Авто-отключение при уходе пользователей из voice-канала

Если бот уже подключён, и в канале больше не осталось разрешённых пользователей, а playback не активен, вызывается `schedule_idle_disconnect(...)` из `on_voice_state_update(...)`.

Это отдельный триггер:

- не по тексту;
- не по очереди TTS;
- а по составу участников voice-канала.

### 3. Явное отключение через команды

Команды:

- `/voicebot off`
- `stop`

Общий путь: `disconnect_guild_voice(...)`.

### 4. Аварийная очистка при ошибке

Если во время подготовки или воспроизведения возникает исключение, worker вызывает `disconnect_guild_voice(...)`, если voice client реально подключён.

Это защитный путь, чтобы бот не оставался висеть в плохом состоянии.

## Что именно НЕ является отключением

### Continuous stream idle stop

`schedule_continuous_idle_stop(...)` — это не отключение voice-соединения как такового.

Это остановка continuous source:

- очищается `continuous_sources`;
- вызывается `source.stop()`;
- при необходимости останавливается player.

То есть это “остановить поток”, а не обязательно “выйти из voice”.

В логах это важно не путать:

- `Continuous stream idle stop executed` — источник остановлен;
- `Idle disconnect executed` — bot реально ушёл из voice.

## Код, который отвечает за авто-отключение

Ниже — ключевые фрагменты.

### Таймер idle disconnect

[`bot.py`](./bot.py#L789-L816)

```python
def schedule_idle_disconnect(self, guild: discord.Guild) -> None:
    self.cancel_idle_disconnect(guild.id)
    task = asyncio.create_task(
        self._idle_disconnect_after_timeout(guild),
        name=f"idle-disconnect-{guild.id}",
    )
    self.idle_disconnect_tasks[guild.id] = task
    log.info("Scheduled idle disconnect guild=%s timeout=%ss", guild.id, IDLE_DISCONNECT_SECONDS)

async def _idle_disconnect_after_timeout(self, guild: discord.Guild) -> None:
    try:
        await asyncio.sleep(IDLE_DISCONNECT_SECONDS)

        vc = discord.utils.get(self.voice_clients, guild=guild)
        if not vc or not vc.is_connected():
            return

        if vc.is_playing() or vc.is_paused():
            log.info("Skip idle disconnect guild=%s reason=playback_active", guild.id)
            return

        await vc.disconnect(force=True)
        self.suppress_auto_connect(guild.id, "idle_disconnect")
        log.info("Idle disconnect executed guild=%s", guild.id)
```

Что здесь важно:

- таймер ставится отдельно от очереди TTS;
- перед disconnect есть защита от активного playback;
- после disconnect включается suppression для автоподключения.

### Момент, когда таймер ставится после playback

[`bot.py`](./bot.py#L1361-L1399)

```python
if TTS_CONTINUOUS_STREAM:
    source = self.ensure_continuous_player(vc)
    frames = await self.prepare_tts_pcm_frames(filename)
    audio_enqueue_ts = time.perf_counter()
    source.enqueue_frames(frames)
    log.info(
        "Queued continuous playback guild=%s channel=%s frames=%s duration=%.3fs message_to_audio_enqueue_s=%.3f queue_to_audio_enqueue_s=%.3f",
        job.voice_channel.guild.id,
        job.voice_channel.id,
        len(frames),
        len(frames) * PCM_FRAME_MS / 1000,
        audio_enqueue_ts - job.message_ts,
        audio_enqueue_ts - job.queued_at,
    )
    await source.wait_until_drained()
else:
    playback_request_ts = time.perf_counter()
    log.info(
        "Starting non-continuous playback metrics message_to_playback_request_s=%.3f queue_to_playback_request_s=%.3f",
        playback_request_ts - job.message_ts,
        playback_request_ts - job.queued_at,
    )
    await self.play_file(vc, filename)

log.info(
    "Playback finished guild=%s channel=%s total_since_queue=%.3fs",
    job.voice_channel.guild.id,
    job.voice_channel.id,
    time.perf_counter() - job.queued_at,
)

self.schedule_continuous_idle_stop(job.voice_channel.guild)
self.schedule_idle_disconnect(job.voice_channel.guild)
```

Здесь видно главное:

- disconnect не происходит сразу после playback;
- сначала бот ждёт завершения воспроизведения;
- только потом ставит idle timers.

### Явное отключение и cleanup

[`bot.py`](./bot.py#L1544-L1558)

```python
async def disconnect_guild_voice(self, guild: discord.Guild) -> None:
    self.cancel_idle_disconnect(guild.id)
    self.cancel_continuous_idle_stop(guild.id)
    source = self.continuous_sources.pop(guild.id, None)
    if source:
        source.stop()
    vc = discord.utils.get(self.voice_clients, guild=guild)
    if not vc:
        return
    try:
        await vc.disconnect(force=True)
        self.suppress_auto_connect(guild.id, "explicit_disconnect")
        log.info("Voice disconnected guild=%s", guild.id)
    except Exception:
        log.exception("Voice disconnect cleanup failed")
```

Эта функция используется:

- при `/voicebot off`;
- при `stop`;
- при ошибках worker-а;
- иногда как cleanup при проблемах соединения/воспроизведения.

### Подключение и cooldown

[`bot.py`](./bot.py#L858-L900)

```python
async def ensure_voice(self, voice_channel: discord.VoiceChannel) -> discord.VoiceClient:
    started = time.perf_counter()
    guild_id = voice_channel.guild.id
    self.cancel_idle_disconnect(guild_id)
    lock = self.get_voice_connect_lock(guild_id)

    async with lock:
        remaining = self.voice_connect_cooldown_remaining(guild_id)
        if remaining > 0:
            raise RuntimeError(f"Voice connect cooldown active for {remaining:.1f}s")

        vc = discord.utils.get(self.voice_clients, guild=voice_channel.guild)

        if not vc or not vc.is_connected():
            log.info("Connecting to voice channel guild=%s channel=%s", guild_id, voice_channel.id)
            try:
                vc = await voice_channel.connect(timeout=60.0, self_deaf=True)
            except Exception as exc:
                self.set_voice_connect_cooldown(guild_id, type(exc).__name__)
                raise
```

Это не disconnect, но важно для поведения после него:

- если connect падает, ставится cooldown;
- бот не будет бесконечно молотить reconnect;
- это дополнительная защита от нестабильного voice-layer.

### Срабатывание по voice-state

[`bot.py`](./bot.py#L1666-L1693)

```python
@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if member.bot:
        return
    if not bot.config_store.is_enabled(member.guild.id):
        return
    if not bot.config_store.is_allowed(member.guild.id, member.id):
        return

    if isinstance(after.channel, discord.VoiceChannel):
        await bot.auto_connect_for_member(member, after.channel)

    guild = member.guild
    vc = discord.utils.get(bot.voice_clients, guild=guild)
    if not vc or not vc.is_connected():
        return

    if isinstance(vc.channel, discord.VoiceChannel):
        whitelisted_present = any(
            (not user.bot) and bot.config_store.is_allowed(guild.id, user.id)
            for user in vc.channel.members
        )
        if not whitelisted_present and not vc.is_playing() and not vc.is_paused():
            bot.schedule_idle_disconnect(guild)
```

Это второй автоматический путь:

- если разрешённых пользователей в voice больше нет;
- и бот не занят playback;
- он ставит idle-disconnect.

### Команды ручного отключения

[`bot.py`](./bot.py#L1753-L1763) и [`bot.py`](./bot.py#L1864-L1874)

```python
@tts_group.command(name="off", description="Отключить озвучку на сервере")
async def slash_tts_off(interaction: discord.Interaction) -> None:
    ...
    await bot.disconnect_guild_voice(guild)

@bot.command()
async def stop(ctx: commands.Context) -> None:
    ...
    await bot.disconnect_guild_voice(ctx.guild)
```

Это явное управление, не авто-логика.

## Что значит “почему бот отключается”

В короткой формулировке:

- бот отключается не из-за текста сообщения;
- он отключается из-за простоя voice-потока после завершения playback;
- или из-за явного stop/off;
- или как cleanup при ошибке;
- или когда в voice больше нет разрешённых пользователей и воспроизведение не идёт.

## Что в логах стоит считать нормой

Для этого кода нормальны такие цепочки:

- `Queued TTS ...`
- `Playback finished ...`
- `Scheduled idle disconnect ...`
- через 900s:
  - `Idle disconnect executed ...`

Также нормален intermediate log:

- `Scheduled continuous stream idle stop ...`

Потому что continuous source и voice client — это разные уровни.

## Что пока не подтверждено историей на этом хосте

На текущем контейнере я не увидел:

- `Voice disconnected guild=...`
- `Voice connect cooldown set ...`
- `Skip idle disconnect ... reason=playback_active`

Это не значит, что код не умеет это делать; это значит, что в доступной истории этого инстанса такие ветки просто не сработали.

## Практический вывод

Авто-отключение сейчас выглядит логически чисто:

- есть явная постановка таймера;
- есть защита от disconnect во время playback;
- есть cleanup-путь;
- есть защита от reconnect-лупов.

По фактическим логам бот действительно один раз вышел из voice корректно, после scheduled idle timeout, без признаков аварии.

