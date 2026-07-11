# План интеграции MiniMax TTS в tts-bot

> Репозиторий: `/srv/dev-disk-by-label-DataDrive/tts-bot`
> Контейнер: `discord_tts_bot` (image `custom-tts-bot:latest`)
> Текущая ветка: `beta` (base `master`)
> Назначение бота: озвучка сообщений пользователя `bussshy` (Discord ID `995245409960214558`, гильдия `1030870431181312060`)
> Спецификация: `F:\AI\minimax_tts_integration_prompt.md`

---

## 1. Резюме текущего флоу (как есть сейчас)

### 1.1 Архитектура в одну строку
Один Python-модуль `bot.py` (2055 строк) делает всё: события Discord → фильтр по пользователю/голосовому каналу → нормализация текста → merge-буфер (selective_hold_v2) → очередь `asyncio.Queue` → фоновый воркер → синтез Piper (локальный ONNX) → FFmpeg-конвертация в PCM-фреймы → continuous-stream в Discord-войс → idle-disconnect.

### 1.2 Поток сообщения — конкретные точки в коде

```
Discord message
  ↓ bot.py:1731 on_message()
    · гейт: guild, enabled, allowed_users, author.voice, VoiceChannel
    · вызов queue_or_merge_message()  ← bot.py:1144
  ↓ bot.py:438 analyze_message_for_merge() внутри очереди
    · _strip_discord_tokens_for_speech()  ← bot.py:426
      - вырезает <a?:name:id>, <@!?id>, <@&id>, <#id>, https?://...
      - символы эмодзи → EMOJI_MAP (алиасы), либо в пустоту
    · классификация (URL-only, mention-only, emoji-only, smashes…)
  ↓ bot.py:1144 queue_or_merge_message()
    · selective_hold_v2: hold_start / append_soft / flush_before_reclassify / immediate_*
    · на flush → bot.py:1014 enqueue_tts(parsed.spoken_text, …)
  ↓ bot.py:1014 enqueue_tts()
    · MAX_TEXT_LENGTH truncate (по умолчанию 500, в .env выставлено)
    · создаёт TTSJob(text, voice_channel, queued_at, author_id, guild_id,
                      text_channel_id, voice_profile, message_ts)
    · кладёт в message_queue (asyncio.Queue, maxsize=50)
  ↓ bot.py:1395 tts_worker()
    · wait_until_ready + warmup_tts  ← bot.py:1000
    · цикл:
        job = await queue.get()
        filename = /dev/shm/tts_<uuid>.wav
        asyncio.gather(
            ensure_voice(job.voice_channel),                 ← параллельный войс-коннект
            generate_tts_file(job.text, filename, profile)   ← ← ← ТОЧКА ВСТРОЙКИ
        )
        ensure_continuous_player(vc) → prepare_tts_pcm_frames(filename)
        → source.enqueue_frames(frames) → wait_until_drained
        → schedule_continuous_idle_stop → schedule_idle_disconnect
```

### 1.3 Точка встройки (единственная, которую трогаем для диспетчера)

**`bot.py:1390 generate_tts_file()`** — на сегодня это 3 строки:

```python
async def generate_tts_file(self, text: str, filename: Path, voice_profile: str | None = None) -> None:
    profile = VOICE_PROFILES.get(voice_profile or DEFAULT_VOICE_PROFILE, VOICE_PROFILES[DEFAULT_VOICE_PROFILE])
    await self.generate_piper_file(text, filename, profile)
    log.info("TTS engine used: piper profile=%s", profile.name)
```

Используется в **двух местах**:
- `bot.py:1003 warmup_tts()` — стартовый прогрев (`"Привет"`)
- `bot.py:1408 tts_worker()` — основной путь

Контракт функции: **пишет WAV-файл по пути `filename`**. Этот контракт идеально сохраняется — наши провайдеры просто будут обязаны выдать валидный WAV на диске.

### 1.4 Текущая препроцессинг-цепочка

