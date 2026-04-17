import asyncio
import ctypes.util
import logging
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse

import aiohttp
import discord
from discord.ext import commands


LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format=LOG_FORMAT)
log = logging.getLogger("tts_bot")

TMP_DIR = Path(os.getenv("TTS_TMP_DIR", "/dev/shm"))
DEFAULT_WHITELIST = "441612025286885397"

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
MAX_TEXT_LENGTH = int(os.getenv("TTS_MAX_TEXT_LENGTH", "500"))
QUEUE_MAXSIZE = int(os.getenv("TTS_QUEUE_MAXSIZE", "50"))
START_PAD_MS = int(os.getenv("TTS_START_PAD_MS", "120"))
IDLE_DISCONNECT_SECONDS = int(os.getenv("TTS_IDLE_DISCONNECT_SECONDS", "900"))

RAW_RHVOICE_URL = os.getenv("RHVOICE_URL", "http://172.20.0.1:5002").strip()
RHVOICE_VOICE = os.getenv("RHVOICE_VOICE", "anna").strip()
RHVOICE_RATE = int(os.getenv("RHVOICE_RATE", "55"))
RHVOICE_PITCH = int(os.getenv("RHVOICE_PITCH", "50"))
RHVOICE_VOLUME = int(os.getenv("RHVOICE_VOLUME", "70"))
RHVOICE_TIMEOUT_SECONDS = int(os.getenv("RHVOICE_TIMEOUT_SECONDS", "15"))

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
intents.guilds = True


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
    text = text.replace("\n", ". ")
    text = " ".join(text.split())
    return text.strip()


def normalize_rhvoice_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    if not parsed.scheme:
        parsed = urlparse(f"http://{raw_url}")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"Invalid RHVOICE_URL: {raw_url!r}")

    port = parsed.port if parsed.port is not None else 5002
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"

    normalized_path = parsed.path.rstrip("/")
    netloc = f"{hostname}:{port}"

    return urlunparse((parsed.scheme or "http", netloc, normalized_path, "", "", "")).rstrip("/")


RHVOICE_URL = normalize_rhvoice_url(RAW_RHVOICE_URL)


def build_rhvoice_url(text: str) -> str:
    params = {
        "text": text,
        "voice": RHVOICE_VOICE,
        "format": "wav",
        "rate": str(RHVOICE_RATE),
        "pitch": str(RHVOICE_PITCH),
        "volume": str(RHVOICE_VOLUME),
    }
    return f"{RHVOICE_URL}/say?{urlencode(params)}"


class TTSBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=("!tts ", "!tts"), intents=intents)
        self.message_queue: asyncio.Queue[tuple[str, discord.VoiceChannel, float]] = asyncio.Queue(
            maxsize=QUEUE_MAXSIZE
        )
        self.worker_task: asyncio.Task[None] | None = None
        self.idle_disconnect_tasks: dict[int, asyncio.Task[None]] = {}
        self.http_session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        timeout = aiohttp.ClientTimeout(total=RHVOICE_TIMEOUT_SECONDS)
        self.http_session = aiohttp.ClientSession(timeout=timeout)
        self.worker_task = asyncio.create_task(self.tts_worker(), name="tts-worker")

    async def close(self) -> None:
        if self.worker_task:
            self.worker_task.cancel()

        for task in self.idle_disconnect_tasks.values():
            task.cancel()

        if self.http_session:
            await self.http_session.close()

        await super().close()

    def cancel_idle_disconnect(self, guild_id: int) -> None:
        task = self.idle_disconnect_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()
            log.info("Cancelled idle disconnect guild=%s", guild_id)

    def schedule_idle_disconnect(self, guild: discord.Guild) -> None:
        self.cancel_idle_disconnect(guild.id)
        task = asyncio.create_task(
            self._idle_disconnect_after_timeout(guild),
            name=f"idle-disconnect-{guild.id}",
        )
        self.idle_disconnect_tasks[guild.id] = task
        log.info("Scheduled idle disconnect guild=%s timeout=%ss", guild.id, IDLE_DISCONNECT_SECONDS)

    async def _idle_disconnect_after_timeout(self, guild: discord.Guild) -> None:
        try:
            await asyncio.sleep(IDLE_DISCONNECT_SECONDS)

            vc = discord.utils.get(self.voice_clients, guild=guild)
            if not vc or not vc.is_connected():
                return

            if vc.is_playing() or vc.is_paused():
                log.info("Skip idle disconnect guild=%s reason=playback_active", guild.id)
                return

            channel = vc.channel
            if isinstance(channel, discord.VoiceChannel):
                whitelisted_present = any(
                    (not member.bot) and member.id in WHITELIST_USERS
                    for member in channel.members
                )
                if whitelisted_present:
                    log.info("Skip idle disconnect guild=%s reason=whitelisted_member_present", guild.id)
                    return

            await vc.disconnect(force=True)
            log.info("Idle disconnect executed guild=%s", guild.id)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Idle disconnect task failed guild=%s", guild.id)

    async def ensure_voice(self, voice_channel: discord.VoiceChannel) -> discord.VoiceClient:
        started = time.perf_counter()
        guild_id = voice_channel.guild.id
        self.cancel_idle_disconnect(guild_id)

        vc = discord.utils.get(self.voice_clients, guild=voice_channel.guild)

        if not vc or not vc.is_connected():
            log.info(
                "Connecting to voice channel guild=%s channel=%s",
                guild_id,
                voice_channel.id,
            )
            vc = await voice_channel.connect(timeout=60.0, self_deaf=True)
            log.info(
                "Voice connect done guild=%s channel=%s took=%.3fs",
                guild_id,
                voice_channel.id,
                time.perf_counter() - started,
            )
        elif vc.channel != voice_channel:
            log.info(
                "Moving voice client guild=%s from=%s to=%s",
                guild_id,
                getattr(vc.channel, "id", "unknown"),
                voice_channel.id,
            )
            await vc.move_to(voice_channel)
            log.info(
                "Voice move done guild=%s channel=%s took=%.3fs",
                guild_id,
                voice_channel.id,
                time.perf_counter() - started,
            )
        else:
            log.info(
                "Voice ready guild=%s channel=%s took=%.3fs",
                guild_id,
                voice_channel.id,
                time.perf_counter() - started,
            )

        return vc

    async def wait_for_rhvoice(self) -> None:
        if not self.http_session:
            raise RuntimeError("HTTP session is not initialized")

        started = time.perf_counter()
        deadline = started + 30.0

        while time.perf_counter() < deadline:
            try:
                async with self.http_session.get(f"{RHVOICE_URL}/info") as response:
                    if response.status == 200:
                        log.info("RHVoice is ready took=%.3fs", time.perf_counter() - started)
                        return
            except Exception:
                pass
            await asyncio.sleep(1.0)

        raise RuntimeError("RHVoice did not become ready in time")

    async def warmup_tts(self) -> None:
        filename = TMP_DIR / f"warmup_{uuid.uuid4().hex}.wav"
        try:
            await self.generate_tts_file("Привет", filename)
            log.info("RHVoice warmup completed")
        except Exception:
            log.exception("RHVoice warmup failed")
        finally:
            if filename.exists():
                try:
                    filename.unlink()
                except OSError:
                    log.exception("Failed to remove warmup file: %s", filename)

    async def generate_tts_file(self, text: str, filename: Path) -> None:
        if not self.http_session:
            raise RuntimeError("HTTP session is not initialized")

        url = build_rhvoice_url(text)
        started = time.perf_counter()

        log.info(
            "Generating RHVoice TTS chars=%s voice=%s rate=%s pitch=%s volume=%s",
            len(text),
            RHVOICE_VOICE,
            RHVOICE_RATE,
            RHVOICE_PITCH,
            RHVOICE_VOLUME,
        )

        async with self.http_session.get(url) as response:
            response.raise_for_status()
            content = await response.read()

        filename.write_bytes(content)

        log.info(
            "RHVoice generated file=%s size=%s took=%.3fs",
            filename,
            filename.stat().st_size if filename.exists() else "unknown",
            time.perf_counter() - started,
        )

    async def tts_worker(self) -> None:
        await self.wait_until_ready()
        await self.wait_for_rhvoice()
        await self.warmup_tts()
        log.info("TTS worker started")

        while not self.is_closed():
            text, voice_channel, queued_at = await self.message_queue.get()
            filename = TMP_DIR / f"tts_{uuid.uuid4().hex}.wav"

            try:
                worker_started = time.perf_counter()

                connect_task = asyncio.create_task(self.ensure_voice(voice_channel))
                tts_task = asyncio.create_task(self.generate_tts_file(text, filename))

                try:
                    vc, _ = await asyncio.gather(connect_task, tts_task)
                except Exception:
                    connect_error = connect_task.exception() if connect_task.done() else None
                    tts_error = tts_task.exception() if tts_task.done() else None
                    if connect_error:
                        log.exception("Voice prepare failed", exc_info=connect_error)
                        await self.disconnect_guild_voice(voice_channel.guild)
                    elif tts_error:
                        log.exception("TTS generation failed; keeping voice session", exc_info=tts_error)
                    else:
                        log.exception("TTS processing failed before playback")
                    continue

                log.info(
                    "Ready to play guild=%s channel=%s queue_wait=%.3fs prep_total=%.3fs",
                    voice_channel.guild.id,
                    voice_channel.id,
                    worker_started - queued_at,
                    time.perf_counter() - worker_started,
                )

                try:
                    await self.play_file(vc, filename)
                except Exception:
                    log.exception("Playback failed; disconnecting voice")
                    await self.disconnect_guild_voice(voice_channel.guild)
                    continue

                log.info(
                    "Playback finished guild=%s channel=%s total_since_queue=%.3fs",
                    voice_channel.guild.id,
                    voice_channel.id,
                    time.perf_counter() - queued_at,
                )

                self.schedule_idle_disconnect(voice_channel.guild)

            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("TTS processing failed")
                await self.disconnect_guild_voice(voice_channel.guild)
            finally:
                if filename.exists():
                    try:
                        filename.unlink()
                    except OSError:
                        log.exception("Failed to remove temp file: %s", filename)
                self.message_queue.task_done()

    async def play_file(self, vc: discord.VoiceClient, filename: Path) -> None:
        if vc.is_playing() or vc.is_paused():
            log.warning("Voice client was already playing; stopping previous source")
            vc.stop()

        finished = asyncio.Event()
        loop = asyncio.get_running_loop()
        started = time.perf_counter()

        def after(error: Exception | None) -> None:
            if error:
                log.exception("Playback callback error", exc_info=error)
            loop.call_soon_threadsafe(finished.set)

        audio = discord.FFmpegPCMAudio(
            str(filename),
            before_options="-hide_banner -loglevel warning",
            options=f'-vn -af "adelay={START_PAD_MS}:all=1"',
        )

        log.info("Starting playback file=%s size=%s", filename, filename.stat().st_size)
        vc.play(audio, after=after)
        await finished.wait()

        log.info("Playback duration took=%.3fs", time.perf_counter() - started)

    async def disconnect_guild_voice(self, guild: discord.Guild) -> None:
        self.cancel_idle_disconnect(guild.id)
        vc = discord.utils.get(self.voice_clients, guild=guild)
        if not vc:
            return
        try:
            await vc.disconnect(force=True)
            log.info("Voice disconnected guild=%s", guild.id)
        except Exception:
            log.exception("Voice disconnect cleanup failed")

    async def auto_connect_for_member(self, member: discord.Member, channel: discord.VoiceChannel) -> None:
        if member.id not in WHITELIST_USERS:
            return

        try:
            await self.ensure_voice(channel)
            log.info(
                "Auto-connected for whitelisted user guild=%s member=%s channel=%s",
                channel.guild.id,
                member.id,
                channel.id,
            )
        except Exception:
            log.exception(
                "Auto-connect failed guild=%s member=%s channel=%s",
                channel.guild.id,
                member.id,
                channel.id,
            )


