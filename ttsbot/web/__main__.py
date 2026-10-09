"""Run the recordings website: ``python -m ttsbot.web``."""

from aiohttp import web

from ttsbot import config
from ttsbot.web.app import build_from_config


def main() -> None:
    config.setup_logging()
    web.run_app(build_from_config(), host=config.WEB_HOST, port=config.WEB_PORT,
                access_log_format='%a "%r" %s %b %Tfs', shutdown_timeout=10)


if __name__ == "__main__":
    main()
