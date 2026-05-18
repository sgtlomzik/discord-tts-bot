Ты — автономный агент-разработчик на сервере с Discord TTS-ботом.

Твоя задача — реализовать новый алгоритм пред-TTS буферизации сообщений в существующем боте.

ВАЖНО: не проводи deep research и не анализируй большие логи. Контекстное окно ограничено. Используй готовые требования ниже как источник истины. Твоя задача — разработка, тестирование, интеграция и git-коммиты.

Главная цель

Заменить текущий грубый fixed debounce для коротких сообщений на легкий selective hold алгоритм.

Новый алгоритм должен:

1. Уменьшить задержку одиночных коротких сообщений.
2. Не задерживать emoji/reaction/special-only сообщения без необходимости.
3. Сохранять возможность склеивать короткие смысловые серии.
4. Не ломать порядок воспроизведения.
5. Быть легким и быстрым.
6. Быть включаемым/отключаемым через конфиг.
7. Не требовать ML, LLM, embeddings, сложного NLP или словарей.

Контекст текущего поведения

Сейчас короткие сообщения попадают в merge-buffer примерно по правилу:

- если TTS_MERGE_SHORT_MESSAGES включен;
- если len(text) <= TTS_MERGE_MAX_CHARS;
- текущий порог около 40 символов;
- затем сообщение ждет TTS_MERGE_WINDOW_MS, примерно 900 ms;
- если приходит еще короткое сообщение того же пользователя в том же voice channel, таймер сбрасывается;
- потом буфер flush’ится в TTS queue.

Проблема: почти все короткие сообщения получают задержку около 900 ms до постановки в очередь. Это плохо для одиночных реакций, emoji, “да”, “нет”, “бб”, “+”, “кек” и подобных реплик. При этом 900 ms не всегда достаточно для полезной склейки смысловых продолжений.

Нужно заменить это на selective_hold_v2.

Новая модель поведения

Алгоритм должен различать два режима:

1. Нет активного буфера.
2. Уже есть активный буфер.

Это принципиально важно.

Короткое сообщение вне буфера часто является одиночной реакцией и должно идти сразу.

Короткое сообщение внутри уже активного буфера может быть продолжением мысли и не должно автоматически ломать склейку.

Пример смысловой серии, которую нельзя ломать только из-за коротких слов:

“ну там просто дается хп”
“при нажатии”
“манты”
“почему-то”
“даже под думом”

Здесь “манты” и “почему-то” короткие и однословные, но это продолжение мысли.

Другой пример:

“где нет шансов”
“уже с минуты”
“3”

Здесь “3” внутри активного буфера может быть смысловым хвостом.

Но emoji-only или custom emoji-only внутри активного буфера должны быть hard break, а не append.

Архитектурные рамки

Не переписывай весь TTS pipeline.

Сохрани:
- on_message проверки;
- process_text, если он уже есть;
- TTSJob queue;
- tts_worker;
- Piper;
- PCM preparation;
- continuous playback source;
- voice connection logic;
- warmup;
- текущую структуру проекта, насколько возможно.

Менять нужно только слой принятия решения перед enqueue_tts:
- parsing/normalization для merge decision;
- merge buffer state;
- selective hold decision;
- flush logic;
- order-preserving behavior;
- logging;
- tests;
- config flags.

Feature flags и конфиг

Добавь конфигурацию так, чтобы можно было безопасно откатиться на старое поведение.

Нужны параметры или их эквиваленты:

