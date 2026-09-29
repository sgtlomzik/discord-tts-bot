# Discord TTS Bot

[English](README.md) | **Русский**

Self-hosted Discord-бот, который озвучивает сообщения из чата в голосовом
канале. Для потоковой озвучки можно использовать Fish Audio и ElevenLabs
(Ogg/Opus), локальные голоса [Piper](https://github.com/OHF-Voice/piper1-gpl)
работают полностью офлайн; доступны также голоса MiniMax и Gemini (через
OpenRouter). При сбое облачного движка бот переключается на Piper.

[![CI](https://github.com/sgtlomzik/discord-tts-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/sgtlomzik/discord-tts-bot/actions/workflows/ci.yml)
[![Docker](https://github.com/sgtlomzik/discord-tts-bot/actions/workflows/docker.yml/badge.svg)](https://github.com/sgtlomzik/discord-tts-bot/actions/workflows/docker.yml)

## Возможности

- **Озвучка чата в войсе** — сообщения пользователей из белого списка
  синтезируются и проигрываются в их голосовом канале; бот сам подключается
  и отключается при простое.
- **Пять TTS-движков** — Fish Audio, ElevenLabs, MiniMax, Gemini (через
  OpenRouter) и локальный Piper. При сбое
  облака circuit breaker переключает на Piper; повторяющиеся фразы берутся
  из LRU-кэша на диске.
- **Низкая задержка** — Fish и ElevenLabs отдают Ogg/Opus по HTTP; бот извлекает 20-мс
  Opus-пакеты и сразу передаёт их в Discord без декодирования и повторного
  кодирования. Следующее сообщение синтезируется, пока играет предыдущее.
  Соединения с облачными провайдерами держатся открытыми 120 с и
  прогреваются, пока пользователь печатает, поэтому сообщение после паузы
  не ждёт TLS-рукопожатия.
- **Умная склейка сообщений** — серия коротких сообщений одного человека
  объединяется в одну естественную фразу (`selective_hold_v2`), а реакции,
  эмодзи и вопросы озвучиваются сразу.
- **Текст готовится к речи** — ссылки и разметка вырезаются, unicode-эмодзи
  проговариваются русскими названиями, упоминания читаются как ники,
  кастомным эмодзи сервера можно задать произношение (каждый сервер —
  только для своих эмодзи).
- **Управление целиком из Discord** — группа slash-команд `/voicebot`:
  включение озвучки, белый список, голоса, клонирование, алиасы эмодзи,
  статистика и очередь.

## Быстрый старт (Docker)

Docker не обязателен — см. [Запуск без Docker](#запуск-без-docker).
Понадобятся: Discord-приложение с токеном бота (включите intent
**Message Content**) и Docker с плагином compose.

```bash
git clone https://github.com/sgtlomzik/discord-tts-bot.git
cd discord-tts-bot

# 1. Конфигурация
cp .env.example .env          # затем заполните DISCORD_TOKEN, WHITELIST_USERS, ...

# 2. Скачать голосовые модели Piper (~120 МБ, один раз)
./scripts/download_models.sh

# 3. Запуск (тянет готовый образ из GHCR)
docker compose pull && docker compose up -d
```

Либо соберите образ локально: `docker compose up -d --build`.
Специфичные для хоста настройки (нестандартная сеть, доп. маунты) кладите в
незакоммиченный `docker-compose.override.yml` — compose подхватит его сам.

Пригласите бота на сервер со scope `bot` + `applications.commands` и правами
в войсе (Connect, Speak), затем в Discord:

```
/voicebot on
/voicebot allow @user
/voicebot test text: привет
```

## Запуск без Docker

Бот — это один Python-процесс; Docker лишь упаковывает ffmpeg/libopus и
подхватывает env-файл.

```bash
sudo apt install ffmpeg libopus0          # системные зависимости (Debian/Ubuntu)
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
./scripts/download_models.sh

cp .env.example .env                      # заполните DISCORD_TOKEN, WHITELIST_USERS
set -a; . ./.env; set +a                  # вне Docker .env никто не загружает за вас
export PIPER_MODELS_DIR=./models BOT_CONFIG_PATH=./data/config.json
python bot.py
```

Для постоянной работы оберните то же самое в systemd-юнит с
`EnvironmentFile=/path/to/.env`.

## Конфигурация

Всё настраивается переменными окружения — полный аннотированный список в
[.env.example](.env.example). Главное:

| Переменная | Назначение |
|---|---|
| `DISCORD_TOKEN` | Токен бота (**обязательно**) |
| `WHITELIST_USERS` | ID пользователей Discord через запятую, чьи сообщения озвучиваются |
| `TTS_DEFAULT_VOICE_PROFILE` | Голос по умолчанию для новых серверов (`piper-ruslan`, `fish-default`, …) |
| `MINIMAX_API_KEY` | Включает облачные голоса MiniMax (опционально) |
| `FISH_API_KEY` | Ключ Fish Audio; хранить только в `.env` |
| `FISH_REFERENCE_ID` | ID голоса Fish; создаёт профиль `fish-default` |
| `FISH_TTFA_TIMEOUT` | Сколько секунд ждать первый аудиопакет от Fish до перехода на Piper (по умолчанию 5) |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_API_KEYS` | Включает ElevenLabs; несколько ключей образуют кольцо (см. ниже) |
| `OPENROUTER_API_KEY` | Включает голоса Gemini через OpenRouter (опционально) |
| `TTS_QUOTA_COOLDOWN_SECONDS` | Пауза для облачного сервиса, когда закончился баланс или лимит тарифа (по умолчанию 1800) |
| `TTS_PRIMARY_PROVIDER` | `local`, `minimax`, `fish`, `gemini` или `elevenlabs` |
| `TTS_MERGE_ALGORITHM` | `selective_hold_v2`, `legacy` или `off` |

Для Fish укажите в `.env` `FISH_API_KEY` и `FISH_REFERENCE_ID`, затем
выберите `/voicebot voice-set voice:fish-default` или задайте
`TTS_DEFAULT_VOICE_PROFILE=fish-default`, чтобы новые серверы сразу
получали Fish. Сам бот голос по умолчанию на Fish не меняет. Настройки по умолчанию:
`s2.1-pro-free`, `opus`, `low`, `chunk_length=150`, `opus_bitrate=48000`.
На сервере с существующими персональными назначениями голосов смените
их отдельно через `/voicebot voice-user` или очистите через
`/voicebot voice-clear`. Кэш прямого воспроизведения хранит готовые пакеты
в `.dopus`; для голосов со сдвигом тона хранится исходный Ogg в `.opus`.
Ключ включает текст, `reference_id`, модель и параметры, которые уходят в
Fish (высота тона в ключ не входит).

Если Fish не прислал ни одного аудиопакета за `FISH_TTFA_TIMEOUT` секунд
(одни заголовки ответа не считаются), сообщение озвучивается резервным
голосом Piper, а в лог пишется предупреждение `Fish first audio exceeded`.
После нескольких сбоев подряд Fish или MiniMax отключаются на
`CB_COOLDOWN_SECONDS`. Если закончился баланс или лимит тарифа (Fish
HTTP 402, MiniMax коды 1008 и 2056), этот сервис отключается на
`TTS_QUOTA_COOLDOWN_SECONDS`; ограничение частоты запросов (HTTP 429,
MiniMax код 1039) считается обычным сбоем.

Чтобы разом перевести существующую установку на Fish, остановите бота и
выполните `python scripts/migrate_fish_default.py data/config.json`. Скрипт
ставит всем серверам голос `fish-default`, сбрасывает персональные голоса
и сохраняет резервную копию рядом с `config.json`.

`/voicebot voice-clone` создаёт приватный голос Fish из приложенного
аудиофайла (любой файл с типом `audio/*` или WAV/MP3/M4A/OGG/Opus/FLAC по
расширению; OGG — это голосовые сообщения Discord). Бот дожидается готовности модели, проверяет
короткую генерацию и сохраняет новый `reference_id` в `data/voices.json`.
Созданный профиль можно назначить через `voice-set` или `voice-user`.
Готовый голос из библиотеки Fish можно добавить без клонирования:
`/voicebot voice-fish-add name:my-voice reference_id:ID_ИЗ_FISH`, затем
`/voicebot voice-user user:@пользователь voice:my-voice`.
Для добавления и назначения нужны права управления сервером. Бот проверяет
голос короткой озвучкой перед сохранением.
Настройка Fish: `/voicebot voice-fish-tune name:my-voice speed:1.2
emotion:happy pitch:2 volume_db:3`. Доступны также `model`, `temperature`
и `top_p`. Общий для всех Fish-голосов режим задержки меняется в Discord:
`/voicebot fish-latency mode:balanced` (варианты `low`, `balanced`, `normal`).
Без `mode` команда показывает текущий режим. По умолчанию используется `low`;
выбор в Discord сохраняется в `data/config.json`, переживает перезапуск и
переопределяет `FISH_LATENCY` из `.env`. Режим учитывается в ключе TTS-кэша.
Скорость, громкость, выразительность и режим задержки передаются в Fish API;
высота тона меняется локально через ffmpeg: её смена не требует новых
запросов к Fish, но отключает прямой Opus-путь для этого профиля. Если Fish выдаст пакеты другой длительности, бот тоже
использует ffmpeg и доигрывает уже полученные данные без второго запроса. Автоэмоция выбирается по тексту
сообщения. Параметры профиля сохраняются при перезапуске; все, кроме высоты тона,
учитываются в ключе кэша.
`/voicebot stats` показывает число успешных запросов Fish и символов в них
за текущую сессию (это счётчик входного текста, а не биллинг Fish), а также
состояние предохранителей Fish и MiniMax с оставшимся временем паузы.

### ElevenLabs

Укажите `ELEVENLABS_API_KEY` (ключу нужно право `text_to_speech`; с правом
`voices_read` работает автодополнение voice_id) и добавьте голоса командой
`/voicebot voice-add name:my-voice voice_id:<id> provider:ElevenLabs`: перед
сохранением бот делает одну короткую пробную генерацию.
`ELEVENLABS_VOICE_ID` по желанию создаёт профиль `eleven-default`. Модель по
умолчанию — `eleven_v4_turbo` (первый звук ~0,2 с, 0,5 кредита за символ).

`ELEVENLABS_FORMAT=opus_48000_64` (по умолчанию) отдаёт Ogg/Opus с 20-мс
пакетами, и они идут в Discord тем же прямым путём, что у Fish. `pcm_48000`
или `pcm_24000` отдают сырой PCM, который нарезается в процессе, без ffmpeg.
`/voicebot voice-tune` задаёт ElevenLabs-голосу `stability`, `similarity` и
модель (`reset` возвращает собственные настройки голоса). Голоса из Voice
Library требуют платного тарифа ElevenLabs; стандартные работают и на
бесплатном.

`ELEVENLABS_API_KEYS=k1,k2,k3` распределяет расход по нескольким аккаунтам.
Если у активного ключа кончились кредиты (осталось меньше 50) или ключ
недействителен, то же сообщение сразу повторяется со следующим ключом, ещё
до начала звука; после последнего ключа идёт первый. Сообщение, которое
просто длиннее остатка ключа, уходит на следующий ключ без смены активного.
Если пусты все ключи, говорит Piper, а ElevenLabs отдыхает
`TTS_QUOTA_COOLDOWN_SECONDS`. Активный ключ переживает перезапуск (в
`data/config.json` хранится его хэш), `/voicebot stats` показывает кредиты
за сессию по каждому ключу. Клоны и голоса из Voice Library должны быть в
каждом аккаунте кольца.

## Команды

Группа `/voicebot`: `on`, `off`, `allow`, `deny`, `voices`, `voice-set`,
`voice-user`, `voice-clear`, `voice-add`, `voice-fish-add`, `voice-clone`,
`voice-tune`, `voice-fish-tune`, `fish-latency`,
`voice-describe`, `voice-say-set`, `voice-say-clear`, `emoji-alias`,
`emoji-aliases`, `emoji-alias-remove`, `status`, `stats`, `test`,
`limit`, `queue-clear` — плюс легаси-команды `!tts join` / `!tts stop`.

## Статистика использования

`scripts/usage_stats.py` считает, сколько символов пользователь отправил на
синтез за период: сколько за всё время, в среднем в день и за 30 дней.
«Символы» — длина текста после нормализации бота (алиасы эмодзи, упоминания,
вырезанные ссылки, лимит `TTS_MAX_CHARS`), то есть ровно то, что получил
TTS-провайдер.

Источники данных:

- **Логи бота** (`docker logs`): строки `Queued TTS ... author=<id>` —
  точные данные, но Docker удаляет их при пересоздании контейнера, поэтому
  обычно они покрывают только дни с последнего деплоя.
- **История каналов Discord** (REST API с токеном бота) — все сообщения
  пользователя за период. Каналы и сервер берутся из логов.

Каждая строка лога сопоставляется со своим сообщением в Discord. Доля
озвученного текста, измеренная в окне логов, переносится на историю до его
начала: так период дополняется до полного значения. Для сверки в отчёте
есть линейная экстраполяция окна логов и верхняя граница («весь текст
пользователя, как если бы он всегда был в голосовом»).

Скрипт запускается в образе бота (нужны `ttsbot`, `discord`, `emoji`) из
корня репозитория на хосте:

```bash
docker logs discord_tts_bot 2>&1 | docker run --rm -i --env-file .env   -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/data:/app/data:ro"   ghcr.io/sgtlomzik/discord-tts-bot:latest   python scripts/usage_stats.py --user <discord_user_id> --days 30 > exports/usage.md
```

Параметры: `--days` — длина периода до текущего момента (по умолчанию 30);
`--guild` и `--channel` (можно несколько) — если у пользователя нет строк в
логах; `--log-file` — файл вместо stdin; `--json` — машиночитаемый вывод.

Ограничения: оценка вне окна логов предполагает, что пользователь сидел в
голосовом канале в той же доле своих сообщений, что и внутри окна. Кэш не
учитывается — повторы фраз в окне логов отчёт показывает отдельно.

## Разработка

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*.py'  # ~360 тестов, сеть не нужна
```

Код приложения — в пакете `ttsbot/`; `bot.py` — точка входа и composition
root. Карта модулей и поток выполнения — в
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Исторические заметки — в
[docs/internal/](docs/internal/).

## Лицензия

[MIT](LICENSE)
