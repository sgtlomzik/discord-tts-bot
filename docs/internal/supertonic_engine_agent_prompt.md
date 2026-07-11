# Задание агенту: добавить Supertonic 3 как альтернативный TTS-движок к существующему Discord TTS-боту на Piper

## 0. Главная цель

Добавить в существующий Discord TTS-бот второй локальный TTS-движок: **Supertonic 3**.

Это не миграция с Piper. Это безопасное расширение.

Piper должен остаться:

- дефолтным движком;
- полностью рабочим без Supertonic;
- rollback-вариантом;
- fallback-вариантом при ошибках Supertonic;
- неизменным по поведению для всех текущих пользователей, если Supertonic выключен.

Supertonic должен быть:

- выключен по умолчанию;
- доступен как экспериментальный `voice profile`;
- выбираем через Discord slash-команду;
- подключён через существующую очередь, worker и playback pipeline;
- интегрирован так, чтобы не ломать текущую архитектуру.

Итоговый критерий: при `SUPERTONIC_ENABLED=0` бот ведёт себя как раньше. При `SUPERTONIC_ENABLED=1` можно выбрать профиль Supertonic через Discord-команду и проиграть тестовую фразу, а весь старый Piper-пайплайн остаётся рабочим.

---

## 1. Контекст текущего проекта

Активная архитектура бота сейчас такая:

```text
Discord message
  -> on_message()
  -> access checks
  -> process_text() / analyze_message_for_merge()
  -> queue_or_merge_message()
  -> asyncio.Queue[TTSJob]
  -> tts_worker()
  -> generate_tts_file()
  -> generate_piper_file()
  -> WAV file
  -> ffmpeg PCM conversion
  -> ContinuousTTSAudioSource
  -> Discord voice playback
```

Основные файлы:

```text
bot.py              # основная логика бота, события Discord, очередь, TTS, playback, команды
test_bot.py         # тесты
docker-compose.yml  # runtime wiring
Dockerfile          # образ бота
requirements.txt    # зависимости Python
.env.example        # env-флаги
models/             # Piper ONNX model/config
data/config.json    # persisted guild/user settings
```

Важные текущие свойства:

- бот использует `asyncio.Queue` для TTS jobs;
- `TTSJob` уже содержит выбранный `voice profile`;
- worker подключается к voice и запускает TTS generation параллельно;
- после синтеза ожидается WAV-файл;
- playback уже общий: WAV → ffmpeg → PCM frames → Discord;
- `VOICE_PROFILES` сейчас содержит только `piper-ruslan`;
- `BotConfigStore` уже хранит default voice profile и per-user voice override;
- `/voicebot test` уже существует и ставит тестовую фразу в очередь;
- тесты уже покрывают config persistence, parser, queue/merge, worker success/failure и voice connect cooldowns.

Ключевой архитектурный seam: **движок нужно подключать около `generate_tts_file()` и `VOICE_PROFILES`, а не размазывать условия по `on_message()`, merge logic, worker и playback**.

---

## 2. Проверенные факты по Supertonic 3, которые учитывать

Перед реализацией перепроверь актуальную документацию, но на момент постановки задачи важно следующее.

Официальный репозиторий Supertonic:

```text
https://github.com/supertone-inc/supertonic
```

Официальный Python SDK / HTTP server:

```text
https://github.com/supertone-inc/supertonic-py
```

Supertonic 3:

- локальный on-device TTS;
- работает через ONNX Runtime;
- не требует GPU;
- имеет публичные fixed voices;
- поддерживает 31 язык, включая русский;
- модель около 99M параметров;
- Python SDK умеет `supertonic serve`;
- `supertonic serve` даёт native endpoint `/v1/tts`;
- также есть OpenAI-compatible endpoint `/v1/audio/speech`;
- response format может быть `wav`;
- first run скачивает модель примерно `~400MB`;
- custom voice JSON можно импортировать в локальный сервер через `/v1/styles/import`, но создание такого JSON через Voice Builder не входит в эту задачу.

Пример запуска локального сервера из документации:

```bash
pip install 'supertonic[serve]'
supertonic serve --host 127.0.0.1 --port 7788
```

Пример native endpoint:

```bash
curl -X POST http://127.0.0.1:7788/v1/tts \
  -H 'content-type: application/json' \
  -d '{
        "text": "Supertonic is a lightning fast, on-device TTS system.",
        "voice": "M1",
        "lang": "en",
        "steps": 8,
        "speed": 1.05,
        "response_format": "wav"
      }' \
  -o output.wav
```

Пример OpenAI-compatible endpoint:

```bash
curl -X POST http://127.0.0.1:7788/v1/audio/speech \
  -H 'content-type: application/json' \
  -d '{
        "model": "supertonic-3",
        "input": "Supertonic is a lightning fast, on-device TTS system.",
        "voice": "M1",
        "response_format": "wav"
      }' \
  -o output.wav
```

Для этой задачи предпочтителен native `/v1/tts`, потому что он явно поддерживает `lang`, `steps`, `speed`, `response_format` и лучше подходит для будущих custom voices.

---

## 3. Жёсткие ограничения

Не ломать то, что уже работает.

Запрещено:

- удалять Piper;
- менять дефолтный профиль с `piper-ruslan` на Supertonic;
- менять смысл `on_message()`;
- переписывать `queue_or_merge_message()` без необходимости;
- менять `selective_hold_v2`;
- менять порядок сообщений в очереди;
- менять voice connection lifecycle;
- переписывать playback pipeline;
- делать Supertonic обязательной зависимостью основного bot process;
- открывать Supertonic HTTP-сервер наружу;
- логировать Discord token, приватные env, полный текст длинных сообщений или содержимое custom voice JSON;
- добавлять voice cloning / Voice Builder workflow в первый этап;
- превращать эту задачу в глобальный рефакторинг.

Разрешено:

- добавить engine abstraction;
- обернуть существующий Piper path в `PiperTTSEngine` без изменения поведения;
- добавить HTTP-клиент для Supertonic;
- добавить новые voice profiles;
- добавить slash-команды выбора профиля;
- добавить fallback на Piper;
- добавить метрики;
- добавить тесты;
- добавить docker-compose service для Supertonic sidecar.

---

## 4. Архитектурное решение

### 4.1. Общая схема

Нужно прийти к такой схеме:

```text
voice_profile_id
  -> VOICE_PROFILES[voice_profile_id]
  -> engine: piper | supertonic
  -> generate_tts_file(text, profile_id)
  -> TTS_ENGINES[engine].synthesize_to_wav(...)
  -> WAV path
  -> existing ffmpeg PCM conversion
  -> existing ContinuousTTSAudioSource
  -> existing Discord playback
```

Важно: Supertonic должен отличаться только на этапе `text -> wav`. Всё после WAV должно остаться общим.

### 4.2. Почему Supertonic лучше подключать sidecar-сервисом

В первой версии Supertonic подключать через отдельный локальный HTTP service, а не импортом Python SDK внутрь `bot.py`.

Причины:

- если Supertonic упадёт, основной бот останется жив;
- если Supertonic съест память, это проще ограничить на уровне контейнера;
- не нужно тащить `supertonic`, `onnxruntime`, `fastapi`, `uvicorn` в основной bot image;
- меньше риск сломать Piper runtime;
- проще выключить Supertonic одним env-флагом;
- проще сделать healthcheck, timeout и fallback.

---

## 5. Реализация engine abstraction

Добавить общий контракт движков.

Если проект пока компактный, допустимо оставить это в `bot.py`. Если код уже разросся, можно вынести в `tts_engines.py`, но не делай большой рефакторинг ради красоты.

Ожидаемая идея:

```python
class TTSEngineError(Exception):
    pass


class BaseTTSEngine:
    name: str

    async def synthesize_to_wav(
        self,
        text: str,
        profile: dict,
        output_path: str,
    ) -> str:
        raise NotImplementedError
```

Engine registry:

```python
TTS_ENGINES = {
    "piper": PiperTTSEngine(...),
    "supertonic": SupertonicTTSEngine(...),
}
```

`generate_tts_file()` должен стать router:

```python
async def generate_tts_file(text: str, voice_profile: str) -> str:
    profile = resolve_voice_profile_or_default(voice_profile)
    engine_name = profile["engine"]
    engine = TTS_ENGINES[engine_name]
    output_path = make_temp_wav_path()
    return await engine.synthesize_to_wav(text, profile, output_path)
```

`generate_piper_file()` не удалять. Существующую реализацию Piper обернуть в `PiperTTSEngine`.

---

## 6. Voice profiles

