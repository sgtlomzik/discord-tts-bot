import discord
from discord.ext import commands
import edge_tts
import asyncio
import os
import uuid
import re
import ctypes.util

# ===== ЗАГРУЗКА OPUS =====
if not discord.opus.is_loaded():
    opus_path = ctypes.util.find_library("opus")
    if opus_path:
        discord.opus.load_opus(opus_path)
        print(f"✅ Opus loaded: {opus_path}")
    else:
        print("❌ Opus not found")

# ===== НАСТРОЙКИ =====
TOKEN = 'PASTE_YOUR_NEW_TOKEN_HERE'
WHITELIST_USERS = [441612025286885397]
VOICE_NAME = 'ru-RU-DmitryNeural'

EMOJI_MAP = {
    "Blya2x": "Бля",
    "pepe_sad": "Грустно",
    "kekw": "Кек"
}

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!tts', intents=intents)
message_queue = asyncio.Queue()

# ===== ОБРАБОТКА ТЕКСТА =====
def process_text(text):
    text = re.sub(r'http[s]?://\S+', '', text)

    def replace_emoji(match):
        return EMOJI_MAP.get(match.group(1), "")

    text = re.sub(r'<a?:([a-zA-Z0-9_]+):[0-9]+>', replace_emoji, text)
    return " ".join(text.split())

# ===== TTS WORKER =====
async def tts_worker():
    await bot.wait_until_ready()

    while not bot.is_closed():
        text, voice_channel = await message_queue.get()

        try:
            vc = discord.utils.get(bot.voice_clients, guild=voice_channel.guild)

            # --- Подключение ---
            if not vc or not vc.is_connected():
                vc = await voice_channel.connect(timeout=60.0)

            elif vc.channel != voice_channel:
                await vc.move_to(voice_channel)

            # --- Генерация TTS ---
            filename = f"/dev/shm/tts_{uuid.uuid4().hex}.mp3"

            communicate = edge_tts.Communicate(
                text,
                VOICE_NAME,
                rate="+10%"
            )
            await communicate.save(filename)

            # --- Проигрывание ---
            audio = discord.FFmpegPCMAudio(
                filename,
                before_options="-loglevel panic",
                options="-vn"
            )

            vc.play(audio)

            while vc.is_playing():
                await asyncio.sleep(0.1)

            # --- Удаление файла ---
            if os.path.exists(filename):
                os.remove(filename)

        except Exception as e:
            print(f"❌ Ошибка TTS: {e}")

            # --- Жёсткий сброс соединения ---
            try:
                vc = discord.utils.get(bot.voice_clients, guild=voice_channel.guild)
                if vc:
                    await vc.disconnect(force=True)
            except Exception as cleanup_error:
                print(f"❌ Ошибка очистки: {cleanup_error}")

        finally:
            message_queue.task_done()

# ===== EVENTS =====
@bot.event
async def on_ready():
    print(f'🚀 TTS Бот {bot.user} запущен!')
    print("Opus loaded:", discord.opus.is_loaded())

    bot.loop.create_task(tts_worker())

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.author.id in WHITELIST_USERS:
        if message.author.voice and message.author.voice.channel:
            final_text = process_text(message.clean_content)

            if final_text:
                await message_queue.put(
                    (final_text[:500], message.author.voice.channel)
                )

    await bot.process_commands(message)

# ===== COMMANDS =====
@bot.command()
async def stop(ctx):
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if vc and vc.is_connected():
        await vc.disconnect()

# ===== RUN =====
bot.run(TOKEN)