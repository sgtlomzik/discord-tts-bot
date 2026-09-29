# План: параллельная генерация TTS

Статус: план, 2026-09-28. Код не менялся.

## 1. Цель

Запросы к провайдеру для следующих сообщений отправлять сразу, по 2–4 одновременно.
Воспроизведение остаётся строго по порядку (FIFO). Так задержка генерации перестаёт
накапливаться в сериях коротких сообщений.

## 2. Как сейчас

- Путь с префетчем (`TTS_PREFETCH_ENABLED=1`, по умолчанию):
  - `_generation_worker` (`ttsbot/pipeline.py:616`) берёт задание из `message_queue`.
  - Кладёт `PreparedAudio` в `ready_queue` (`maxsize=TTS_PREFETCH_LOOKAHEAD`, по умолчанию 1).
  - Затем **ждёт** `_prepare_into` целиком и только после этого берёт следующее задание.
- `_playback_worker` (`ttsbot/playback.py:34`) читает `ready_queue` по порядку и сливает
  `prepared.channel` в непрерывный плеер.
- Итог: в каждый момент генерируется не больше одного сообщения, плюс одно готовое ждёт впереди.
  Генерация N+1 стартует только после **полного** окончания генерации N. Третье
  сообщение серии не может начать генерироваться, пока первое не доиграло.

### Что показывают логи (500 последних `Audio start`, 473 из них Fish)

| метрика | p50 | p90 | p95 |
|---|---|---|---|
| message_to_audio | 1.17 с | 1.91 с | 2.21 с |
| queue_wait (ждал предыдущее аудио) | 0.00 с | 1.36 с | 1.69 с |
| старт звука после pickup, если сообщение ждало в очереди (147 шт.) | 0.00 с | 0.59 с | — |
| то же для сообщений без очереди (314 шт.) | 1.14 с | 1.88 с | — |

Вывод: на Fish текущий префетч уже прячет генерацию в большинстве серий. Параллельность
уберёт хвост (p90 0.59 с → ~0). Основная часть `queue_wait` — ожидание, пока доиграет
предыдущее сообщение, и она неустранима.

Большой выигрыш будет у провайдеров, где генерация дольше звучания. Пример: Gemini через
OpenRouter без стриминга даёт 1.5–4 с на реплику, а медианная реплика bussshy (12 символов)
звучит около 1 с. Сейчас каждое такое сообщение в серии добавляет примерно +0.5–1 с,
при параллельности задержка держится на уровне ~1.7 с.

## 3. Дизайн

### 3.1 Генерационный воркер: окно вместо последовательного ожидания

```python
async def _generation_worker(self) -> None:
    ...
    while not self.is_closed():
        job = await self.message_queue.get()
        prepared = PreparedAudio(job=job, channel=asyncio.Queue())
        self.active_prepared.add(prepared)
        try:
            await self.ready_queue.put(prepared)      # порядок + backpressure
            await self.generation_slots.acquire()     # общий лимит, FIFO
        except BaseException:
            prepared.channel.put_nowait(None)
            self.message_queue.task_done()
            raise
        prepared.task = asyncio.create_task(
            self._run_prepare(prepared), name=f"tts-gen-{job.guild_id}")
        self.generation_tasks.add(prepared.task)
        prepared.task.add_done_callback(self.generation_tasks.discard)

async def _run_prepare(self, prepared: PreparedAudio) -> None:
    try:
        await self._prepare_into(prepared)            # уже всегда ставит sentinel None
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("TTS generation pipeline error")
    finally:
        self.generation_slots.release()
        self.message_queue.task_done()
```

Почему так:

- **Порядок** задаётся порядком `ready_queue.put`, и его никто не меняет. Каждое сообщение
  пишет в свой `channel`, плеер читает их по очереди. Вне порядка может завершаться только
  генерация, но не воспроизведение.
- **Слот семафора берётся в цикле, до `create_task`.** Слоты выдаются строго по порядку
  сообщений, поэтому более позднее сообщение не может занять слот раньше более раннего и
  дедлока нет.
- **Размер окна:** `ready_queue.maxsize = max(TTS_PREFETCH_LOOKAHEAD, TTS_GENERATION_CONCURRENCY)`.
  Одновременно генерируются максимум `CONCURRENCY` сообщений. Вперёд буферизуется не больше
  `maxsize` + одно играющее.
- В `_prepare_into` финальный `await prepared.channel.put(None)` заменить на `put_nowait(None)`.
  Очередь безлимитная, а под отменой не должно быть точки ожидания.

### 3.2 Лимиты по провайдерам

Общий семафор ограничивает число сообщений в работе. Отдельные семафоры (`self.provider_slots`)
ограничивают сетевые и CPU-запросы к конкретному провайдеру. Держатся только на время вызова;
попадания в кэш их не берут.

| провайдер | где взять слот | по умолчанию |
|---|---|---|
| Fish | вокруг `stream(...)` в `_generate_fish_stream_into` | `FISH_MAX_CONCURRENCY=2` |
| MiniMax | вокруг `_stream_to_channel` в `_generate_stream_into` | `MINIMAX_MAX_CONCURRENCY=2` |
| Piper (local) | вокруг `tts_dispatcher.synthesize` в `_generate_file_into` | `PIPER_MAX_CONCURRENCY=1` (CPU) |
| будущие Gemini/Inworld | в их `_generate_*_into` | 3 |

Лимиты Fish по тарифу нужно проверить до выкатки: при превышении concurrency Fish отдаёт 429.
Сейчас 429 засчитывается как отказ и может открыть breaker. План на этот случай:
429 → повтор с backoff 200–400 мс внутри слота, как `pre_audio`, без записи в breaker.