- TTS_MERGE_ALGORITHM = legacy | selective_hold_v2 | off
- TTS_SELECTIVE_HOLD_ENABLED = true/false
- TTS_SELECTIVE_HOLD_TARGET_USERS = list или аналогичный механизм, если бот уже имеет allowed users
- TTS_SELECTIVE_HOLD_HARD_CAP_MS = 1200
- TTS_SELECTIVE_HOLD_START_EFFECTIVE_LEN = 10
- TTS_SELECTIVE_HOLD_START_MIN_WORDS_ALT = 2
- TTS_SELECTIVE_HOLD_START_MIN_EFFECTIVE_LEN_ALT = 6
- TTS_SELECTIVE_HOLD_REACTION_PAUSE_MS = 5000
- TTS_SELECTIVE_HOLD_MAX_PARTS = 3
- TTS_SELECTIVE_HOLD_MAX_GROUP_EFFECTIVE_LEN = 56
- TTS_SELECTIVE_HOLD_JOIN_SEPARATOR = ", "
- TTS_SELECTIVE_HOLD_DROP_URL_ONLY = true
- TTS_SELECTIVE_HOLD_DROP_MENTION_ONLY = true
- TTS_SELECTIVE_HOLD_LOG_DECISIONS = true/false
- TTS_SELECTIVE_HOLD_ENABLE_ORDER_PRESERVING_FLUSH = true

Названия можно адаптировать под стиль проекта, но смысл должен сохраниться.

Новые параметры должны иметь backward-compatible defaults. Старый режим должен остаться доступен.

Основные параметры v1

Используй эти значения как v1:

- IMMEDIATE_LONG_EFFECTIVE_LEN = 40
- HOLD_START_EFFECTIVE_LEN = 10
- HOLD_START_MIN_WORDS_ALT = 2 при effective_length >= 6
- REACTION_PAUSE_MS = 5000
- HOLD_HARD_CAP_MS = 1200
- MAX_PARTS = 3
- MAX_GROUP_EFFECTIVE_LEN = 56
- JOIN_SEPARATOR = ", "
- QUEUE_PUT_TIMEOUT_MS = 500

Не используй ожидания 3–5 секунд как основной алгоритм. Максимальное ожидание первого сообщения в буфере должно быть ограничено hard cap около 1200 ms.

Термины

raw_text — текст после текущего Discord/process_text этапа или до безопасной TTS-нормализации.

spoken_text — текст, который реально уйдет в TTS.

raw_length — обычная длина строки.

effective_length — легкая оценка смысловой/произносимой длины для merge decision.

special-only — сообщение, состоящее только из emoji/custom emoji/mention/url/служебного токена.

hard break — входящее сообщение не может быть добавлено к активному буферу; старый буфер нужно сначала flush’ить, потом обработать новое сообщение отдельно.

append_soft — входящее короткое текстовое сообщение можно добавить в активный буфер, даже если само по себе оно выглядело бы как реакция.

Parsing и normalization

Реализуй легкий parser или набор функций для Discord-спецтокенов. Не нужен сложный NLP.

Нужно корректно распознавать:

1. Unicode emoji.
2. Discord custom emoji: <:name:id>.
3. Discord animated custom emoji: <a:name:id>.
4. User mentions: <@id>, <@!id>.
5. Role mentions: <@&id>.
6. Channel mentions: <#id>.
7. URLs.
8. Пустые сообщения после process_text.
9. Single digit.
10. Single symbol.
11. Caps shout.
12. Keyboard smash, например “ФЫВЩХЪ”, “WWW”, “АЪАЪАЪАА”.

Raw Discord IDs не должны попадать в spoken_text.

Custom emoji не должен считаться длинным сообщением только потому, что raw markup длинный.

Пример: <:Kekis:1035577866370416721> может иметь raw_length около 29+, но effective_length должен быть около 1, а не 29+.

Animated emoji <a:name:id> обрабатывать так же.

URL-only и mention-only по умолчанию лучше не озвучивать как содержательное сообщение. Их можно drop’ать или immediate_special, но не склеивать с текстом.

Effective length

Реализуй функцию effective_length.

Примерная логика:

- обычное слово: длина слова;
- число: длина числа;
- Unicode emoji: 1 за emoji token/grapheme;
- custom emoji: 1;
- animated custom emoji: 1;
- mention-only: 0 или 1, но не raw id length;
- mention mixed with text: максимум +1;
- URL-only: 0 или 1, но не длина URL;
- URL mixed with text: максимум +1;
- punctuation/whitespace: 0;
- repeated emoji не должны бесконечно раздувать effective_length.