bot = TTSBot()


@bot.event
async def on_ready() -> None:
    log.info("TTS bot logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    log.info("Opus loaded: %s", discord.opus.is_loaded())
    log.info("Whitelist users: %s", ",".join(str(user_id) for user_id in sorted(WHITELIST_USERS)))
    log.info("RHVoice URL: %s", RHVOICE_URL)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    if (
        message.guild is not None
        and message.author.id in WHITELIST_USERS
        and message.author.voice
        and isinstance(message.author.voice.channel, discord.VoiceChannel)
    ):
        final_text = process_text(message.clean_content)
        if final_text:
            try:
                now = time.perf_counter()
                bot.message_queue.put_nowait((final_text[:MAX_TEXT_LENGTH], message.author.voice.channel, now))
                log.info(
                    "Queued TTS guild=%s text_channel=%s voice_channel=%s author=%s queue=%s",
                    message.guild.id,
                    message.channel.id,
                    message.author.voice.channel.id,
                    message.author.id,
                    bot.message_queue.qsize(),
                )
            except asyncio.QueueFull:
                log.warning("TTS queue full; dropping message author=%s", message.author.id)

    await bot.process_commands(message)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if member.bot:
        return
    if member.id not in WHITELIST_USERS:
        return

    if isinstance(after.channel, discord.VoiceChannel):
        await bot.auto_connect_for_member(member, after.channel)

    guild = member.guild
    vc = discord.utils.get(bot.voice_clients, guild=guild)
    if not vc or not vc.is_connected():
        return

    if isinstance(vc.channel, discord.VoiceChannel):
        whitelisted_present = any(
            (not user.bot) and user.id in WHITELIST_USERS
            for user in vc.channel.members
        )
        if not whitelisted_present and not vc.is_playing() and not vc.is_paused():
            bot.schedule_idle_disconnect(guild)


@bot.command()
async def stop(ctx: commands.Context) -> None:
    if ctx.guild is None:
        await ctx.reply("Command can only be used in a guild.", mention_author=False)
        return

    await bot.disconnect_guild_voice(ctx.guild)
    await ctx.reply("TTS disconnected.", mention_author=False)


@bot.command()
async def join(ctx: commands.Context) -> None:
    if ctx.guild is None:
        await ctx.reply("Command can only be used in a guild.", mention_author=False)
        return

    if not ctx.author.voice or not isinstance(ctx.author.voice.channel, discord.VoiceChannel):
        await ctx.reply("You must be in a voice channel.", mention_author=False)
        return

    await bot.ensure_voice(ctx.author.voice.channel)
    await ctx.reply("TTS connected.", mention_author=False)


def main() -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set")
    if not WHITELIST_USERS:
        raise RuntimeError("WHITELIST_USERS is empty")
    if not load_opus():
        raise RuntimeError("Opus is required for Discord voice playback")

    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
