"""Unit tests for TTSPhraseCache (the optional commit 7 LRU cache)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ttsbot.providers import TTSCacheConfig, TTSPhraseCache


class TTSPhraseCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmp.name) / "cache"
        self.cfg = TTSCacheConfig(
            enabled=True, max_entries=3, cache_dir=self.cache_dir,
        )
        self.cache = TTSPhraseCache(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_source(self, name: str, payload: bytes = b"MP3") -> Path:
        src = Path(self.tmp.name) / name
        src.write_bytes(payload)
        return src

    def test_disabled_cache_lookup_always_misses(self):
        cfg = TTSCacheConfig(enabled=False, max_entries=3, cache_dir=self.cache_dir)
        cache = TTSPhraseCache(cfg)
        src = self._write_source("x.mp3")
        cache.store("hello", src)
        self.assertIsNone(cache.lookup("hello"))

    def test_store_then_lookup_returns_cached_path(self):
        src = self._write_source("hello.mp3", b"PAYLOAD_HELLO")
        self.cache.store("hello", src)
        hit = self.cache.lookup("hello")
        self.assertIsNotNone(hit)
        self.assertTrue(hit.exists())
        self.assertEqual(hit.read_bytes(), b"PAYLOAD_HELLO")

    def test_lookup_miss_for_unknown_text(self):
        self.assertIsNone(self.cache.lookup("never-stored"))

    def test_lookup_evicts_when_source_vanishes(self):
        src = self._write_source("hi.mp3")
        self.cache.store("hi", src)
        # Simulate external cleanup wiping the cache file.
        for f in self.cache_dir.iterdir():
            f.unlink()
        self.assertIsNone(self.cache.lookup("hi"))
        self.assertEqual(self.cache.size, 0)

    def test_lru_eviction_when_over_max_entries(self):
        # max_entries=3 -> adding a 4th should evict the oldest.
        for i in range(4):
            src = self._write_source(f"s{i}.mp3", f"PAYLOAD_{i}".encode())
            self.cache.store(f"text{i}", src)
        self.assertEqual(self.cache.size, 3)
        # text0 was the oldest; should be evicted from cache entries
        # (its file may still exist if store() wrote it before the LRU
        # eviction pass — but the lookup must miss).
        self.assertIsNone(self.cache.lookup("text0"))
        # And the more recent ones are still cached.
        self.assertIsNotNone(self.cache.lookup("text3"))

    def test_repeated_lookup_promotes_lru_position(self):
        # Store A, B, C, then look up A (oldest). Cache max = 3.
        # Now storing D should evict B (the new oldest), not A.
        for label in ("A", "B", "C"):
            src = self._write_source(f"{label}.mp3", label.encode())
            self.cache.store(label, src)
        self.cache.lookup("A")  # touch A
        src_d = self._write_source("D.mp3", b"D")
        self.cache.store("D", src_d)
        # A is still cached (just touched); B is evicted.
        self.assertIsNotNone(self.cache.lookup("A"))
        self.assertIsNotNone(self.cache.lookup("C"))
        self.assertIsNotNone(self.cache.lookup("D"))
        self.assertIsNone(self.cache.lookup("B"))

    def test_hash_is_stable_across_instances(self):
        # Same text -> same hash. Important so a bot restart can
        # warm up cache hits from previous lifetime (when both point
        # at the same /dev/shm/tts_cache dir).
        a = TTSPhraseCache.hash_text("привет")
        b = TTSPhraseCache.hash_text("привет")
        self.assertEqual(a, b)
        # Different text -> different hash.
        self.assertNotEqual(a, TTSPhraseCache.hash_text("пока"))

    def test_hash_differs_by_voice(self):
        # Same text, different voice => different key (no collision).
        t = "привет"
        self.assertNotEqual(
            TTSPhraseCache.hash_text(t, "piper-ruslan"),
            TTSPhraseCache.hash_text(t, "bussshy01"),
        )
        self.assertEqual(
            TTSPhraseCache.hash_text(t, "v1"), TTSPhraseCache.hash_text(t, "v1")
        )

    def test_cache_key_includes_voice_no_collision(self):
        # Storing the same text under two voices must yield two distinct
        # cache files; each voice looks up its own audio.
        src_a = self._write_source("a.mp3", b"VOICE_A_AUDIO")
        src_b = self._write_source("b.mp3", b"VOICE_B_AUDIO")
        self.cache.store("привет", src_a, "piper-ruslan")
        self.cache.store("привет", src_b, "bussshy01")

        hit_a = self.cache.lookup("привет", "piper-ruslan")
        hit_b = self.cache.lookup("привет", "bussshy01")
        self.assertIsNotNone(hit_a)
        self.assertIsNotNone(hit_b)
        self.assertNotEqual(hit_a, hit_b)
        self.assertEqual(hit_a.read_bytes(), b"VOICE_A_AUDIO")
        self.assertEqual(hit_b.read_bytes(), b"VOICE_B_AUDIO")
        # The unqualified key is a different key again -> miss.
        self.assertIsNone(self.cache.lookup("привет"))

    def test_hit_miss_counters(self):
        src = self._write_source("a.mp3", b"A")
        self.cache.store("hi", src)
        self.assertIsNotNone(self.cache.lookup("hi"))   # hit
        self.assertIsNone(self.cache.lookup("nope"))    # miss
        self.assertIsNone(self.cache.lookup("hi", "v2"))  # miss (other voice)
        self.assertEqual(self.cache.hits, 1)
        self.assertEqual(self.cache.misses, 2)

    def test_store_creates_cache_dir_on_demand(self):
        new_dir = Path(self.tmp.name) / "deeper" / "cache"
        cfg = TTSCacheConfig(enabled=True, max_entries=2, cache_dir=new_dir)
        cache = TTSPhraseCache(cfg)
        self.assertFalse(new_dir.exists())
        src = self._write_source("x.mp3")
        cache.store("x", src)
        self.assertTrue(new_dir.exists())


class TTSCacheByteEvictionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmp.name) / "cache"

    def tearDown(self):
        self.tmp.cleanup()

    def _cache(self, max_bytes):
        return TTSPhraseCache(
            TTSCacheConfig(enabled=True, max_bytes=max_bytes, max_entries=0,
                           cache_dir=self.cache_dir)
        )

    def _src(self, name, payload):
        p = Path(self.tmp.name) / name
        p.write_bytes(payload)
        return p

    def test_evicts_by_total_bytes(self):
        cache = self._cache(max_bytes=250)  # ~2.5 x 100-byte files
        for i in range(4):
            cache.store(f"t{i}", self._src(f"s{i}.mp3", b"x" * 100))
        self.assertLessEqual(cache.total_bytes, 250)
        self.assertEqual(cache.size, 2)            # only 2 of 100B fit under 250
        self.assertIsNone(cache.lookup("t0"))      # oldest evicted
        self.assertIsNone(cache.lookup("t1"))
        self.assertIsNotNone(cache.lookup("t3"))   # newest kept

    def test_total_bytes_tracks_store_and_evict(self):
        cache = self._cache(max_bytes=10_000)
        cache.store("a", self._src("a.mp3", b"x" * 300))
        cache.store("b", self._src("b.mp3", b"x" * 200))
        self.assertEqual(cache.total_bytes, 500)
        self.assertEqual(cache.size, 2)

    def test_skip_file_larger_than_cap(self):
        cache = self._cache(max_bytes=100)
        cache.store("big", self._src("big.mp3", b"x" * 500))  # > cap
        self.assertEqual(cache.size, 0)
        self.assertEqual(cache.total_bytes, 0)

    def test_rehydrate_from_disk_survives_restart(self):
        c1 = self._cache(max_bytes=10_000)
        c1.store("привет", self._src("p.mp3", b"HELLO_AUDIO"), "bussshy01")
        # New instance on the same dir = simulated restart.
        c2 = self._cache(max_bytes=10_000)
        self.assertEqual(c2.size, 1)
        hit = c2.lookup("привет", "bussshy01")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.read_bytes(), b"HELLO_AUDIO")
        self.assertEqual(c2.total_bytes, len(b"HELLO_AUDIO"))

    def test_a_used_entry_outlives_unused_ones_across_a_restart(self):
        import os
        import time

        config = TTSCacheConfig(enabled=True, max_entries=2, cache_dir=self.cache_dir)
        cache = TTSPhraseCache(config)
        for text in ("a", "b"):
            cache.store(text, self._src(f"{text}.mp3", text.encode()), "v")
        now = time.time()
        os.utime(cache.lookup("a", "v"), (now - 100, now - 100))  # a: stored long ago
        os.utime(cache.lookup("b", "v"), (now - 50, now - 50))
        self.assertIsNotNone(cache.lookup("a", "v"))  # ...but just used
        restarted = TTSPhraseCache(config)
        restarted.store("c", self._src("c.mp3", b"c"), "v")
        self.assertIsNotNone(restarted.lookup("a", "v"))
        self.assertIsNone(restarted.lookup("b", "v"))

    def test_commit_file_registers_streaming_output(self):
        cache = self._cache(max_bytes=10_000)
        target = cache.cache_path_for("да", "bussshy01")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"STREAMED_MP3")  # simulate streaming writing chunks
        registered = cache.commit_file("да", target, "bussshy01")
        self.assertEqual(registered, target)
        hit = cache.lookup("да", "bussshy01")
        self.assertEqual(hit, target)
        self.assertEqual(cache.total_bytes, len(b"STREAMED_MP3"))

    def test_commit_file_missing_returns_none(self):
        cache = self._cache(max_bytes=10_000)
        self.assertIsNone(cache.commit_file("x", self.cache_dir / "nope.mp3", "v"))


if __name__ == "__main__":
    unittest.main()