raw_length использовать только для технических защит и диагностики, но не как главный признак merge decision.

Decision tree: нет активного буфера

После process_text и parsing:

1. Если text/spoken_text пустой:
   - drop;
   - ничего не enqueue.

2. Если effective_length >= 40:
   - immediate_long;
   - сразу enqueue_tts.

3. Если сообщение special-only:
   - emoji-only;
   - custom emoji-only;
   - animated custom emoji-only;
   - mention-only;
   - URL-only;
   - immediate_special или drop according to config;
   - не открывать буфер.

4. Если сообщение содержит явный вопрос или terminal punctuation:
   - immediate_terminal;
   - не открывать буфер.

5. Если сообщение является isolated reaction-like после длинной паузы:
   - gap_prev_ms > 5000;
   - immediate_isolated_reaction;
   - не открывать буфер.

6. Если сообщение является strong starter:
   - открыть буфер;
   - поставить hard deadline first_ts + 1200 ms;
   - не enqueue сразу.

7. Иначе:
   - immediate_default;
   - сразу enqueue_tts.

Strong starter predicate

Сообщение может открыть буфер, если:

- не special-only;
- не emoji-only;
- не custom emoji-only;
- не animated custom emoji-only;
- не mention-only;
- не URL-only;
- не caps-shout;
- не keyboard-smash;
- не terminal/question;
- не isolated reaction after long pause;
- и выполняется одно из:
  - effective_length >= 10;
  - effective_length >= 6 and word_count >= 2;
  - contains_digit and effective_length >= 4 and not single_digit isolated.

Decision tree: активный буфер уже есть

Если буфер активен и приходит новое сообщение того же buffer key:

1. Если voice context изменился:
   - безопасно инвалидировать старый буфер;
   - не озвучивать его в неправильном канале;
   - логировать reason voice_context_changed.

2. Если incoming hard break:
   - сначала flush старого буфера;
   - потом обработать новое сообщение как isolated/immediate;
   - порядок должен сохраниться.

Hard break внутри активного буфера:

- emoji-only;
- custom emoji-only;
- animated custom emoji-only;
- mention-only;
- URL-only;
- question/terminal punctuation;
- caps shout;
- keyboard smash;
- incompatible voice/text context.

3. Если incoming можно append_soft:
   - добавить в буфер;
   - не сбрасывать hard cap от первого сообщения;
   - проверить MAX_PARTS;
   - проверить MAX_GROUP_EFFECTIVE_LEN;
   - если prospective append превышает лимит, сначала flush старого буфера, потом обработать incoming заново как isolated/start.

append_soft разрешен, если:

- буфер уже содержит substantive starter;
- incoming не special-only;
- incoming не hard break;
- incoming effective_length >= 1;
- incoming не emoji-only;
- incoming не custom emoji-only;
- incoming не mention-only;
- incoming не URL-only;
- now <= first_ts + HOLD_HARD_CAP_MS;
- prospective parts <= MAX_PARTS;
- prospective effective length <= MAX_GROUP_EFFECTIVE_LEN.

Важно: single word и single digit могут append_soft внутри активного буфера.

Это главное отличие от isolated reaction-like.

Buffer state

Буфер должен хранить примерно:

- key = (author_id, voice_channel_id) или текущий аналог;
- generation_id / timer_token;
- first_ts;
- last_ts;
- deadline_ts;
- items;
- effective_len_total;
- word_count_total;
- target voice channel snapshot;
- target text channel id;
- has_substantive_starter;
- current timer task.

Hard cap

Таймер не должен бесконечно продлеваться.

Если первое сообщение попало в буфер в T0, максимальный flush должен быть не позже T0 + HOLD_HARD_CAP_MS.

При append нового сообщения не делай “now + 1200 ms” как полный reset без ограничений.