### 3.3 Конфиг (`ttsbot/config.py`, раздел префетча)

- `TTS_GENERATION_CONCURRENCY` — по умолчанию **1**, что совпадает с текущим поведением.
  Рекомендация для прода — 3.
- `FISH_MAX_CONCURRENCY`, `MINIMAX_MAX_CONCURRENCY`, `PIPER_MAX_CONCURRENCY`.
- Семафоры создаются в `core.py` рядом с `ready_queue`. `config.reload()` на живом боте их
  не пересоздаёт: как и `QUEUE_MAXSIZE`, значения применяются после рестарта. Это надо
  записать в README.

### 3.4 Отмена и остановка

- В `PreparedAudio` (`ttsbot/models.py:47`) добавить поле `task: asyncio.Task | None = None`.
- `queue-clear` (`merge.py:clear_queue_for_guild`) по-прежнему только выставляет `cancelled=True`.
  Стрим-циклы уже проверяют флаг и корректно закрывают ffmpeg и кэш. Жёсткий `task.cancel()`
  на этапе 1 **не делаем**: ветки `CancelledError` в `_decode_stream_to_channel` и
  `_stream_fish_opus_to_channel` не проверены на утечки ffmpeg и `.tmp`. Кандидат на этап 2.
- В `close()` (`core.py`) отменять все `generation_tasks` и ждать их через `gather(..., return_exceptions=True)`.

### 3.5 Общее состояние: что проверено

- **Circuit breaker** (`providers.py:144`): операции синхронные, в asyncio атомарны.
  Пробный запрос в HALF_OPEN уже одноразовый. Одно изменение в поведении: 3 параллельных
  отказа откроют breaker сразу, что даже полезно. Поздний `record_success` от запроса,
  стартовавшего до открытия, закроет breaker — это допустимо, фиксируем тестом.
- **Кэш:** в путях префетча временные файлы с `uuid` (`pipeline.py:804, 991`), `os.replace`
  атомарен, `commit_file` синхронный. Два одинаковых текста параллельно дадут два запроса и
  один выживший файл, это безопасно. Дедупликацию генерации одинаковых текстов в полёте
  пока не делаем, она редкая. Фиксированный `.part` (`pipeline.py:491`) есть только в
  legacy `tts_worker`, параллельность его не затрагивает.
- **Слияние сообщений** (`merge.py`) происходит до `message_queue`. Более раннее
  извлечение из очереди на него не влияет.
- **Память:** окно не больше 4 сообщений × 300 символов, в худшем случае это ~4 × 3.8 МБ PCM.
- **Legacy-режим** (`TTS_PREFETCH_ENABLED=0`, `tts_worker`) не трогаем.

### 3.6 Метрики

Добавить в `Audio start`:

- `gen_wait` — от постановки в очередь до старта генерации (слот);
- `gen_first` — от старта генерации до первого батча;
- `ready_ahead` — сколько первый батч ждал плеера;
- `in_flight` — число генераций в этот момент.

С ними можно сравнить до и после на тех же 500 событиях через тот же grep, что в §2.

## 4. Тесты (`tests/test_bot_prefetch.py` + новый `tests/test_parallel_generation.py`)

1. **Порядок:** 3 задания, фейковый провайдер с задержками 0.5/0.1/0.1 с. Воспроизведение
   идёт 1, 2, 3, а генерация 2 и 3 стартует до конца 1.
2. **Лимит:** счётчик одновременных генераций не превышает `CONCURRENCY`.
   При `CONCURRENCY=1` все существующие тесты префетча проходят без изменений.
3. **Лимит провайдера:** Piper никогда не параллелится, Fish ограничен `FISH_MAX_CONCURRENCY`.
4. **Ошибка в середине:** исключение в задании 2 не ломает 1 и 3; sentinel есть, плеер не виснет.
5. **queue-clear** при 3 генерациях в полёте: ничего не проигрывается, все каналы закрыты,
   слоты возвращены (следующее задание стартует).
6. **Breaker:** 3 параллельных отказа открывают breaker, 4-е сообщение идёт в Piper.
7. **Остановка:** `close()` не оставляет задач (без предупреждений «Task was destroyed»).
8. **Pre-audio fallback** на Piper под параллельностью всё так же отдаёт звук по порядку.

Полный прогон: `python -m unittest discover -s tests -p 'test_*.py'` внутри образа.

## 5. Выкатка

1. Отдельная ветка `feat/parallel-generation` от `main`. Текущая `feat/gemini-openrouter`
   не трогается.
2. Код с `TTS_GENERATION_CONCURRENCY=1` по умолчанию, тесты, ревью, мерж. Поведение прода
   не меняется.
3. В `.env` выставить `TTS_GENERATION_CONCURRENCY=3`, `FISH_MAX_CONCURRENCY=2`, затем рестарт.
4. 1–2 дня смотреть на новые метрики, 429 от Fish и состояния breaker.
5. Откат — вернуть `TTS_GENERATION_CONCURRENCY=1` и перезапустить, без изменений кода.

## 6. Вне рамок

- Один глобальный `_playback_worker` на все гильдии: гильдии по-прежнему звучат по очереди.
  Воркер на гильдию — отдельная задача.
- Жёсткая отмена HTTP-стримов при `queue-clear` (см. §3.4).
- Дедупликация одинаковых текстов в полёте.

## 7. Объём

~100–130 строк в `pipeline.py`, `core.py`, `models.py`, `config.py`, ~250 строк тестов,
README (две версии) и `docs/ARCHITECTURE.md`. Оценка — около рабочего дня вместе с ревью.