| Функция | Где | Что делает |
|---|---|---|
| `process_text()` | `bot.py:396` | URL → `""`, `<a?:name:id>` → алиас из `EMOJI_MAP` (или `""`), `\n` → `". "`, схлопывание пробелов. **Не** вырезает `<@id>`, `<#id>` |
| `_strip_discord_tokens_for_speech()` | `bot.py:426` | `CUSTOM_EMOJI_RE`, `MENTION_RE`, `URL_RE` → пробел. Жёстче, чем `process_text()` |
| `analyze_message_for_merge()` | `bot.py:435` | Парсит + классифицирует, заполняет `ParsedMessage.spoken_text` (через `_strip_discord_tokens_for_speech`) |
| `enqueue_tts()` | `bot.py:1014` | `text[:MAX_TEXT_LENGTH]` — текущий лимит длины |
| `/voicebot test` | `bot.py:1989` | Использует **legacy `process_text()`**, не новый стриппер — нужно унифицировать |

**Расхождение со спекой:** спек требует вырезать упоминания, каналы, ссылки, кастомные эмодзи **всегда** (не только для классификации). Сейчас в основном пути (`queue_or_merge_message`) это уже происходит через `_strip_discord_tokens_for_speech`. В команде `/voicebot test` — нет. Нужен единый нормализатор на входе в TTS.

### 1.5 Голосовые профили и модель Piper

`bot.py:228` — `VOICE_PROFILES` — это `dict[str, VoiceProfile]`, где `VoiceProfile` (`bot.py:146`) — `@dataclass(frozen=True)` с полями:
- `name`, `label`
- `piper_model_path`, `piper_config_path`, `piper_speaker`, `piper_length_scale`

Сейчас в проде 4 профиля в `/srv/dev-disk-by-label-DataDrive/tts-bot/models/` (ruslan, irina, denis, dmitri), но в `VOICE_PROFILES` зарегистрировано только 2 (`piper-ruslan`, `piper-irina`). Это отдельный вопрос, не блокирует интеграцию.

### 1.6 Среда и тесты

- Python 3.11 (Dockerfile slim)
- `requirements.txt`: `discord.py==2.7.1`, `PyNaCl==1.6.2`, `davey==0.1.5`, `piper-tts==1.4.2`
- `test_bot.py` — 1091 строка, покрывает: нормализацию текста, парсинг, конфиг, очередь/merge, selective hold, войс-cooldown, воркер
- `.venv` уже есть в репо (`/srv/dev-disk-by-label-DataDrive/tts-bot/.venv`)
- Команда тестов: `.venv/bin/python -m unittest -v test_bot.py`
- Контейнер: `docker compose build && docker compose up -d`, монтирует `./bot.py` напрямую → изменения применяются без пересборки образа

### 1.7 Что в .env уже есть (но не относится к MiniMax)

`SUPERTONIC_*` переменные — это остатки предыдущего эксперимента с Supertonic-движком, к нашему MiniMax никакого отношения не имеют. Их не трогаем (если только не будет прямого конфликта имён).

---

## 2. Архитектурное решение

### 2.1 Контракт диспетчера — **через файл, не через буфер**

Спека предлагает `synthesize(text) -> audio_buffer`. **Но** существующий пайплайн после синтеза делает FFmpeg-конвертацию WAV → PCM-фреймы (`prepare_tts_pcm_frames(filename)`). Чтобы ничего не сломать:

**Решение:** сохраняем контракт `synthesize(text, filename)` — каждый провайдер пишет готовый WAV/MP3-файл по указанному пути. Минимум инвазивности, диспетчер просто выбирает, кого позвать, имя файла остаётся в его руках.

WAV — потому что в нём уже умеет FFmpeg-конвертер без изменений. MiniMax отдаёт `mp3` (по спеке: `output_format=hex`, `format=mp3`) → декодируем hex → пишем `.mp3` во временный файл → FFmpeg его всё равно съест (`build_tts_pcm_command` уже принимает любой формат через FFmpeg).