Используй deadline = first_ts + HOLD_HARD_CAP_MS.

Можно пересоздавать timer task, но deadline остается hard cap от первого сообщения.

Order-preserving flush

Обязательно.

Если есть активный буфер и пришло сообщение, которое нельзя append:

1. Создай TTSJob из старого буфера.
2. Затем создай TTSJob из нового сообщения, если оно не drop.
3. Enqueue jobs строго в этом порядке.
4. Если enqueue старого job не удался, не ставь новый job раньше него.
5. Логируй flush_reason:
   - timer_flush;
   - max_parts;
   - max_group_effective_len;
   - flush_before_immediate;
   - flush_before_hard_break;
   - flush_before_reclassify;
   - voice_context_changed;
   - shutdown_drop;
   - enqueue_failed.

Queue behavior

Если текущий код использует put_nowait, аккуратно оцени, не нужно ли заменить на await queue.put с timeout.

Нужно избежать ситуации, где:
- старый буфер не попал в очередь;
- новое сообщение попало;
- порядок сломан.

Если очередь заполнена:
- логировать ошибку;
- не нарушать порядок;
- лучше drop/abort later jobs, чем enqueue более поздний job раньше старого.

Timers и race conditions

Нужно защититься от:

1. Таймер проснулся одновременно с новым сообщением.
2. Старый отмененный таймер проснулся позже.
3. Двойной flush одного буфера.
4. Flush после удаления буфера.
5. Смена voice channel.
6. User left voice.
7. Shutdown with active buffer.

Используй:
- per-key asyncio.Lock;
- generation_id/timer_token;
- проверку stale timer;
- безопасный remove buffer;
- явные flush/drop reasons.

Logging

Добавь логирование решений. Не обязательно логировать все всегда на info; можно debug/config flag.

Минимальные поля:

- message_id;
- guild_id;
- author_id или hash;
- voice_channel_id;
- text_channel_id;
- buffer_key;
- raw_length;
- effective_length;
- word_count;
- emoji_count;
- custom_emoji_count;
- animated_custom_emoji_count;
- mention_count;
- url_count;
- is_unicode_emoji_only;
- is_custom_emoji_only;
- is_animated_custom_emoji_only;
- is_mention_only;
- is_url_only;
- is_single_digit;
- is_single_symbol;
- is_caps_shout;
- is_keyboard_smash;
- is_isolated_reaction_like;
- decision_policy;
- chosen_timeout_ms;
- buffer_generation_id;
- messages_in_buffer;
- buffer_effective_length;
- buffer_age_ms;
- flush_reason;
- join_separator;
- arrived_ts;
- processed_ts;
- queued_ts;
- worker_start_ts;
- wav_ready_ts;
- pcm_ready_ts;
- playback_start_ts;
- playback_end_ts;
- message_to_queue_s;
- message_to_playback_start_s;
- queue_to_playback_start_s;
- enqueue_fail_reason.

Критически важно добавить или уточнить playback_start_ts.

Не путай:
- queue -> playback_done;
- message -> playback_start.

Главная latency-метрика — message -> playback_start.

Join separator

Для нового алгоритма используй по умолчанию:

JOIN_SEPARATOR = ", "

Не оставляй ". " как default для selective_hold_v2.

Причина:
- точка делает короткие Discord-фрагменты слишком рублеными;
- запятая лучше имитирует живую паузу;
- пробел может слишком склеивать слова;
- SSML не использовать, пока нет явной поддержки в текущем Piper pipeline.

Сделай separator конфигурируемым.

Shadow mode

Если это удобно по архитектуре, реализуй shadow mode.

Но это не обязательно должно блокировать production-код, если займет слишком много времени.

Shadow mode означает:
- legacy алгоритм реально управляет озвучкой;
- selective_hold_v2 только логирует, что он бы сделал.

Если shadow mode сложен, сделай хотя бы offline simulation/test harness на маленьких fixtures и подготовь код policy так, чтобы его можно было тестировать без Discord.