Расширить `VOICE_PROFILES` так, чтобы profile описывал engine.

Пример:

```python
VOICE_PROFILES = {
    "piper-ruslan": {
        "engine": "piper",
        "label": "Piper Ruslan RU",
        "lang": "ru",
        "model_path": PIPER_MODEL_PATH,
        "config_path": PIPER_CONFIG_PATH,
        "speaker_id": None,
        "length_scale": None,
        "experimental": False,
    },
    "supertonic-m1-ru": {
        "engine": "supertonic",
        "label": "Supertonic M1 RU",
        "lang": "ru",
        "voice": "M1",
        "speed": 1.05,
        "steps": 8,
        "response_format": "wav",
        "experimental": True,
    },
    "supertonic-f1-ru": {
        "engine": "supertonic",
        "label": "Supertonic F1 RU",
        "lang": "ru",
        "voice": "F1",
        "speed": 1.05,
        "steps": 8,
        "response_format": "wav",
        "experimental": True,
    },
}
```

`TTS_DEFAULT_VOICE_PROFILE=piper-ruslan` должен остаться дефолтом.

В config хранить только profile id. Не хранить отдельно engine.

Правильно:

```json
{
  "default_voice_profile": "supertonic-m1-ru",
  "user_voice_profiles": {
    "123456789": "piper-ruslan"
  }
}
```

Неправильно:

```json
{
  "engine": "supertonic",
  "voice": "M1"
}
```

---

## 7. Supertonic HTTP engine

### 7.1. Env

Добавить в `.env.example`:

```env
# Alternative local TTS engine: Supertonic 3
SUPERTONIC_ENABLED=0
SUPERTONIC_BASE_URL=http://supertonic:7788
SUPERTONIC_TIMEOUT_SECONDS=20
SUPERTONIC_CONNECT_TIMEOUT_SECONDS=3
SUPERTONIC_MAX_TEXT_CHARS=500
SUPERTONIC_MAX_CONCURRENCY=1
SUPERTONIC_DEFAULT_VOICE=M1
SUPERTONIC_DEFAULT_LANG=ru
SUPERTONIC_DEFAULT_STEPS=8
SUPERTONIC_DEFAULT_SPEED=1.05
SUPERTONIC_RESPONSE_FORMAT=wav
TTS_SUPERTONIC_FALLBACK_TO_PIPER=1
TTS_SUPERTONIC_CIRCUIT_BREAKER_FAILURES=3
TTS_SUPERTONIC_CIRCUIT_BREAKER_COOLDOWN_SECONDS=60
```

Пояснение:

- `SUPERTONIC_ENABLED=0` гарантирует, что старый бот работает без Supertonic.
- `SUPERTONIC_MAX_CONCURRENCY=1` важно для слабого сервера, чтобы несколько сообщений не убили CPU/RAM.
- `SUPERTONIC_MAX_TEXT_CHARS=500` защищает от слишком тяжёлых запросов.
- Circuit breaker нужен, чтобы бот не пытался на каждое сообщение стучаться в сломанный сервис.

### 7.2. HTTP-клиент

Использовать `httpx`.

В `requirements.txt` основного бота добавить только:

```txt
httpx
```

Не добавлять `supertonic` в основной bot container на v1.

Желательно создать один переиспользуемый `httpx.AsyncClient`, а не создавать новый client на каждый TTS job.

Пример логики:

```python
class SupertonicTTSEngine(BaseTTSEngine):
    name = "supertonic"

    def __init__(self, base_url: str, timeout: float, max_concurrency: int):
        self.base_url = base_url.rstrip("/")
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=3.0),
            follow_redirects=False,
        )

    async def synthesize_to_wav(self, text: str, profile: dict, output_path: str) -> str:
        if not SUPERTONIC_ENABLED:
            raise TTSEngineError("Supertonic is disabled")

        safe_text = clamp_text(text, max_chars=SUPERTONIC_MAX_TEXT_CHARS)
        payload = {
            "text": safe_text,
            "voice": profile.get("voice", SUPERTONIC_DEFAULT_VOICE),
            "lang": profile.get("lang", SUPERTONIC_DEFAULT_LANG),
            "steps": int(profile.get("steps", SUPERTONIC_DEFAULT_STEPS)),
            "speed": float(profile.get("speed", SUPERTONIC_DEFAULT_SPEED)),
            "response_format": "wav",
        }

        async with self.semaphore:
            try:
                response = await self.client.post(f"{self.base_url}/v1/tts", json=payload)
                response.raise_for_status()
            except Exception as exc:
                raise TTSEngineError(f"Supertonic request failed: {type(exc).__name__}: {exc}") from exc

        content_type = response.headers.get("content-type", "")
        if "audio" not in content_type and not response.content.startswith(b"RIFF"):
            raise TTSEngineError(f"Unexpected Supertonic response type: {content_type}")

        write_bytes_atomically(output_path, response.content)
        return output_path
```

