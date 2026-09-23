# Discord TTS Bot

[English](README.md) | **Русский**

Self-hosted Discord-бот, который озвучивает сообщения из чата в голосовом
канале. Для потоковой озвучки можно использовать Fish Audio (Ogg/Opus),
локальные голоса [Piper](https://github.com/OHF-Voice/piper1-gpl) работают
полностью офлайн; голоса MiniMax остаются доступными. При сбое облачного
движка бот переключается на Piper.

[![CI](https://github.com/sgtlomzik/discord-tts-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/sgtlomzik/discord-tts-bot/actions/workflows/ci.yml)
[![Docker](https://github.com/sgtlomzik/discord-tts-bot/actions/workflows/docker.yml/badge.svg)](https://github.com/sgtlomzik/discord-tts-bot/actions/workflows/docker.yml)

## Возможности

- **Озвучка чата в войсе** — сообщения пользователей из белого списка
  синтезируются и проигрываются в их голосовом канале; бот сам подключается
  и отключается при простое.
- **Три TTS-движка** — Fish Audio, MiniMax и локальный Piper. При сбое
  облака circuit breaker переключает на Piper; повторяющиеся фразы берутся
  из LRU-кэша на диске.
- **Низкая задержка** — Fish отдаёт Ogg/Opus по HTTP, ffmpeg декодирует
  поток в PCM во время генерации, а следующее сообщение синтезируется,
  пока играет предыдущее. Один HTTP-клиент держит соединение открытым.
- **Умная склейка сообщений** — серия коротких сообщений одного человека
  объединяется в одну естественную фразу (`selective_hold_v2`), а реакции,
  эмодзи и вопросы озвучиваются сразу.
- **Текст готовится к речи** — ссылки и разметка вырезаются, unicode-эмодзи
  проговариваются русскими названиями, упоминания читаются как ники,
  кастомным эмодзи сервера можно задать произношение.
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
| `TTS_DEFAULT_VOICE_PROFILE` | Голос по умолчанию (`piper-ruslan`, `piper-irina`, …) |
| `MINIMAX_API_KEY` | Включает облачные голоса MiniMax (опционально) |
| `FISH_API_KEY` | Ключ Fish Audio; хранить только в `.env` |
| `FISH_REFERENCE_ID` | ID голоса Fish; создаёт профиль `fish-default` |
| `TTS_PRIMARY_PROVIDER` | `local`, `minimax` или `fish` |
| `TTS_MERGE_ALGORITHM` | `selective_hold_v2`, `legacy` или `off` |

Для Fish укажите в `.env` `FISH_API_KEY` и `FISH_REFERENCE_ID`, затем
выберите `/voicebot voice-set voice:fish-default`. Настройки по умолчанию:
`s2.1-pro-free`, `opus`, `low`, `chunk_length=150`, `opus_bitrate=48000`.
На сервере с существующими персональными назначениями голосов смените
их отдельно через `/voicebot voice-user` или очистите через
`/voicebot voice-clear`. Кэш Fish хранит `.opus`, ключ включает текст,
`reference_id`, модель и настройки генерации.

`/voicebot voice-clone` теперь создаёт приватный голос Fish из приложенного
аудиофайла (WAV/MP3/M4A/Opus). Бот дожидается готовности модели, проверяет
короткую генерацию и сохраняет новый `reference_id` в `data/voices.json`.
Созданный профиль можно назначить через `voice-set` или `voice-user`.

## Команды

Группа `/voicebot`: `on`, `off`, `allow`, `deny`, `voices`, `voice-set`,
`voice-user`, `voice-clear`, `voice-add`, `voice-clone`, `voice-tune`,
`voice-describe`, `voice-say-set`, `voice-say-clear`, `emoji-alias`,
`emoji-aliases`, `emoji-alias-remove`, `status`, `stats`, `test`,
`limit`, `queue-clear` — плюс легаси-команды `!tts join` / `!tts stop`.

## Разработка

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*.py'  # ~300 тестов, сеть не нужна
```

Код приложения — в пакете `ttsbot/`; `bot.py` — точка входа и composition
root. Карта модулей и поток выполнения — в
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Исторические заметки — в
[docs/internal/](docs/internal/).

## Лицензия

[MIT](LICENSE)