Обязательные тесты

Добавь автоматические тесты. Используй pytest или существующий тестовый фреймворк проекта.

Unit tests: parsing/effective_length

Покрыть:

1. Plain text.
2. Unicode emoji.
3. Repeated Unicode emoji.
4. Discord custom emoji <:name:id>.
5. Discord animated custom emoji <a:name:id>.
6. User mention <@id>.
7. User mention <@!id>.
8. Role mention <@&id>.
9. Channel mention <#id>.
10. URL-only.
11. URL + text.
12. Text + emoji.
13. Text + custom emoji.
14. Empty text.
15. Empty after process_text.
16. Single digit.
17. Single symbol.
18. Caps shout.
19. Keyboard smash.

Expected:
- effective_length не равен raw markup length для emoji/mentions/urls;
- raw Discord ids не попадают в spoken_text;
- custom emoji классифицируется корректно;
- animated custom emoji классифицируется корректно.

Unit tests: isolated decision

Покрыть:

1. Long text -> immediate_long.
2. Question -> immediate_terminal.
3. Terminal punctuation -> immediate_terminal.
4. Emoji-only -> immediate_special.
5. Custom emoji-only -> immediate_special.
6. Animated custom emoji-only -> immediate_special.
7. Mention-only -> drop/immediate_special.
8. URL-only -> drop/immediate_special.
9. Short isolated reaction after long pause -> immediate_isolated_reaction.
10. Short isolated reaction without active buffer -> immediate_default or immediate_isolated_reaction according to policy.
11. Meaningful starter -> hold_start.
12. Caps shout -> immediate_special.
13. Keyboard smash -> immediate_special.

Unit tests: active buffer

Покрыть:

1. Short word inside active buffer -> append_soft.
2. Single digit inside active buffer -> append_soft.
3. Emoji-only inside active buffer -> hard break.
4. Custom emoji-only inside active buffer -> hard break.
5. Animated custom emoji-only inside active buffer -> hard break.
6. Mention-only inside active buffer -> hard break.
7. URL-only inside active buffer -> hard break.
8. Question inside active buffer -> hard break.
9. Terminal punctuation inside active buffer -> hard break.
10. Caps shout inside active buffer -> hard break.
11. Keyboard smash inside active buffer -> hard break.
12. max_parts prevents append.
13. max_group_effective_len prevents append.
14. hard cap prevents append.

Unit tests: order preserving

Покрыть:

1. Active buffer + immediate message:
   - old buffer enqueued first;
   - new message enqueued second.

2. Active buffer + hard break:
   - old buffer flushed first;
   - hard break message handled separately.

3. Queue failure on old buffer:
   - new message does not jump ahead.

4. flush_reason logged correctly.

Unit tests: timers

Покрыть:

1. Timer flushes buffer.
2. Append does not extend beyond hard cap.
3. New generation_id invalidates stale timer.
4. Stale timer does nothing.
5. No double flush.
6. Shutdown drop/flush behavior.

Integration / fixture tests

Создай маленькие representative fixtures.

Fixture 1:

“ну там просто дается хп”
“при нажатии”
“манты”
“почему-то”
“даже под думом”

Expected:
- first meaningful starter opens buffer;
- follow-ups append_soft where allowed;
- no premature break just because “манты” or “почему-то” are short;
- flush by timer/hard cap/limits;
- separator is configured separator.

Fixture 2:

“где нет шансов”
“уже с минуты”
“3”

Expected:
- “3” appends inside active buffer.

Fixture 3:

text starter
custom emoji-only

Expected:
- text buffer flushes first;
- custom emoji handled separately;
- no raw emoji id in spoken_text.

Fixture 4:

single “бб” after long pause

Expected:
- no forced 900 ms legacy wait in selective_hold_v2;
- immediate path according to policy.

Fixture 5:

custom emoji-only with raw length > 40

Expected:
- not classified as long meaningful text;
- effective_length small;
- no raw id in TTS.

