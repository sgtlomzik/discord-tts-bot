"""The /voicebot slash command surface and legacy prefix commands.

``build_commands(bot)`` creates a fresh ``app_commands.Group`` with every
command bound (via closure) to *bot* and returns a namespace dict that the
composition root re-exports. Building per bot instance mirrors the old
module-global layout: each exec of bot.py gets its own command objects
tied to its own TTSBot.

Helpers that tests patch (``is_guild_manager``,
``resolve_tts_command_voice_channel``) stay module-level so
``unittest.mock.patch`` can intercept the lookups the commands do at call
time.
"""

import logging
import re
import time
import uuid

import discord
from discord import app_commands
from discord.ext import commands

from ttsbot import voice_registry
from dataclasses import replace

from ttsbot import config
from ttsbot.textnorm import (
    build_mention_say_map_from_guild,
    normalize_for_tts,
    parse_custom_emoji_arg,
    sanitize_pronunciation,
)

log = logging.getLogger("tts_bot")


def is_guild_manager(member: discord.Member | discord.User) -> bool:
    permissions = getattr(member, "guild_permissions", None)
    return bool(
        permissions
        and (getattr(permissions, "manage_guild", False) or getattr(permissions, "administrator", False))
    )


async def require_guild_manager(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return False
    if not is_guild_manager(interaction.user):
        await interaction.response.send_message("Нужны права Manage Server или Administrator.", ephemeral=True)
        return False
    return True


def _derive_minimax_voice_id(name: str) -> str:
    """Compatibility helper retained for imports from bot.py."""
    base = re.sub(r"[^a-z0-9]", "", name.lower())
    if not base or not base[0].isalpha():
        base = "voice" + base
    base = base[:16]
    suffix = str(uuid.uuid4().int)[:4]
    vid = base + suffix
    if len(vid) < 8:
        vid = (vid + "00000000")[:8]
    return vid


_EMOTION_CHOICES = [
    app_commands.Choice(name="auto (по тексту)", value="auto"),
    app_commands.Choice(name="без эмоции", value="none"),
    app_commands.Choice(name="нейтрально", value="neutral"),
    app_commands.Choice(name="радость", value="happy"),
    app_commands.Choice(name="грусть", value="sad"),
    app_commands.Choice(name="злость", value="angry"),
    app_commands.Choice(name="страх", value="fearful"),
    app_commands.Choice(name="отвращение", value="disgusted"),
    app_commands.Choice(name="удивление", value="surprised"),
]

# Current MiniMax T2A model ids (platform.minimax.io/docs/api-reference,
# checked 2026-07-05). HD = higher quality/cloning fidelity, Turbo = lower
# latency; 2.8 is the newest generation.
_MODEL_CHOICES = [
    app_commands.Choice(name="2.8 HD (новейшая, макс. качество)", value="speech-2.8-hd"),
    app_commands.Choice(name="2.8 Turbo (новейшая, быстрее)", value="speech-2.8-turbo"),
    app_commands.Choice(name="2.6 HD", value="speech-2.6-hd"),
    app_commands.Choice(name="2.6 Turbo", value="speech-2.6-turbo"),
    app_commands.Choice(name="02 HD", value="speech-02-hd"),
    app_commands.Choice(name="02 Turbo", value="speech-02-turbo"),
]

_FISH_MODEL_CHOICES = [
    app_commands.Choice(name="S2.1 Pro Free", value="s2.1-pro-free"),
    app_commands.Choice(name="S2.1 Pro", value="s2.1-pro"),
    app_commands.Choice(name="S2 Pro", value="s2-pro"),
]

_FISH_LATENCY_CHOICES = [
    app_commands.Choice(name="low (минимальная задержка)", value="low"),
    app_commands.Choice(name="balanced (баланс скорости и качества)", value="balanced"),
    app_commands.Choice(name="normal (максимальное качество)", value="normal"),
]


def _render_emoji(guild: discord.Guild | None, emoji_id: str, name: str) -> str:
    """Render a stored alias's emoji: prefer the live guild emoji object (so
    animated emoji and exact image render correctly); fall back to a shortcode
    if the emoji was deleted."""
    if guild is not None:
        try:
            live = discord.utils.get(guild.emojis, id=int(emoji_id))
        except (TypeError, ValueError):
            live = None
        if live is not None:
            return str(live)
    return f"`:{name or emoji_id}:`"


def _fmt_uptime(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    parts.append(f"{minutes}м")
    return " ".join(parts)


def _fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")



def resolve_tts_command_voice_channel(
    bot,
    interaction: discord.Interaction,
    requested_channel: discord.VoiceChannel | None = None,
) -> discord.VoiceChannel | None:
    if requested_channel is not None:
        return requested_channel

    if interaction.guild is not None:
        vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        if vc and vc.is_connected() and isinstance(vc.channel, discord.VoiceChannel):
            return vc.channel

    user_voice = getattr(getattr(interaction.user, "voice", None), "channel", None)
    if isinstance(user_voice, discord.VoiceChannel):
        return user_voice

    return None



def build_commands(bot):
    """Create the /voicebot group + prefix commands bound to *bot*.

    Returns a dict of every public object (group, command objects, embed
    builders, StatsView) for bot.py to re-export.
    """
    def validate_voice_profile(voice: str) -> str | None:
        voice_name = voice.strip().lower()
        return voice_name if voice_name in bot.voice_registry else None


    async def voice_profile_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current = current.lower()
        choices: list[app_commands.Choice[str]] = []
        for name in bot.voice_registry.names():
            rec = bot.voice_registry.get(name)
            label = rec.label if rec is not None else name
            if current in name.lower() or current in label.lower():
                choices.append(app_commands.Choice(name=f"{name} - {label}", value=name))
        return choices[:25]


    tts_group = app_commands.Group(name="voicebot", description="Управление озвучкой сообщений")

    @tts_group.command(name="off", description="Отключить озвучку на сервере")
    async def slash_tts_off(interaction: discord.Interaction) -> None:
        if not await require_guild_manager(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        bot.config_store.set_enabled(guild.id, False)
        bot.clear_merge_buffers(guild.id)
        cleared = bot.clear_queue_for_guild(guild.id)
        await bot.disconnect_guild_voice(guild)
        await interaction.response.send_message(f"TTS отключен. Очередь очищена: {cleared}.", ephemeral=True)


    @tts_group.command(name="on", description="Включить озвучку на сервере")
    async def slash_tts_on(interaction: discord.Interaction) -> None:
        if not await require_guild_manager(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        bot.config_store.set_enabled(guild.id, True)
        await interaction.response.send_message("TTS включен.", ephemeral=True)


    @tts_group.command(name="allow", description="Добавить пользователя в озвучку")
    @app_commands.describe(user="Пользователь, чьи сообщения нужно озвучивать")
    async def slash_tts_allow(interaction: discord.Interaction, user: discord.Member) -> None:
        if not await require_guild_manager(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        bot.config_store.add_user(guild.id, user.id)
        await interaction.response.send_message(f"Добавлен в озвучку: {user.mention}", ephemeral=True)


    @tts_group.command(name="deny", description="Исключить пользователя из озвучки")
    @app_commands.describe(user="Пользователь, чьи сообщения больше не нужно озвучивать")
    async def slash_tts_deny(interaction: discord.Interaction, user: discord.Member) -> None:
        if not await require_guild_manager(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        bot.config_store.remove_user(guild.id, user.id)
        await interaction.response.send_message(f"Исключен из озвучки: {user.mention}", ephemeral=True)


    @tts_group.command(name="voice-set", description="Сменить озвучку по умолчанию")
    @app_commands.describe(voice="Профиль голоса")
    @app_commands.autocomplete(voice=voice_profile_autocomplete)
    async def slash_tts_voice_set(interaction: discord.Interaction, voice: str) -> None:
        if not await require_guild_manager(interaction):
            return
        voice_name = validate_voice_profile(voice)
        if not voice_name:
            await interaction.response.send_message(
                "Неизвестный профиль голоса. Используйте `/voicebot voices`.",
                ephemeral=True,
            )
            return
        guild = interaction.guild
        assert guild is not None
        bot.config_store.set_default_voice(guild.id, voice_name)
        await interaction.response.send_message(f"Озвучка по умолчанию: `{voice_name}`.", ephemeral=True)


    @tts_group.command(name="voice-user", description="Назначить отдельную озвучку пользователю")
    @app_commands.describe(user="Пользователь", voice="Профиль голоса")
    @app_commands.autocomplete(voice=voice_profile_autocomplete)
    async def slash_tts_voice_user(interaction: discord.Interaction, user: discord.Member, voice: str) -> None:
        if not await require_guild_manager(interaction):
            return
        voice_name = validate_voice_profile(voice)
        if not voice_name:
            await interaction.response.send_message(
                "Неизвестный профиль голоса. Используйте `/voicebot voices`.",
                ephemeral=True,
            )
            return
        guild = interaction.guild
        assert guild is not None
        bot.config_store.set_user_voice(guild.id, user.id, voice_name)
        await interaction.response.send_message(f"Для {user.mention} назначено: `{voice_name}`.", ephemeral=True)


    @tts_group.command(name="voice-clear", description="Сбросить персональную озвучку пользователя")
    @app_commands.describe(user="Пользователь")
    async def slash_tts_voice_clear(interaction: discord.Interaction, user: discord.Member) -> None:
        if not await require_guild_manager(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        bot.config_store.clear_user_voice(guild.id, user.id)
        await interaction.response.send_message(f"Персональная озвучка сброшена: {user.mention}", ephemeral=True)


    @tts_group.command(name="voice-say-set", description="Всегда озвучивать вместо сообщений пользователя фиксированную фразу")
    @app_commands.describe(user="Пользователь", phrase="Фраза, которая будет озвучиваться вместо его сообщений")
    async def slash_tts_voice_say_set(interaction: discord.Interaction, user: discord.Member, phrase: str) -> None:
        if not await require_guild_manager(interaction):
            return
        phrase = phrase.strip()
        if not phrase:
            await interaction.response.send_message("Фраза не может быть пустой.", ephemeral=True)
            return
        guild = interaction.guild
        assert guild is not None
        bot.config_store.set_user_fixed_phrase(guild.id, user.id, phrase)
        await interaction.response.send_message(
            f"Для {user.mention} вместо сообщений теперь всегда озвучивается: «{phrase}».",
            ephemeral=True,
        )


    @tts_group.command(name="voice-say-clear", description="Убрать фиксированную фразу пользователя")
    @app_commands.describe(user="Пользователь")
    async def slash_tts_voice_say_clear(interaction: discord.Interaction, user: discord.Member) -> None:
        if not await require_guild_manager(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        bot.config_store.clear_user_fixed_phrase(guild.id, user.id)
        await interaction.response.send_message(f"Фиксированная фраза сброшена: {user.mention}", ephemeral=True)


    @tts_group.command(name="voices", description="Показать доступные озвучки")
    async def slash_tts_voices(interaction: discord.Interaction) -> None:
        reg = bot.voice_registry
        lines: list[str] = []
        for name in reg.names():
            rec = reg.get(name)
            if rec is None:
                continue
            tag = "Fish Audio" if rec.is_fish else "MiniMax" if rec.is_minimax else "Piper"
            fb = " (fallback)" if name == reg.fallback_profile else ""
            desc = f" — {rec.description}" if rec.description else ""
            lines.append(f"`{name}` [{tag}]{fb} - {rec.label}{desc}")
        await interaction.response.send_message(
            "\n".join(lines) or "Каталог пуст.", ephemeral=True
        )


    @tts_group.command(name="voice-add", description="Зарегистрировать MiniMax-голос (системный или клон)")
    @app_commands.describe(
        name="Имя профиля (kebab-case: a-z, 0-9, дефис)",
        voice_id="MiniMax voice_id (системный или клон)",
        description="Описание (необязательно)",
    )
    async def slash_tts_voice_add(
        interaction: discord.Interaction,
        name: str,
        voice_id: str,
        description: str = "",
    ) -> None:
        if not await require_guild_manager(interaction):
            return
        name = name.strip().lower()
        voice_id = voice_id.strip()
        if not voice_registry.valid_voice_name(name):
            await interaction.response.send_message(
                "Имя должно быть в kebab-case: строчные буквы, цифры и дефис, "
                "начинаться с буквы или цифры (например `mm-qingse`).",
                ephemeral=True,
            )
            return
        if name in bot.voice_registry:
            await interaction.response.send_message(
                f"Голос `{name}` уже существует. Выберите другое имя.", ephemeral=True
            )
            return
        if not voice_id:
            await interaction.response.send_message("Укажите voice_id.", ephemeral=True)
            return
        # Validation hits the network; defer so the interaction does not expire.
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, err = await bot.validate_minimax_voice(voice_id)
        if not ok:
            await interaction.followup.send(f"Не добавлено: {err}", ephemeral=True)
            return
        record = voice_registry.VoiceRecord(
            name=name,
            label=f"{name} (MiniMax)",
            description=description.strip(),
            provider=voice_registry.PROVIDER_MINIMAX,
            minimax=voice_registry.MiniMaxParams(voice_id=voice_id),
        )
        bot.voice_registry.add(record)
        try:
            bot.persist_voice_registry()
        except OSError as exc:
            log.exception("Failed to persist voices.json after voice-add")
            await interaction.followup.send(
                f"Голос проверен, но не сохранён на диск: {exc}", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"Добавлен голос `{name}` (voice_id=`{voice_id}`). "
            f"Назначьте его через `/voicebot voice-user` или `/voicebot voice-set`.",
            ephemeral=True,
        )


    @tts_group.command(name="voice-fish-add", description="Добавить готовый голос Fish по reference_id")
    @app_commands.describe(
        name="Имя профиля (строчные буквы, цифры и дефис)",
        reference_id="ID голоса из библиотеки Fish Audio",
        description="Описание (необязательно)",
    )
    async def slash_tts_voice_fish_add(
        interaction: discord.Interaction,
        name: str,
        reference_id: str,
        description: str = "",
    ) -> None:
        if not await require_guild_manager(interaction):
            return
        name = name.strip().lower()
        reference_id = reference_id.strip()
        if not voice_registry.valid_voice_name(name):
            await interaction.response.send_message(
                "Имя должно состоять из строчных букв, цифр и дефисов.", ephemeral=True,
            )
            return
        if name in bot.voice_registry:
            await interaction.response.send_message(
                f"Голос `{name}` уже существует. Выберите другое имя.", ephemeral=True,
            )
            return
        if not reference_id or len(reference_id) > 128 or any(ch.isspace() for ch in reference_id):
            await interaction.response.send_message(
                "Укажите Fish reference_id из библиотеки.", ephemeral=True,
            )
            return
        fish = bot.tts_dispatcher.fish
        if fish is None:
            await interaction.response.send_message(
                "Fish Audio не настроен (нет FISH_API_KEY).", ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        probe = config.TMP_DIR / f"fish_import_{uuid.uuid4().hex}.opus"
        try:
            await fish.synthesize("Проверка голоса.", probe, reference_id=reference_id)
            with probe.open("rb") as audio:
                if audio.read(4) != b"OggS":
                    raise ValueError("Fish не вернул аудио Ogg/Opus")
        except Exception as exc:
            await interaction.followup.send(
                f"Голос недоступен для озвучки: {type(exc).__name__}: {exc}", ephemeral=True,
            )
            return
        finally:
            probe.unlink(missing_ok=True)
        record = voice_registry.VoiceRecord(
            name=name,
            label=f"{name} (Fish)",
            description=description.strip(),
            provider=voice_registry.PROVIDER_FISH,
            fish=voice_registry.FishParams(reference_id=reference_id),
        )
        if name in bot.voice_registry:
            await interaction.followup.send(f"Голос `{name}` уже существует.", ephemeral=True)
            return
        bot.voice_registry.add(record)
        try:
            bot.persist_voice_registry()
        except OSError as exc:
            bot.voice_registry.voices.pop(name, None)
            await interaction.followup.send(f"Не удалось сохранить голос: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Добавлен голос `{name}` (Fish reference_id=`{reference_id}`). "
            f"Назначьте его через `/voicebot voice-user`.", ephemeral=True,
        )

    @tts_group.command(name="voice-clone", description="Клонировать голос в Fish Audio из аудиофайла")
    @app_commands.describe(
        name="Имя профиля (kebab-case: a-z, 0-9, дефис)",
        sample="Аудиосэмпл (mp3/m4a/wav/opus, от 10 сек, до 20 МБ)",
        description="Описание (необязательно)",
    )
    async def slash_tts_voice_clone(
        interaction: discord.Interaction,
        name: str,
        sample: discord.Attachment,
        description: str = "",
    ) -> None:
        if not await require_guild_manager(interaction):
            return
        name = name.strip().lower()
        if not voice_registry.valid_voice_name(name):
            await interaction.response.send_message(
                "Имя должно быть в kebab-case: строчные буквы, цифры и дефис, "
                "начинаться с буквы или цифры (например `serega-pirat`).",
                ephemeral=True,
            )
            return
        if name in bot.voice_registry:
            await interaction.response.send_message(
                f"Голос `{name}` уже существует. Выберите другое имя.", ephemeral=True
            )
            return
        if sample.size > 20 * 1024 * 1024:
            await interaction.response.send_message(
                f"Файл {sample.size / 1024 / 1024:.1f} МБ — лимит загрузки бота 20 МБ.",
                ephemeral=True,
            )
            return
        fname = sample.filename.lower()
        if not fname.endswith((".mp3", ".m4a", ".wav", ".opus")):
            await interaction.response.send_message(
                "Нужен аудиофайл (mp3/m4a/wav/opus).", ephemeral=True
            )
            return
        if bot.tts_dispatcher.fish is None:
            await interaction.response.send_message(
                "Fish Audio не настроен (нет FISH_API_KEY) — клонирование недоступно.",
                ephemeral=True,
            )
            return
        # Download + model creation + probe all hit the network; defer so the
        # interaction token does not expire (3s limit) before we finish.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await sample.read()
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"Не удалось скачать файл: {exc}", ephemeral=True
            )
            return
        ok, result = await bot.clone_fish_voice(
            name=name,
            sample=data,
            filename=sample.filename,
            description=description.strip(),
        )
        if not ok:
            await interaction.followup.send(f"Не удалось: {result}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Готово! Голос `{name}` создан в Fish (reference_id=`{result}`).\n"
            f"Назначьте его: `/voicebot voice-user @user {name}` "
            f"или `/voicebot voice-set {name}`.",
            ephemeral=True,
        )


    @tts_group.command(name="voice-describe", description="Изменить описание голоса")
    @app_commands.describe(name="Имя профиля", text="Новое описание")
    @app_commands.autocomplete(name=voice_profile_autocomplete)
    async def slash_tts_voice_describe(
        interaction: discord.Interaction, name: str, text: str
    ) -> None:
        if not await require_guild_manager(interaction):
            return
        name = name.strip().lower()
        rec = bot.voice_registry.get(name)
        if rec is None:
            await interaction.response.send_message(
                f"Голос `{name}` не найден. Список: `/voicebot voices`.", ephemeral=True
            )
            return
        bot.voice_registry.add(replace(rec, description=text.strip()))
        try:
            bot.persist_voice_registry()
        except OSError as exc:
            log.exception("Failed to persist voices.json after voice-describe")
            await interaction.response.send_message(
                f"Не удалось сохранить: {exc}", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"Описание `{name}` обновлено.", ephemeral=True
        )


    @tts_group.command(name="voice-fish-tune", description="Настроить голос Fish Audio")
    @app_commands.describe(
        name="Имя Fish-профиля",
        emotion="Эмоция (auto = подбор по сообщению, none = без метки)",
        speed="Скорость речи 0.5–2.0",
        pitch="Высота тона -12..12 полутонов (обработка ffmpeg)",
        volume_db="Громкость в децибелах -20..20",
        model="Модель Fish Audio",
        temperature="Выразительность 0–1",
        top_p="Разнообразие 0–1",
    )
    @app_commands.autocomplete(name=voice_profile_autocomplete)
    @app_commands.choices(emotion=_EMOTION_CHOICES, model=_FISH_MODEL_CHOICES)
    async def slash_tts_voice_fish_tune(
        interaction: discord.Interaction,
        name: str,
        emotion: app_commands.Choice[str] | None = None,
        speed: app_commands.Range[float, 0.5, 2.0] | None = None,
        pitch: app_commands.Range[int, -12, 12] | None = None,
        volume_db: app_commands.Range[float, -20.0, 20.0] | None = None,
        model: app_commands.Choice[str] | None = None,
        temperature: app_commands.Range[float, 0.0, 1.0] | None = None,
        top_p: app_commands.Range[float, 0.0, 1.0] | None = None,
    ) -> None:
        if not await require_guild_manager(interaction):
            return
        name = name.strip().lower()
        rec = bot.voice_registry.get(name)
        if rec is None or not rec.is_fish or rec.fish is None:
            await interaction.response.send_message(
                f"Fish-голос `{name}` не найден. Список: `/voicebot voices`.", ephemeral=True,
            )
            return
        if all(value is None for value in (emotion, speed, pitch, volume_db, model, temperature, top_p)):
            await interaction.response.send_message(
                "Укажите хотя бы один параметр настройки.", ephemeral=True,
            )
            return
        old = rec.fish
        tuned = replace(
            old,
            emotion=old.emotion if emotion is None else ("" if emotion.value == "none" else emotion.value),
            speed=old.speed if speed is None else float(speed),
            pitch=old.pitch if pitch is None else int(pitch),
            volume_db=old.volume_db if volume_db is None else float(volume_db),
            model=old.model if model is None else model.value,
            temperature=old.temperature if temperature is None else float(temperature),
            top_p=old.top_p if top_p is None else float(top_p),
        )
        bot.voice_registry.add(replace(rec, fish=tuned))
        try:
            bot.persist_voice_registry()
        except OSError as exc:
            bot.voice_registry.add(rec)
            await interaction.response.send_message(f"Не удалось сохранить: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Fish-голос `{name}` настроен: эмоция `{tuned.emotion or 'без метки'}`, "
            f"скорость `{tuned.speed}`, тон `{tuned.pitch}`, громкость `{tuned.volume_db} dB`, "
            f"модель `{tuned.model or (bot.tts_dispatcher.fish.config.model if bot.tts_dispatcher.fish else 's2.1-pro-free')}`, "
            f"temperature `{tuned.temperature}`, top_p `{tuned.top_p}`.",
            ephemeral=True,
        )

    @tts_group.command(name="fish-latency", description="Сменить режим задержки Fish для всего бота")
    @app_commands.describe(mode="low / balanced / normal; пусто — показать текущий режим")
    @app_commands.choices(mode=_FISH_LATENCY_CHOICES)
    async def slash_tts_fish_latency(
        interaction: discord.Interaction,
        mode: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await require_guild_manager(interaction):
            return
        if mode is not None:
            try:
                bot.set_fish_latency(mode.value)
            except (OSError, ValueError) as exc:
                await interaction.response.send_message(
                    f"Не удалось сохранить режим Fish: {exc}", ephemeral=True,
                )
                return
        current = bot.fish_latency
        await interaction.response.send_message(
            f"Fish latency: `{current}` для всех Fish-голосов. "
            "Следующие генерации используют этот режим.", ephemeral=True,
        )

    @tts_group.command(
        name="voice-tune", description="Настроить выразительность голоса (MiniMax)"
    )
    @app_commands.describe(
        name="Имя голоса",
        emotion="Эмоция (auto = подбор по тексту сообщения)",
        speed="Скорость речи 0.5–2.0 (норма 1.0)",
        pitch="Высота тона -12..12 (норма 0)",
        vol="Громкость 0.1–10 (норма 1.0)",
        model="Модель синтеза MiniMax (HD = качество, Turbo = скорость)",
    )
    @app_commands.autocomplete(name=voice_profile_autocomplete)
    @app_commands.choices(emotion=_EMOTION_CHOICES, model=_MODEL_CHOICES)
    async def slash_tts_voice_tune(
        interaction: discord.Interaction,
        name: str,
        emotion: app_commands.Choice[str] | None = None,
        speed: app_commands.Range[float, 0.5, 2.0] | None = None,
        pitch: app_commands.Range[int, -12, 12] | None = None,
        vol: app_commands.Range[float, 0.1, 10.0] | None = None,
        model: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await require_guild_manager(interaction):
            return
        name = name.strip().lower()
        rec = bot.voice_registry.get(name)
        if rec is None:
            await interaction.response.send_message(
                f"Голос `{name}` не найден. Список: `/voicebot voices`.", ephemeral=True
            )
            return
        if not rec.is_minimax or rec.minimax is None:
            await interaction.response.send_message(
                "Выразительность доступна только для MiniMax-голосов.", ephemeral=True
            )
            return
        if emotion is None and speed is None and pitch is None and vol is None and model is None:
            await interaction.response.send_message(
                "Укажите хотя бы один параметр: emotion / speed / pitch / vol / model.",
                ephemeral=True,
            )
            return
        mm = rec.minimax
        new_emotion = mm.emotion
        if emotion is not None:
            new_emotion = "" if emotion.value == "none" else emotion.value
        new_speed = mm.speed if speed is None else max(0.5, min(2.0, float(speed)))
        new_pitch = mm.pitch if pitch is None else max(-12, min(12, int(pitch)))
        new_vol = mm.vol if vol is None else max(0.1, min(10.0, float(vol)))
        new_model = mm.model if model is None else model.value
        bot.voice_registry.add(
            replace(
                rec,
                minimax=replace(
                    mm,
                    emotion=new_emotion,
                    speed=new_speed,
                    pitch=new_pitch,
                    vol=new_vol,
                    model=new_model,
                ),
            )
        )
        try:
            bot.persist_voice_registry()
        except OSError as exc:
            log.exception("Failed to persist voices.json after voice-tune")
            await interaction.response.send_message(
                f"Не удалось сохранить: {exc}", ephemeral=True
            )
            return
        emotion_label = new_emotion or "—"
        await interaction.response.send_message(
            f"Голос `{name}` настроен: эмоция `{emotion_label}`, "
            f"скорость `{new_speed}`, тон `{new_pitch}`, громкость `{new_vol}`, "
            f"модель `{new_model}`.",
            ephemeral=True,
        )


    async def emoji_alias_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current = current.lower()
        choices: list[app_commands.Choice[str]] = []
        for emoji_id, entry in bot.config_store.emoji_aliases.items():
            name = entry.get("name") or emoji_id
            say = entry.get("say", "")
            if current in name.lower() or current in say.lower() or current in emoji_id:
                label = f":{name}: → {say}"[:100]
                choices.append(app_commands.Choice(name=label, value=emoji_id))
        return choices[:25]


    @tts_group.command(
        name="emoji-alias", description="Задать, как бот произносит кастомный эмодзи"
    )
    @app_commands.describe(
        emoji="Кастомный эмодзи сервера (или :имя:)",
        pronunciation="Чем его озвучивать",
    )
    async def slash_emoji_alias(
        interaction: discord.Interaction, emoji: str, pronunciation: str
    ) -> None:
        if not await require_guild_manager(interaction):
            return
        parsed = parse_custom_emoji_arg(emoji, interaction.guild)
        if parsed is None:
            await interaction.response.send_message(
                "Нужен кастомный эмодзи этого сервера — пришлите сам эмодзи, "
                "токен `<:Имя:123…>` или `:Имя:`. Обычные Unicode-эмодзи и текст "
                "не поддерживаются.",
                ephemeral=True,
            )
            return
        emoji_id, name = parsed
        say = sanitize_pronunciation(pronunciation)
        if not say:
            await interaction.response.send_message(
                "Произношение пустое или состоит только из недопустимых символов.",
                ephemeral=True,
            )
            return
        bot.config_store.set_emoji_alias(emoji_id, name, say)
        rendered = _render_emoji(interaction.guild, emoji_id, name)
        await interaction.response.send_message(
            f"Готово: {rendered} будет озвучиваться как «{say}».", ephemeral=True
        )


    @tts_group.command(name="emoji-alias-remove", description="Удалить алиас эмодзи")
    @app_commands.describe(emoji="Эмодзи или его текущий алиас")
    @app_commands.autocomplete(emoji=emoji_alias_autocomplete)
    async def slash_emoji_alias_remove(
        interaction: discord.Interaction, emoji: str
    ) -> None:
        if not await require_guild_manager(interaction):
            return
        parsed = parse_custom_emoji_arg(emoji, interaction.guild)
        if parsed is not None:
            emoji_id = parsed[0]
        elif emoji.strip().isdigit():  # raw id, e.g. picked from autocomplete
            emoji_id = emoji.strip()
        else:
            await interaction.response.send_message(
                "Не распознан эмодзи. Выберите из списка автодополнения "
                "или пришлите сам эмодзи.",
                ephemeral=True,
            )
            return
        if bot.config_store.remove_emoji_alias(emoji_id):
            await interaction.response.send_message(
                f"Алиас удалён (id `{emoji_id}`).", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"Алиаса для id `{emoji_id}` нет.", ephemeral=True
            )


    @tts_group.command(name="emoji-aliases", description="Показать заданные алиасы эмодзи")
    async def slash_emoji_aliases(interaction: discord.Interaction) -> None:
        if not await require_guild_manager(interaction):
            return
        aliases = bot.config_store.emoji_aliases
        if not aliases:
            await interaction.response.send_message(
                "Алиасы эмодзи не заданы. Добавьте: `/voicebot emoji-alias`.",
                ephemeral=True,
            )
            return
        lines: list[str] = []
        for emoji_id, entry in aliases.items():
            rendered = _render_emoji(interaction.guild, emoji_id, entry.get("name", ""))
            lines.append(f"{rendered} → «{entry.get('say', '')}»")
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)


    def _build_stats_embed() -> discord.Embed:
        disp = bot.tts_dispatcher
        cache = getattr(disp, "cache", None)
        cb = getattr(disp, "circuit_breaker", None)
        cloud = getattr(disp, "cloud", None)
        fish = getattr(disp, "fish", None)
        embed = discord.Embed(title="📊 TTS • Статистика", color=0x5865F2)
        if cache is not None:
            hits, misses = cache.hits, cache.misses
            total = hits + misses
            rate = f"{100.0 * hits / total:.0f}%" if total else "—"
            mb = cache.total_bytes / (1024 * 1024)
            embed.add_field(
                name="Кэш",
                value=f"{rate} попаданий ({hits}/{total}) · {cache.size} фраз · {mb:.1f} МБ",
                inline=False,
            )
        else:
            embed.add_field(name="Кэш", value="выключен", inline=False)
        embed.add_field(name="Очередь", value=str(bot.message_queue.qsize()), inline=True)
        active_cb = getattr(disp, "fish_circuit_breaker", None) if fish is not None else cb
        state = active_cb.state.value if active_cb is not None else "—"
        embed.add_field(name="Circuit breaker", value=f"`{state}`", inline=True)
        if fish is not None:
            embed.add_field(
                name="Fish запросы / символы (сессия)",
                value=f"{_fmt_int(fish.session_requests)} / {_fmt_int(fish.session_chars)}",
                inline=True,
            )
        chars = getattr(cloud, "session_chars", None)
        embed.add_field(
            name="MiniMax символы (сессия)",
            value=_fmt_int(chars) if chars is not None else "—",
            inline=True,
        )
        embed.add_field(
            name="Аптайм", value=_fmt_uptime(time.time() - bot.started_at), inline=True
        )
        return embed


    def _build_settings_embed() -> discord.Embed:
        cache_on = getattr(bot.tts_dispatcher, "cache", None) is not None
        embed = discord.Embed(title="⚙️ TTS • Настройки", color=0x57F287)
        embed.add_field(name="Склейка сообщений", value=f"`{config.TTS_MERGE_ALGORITHM}`", inline=True)
        embed.add_field(name="Кэш", value="вкл" if cache_on else "выкл", inline=True)
        embed.add_field(name="Стриминг", value="вкл" if config.TTS_STREAMING_ENABLED else "выкл", inline=True)
        embed.add_field(name="Префетч", value="вкл" if config.TTS_PREFETCH_ENABLED else "выкл", inline=True)
        embed.add_field(
            name="Непрерывный поток",
            value="вкл" if config.TTS_CONTINUOUS_STREAM else "выкл",
            inline=True,
        )
        embed.add_field(name="Лимит символов", value=str(config.TTS_MAX_CHARS), inline=True)
        embed.add_field(name="Fish latency", value=f"`{bot.fish_latency}`", inline=True)
        if config.TTS_AUDIO_LIMIT_ENABLED:
            audio_limit = (
                f"{config.TTS_AUDIO_CHARS_PER_SECOND:g} симв/с × "
                f"{config.TTS_AUDIO_LIMIT_SAFETY:g}, мин {config.TTS_AUDIO_LIMIT_MIN_SECONDS:g}с"
            )
        else:
            audio_limit = "выкл"
        embed.add_field(name="Лимит аудио", value=audio_limit, inline=True)
        embed.add_field(name="Авто-отключение", value=f"{config.IDLE_DISCONNECT_SECONDS}с", inline=True)
        return embed


    def _build_voices_embed(guild: discord.Guild | None) -> discord.Embed:
        reg = bot.voice_registry
        embed = discord.Embed(title="🎙️ TTS • Голоса", color=0xEB459E)
        if guild is not None:
            cfg = bot.config_store.get_guild(guild.id)
            embed.add_field(name="По умолчанию", value=f"`{cfg.default_voice}`", inline=True)
            embed.add_field(name="Персональных", value=str(len(cfg.user_voices)), inline=True)
            embed.add_field(name="В озвучке", value=str(len(cfg.allowed_users)), inline=True)
        embed.add_field(name="Всего голосов", value=str(len(reg.names())), inline=True)
        embed.add_field(name="Fallback", value=f"`{reg.fallback_profile}`", inline=True)
        embed.add_field(
            name="Алиасов эмодзи", value=str(len(bot.config_store.emoji_aliases)), inline=True
        )
        lines: list[str] = []
        for name in reg.names():
            rec = reg.get(name)
            if rec is None:
                continue
            tag = "Fish Audio" if rec.is_fish else "MiniMax" if rec.is_minimax else "Piper"
            extra = ""
            if rec.is_minimax and rec.minimax is not None:
                extra = f" · {rec.minimax.model}"
                if rec.minimax.emotion:
                    extra += f" · {rec.minimax.emotion}"
            elif rec.is_fish and rec.fish is not None:
                configured = bot.tts_dispatcher.fish.config.model if bot.tts_dispatcher.fish else "s2.1-pro-free"
                extra = f" · {rec.fish.model or configured}"
                if rec.fish.emotion:
                    extra += f" · {rec.fish.emotion}"
            lines.append(f"`{name}` [{tag}]{extra}")
        if lines:
            embed.add_field(name="Список", value="\n".join(lines)[:1000], inline=False)
        return embed


    class StatsView(discord.ui.View):
        """Tabbed stats menu: buttons swap the embed in place (ephemeral)."""

        def __init__(self, guild: discord.Guild | None, *, author_id: int) -> None:
            super().__init__(timeout=120)
            self.guild = guild
            self.author_id = author_id
            self.message: discord.Message | None = None

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id != self.author_id:
                await interaction.response.send_message("Это не ваше меню.", ephemeral=True)
                return False
            return True

        @discord.ui.button(label="Статистика", emoji="📊", style=discord.ButtonStyle.primary)
        async def stats_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
            await interaction.response.edit_message(embed=_build_stats_embed(), view=self)

        @discord.ui.button(label="Настройки", emoji="⚙️", style=discord.ButtonStyle.secondary)
        async def settings_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
            await interaction.response.edit_message(embed=_build_settings_embed(), view=self)

        @discord.ui.button(label="Голоса", emoji="🎙️", style=discord.ButtonStyle.secondary)
        async def voices_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
            await interaction.response.edit_message(embed=_build_voices_embed(self.guild), view=self)

        async def on_timeout(self) -> None:
            for child in self.children:
                child.disabled = True
            if self.message is not None:
                try:
                    await self.message.edit(view=self)
                except discord.HTTPException:
                    pass


    @tts_group.command(name="stats", description="Статистика и настройки TTS")
    async def slash_tts_stats(interaction: discord.Interaction) -> None:
        if not await require_guild_manager(interaction):
            return
        view = StatsView(interaction.guild, author_id=interaction.user.id)
        await interaction.response.send_message(
            embed=_build_stats_embed(), view=view, ephemeral=True
        )
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass


    @tts_group.command(name="status", description="Показать состояние TTS на сервере")
    async def slash_tts_status(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        guild_config = bot.config_store.get_guild(interaction.guild.id)
        vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        channel_name = getattr(getattr(vc, "channel", None), "name", "не подключен") if vc else "не подключен"
        await interaction.response.send_message(
            "\n".join(
                [
                    f"Enabled: `{guild_config.enabled}`",
                    f"Voice channel: `{channel_name}`",
                    f"Queue: `{bot.message_queue.qsize()}`",
                    f"Default voice: `{guild_config.default_voice}`",
                    f"Allowed users: `{len(guild_config.allowed_users)}`",
                    f"Merge: `{config.TTS_MERGE_SHORT_MESSAGES}`",
                ]
            ),
            ephemeral=True,
        )


    @tts_group.command(name="test", description="Проиграть тестовую фразу")
    @app_commands.describe(
        text="Текст для проверки",
        voice_channel="Голосовой канал для удаленного запуска",
    )
    async def slash_tts_test(
        interaction: discord.Interaction,
        text: str,
        voice_channel: discord.VoiceChannel | None = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        allowed = bot.config_store.is_allowed(interaction.guild.id, interaction.user.id)
        if not allowed and not is_guild_manager(interaction.user):
            await interaction.response.send_message("Вы не добавлены в озвучку.", ephemeral=True)
            return
        target_channel = resolve_tts_command_voice_channel(bot, interaction, voice_channel)
        if target_channel is None:
            await interaction.response.send_message(
                "Выберите голосовой канал или зайдите в него сами.",
                ephemeral=True,
            )
            return
        final_text = normalize_for_tts(
            text,
            emoji_aliases=bot.config_store.emoji_say_map(),
            mentions=build_mention_say_map_from_guild(text, interaction.guild),
        ) or ""
        if not final_text:
            await interaction.response.send_message("Нет текста для озвучки.", ephemeral=True)
            return
        await interaction.response.send_message("Тестовая фраза добавлена в очередь.", ephemeral=True)
        await bot.enqueue_tts(final_text, target_channel, interaction.user.id, interaction.channel_id or 0)


    @tts_group.command(
        name="limit", description="Максимальная длина озвучиваемого текста (символов)"
    )
    @app_commands.describe(
        chars="Новый лимит символов на сообщение (50–2000); пусто — показать текущий"
    )
    async def slash_tts_limit(
        interaction: discord.Interaction,
        chars: app_commands.Range[int, 50, 2000] | None = None,
    ) -> None:
        if not await require_guild_manager(interaction):
            return
        if chars is None:
            await interaction.response.send_message(
                f"Текущий лимит: `{config.TTS_MAX_CHARS}` символов на сообщение.",
                ephemeral=True,
            )
            return
        bot.config_store.set_tts_max_chars(int(chars))
        await interaction.response.send_message(
            f"Лимит длины текста: `{config.TTS_MAX_CHARS}` символов на сообщение. "
            "Более длинные сообщения будут обрезаться.",
            ephemeral=True,
        )


    @tts_group.command(name="queue-clear", description="Очистить очередь TTS")
    async def slash_tts_queue_clear(interaction: discord.Interaction) -> None:
        if not await require_guild_manager(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        bot.clear_merge_buffers(guild.id)
        cleared = bot.clear_queue_for_guild(guild.id)
        await interaction.response.send_message(f"Очередь очищена: {cleared}.", ephemeral=True)


    @bot.command()
    async def stop(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.reply("Command can only be used in a guild.", mention_author=False)
            return
        if not isinstance(ctx.author, discord.Member) or not is_guild_manager(ctx.author):
            await ctx.reply("Only server managers can stop TTS.", mention_author=False)
            return

        await bot.disconnect_guild_voice(ctx.guild)
        await ctx.reply("TTS disconnected.", mention_author=False)


    @bot.command()
    async def join(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.reply("Command can only be used in a guild.", mention_author=False)
            return
        if not isinstance(ctx.author, discord.Member):
            await ctx.reply("Command can only be used by guild members.", mention_author=False)
            return
        if not bot.config_store.is_allowed(ctx.guild.id, ctx.author.id) and not is_guild_manager(ctx.author):
            await ctx.reply("You are not allowed to use TTS.", mention_author=False)
            return

        if not ctx.author.voice or not isinstance(ctx.author.voice.channel, discord.VoiceChannel):
            await ctx.reply("You must be in a voice channel.", mention_author=False)
            return

        await bot.ensure_voice(ctx.author.voice.channel)
        await ctx.reply("TTS connected.", mention_author=False)


    ns = dict(locals())
    ns.pop("bot", None)
    return ns