### 2.2 Модульная структура

Создаём **новый файл** `tts_providers.py` рядом с `bot.py`. Это сохранит компактность `bot.py` и даст чистый юнит-тестируемый слой.

```
tts-bot/
  bot.py                  # только изменяем generate_tts_file + warmup_tts + import диспетчера
  tts_providers.py        # NEW: ABC + LocalProvider + MiniMaxProvider + Dispatcher + CircuitBreaker + warm_local()
  scripts/
    clone_voice.py        # NEW: одноразовая утилита клонирования голоса (коммит 4а)
    voice_samples/
      .gitkeep            # место для семпла (коммит 4а)
  test_bot.py             # добавляем тесты провайдеров
  .env.example            # добавляем MINIMAX_* переменные
  requirements.txt        # +httpx (или aiohttp) для MiniMax
```

### 2.3 Слои внутри `tts_providers.py`

```python
class TTSProvider(Protocol):
    async def synthesize(self, text: str, filename: Path) -> None: ...

class LocalProvider:
    """тонкая обёртка над существующим generate_piper_file.
       Получает callable, не зависит от TTSBot напрямую."""
    def __init__(self, synthesize_fn: Callable[[str, Path, str], Awaitable[None]]): ...
    async def synthesize(self, text, filename): ...

class MiniMaxProvider:
    """POST https://api.minimax.io/v1/t2a_v2
       Извлекает data.audio (hex) → bytes → filename.
       Использует httpx.AsyncClient с timeout=TTS_REQUEST_TIMEOUT.
       Маппит ошибки → MiniMaxError(код/quota/timeout)."""
    def __init__(self, cfg: MiniMaxConfig): ...

class CircuitBreaker:
    """Состояния: CLOSED (норма) → OPEN (skip API) → HALF_OPEN (одна проба).
       Счётчик consecutive_failures, время last_open_ts.
       Threshold/cooldown из ENV."""

class TTSDispatcher:
    """Главный фасад. По сути заменяет generate_tts_file.
       Выбирает primary по ENV, делегирует, при ошибке → fallback, 
       сообщает CB. Использует in-memory LRU кэш по хэшу текста."""
    def __init__(self, local: LocalProvider, cloud: MiniMaxProvider, cb: CircuitBreaker, cfg: DispatcherConfig): ...
    async def synthesize(self, text: str, filename: Path) -> TTSResult: ...
    async def warm_local(self, text: str, filename: Path) -> None:
        """Прогрев локального провайдера (Piper ONNX) в обход primary/CB.
           Вызывается из bot.warmup_tts() — MiniMax греть не нужно."""
        ...
```

### 2.4 Поток с диспетчером

```
Discord message → on_message → queue_or_merge_message → enqueue_tts
                                                          ↓ text[:300]
                                                        message_queue
                                                          ↓
                                                       tts_worker
                                                         ↓ job.text, filename
                                                  dispatcher.synthesize()
                                                         ↓
                                  ┌─ primary=local? ─→ LocalProvider → piper → wav
                                  │
                                  └─ primary=minimax? ─→ CB CLOSED?
                                                          ├─ yes → MiniMaxProvider (3-4s timeout)
                                                          │         ├─ ok    → mp3
                                                          │         └─ fail  → CB.record_failure
                                                          │                     ↓
                                                          │                  LocalProvider (fallback)
                                                          └─ no (OPEN) ────→ LocalProvider сразу
                                         ↓
                                  filename (wav или mp3 — FFmpeg съест оба)
                                         ↓
                                  prepare_tts_pcm_frames → continuous stream → Discord
```

---

## 3. Конфигурация — что добавить в `.env.example` (и `.env` прода)