Fixture 6:

active buffer + question

Expected:
- old buffer flushes first;
- question handled separately.

Commands to run

Перед финальным отчетом запусти:

- git status
- python -m compileall .
- python -m pytest

Если в проекте используются:
- ruff
- flake8
- black --check
- mypy

то запусти их тоже.

Не добавляй тяжелые зависимости без необходимости.

Git workflow

Работай через git.

Перед изменениями:
1. Проверь git status.
2. Не затирай чужие изменения.
3. Если рабочее дерево грязное, укажи это в отчете.

Сделай логические коммиты.

Желательная структура коммитов:

Commit 1:
- parsing/effective_length utilities;
- tests for parsing/effective_length.

Commit 2:
- selective_hold_v2 policy/state machine;
- tests for decision logic.

Commit 3:
- integration into merge/enqueue path;
- order-preserving flush;
- timer/race protection.

Commit 4:
- logging fields;
- playback_start_ts;
- config flags.

Commit 5:
- fixtures/integration tests;
- documentation/update report.

Коммиты могут быть объединены, если проект маленький, но не делай один огромный непрозрачный коммит без необходимости.

Примеры commit messages:

- tts: add Discord token parsing and effective length
- tts: implement selective hold buffering policy
- tts: preserve order when flushing TTS buffers
- tts: add decision logging and playback start timing
- tests: cover selective TTS buffering edge cases

Итоговый отчет

После разработки напиши отчет.

В отчете укажи:

1. Какие файлы изменены.
2. Что добавлено в конфиг.
3. Как работает selective_hold_v2.
4. Как откатиться на legacy.
5. Как включить новый алгоритм.
6. Как включить/выключить decision logging.
7. Какие тесты добавлены.
8. Какие команды проверок запущены.
9. Результаты тестов.
10. Какие git-коммиты сделаны.
11. Какие риски остались.
12. Что нужно наблюдать после деплоя.
13. Какие метрики смотреть:
    - message -> playback_start p50;
    - message -> playback_start p90;
    - короткие singleton latency;
    - число TTS jobs;
    - средний размер merge group;
    - ошибочные emoji/reaction merges;
    - order inversion count;
    - queue failures;
    - stale timer count;
    - flush reasons distribution.

Критерии готовности

Работа считается готовой, если:

- selective_hold_v2 реализован;
- legacy mode сохранен;
- новый алгоритм можно выключить конфигом;
- параметры вынесены в конфиг/константы;
- effective_length реализован;
- Discord custom emoji и animated emoji обработаны;
- mentions/URLs не озвучиваются raw ids;
- isolated reaction-like не получает forced 900 ms wait;
- короткие текстовые хвосты внутри активного буфера могут append_soft;
- emoji/custom emoji внутри буфера hard break;
- order-preserving flush реализован;
- hard cap от первого сообщения работает;
- stale timers не делают double flush;
- playback_start_ts или эквивалентная метрика добавлена;
- тесты проходят;
- git-коммиты сделаны;
- итоговый отчет написан.

Не делай

- Не анализируй большие логи.
- Не проводи новый deep research.
- Не внедряй ML/LLM/embeddings/NLP.
- Не переписывай весь бот без необходимости.
- Не делай ожидания 3–5 секунд.
- Не используй raw len как главный merge-признак.
- Не отправляй raw Discord ids в TTS.
- Не ломай порядок сообщений.
- Не считай queue -> playback_done задержкой до начала речи.
- Не хардкодь пользователя без конфига, если можно сделать нормально.
- Не делай один огромный коммит без тестов.

Финальный результат

Нужен production-safe v1:

- selective_hold_v2;
- fast immediate path для одиночных реакций и спецсообщений;
- append_soft для коротких текстовых хвостов внутри active buffer;
- hard cap 1200 ms;
- order-preserving flush;
- effective_length;
- безопасная нормализация Discord токенов;
- message -> playback_start logging;
- тесты;
- git-коммиты;
- отчет.