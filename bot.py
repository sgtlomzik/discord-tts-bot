import asyncio
import ctypes.util
import logging
import os
import re
import uuid
from pathlib import Path

import discord
import edge_tts
from discord.ext import commands


LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format=LOG_FORMAT)
log = logging.getLogger("tts_bot")

TMP_DIR = Path(os.getenv("TTS_TMP_DIR", "/dev/shm"))
DEFAULT_WHITELIST = "441612025286885397"

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
VOICE_NAME = os.getenv("TTS_VOICE", "ru-RU-DmitryNeural").strip()
TTS_RATE = os.getenv("TTS_RATE", "+10%").strip()
MAX_TEXT_LENGTH = int(os.getenv("TTS_MAX_TEXT_LENGTH", "500"))
QUEUE_MAXSIZE = int(os.getenv("TTS_QUEUE_MAXSIZE", "50"))

EMOJI_MAP = {
    "Blya2x": "Бля",
    "pepe_sad": "Грустно",
    "kekw": "Кек",
}


def parse_user_ids(value: str) -> set[int]:
    user_ids: set[int] = set()
    for part in value.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            user_ids.add(int(part))
        except ValueError:
            log.warning("Ignoring invalid WHITELIST_USERS entry: %r", part)
    return user_ids


WHITELIST_USERS = parse_user_ids(os.getenv("WHITELIST_USERS", DEFAULT_WHITELIST))

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True


def load_opus() -> bool:
    if discord.opus.is_loaded():
        return True

    opus_path = ctypes.util.find_library("opus")
    if not opus_path:
        log.error("Opus library not found")
        return False

    discord.opus.load_opus(opus_path)
    log.info("Opus loaded: %s", opus_path)
    return discord.opus.is_loaded()


def process_text(text: str) -> str:
    text = re.sub(r"http[s]?://\S+", "", text)

    def replace_emoji(match: re.Match[str]) -> str:
        return EMOJI_MAP.get(match.group(1), "")

    text = re.sub(r"<a?:([a-zA-Z0-9_]+):[0-9]+>", replace_emoji, text)
    return " ".join(text.split())


class TTSBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=("!tts ", "!tts"), intents=intents)
        self.message_queue: asyncio.Queue[tuple[str, discord.VoiceChannel]] = asyncio.Queue(
            maxsize=QUEUE_MAXSIZE
        )
        self.worker_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        self.worker_task = asyncio.create_task(self.tts_worker(), name="tts-worker")

    async def close(self) -> None:
        if self.worker_task:
            self.worker_task.cancel()
        await super().close()

    async def tts_worker(self) -> None:
        await self.wait_until_ready()
        log.info("TTS worker started")

        while not self.is_closed():
            text, voice_channel = await self.message_queue.get()
            filename: Path | None = None

            try:
                vc = discord.utils.get(self.voice_clients, guild=voice_channel.guild)

                if not vc or not vc.is_connected():
                    log.info(
                        "Connecting to voice channel guild=%s channel=%s",
                        voice_channel.guild.id,
                        voice_channel.id,
                    )
                    vc = await voice_channel.connect(timeout=60.0, self_deaf=True)
                elif vc.channel != voice_channel:
                    log.info(
                        "Moving voice client guild=%s channel=%s",
                        voice_channel.guild.id,
                        voice_channel.id,
                    )
                    await vc.move_to(voice_channel)

                filename = TMP_DIR / f"tts_{uuid.uuid4().hex}.mp3"
                log.info("Generating TTS chars=%s voice=%s", len(text), VOICE_NAME)
                communicate = edge_tts.Communicate(text, VOICE_NAME, rate=TTS_RATE)
                await communicate.save(str(filename))

                await self.play_file(vc, filename)
                log.info("Playback finished guild=%s channel=%s", voice_channel.guild.id, voice_channel.id)

            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("TTS processing failed")
                await self.disconnect_guild_voice(voice_channel.guild)
            finally:
                if filename and filename.exists():
                    try:
                        filename.unlink()
                    except OSError:
                        log.exception("Failed to remove temp file: %s", filename)
                self.message_queue.task_done()

    async def play_file(self, vc: discord.VoiceClient, filename: Path) -> None:
        if vc.is_playing() or vc.is_paused():
            vc.stop()

        finished = asyncio.Event()
        loop = asyncio.get_running_loop()

        def after(error: Exception | None) -> None:
            if error:
                log.error("Playback callback error", exc_info=error)
            loop.call_soon_threadsafe(finished.set)

        audio = discord.FFmpegPCMAudio(
            str(filename),
            before_options="-hide_banner -loglevel warning",
            options="-vn",
        )
        log.info("Starting playback file=%s size=%s", filename, filename.stat().st_size)
        vc.play(audio, after=after)
        await finished.wait()

    async def disconnect_guild_voice(self, guild: discord.Guild) -> None:
        vc = discord.utils.get(self.voice_clients, guild=guild)
        if not vc:
            return
        try:
            await vc.disconnect(force=True)
        except Exception:
            log.exception("Voice disconnect cleanup failed")


bot = TTSBot()


@bot.event
async def on_ready() -> None:
    log.info("TTS bot logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    log.info("Opus loaded: %s", discord.opus.is_loaded())
    log.info("Whitelist users: %s", ",".join(str(user_id) for user_id in sorted(WHITELIST_USERS)))


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    if message.author.id in WHITELIST_USERS and message.author.voice and message.author.voice.channel:
        final_text = process_text(message.clean_content)
        if final_text:
            try:
                bot.message_queue.put_nowait((final_text[:MAX_TEXT_LENGTH], message.author.voice.channel))
                log.info(
                    "Queued TTS guild=%s channel=%s author=%s queue=%s",
                    message.guild.id if message.guild else "dm",
                    message.channel.id,
                    message.author.id,
                    bot.message_queue.qsize(),
                )
            except asyncio.QueueFull:
                log.warning("TTS queue full; dropping message author=%s", message.author.id)

    await bot.process_commands(message)


@bot.command()
async def stop(ctx: commands.Context) -> None:
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if vc and vc.is_connected():
        await vc.disconnect(force=True)
        await ctx.reply("TTS disconnected.", mention_author=False)


def main() -> None:
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set")
    if not WHITELIST_USERS:
        raise RuntimeError("WHITELIST_USERS is empty")
    if not load_opus():
        raise RuntimeError("Opus is required for Discord voice playback")

    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
