import asyncio
import importlib.util
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, MagicMock


def load_bot_module():
    module_path = Path(__file__).with_name("bot.py")
    spec = importlib.util.spec_from_file_location("tts_bot_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TTSBotTests(unittest.TestCase):
    def test_process_text_removes_urls_and_collapses_spaces(self):
        bot = load_bot_module()

        self.assertEqual(bot.process_text("hello  https://example.com/a  world"), "hello world")

    def test_process_text_replaces_known_custom_emoji(self):
        bot = load_bot_module()

        self.assertEqual(bot.process_text("one <:kekw:123> two <a:pepe_sad:456>"), "one Кек two Грустно")

    def test_parse_user_ids_ignores_invalid_entries(self):
        bot = load_bot_module()

        self.assertEqual(bot.parse_user_ids("1, bad; 2,,3"), {1, 2, 3})

    def test_process_text_replaces_newlines(self):
        bot = load_bot_module()

        self.assertEqual(bot.process_text("hello\nworld"), "hello. world")

    def test_normalize_rhvoice_url_adds_default_port(self):
        bot = load_bot_module()

        self.assertEqual(bot.normalize_rhvoice_url("http://172.20.0.1"), "http://172.20.0.1:5002")

    def test_build_rhvoice_url_contains_required_params(self):
        bot = load_bot_module()

        url = bot.build_rhvoice_url("test phrase")
        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        self.assertEqual(parsed.path, "/say")
        self.assertEqual(params["text"], ["test phrase"])
        self.assertEqual(params["voice"], [bot.RHVOICE_VOICE])
        self.assertEqual(params["format"], ["wav"])
        self.assertEqual(params["rate"], [str(bot.RHVOICE_RATE)])
        self.assertEqual(params["pitch"], [str(bot.RHVOICE_PITCH)])
        self.assertEqual(params["volume"], [str(bot.RHVOICE_VOLUME)])


class TTSBotWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tts_worker_does_not_disconnect_on_tts_generation_error(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()

        tts_bot.wait_until_ready = AsyncMock()
        tts_bot.wait_for_rhvoice = AsyncMock()
        tts_bot.warmup_tts = AsyncMock()
        tts_bot.ensure_voice = AsyncMock(return_value=object())
        tts_bot.generate_tts_file = AsyncMock(side_effect=RuntimeError("tts failed"))
        tts_bot.play_file = AsyncMock()
        tts_bot.disconnect_guild_voice = AsyncMock()
        tts_bot.is_closed = MagicMock(side_effect=[False, True])

        guild = types.SimpleNamespace(id=1)
        voice_channel = types.SimpleNamespace(id=2, guild=guild)
        await tts_bot.message_queue.put(("hello", voice_channel, 0.0))

        await asyncio.wait_for(tts_bot.tts_worker(), timeout=1.0)

        tts_bot.disconnect_guild_voice.assert_not_awaited()
        tts_bot.play_file.assert_not_awaited()

    async def test_tts_worker_disconnects_on_voice_connect_error(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()

        tts_bot.wait_until_ready = AsyncMock()
        tts_bot.wait_for_rhvoice = AsyncMock()
        tts_bot.warmup_tts = AsyncMock()
        tts_bot.ensure_voice = AsyncMock(side_effect=RuntimeError("connect failed"))
        tts_bot.generate_tts_file = AsyncMock(return_value=None)
        tts_bot.play_file = AsyncMock()
        tts_bot.disconnect_guild_voice = AsyncMock()
        tts_bot.is_closed = MagicMock(side_effect=[False, True])

        guild = types.SimpleNamespace(id=10)
        voice_channel = types.SimpleNamespace(id=20, guild=guild)
        await tts_bot.message_queue.put(("hello", voice_channel, 0.0))

        await asyncio.wait_for(tts_bot.tts_worker(), timeout=1.0)

        tts_bot.disconnect_guild_voice.assert_awaited_once_with(guild)
        tts_bot.play_file.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