Это не финальный код, а ориентир. Реализацию адаптировать под стиль проекта.

### 7.3. Обязательные защиты

Добавить:

- timeout;
- connect timeout;
- max text length;
- max concurrency;
- response content-type / RIFF validation;
- atomic file write;
- temp file cleanup;
- fallback на Piper;
- circuit breaker;
- structured logs without secrets.

---

## 8. Fallback на Piper

Нельзя позволить Supertonic сломать обычную озвучку.

Логика:

```text
selected profile = supertonic-m1-ru
  -> try Supertonic
       -> success: play Supertonic WAV
       -> failure:
            log warning
            if TTS_SUPERTONIC_FALLBACK_TO_PIPER=1:
                generate Piper WAV using piper-ruslan
                play Piper WAV
            else:
                skip job safely
```

Для обычных сообщений fallback должен быть тихим, но логируемым.

Для `/voicebot test` нужно явно сообщать пользователю, что тест был проигран через fallback или что Supertonic недоступен.

Лог должен быть примерно такой:

```text
tts_engine=supertonic profile=supertonic-m1-ru status=failed fallback=piper-ruslan error_type=ConnectError
```

Не логировать полный текст длинных сообщений. Можно логировать `text_len`, hash или первые 50 символов только при debug-флаге.

---

## 9. Circuit breaker

Добавить простую защиту от постоянных повторных запросов к упавшему Supertonic.

Логика:

```text
if failures >= TTS_SUPERTONIC_CIRCUIT_BREAKER_FAILURES:
    mark supertonic unavailable until now + cooldown

while unavailable:
    skip Supertonic request immediately
    fallback to Piper

on successful Supertonic request:
    reset failures
```

Это особенно важно для слабого сервера и Discord-чата, где несколько сообщений подряд могут вызвать лавину ошибок.

---

## 10. Docker Compose

Добавить отдельный service `supertonic`.

Вариант для v1:

```yaml
services:
  supertonic:
    image: python:3.11-slim
    command: >
      sh -c "pip install 'supertonic[serve]' &&
             supertonic serve --host 0.0.0.0 --port 7788"
    expose:
      - "7788"
    volumes:
      - supertonic-cache:/root/.cache
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7788/docs', timeout=3).read(100)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 120s

volumes:
  supertonic-cache:
```

Важно по безопасности:

- использовать `expose`, а не `ports`, чтобы не публиковать Supertonic наружу;
- `--host 0.0.0.0` допустим только внутри Docker network, если нет `ports`;
- если запускается без Docker sidecar, тогда bind должен быть `127.0.0.1`;
- не добавлять reverse proxy наружу;
- не давать контейнеру доступ к Docker socket;
- не запускать с `privileged: true`;
- не монтировать лишние директории хоста;
- cache volume нужен, чтобы модель не скачивалась заново каждый старт.

Если в текущем compose bot service не должен ждать Supertonic, не делать жёсткий `depends_on` как обязательное условие запуска бота. Supertonic должен быть optional.

Можно добавить soft dependency:

```yaml
  bot:
    environment:
      - SUPERTONIC_ENABLED=1
      - SUPERTONIC_BASE_URL=http://supertonic:7788
```

Но бот обязан стартовать даже если `supertonic` не поднялся.

---

## 11. Discord-команды

Сейчас есть `/voicebot` group. Добавить команды, не ломая старые.

Нужно реализовать:

```text
/voicebot voices
/voicebot set-voice profile:<profile>
/voicebot set-user-voice member:<member> profile:<profile>
/voicebot reset-voice
/voicebot test text:<text> profile:<optional>
```

Поведение:

### `/voicebot voices`

Показывает доступные профили:

```text
piper-ruslan        Piper Ruslan RU        engine=piper       default
supertonic-m1-ru    Supertonic M1 RU       engine=supertonic  experimental available/unavailable
supertonic-f1-ru    Supertonic F1 RU       engine=supertonic  experimental available/unavailable
```

Если `SUPERTONIC_ENABLED=0`, Supertonic профили можно:

- либо скрыть;
- либо показать как `disabled`.

Лучше показать как `disabled`, чтобы было понятно, что функциональность существует, но выключена.

### `/voicebot set-voice profile:<profile>`

Меняет default voice profile для guild.

Если профиль неизвестен — ошибка.

Если профиль Supertonic, но `SUPERTONIC_ENABLED=0` — не сохранять, вернуть понятное сообщение.

### `/voicebot set-user-voice member:<member> profile:<profile>`

Меняет профиль конкретного пользователя.

Проверки такие же.

### `/voicebot reset-voice`

Возвращает guild default voice profile на `piper-ruslan`.

### `/voicebot test text:<text> profile:<optional>`

Если `profile` указан, тестирует именно этот профиль без сохранения настройки.

Если `profile` не указан, использует текущий resolved profile.

На ошибке Supertonic команда должна явно сообщить:

```text
Supertonic недоступен, тест проигран через Piper fallback.
```

или:

```text
Supertonic недоступен: connection refused.
```

Формулировку адаптировать под текущий стиль ответов бота.

---

## 12. Startup и warmup

Существующий Piper warmup оставить как есть.

Supertonic warmup сделать optional и non-blocking.

Логика:

```text
on_ready()
  -> start worker
  -> warmup Piper as before
  -> if SUPERTONIC_ENABLED:
        create_task(warmup_supertonic())
```

Supertonic warmup:

- не должен валить запуск бота;
- должен иметь timeout;
- должен учитывать circuit breaker;
- должен писать warning при ошибке;
- должен использовать короткую фразу: `Проверка синтеза речи.`

---

## 13. Playback не менять

Supertonic должен отдавать WAV.

Текущий pipeline уже умеет:

```text
WAV -> ffmpeg PCM frames -> ContinuousTTSAudioSource -> Discord voice client
```

Проверить только:

- текущий ffmpeg command не завязан жёстко на sample rate Piper;
- Supertonic WAV 44.1 kHz корректно ресемплится в Discord-compatible PCM;
- `response_format=wav` всегда используется;
- если Supertonic вернул пустой или битый файл, это ловится до playback.

Не переписывать `ContinuousTTSAudioSource` в этой задаче.

---

## 14. Эффективность

Добавить минимальные оптимизации, но не усложнять v1.

Обязательно:

- persistent Supertonic sidecar, не запускать CLI на каждую реплику;
- переиспользуемый `httpx.AsyncClient`;
- `SUPERTONIC_MAX_CONCURRENCY=1` по умолчанию;
- timeout и circuit breaker;
- короткий warmup;
- cache volume для модели;
- engine metrics в логах;
- не блокировать event loop CPU-heavy операциями в bot process.

Не обязательно в v1:

- кэш готовых Supertonic WAV/PCM;
- batch synthesis;
- разделение synth worker и playback worker;
- dynamic quality steps;
- custom voice import command;
- preloading всех голосов.

Но код должен быть написан так, чтобы позже можно было добавить кэш коротких фраз.

---

## 15. Безопасность

Обязательные требования:

1. Supertonic HTTP endpoint не должен быть доступен из интернета.
2. В docker-compose использовать `expose`, не `ports`.
3. Если сервис запускается на host network, bind только на `127.0.0.1`.
4. Любые URL в `SUPERTONIC_BASE_URL` валидировать хотя бы грубо: разрешить только `http://supertonic:7788`, `http://127.0.0.1:*`, `http://localhost:*` или явно документировать trusted env.
5. Не принимать произвольный URL Supertonic из Discord-команд.
6. Не давать пользователям импортировать custom voice JSON в v1.
7. Не логировать токены, env secrets, полный текст длинных сообщений, binary audio, JSON voice embeddings.
8. Ограничить длину текста для Supertonic.
9. Ограничить concurrency.
10. Все temp files создавать в безопасной temp directory и удалять после playback.
11. Не использовать shell-команды с пользовательским текстом.
12. Если где-то вызывается subprocess, передавать аргументы списком, без `shell=True`.
13. При ошибках Supertonic не падать процессом бота.
14. При ошибке Supertonic не зацикливаться на retries.
15. На уровне команд проверять permissions так же, как у текущих admin voicebot-команд.