```bash
# ── MiniMax TTS (новое) ─────────────────────────────────────────────
MINIMAX_API_KEY=                  # Subscription Key (получим в чате отдельно)
MINIMAX_GROUP_ID=                 # опционально, если API потребует ?GroupId=
MINIMAX_MODEL=speech-2.8-turbo
MINIMAX_VOICE_ID=                 # клонированный voice_id пользователя bussshy
MINIMAX_BASE_URL=https://api.minimax.io
MINIMAX_LANGUAGE_BOOST=Russian
MINIMAX_BITRATE=128000
MINIMAX_SAMPLE_RATE=32000

# ── Диспетчер ───────────────────────────────────────────────────────
TTS_PRIMARY_PROVIDER=local        # local | minimax (переключение без правки кода)
TTS_REQUEST_TIMEOUT=2.5           # единственный таймаут API-запроса (см. §11.4 по RTT, обоснование 2.5 сек)
TTS_MAX_CHARS=300                 # спек требует ~300; в текущем MAX_TEXT_LENGTH=500 — сужаем

# ── Circuit breaker ─────────────────────────────────────────────────
CB_FAILURE_THRESHOLD=3
CB_COOLDOWN_SECONDS=60

# ── Кэш частых фраз (опц., шаг 7) ──────────────────────────────────
TTS_CACHE_ENABLED=0
TTS_CACHE_MAX_ENTRIES=128
TTS_CACHE_DIR=/dev/shm/tts_cache
```

В `requirements.txt` добавляем `httpx==0.27.0` (или `aiohttp` — на вкус, httpx удобнее в тестах через MockTransport).

---

## 4. Препроцессинг — где встроить

Спека просит: вырезать `<:name:id>`, `<a:name:id>`, `<@id>`, `<@!id>`, `<@&id>`, `<#id>`, `https?://…` + пропускать пустые + лимит ~300 символов.

**Решение:** создаём единый нормализатор `normalize_for_tts(raw: str) -> str | None` в `bot.py` (или выносим в `tts_providers.py`, но логичнее в `bot.py` рядом с существующими хелперами), который:
1. Прогоняет те же регэкспы, что и `_strip_discord_tokens_for_speech`, но **всегда** (а не только для классификации).
2. Схлопывает пробелы, заменяет `\n` на `". "`.
3. Возвращает `None`, если после чистки пусто или длина > `TTS_MAX_CHARS`.

Используем его:
- В `queue_or_merge_message()` — заменяем `parsed.spoken_text` на результат `normalize_for_tts` (или выбрасываем сообщение как пустое).
- В `slash_tts_test()` — заменяем старый `process_text(text)` на новый нормализатор.

Существующие `process_text()`, `_strip_discord_tokens_for_speech()`, `analyze_message_for_merge()` **оставляем** для классификации и merge-логики — они не мешают, если финальный spoken_text пересчитывается после классификации.

---

## 5. План по коммитам (как требует спек)

Каждый шаг = рабочий бот + возможность отката одним флагом. Все коммиты идут в **новую ветку** `feature/minimax-tts` от `beta` (или от `master`, если хотим чистую историю — уточнить у пользователя).

### Коммит 1 — Резюме флоу (без кода)
Этот документ. Коммитим как `docs: add minimax integration plan and current flow analysis` — без изменений в коде.

### Коммит 2 — Прослойка + `LocalProvider` (поведение бота не меняется)

**Что делаем:**
- Создаём `tts_providers.py` с `TTSProvider` (Protocol), `LocalProvider` (обёртка над текущим `generate_piper_file` через DI-callback), `CircuitBreaker` (заглушка — пока только структура, `allow_request` всегда True), `TTSDispatcher` (пока тупо вызывает `LocalProvider`), плюс метод `warm_local(text, filename)` для прогрева в обход primary-флага.
- В `bot.py`:
  - В `__init__` создаём `self.tts_dispatcher = TTSDispatcher(local=LocalProvider(self.generate_piper_file))`.
  - `generate_tts_file()` теперь вызывает `self.tts_dispatcher.synthesize(text, filename, voice_profile)` — это основной путь.
  - `warmup_tts()` **не** ходит в `generate_tts_file`; вместо этого зовёт `self.tts_dispatcher.warm_local("Привет", filename)` напрямую. Это принципиально: иначе при `primary=minimax` прогрев уйдёт в облако, Piper останется холодным, и первый fallback будет лагать.
  - `TTS_PRIMARY_PROVIDER` пока игнорируется (или `local` по умолчанию).
