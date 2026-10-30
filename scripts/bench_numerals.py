#!/usr/bin/env python3
"""What spelling out numbers (ttsbot/numerals.py) costs the bot.

Measures, in this process:

* startup: importing the module, loading the morphology dictionary and
  spelling the small numbers (``warm_up``), and the memory that adds;
* latency: ``normalize_for_tts`` with ``TTS_NUMBERS`` on and off over chat
  scenarios, and the difference (the owner's budget is 10 ms a message);
* accuracy: the share of tests/test_numerals.py CASES spoken as expected.

Run it inside the bot image, on the server the bot runs on, limited to one
core like a busy bot:

    docker run --rm --cpus=1 -v "$PWD":/app -w /app --entrypoint python \\
        ghcr.io/sgtlomzik/discord-tts-bot:<tag> scripts/bench_numerals.py
"""

from __future__ import annotations

import gc
import importlib
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

SCENARIOS = {
    "no digits": "го играть, кто со мной? давайте уже начнём",
    "short, 1 number": "го в 5",
    "typical, 3 numbers": "с 5 до 7 вечера в 3 играх, у меня 2 машины",
    "long, 300 chars": (
        "короче вчера сыграли 3 катки, во 2-й я был на 1 месте, набил 25 фрагов "
        "за 40 минут, а потом в 21:00 зашли ещё 4 человека и мы играли до 2 ночи. "
        "в итоге у меня 1500 голды и 3,5 часа онлайна, завтра в 18:30 продолжим, "
        "кто будет — пишите, нужно ещё 2 игрока на 5х5. ставлю 100$ что выиграем"),
    "worst, 300 chars of numbers": (
        "1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 21-го 22-м 2026 году 30.10.2026 "
        "в 21:00 до 15:30 3,5 7,25 100% 50% 100$ 500р 1.5к 3к x2 1v1 5-7 2-3 "
        "с 5 до 7 в 3 играх с 2 друзьями о 5 машинах к 5 часам около 100 человек "
        "1000000 12345 999 888 777 666 555 444 333 222 111 1-й 2-я 3-е 4-го 5-му "
        "6-м 7-ми 8-ми 9-ти 10-й 11-го 12-му"),
    "english": "see you at 5, gg wp 2:1",
}
RUNS = 300


def rss_mb() -> float:
    with open("/proc/self/status") as status:
        for line in status:
            if line.startswith("VmRSS"):
                return int(line.split()[1]) / 1024
    return 0.0


def percentile(times: list[float], q: float) -> float:
    return sorted(times)[min(len(times) - 1, int(len(times) * q))]


def latency(normalize, text: str, caches=()) -> list[float]:
    """Milliseconds per call; ``caches`` are cleared before each one, as for
    words and numbers the bot meets for the first time."""
    times = []
    for _ in range(RUNS):
        for cache in caches:
            cache.cache_clear()
        started = time.perf_counter()
        normalize(text)
        times.append((time.perf_counter() - started) * 1000)
    return times


def main() -> None:
    import discord  # noqa: F401  (loaded by the bot anyway)
    import emoji  # noqa: F401

    before = rss_mb()
    started = time.perf_counter()
    from ttsbot import numerals
    imported = time.perf_counter()
    numerals.warm_up()
    warmed = time.perf_counter()
    gc.collect()
    print(f"import {1000 * (imported - started):.0f} ms, warm_up {1000 * (warmed - imported):.0f} ms, "
          f"memory +{rss_mb() - before:.1f} MB")

    from ttsbot import config
    from ttsbot.textnorm import normalize_for_tts

    # The numbers warm_up spelled at startup stay: they are there for every message.
    caches = (numerals._word, numerals._counted, numerals._gender_of,
              numerals._spell_cardinal, numerals._spell_ordinal)
    for title, cleared in (("repeated text (caches warm)", ()), ("new text (caches cleared)", caches)):
        print(f"\n{title:30} {'p50 ms':>8} {'p99 ms':>8} {'+p50 ms':>8} {'+p99 ms':>8} {'max ms':>8}")
        for name, text in SCENARIOS.items():
            result = {}
            for flag in ("0", "1"):
                os.environ["TTS_NUMBERS"] = flag
                config.reload()
                normalize_for_tts(text)  # the emoji library's first-call setup
                result[flag] = latency(normalize_for_tts, text, cleared)
            added = [on - off for on, off in zip(sorted(result["1"]), sorted(result["0"]))]
            print(f"{name:30} {percentile(result['1'], 0.5):8.3f} {percentile(result['1'], 0.99):8.3f} "
                  f"{percentile(added, 0.5):8.3f} {percentile(added, 0.99):8.3f} {max(result['1']):8.3f}")

    cases_by_group: dict[str, list[tuple[str, str]]] = importlib.import_module("test_numerals").CASES
    total = sum(len(cases) for cases in cases_by_group.values())
    right = sum(numerals.speak_numbers(raw) == spoken
                for cases in cases_by_group.values() for raw, spoken in cases)
    print(f"\naccuracy on tests/test_numerals.py: {right}/{total}")


if __name__ == "__main__":
    main()