---

## 16. Метрики и логи

Добавить engine-aware logs.

Минимально:

```text
tts_job_start engine=supertonic profile=supertonic-m1-ru text_len=42
tts_synth_done engine=supertonic synth_ms=831 wav_bytes=123456 audio_duration=2.41
tts_pcm_done engine=supertonic pcm_ms=194
tts_playback_done engine=supertonic total_ms=3120
tts_fallback engine=supertonic fallback=piper-ruslan error_type=ConnectError
```

Если доступны HTTP response headers от Supertonic:

- `X-Audio-Duration`;
- `X-Sample-Rate`;
- `X-Supertonic-Version`.

Логировать их как debug/info без избыточного шума.

---

## 17. Тесты

Добавить тесты в `test_bot.py`. Не удалять старые.

Минимальный набор:

1. При `SUPERTONIC_ENABLED=0` дефолтный профиль остаётся `piper-ruslan`.
2. `VOICE_PROFILES` содержит `piper-ruslan` с `engine=piper`.
3. При включённом Supertonic доступны `supertonic-m1-ru` и `supertonic-f1-ru`.
4. `generate_tts_file()` маршрутизирует `piper-ruslan` в Piper engine.
5. `generate_tts_file()` маршрутизирует `supertonic-m1-ru` в Supertonic engine.
6. Unknown profile безопасно fallback'ается на default profile или возвращает контролируемую ошибку, согласно текущему стилю проекта.
7. Supertonic HTTP success: mock `audio/wav`, bytes начинаются с `RIFF`, файл записан.
8. Supertonic timeout: ошибка поймана, bot process не падает.
9. Supertonic connection error: ошибка поймана, fallback на Piper вызывается.
10. Supertonic bad content-type / bad bytes: ошибка поймана, fallback на Piper вызывается.
11. Circuit breaker открывается после N ошибок.
12. Circuit breaker сбрасывается после успешного запроса или после cooldown.
13. `/voicebot voices` показывает Piper всегда.
14. `/voicebot voices` показывает Supertonic как disabled/unavailable при `SUPERTONIC_ENABLED=0`.
15. `/voicebot set-voice profile:supertonic-m1-ru` не сохраняет профиль при disabled Supertonic.
16. `/voicebot set-voice profile:supertonic-m1-ru` сохраняет профиль при enabled Supertonic.
17. `/voicebot reset-voice` возвращает `piper-ruslan`.
18. `/voicebot test profile:<profile>` не меняет persisted guild config.
19. Старые команды `on`, `off`, `allow`, `deny`, `status`, `queue-clear`, `test` не ломаются.
20. При `SUPERTONIC_ENABLED=0` все старые тесты проходят без обязательного Supertonic service.

Тесты не должны требовать реального Supertonic-сервера. Используй mocks/stubs.

---

## 18. Ручная проверка

После реализации выполнить:

```bash
git status
pytest -q
```

Запуск Supertonic sidecar:

```bash
docker compose up -d supertonic
docker compose logs -f supertonic
```

Проверка из bot container:

```bash
docker compose exec bot python - <<'PY'
import httpx
r = httpx.get('http://supertonic:7788/docs', timeout=5)
print(r.status_code)
print(r.text[:100])
PY
```

Проверка native TTS endpoint:

```bash
curl -X POST http://localhost:7788/v1/tts \
  -H 'content-type: application/json' \
  -d '{
        "text":"Привет, это проверка Supertonic.",
        "voice":"M1",
        "lang":"ru",
        "steps":8,
        "speed":1.05,
        "response_format":"wav"
      }' \
  -o /tmp/supertonic_test.wav
```

Если `ports` не публикуются наружу, проверку `curl http://localhost:7788` с host может быть невозможно выполнить. Тогда проверять из контейнера внутри Docker network.

Discord smoke test:

```text
/voicebot voices
/voicebot test text:Привет, это Piper profile:piper-ruslan
/voicebot test text:Привет, это Supertonic profile:supertonic-m1-ru
/voicebot set-voice profile:supertonic-m1-ru
/voicebot status
/voicebot reset-voice
/voicebot status
```

Проверка fallback:

```bash
docker compose stop supertonic
```

Потом в Discord:

```text
/voicebot test text:Проверка fallback profile:supertonic-m1-ru
```

Ожидаемо: бот не падает, тест либо сообщает об ошибке Supertonic, либо проигрывает через Piper fallback с понятным сообщением.

---

## 19. Rollback

Rollback должен быть простым.

Способ 1:

```env
SUPERTONIC_ENABLED=0
TTS_DEFAULT_VOICE_PROFILE=piper-ruslan
```

Способ 2:

```bash
docker compose stop supertonic
```

Бот должен продолжить работать на Piper.

Способ 3:

```bash
git revert <supertonic_commit>
```

---

## 20. Что не входит в эту задачу

Не реализовывать сейчас:

- voice cloning;
- покупку/создание Voice Builder JSON;
- Discord-команду импорта custom voice JSON;
- RVC / GPT-SoVITS / XTTS;
- автоматическое переключение всех пользователей на Supertonic;
- замену Piper;
- оптимизацию emoji policy;
- кэш коротких реакций;
- отдельный synth worker / playback worker;
- UI для управления параметрами `steps`, `speed`, `voice` из Discord;
- публичный HTTP API для TTS.

Эти пункты можно оставить как future work.

---

## 21. Ожидаемый diff по файлам

```text
bot.py
  + BaseTTSEngine / TTSEngineError или эквивалент
  + PiperTTSEngine wrapper над текущим Piper path
  + SupertonicTTSEngine HTTP client
  + engine registry
  + profile resolver
  + fallback logic
  + circuit breaker
  + engine-aware logs/metrics
  + slash commands for profile listing/selection/reset
  + /voicebot test profile:<optional>

requirements.txt
  + httpx

.env.example
  + SUPERTONIC_* flags
  + TTS_SUPERTONIC_* flags

docker-compose.yml
  + optional supertonic service
  + supertonic-cache volume
  + no public ports for Supertonic

test_bot.py
  + engine routing tests
  + Supertonic HTTP mock tests
  + fallback tests
  + circuit breaker tests
  + voice profile command tests

data/config.json
  no manual migration required; old configs must still load
```

---

## 22. Критерии готовности

Задача готова только если выполнено всё ниже:

1. `pytest -q` проходит.
2. При `SUPERTONIC_ENABLED=0` бот работает как раньше.
3. Piper остаётся дефолтом.
4. Старый `piper-ruslan` профиль работает.
5. Supertonic profiles не ломают запуск бота, даже если sidecar выключен.
6. При `SUPERTONIC_ENABLED=1` `/voicebot voices` показывает Supertonic profiles.
7. `/voicebot test ... profile:supertonic-m1-ru` пытается использовать Supertonic.
8. Supertonic output проходит через старый WAV → ffmpeg → PCM → Discord playback path.
9. При падении/таймауте Supertonic бот не падает.
10. При падении/таймауте Supertonic обычная озвучка fallback'ается на Piper, если `TTS_SUPERTONIC_FALLBACK_TO_PIPER=1`.
11. Supertonic HTTP endpoint не опубликован наружу через `ports`.
12. Нет логов с secrets или большими пользовательскими текстами.
13. Есть понятный rollback через env.

---

## 23. Рекомендуемый порядок работы

1. Сделай checkpoint commit перед изменениями.
2. Прогони текущие тесты.
3. Добавь profile schema и engine abstraction.
4. Оберни Piper в `PiperTTSEngine`, не меняя поведения.
5. Добавь Supertonic profile definitions, но оставь disabled by default.
6. Добавь `SupertonicTTSEngine` с mock-friendly HTTP client.
7. Добавь fallback и circuit breaker.
8. Добавь slash commands.
9. Добавь env flags.
10. Добавь docker-compose sidecar.
11. Добавь тесты.
12. Прогони тесты.
13. Сделай ручной smoke test с Supertonic sidecar.
14. Проверь rollback.
15. Закоммить изменения.

---

## 24. Итоговая формулировка для коммита

```text
Add optional Supertonic TTS engine profile
```

Описание коммита:

```text
- introduce TTS engine abstraction and profile-level engine selection
- keep Piper as default and fallback engine
- add optional Supertonic HTTP sidecar client
- add Supertonic voice profiles behind SUPERTONIC_ENABLED
- add voice profile listing/selection/reset commands
- add fallback, timeout, circuit breaker, and engine metrics
- add docker-compose sidecar and tests
```