- В `test_bot.py` добавляем тесты: `LocalProvider` корректно делегирует; `TTSDispatcher.synthesize` идёт через local; `TTSDispatcher.warm_local` идёт через local даже если `primary=minimax` (это и есть смысл прогрева).

**Откат:** revert коммита. Бот работает через старый код.

**Проверка:** `docker compose build && docker compose up -d && docker logs --tail 60 discord_tts_bot` — должна быть строчка `TTS warmup completed` (от `warm_local`), затем `TTS engine used: piper profile=piper-ruslan` при первом сообщении. `/voicebot test` отрабатывает.

### Коммит 3 — `MiniMaxProvider` + диспетчер (но `primary=local`)

**Что делаем:**
- В `tts_providers.py` дописываем `MiniMaxProvider` (httpx-клиент, парсинг hex/base_resp.status_code, маппинг ошибок в `MiniMaxError`).
- В `requirements.txt` добавляем `httpx`.
- `TTSDispatcher` начинает читать `TTS_PRIMARY_PROVIDER` из ENV; если `minimax` — пробует MiniMax, при ошибке падает в `LocalProvider`. **Но `primary=local` по умолчанию**, чтобы прода не уехала.
- `LocalProvider` остаётся **неизменённым** по логике (просто обёртка).
- В `test_bot.py` добавляем юнит-тесты MiniMaxProvider с `httpx.MockTransport`: успех → байты, `status_code != 0` → `MiniMaxError`, 429 → `MiniMaxError(quota)`, таймаут → `MiniMaxError(timeout)`.

**Откат:** `TTS_PRIMARY_PROVIDER=local` уже выставлен → revert не нужен для безопасности.

**Проверка:** `python -c "from tts_providers import MiniMaxProvider; ..."` со вставленным ключом и подсунутым MockTransport. В проде бот по-прежнему на Piper.

### Коммит 4 — Препроцессинг

**Что делаем:**
- Добавляем `normalize_for_tts(raw)` в `bot.py` (или в `tts_providers.py`, если хотим чистый слой).
- В `queue_or_merge_message()` финальный `parsed.spoken_text` пропускаем через нормализатор; если получили `None` или пустую строку — сообщение не уходит в очередь вообще.
- В `enqueue_tts()` дополнительно режем по `TTS_MAX_CHARS` (а не по `TTS_MAX_TEXT_LENGTH` — добавляем мягкий лимит 300).
- В `slash_tts_test()` тоже используем нормализатор.
- В `test_bot.py` — параметризованные кейсы: «`<@123> привет` → `привет`», «`https://x.com` → None», «300+ символов → обрезка или None».

**Откат:** revert коммита — старый путь остаётся.

**Проверка:** `/voicebot test` с упоминаниями/ссылками не озвучивает мусор; нагрузочное сообщение от bussshy с эмодзи идёт чистым.

### Коммит 5 — Включаем `primary=minimax`

**Что делаем:**
- В `.env.example` (комментарий) и в `.env` прода: `TTS_PRIMARY_PROVIDER=minimax`. **Делаем это в коммите только в `.env.example`**. Реальный `.env` не в git (он в `.gitignore`).
- Тестируем fallback: подменяем `MINIMAX_API_KEY` на заведомо неверный → ждём, что в логах видна запись `MiniMax provider failed …, falling back to local` и бот продолжает озвучивать через Piper.
- Возвращаем правильный ключ.

**Откат:** `TTS_PRIMARY_PROVIDER=local` в `.env` + `docker compose restart`.

**Проверка:** `.venv/bin/python -m unittest -v test_bot.py` → всё зелёное. В логах контейнера — `provider_used=minimax` на каждом сообщении.

### Коммит 6 — Circuit breaker

