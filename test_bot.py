import importlib.util
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