**Что делаем:**
- Доводим `CircuitBreaker` до боевого состояния:
  - `CLOSED`: считаем подряд неудачи, при `>= CB_FAILURE_THRESHOLD` → `OPEN` + запоминаем `opened_at`.
  - `OPEN`: всё идёт в fallback. После `CB_COOLDOWN_SECONDS` → `HALF_OPEN`.
  - `HALF_OPEN`: одна проба. Успех → `CLOSED`. Провал → обратно `OPEN` с новым `opened_at`.
- В `TTSDispatcher.synthesize()`:
  - если CB в `OPEN` (и cooldown не вышел) → сразу fallback;
  - если пробуем MiniMax — ловим `MiniMaxError` → `cb.record_failure()` → fallback;
  - если успех — `cb.record_success()`.
- `CB_FAILURE_THRESHOLD=3`, `CB_COOLDOWN_SECONDS=60` из ENV.
- Тесты: три подряд ошибки → CB `OPEN`, четвёртый вызов даже не дёргает MiniMax (мокаем httpx и считаем вызовы), через 60с — одна проба.

**Откат:** revert или `CB_FAILURE_THRESHOLD=99999`.

**Проверка:** `TTS_REQUEST_TIMEOUT=0.001` при плохом ключе → три мгновенных сбоя → цепь разомкнулась → последующие сообщения идут мгновенно через Piper.

### Коммит 7 (опц.) — Кэш частых фраз

**Что делаем:**
- В `TTSDispatcher` добавляем LRU-кэш `dict[bytes, Path]` с ключом = sha256(нормализованный текст).
- Файлы живут в `TTS_CACHE_DIR` (по умолчанию `/dev/shm/tts_cache`, не персистится между перезапусками — это нормально для realtime-бота).
- Перед вызовом любого провайдера проверяем кэш; при hit — копируем из кэша в `filename` (через `os.link` если тот же FS, иначе `shutil.copyfile`) и возвращаем.
- Лимит по числу записей `TTS_CACHE_MAX_ENTRIES`; при превышении — LRU-вытеснение (можно простой `collections.OrderedDict.move_to_end` + `popitem(last=False)`).
- Тесты: повторный текст не вызывает провайдер (счётчик вызовов == 1).

**Откат:** `TTS_CACHE_ENABLED=0`.

---

## 6. Клонирование голоса — отдельный одноразовый скрипт

**Важно:** клонирование голоса — это **отдельный коммит ДО коммита «включаем primary=minimax»**, иначе включать просто нечего: `MINIMAX_VOICE_ID` будет пустой. Порядок: **коммит 4а** (между препроцессингом и включением MiniMax), не после CircuitBreaker/кэша. Раньше я этот шаг поставил в конец — это была ошибка нумерации.

`scripts/clone_voice.py` (по спеке). Скрипт:

1. `POST /v1/files/upload` с `purpose=voice_clone` (читает путь к mp3-семплу из CLI-аргумента).
2. `POST /v1/voice_clone` с полученным `file_id` и заданным `voice_id` (или автогенеренным), `model=speech-2.8-hd` для превью.
3. Печатает итоговый `voice_id` + краткий sanity-check через `POST /v1/t2a_v2` с этим голосом и короткой фразой.
4. В `README`/`docs` добавить инструкцию: положить семпл в `voice_samples/bussshy.mp3`, выполнить `python scripts/clone_voice.py voice_samples/bussshy.mp3 bussshy`, скопировать `voice_id` в `.env` как `MINIMAX_VOICE_ID`.

Требования к семплу (по доке API): mp3/m4a/wav, 10 сек – 5 мин, до 20 МБ, **чистый голос без музыки и шума**.

---

## 7. Тестовая стратегия

| Уровень | Что | Когда |
|---|---|---|
| **Unit (test_bot.py)** | `LocalProvider`, `MiniMaxProvider` (MockTransport), `CircuitBreaker` (state-машина), `TTSDispatcher` (hit/miss/fallback/CB), `normalize_for_tts` (регэкспы, лимит длины, пустые) | После каждого коммита |
| **Integration (вручную)** | Прогон `scripts/clone_voice.py` + sanity-синтез | Перед коммитом 5 |
| **Smoke в проде** | `/voicebot test` с разными фразами (с эмодзи, ссылками, упоминаниями) | После коммита 5 |
| **Fallback-тест** | Подсунуть `MINIMAX_API_KEY=invalid` → убедиться, что сообщения всё равно озвучиваются (через Piper), в логах есть `fallback reason=invalid_api_key` | После коммита 5 |
| **CB-тест** | `TTS_REQUEST_TIMEOUT=0.001` → три сообщения за ~0.001с каждый → четвёртое и далее мгновенно через Piper | После коммита 6 |

Все юнит-тесты — `python -m unittest -v test_bot.py` (без сети, без Discord).

---

## 8. Точки внимания / риски

1. **WAV vs MP3 на выходе.** Piper пишет WAV. MiniMax отдаёт MP3. FFmpeg съест оба, но `prepare_tts_pcm_frames` сейчас зовёт `build_tts_pcm_command(source)` — нужно убедиться, что FFmpeg-команда не завязана жёстко на расширение `.wav`. Проверить при коммите 3.
2. **Голосовой профиль (`voice_profile`) на стороне MiniMax.** В `TTSJob` сейчас приходит имя (`piper-ruslan`/`piper-irina`). Для MiniMax это не имеет смысла. Решение: в `MiniMaxProvider.synthesize()` принимаем только текст, голос берём из `MINIMAX_VOICE_ID`. Маппинг `piper-*` → MiniMax не делаем: по спеке у нас один клонированный голос bussshy. Если потом захочется несколько голосов — расширим через `voice_profile -> {piper: ..., minimax: ...}` маппинг.
3. **Таймаут 2.5 сек при уже установленном голосовом соединении.** Замер RTT с хоста бота (`api.minimax.io` через алиас `lb-ali.minimax.io`): ICMP **188 мс стабильно**, TLS+TTFB **0.59–0.65 сек** на трёх пробах. Реальный минимум одного запроса ≈ 0.6 сек (TLS+сеть) + ~0.25 сек (модель) ≈ **0.85–0.95 сек**. На тёплом keep-alive коннекте каждый запрос ≈ RTT (~0.19 сек) + модель (~0.25 сек) ≈ **0.5–0.6 сек** в установившемся режиме. **На этом хосте `api.minimax.io` — однозначный выбор** (см. правку №2 корректировок: api-uw убираем из примеров, оставляем только как настраиваемую переменную на случай переезда). Таймаут **2.5 сек** — двойной запас над нормой 1.2 сек; при провисании MiniMax до 2.5+ сек — fallback. CB после 3-х таких защитит.
4. **Race в кэше.** При двух одновременных запросах одной фразы → две параллельных записи в `TTS_CACHE_DIR`. Решается блокировкой через `asyncio.Lock` на ключ (или проще — `setdefault` + проверка).
5. **Supertonic-эксперименты в .env.** Не трогаем. Если потом захотим три провайдера — расширим флаг `TTS_PRIMARY_PROVIDER` до `local|minimax|supertonic`.
6. **`process_text` в `/voicebot test`.** Сейчас legacy. В коммите 4 заменим на `normalize_for_tts` — иначе тест-команда обходит новую препроцессинг-цепочку.

---

## 9. Дерево ветки и порядок PR

```
master
  └─ beta                                  (текущая прод-ветка)
       └─ feature/minimax-tts             (новая)
            · commit 1: docs (этот план)
            · commit 2: LocalProvider + dispatcher skeleton
                         + warmup_tts бьёт в локальный провайдер напрямую
            · commit 3: MiniMaxProvider + httpx + tests
            · commit 4: normalize_for_tts + wire-in
            · commit 4а: scripts/clone_voice.py + voice_samples/.gitkeep
                         (вручную прогоняется юзером; voice_id кладётся в .env)
            · commit 5: primary=minimax (только .env.example; .env руками)
            · commit 6: CircuitBreaker
            · commit 7 (опц.): LRU-кэш
```

**Жёсткая зависимость:** коммит 5 не имеет смысла без 4а (без `voice_id` MiniMax вернёт ошибку или заглушку). В README/PR-описании явно проговариваем: «не мержим 5 раньше, чем юзер реально прогонит `clone_voice.py` и положит `MINIMAX_VOICE_ID` в `.env`».

На каждом этапе бот собирается, тесты зелёные, можно катить как `docker compose build && docker compose up -d`. После **каждого** коммита — smoke `/voicebot test` в канале.

### 9.1. Warmup и диспетчер (учли критику)

Прогрев (`warmup_tts` в `bot.py:1000`) **не ходит через диспетчер**. Смысл прогрева — загрузить ONNX-модель Piper в память до первого реального сообщения. Если при `primary=minimax` прогрев пойдёт в MiniMax, то:

- Piper останется холодным → первый реальный fallback будет на 1–3 сек длиннее (Piper на холодную грузит ONNX + warm up JIT);
- на каждом рестарте будем жечь лишний вызов API и квоту.

Реализация: у `TTSDispatcher` отдельный метод `warm_local(text, filename)`, который **всегда** зовёт локального провайдера, игнорируя primary и CB. В `bot.py:warmup_tts` теперь:

```python
async def warmup_tts(self) -> None:
    filename = TMP_DIR / f"warmup_{uuid.uuid4().hex}.wav"
    try:
        await self.tts_dispatcher.warm_local("Привет", filename)
        log.info("TTS warmup completed")
    except Exception:
        log.exception("TTS warmup failed")
    finally:
        ...
```

Это закрывает оба случая: и `primary=local` (прогрели Piper), и `primary=minimax` (всё равно прогрели Piper — потому что fallback должен быть горячим). МиниМакс прогревать не нужно — это HTTP.

---

---

## 11. Заметки по итерации плана (что изменилось после первого ревью)

| Что заметил юзер | Как исправлено |
|---|---|
| Конфликт нумерации: clone_voice был «после CircuitBreaker», но нужен ДО включения `primary=minimax` | Перенесён в **коммит 4а** (между препроцессингом и включением). В §9 дерево перенумеровано |
| `warmup_tts()` после подключения диспетчера уйдёт в MiniMax → Piper не прогреется, сожжём квоту | Добавлен `TTSDispatcher.warm_local()`; `bot.py:warmup_tts` зовёт его напрямую, минуя primary/CB |
| Дублирование `MINIMAX_TIMEOUT_SECONDS` и `TTS_REQUEST_TIMEOUT` | Схлопнуто в одну переменную `TTS_REQUEST_TIMEOUT` (диспетчер — единственная точка вызова API) |
| «MiniMax отвечает за <300 мс» — не учитывает сеть | Замер с хоста: ICMP до `api.minimax.io` = 188 мс, TLS+TTFB = 0.59–0.65 сек, типичный запрос ≈ 0.9 сек end-to-end. На этом хосте `api.minimax.io` однозначный выбор. Таймаут ужали с 3.5 до **2.5 сек** (правка №4 корректировок): 2.5 — всё ещё двойной запас над нормой 1.2 сек, но окно тупняка до срабатывания CB сокращается с ~10.5 до ~7.5 сек |

---

## 10. Что я покажу в конце (по спеке, § «Что мне показать в конце»)

1. Краткое описание старого флоу vs нового (раздел 1 этого плана + diff `generate_tts_file`).
2. Где лежит конфиг и какие переменные заполнить (раздел 3).
3. Как прогнать `scripts/clone_voice.py` (раздел 6).
4. Как проверить fallback реально (раздел 7, «Fallback-тест»).
5. Diff-сводка по `bot.py` (ожидаемо: < 50 строк изменений — за счёт выноса логики в `tts_providers.py`).