"""/voicebot voice-* commands: choosing, adding, cloning and tuning voices, Fish latency."""

import logging
import re

import discord
from discord import app_commands

from ttsbot import voice_registry
from dataclasses import replace

from ttsbot.commands import common
from ttsbot.commands.common import (
    _ELEVENLABS_MODEL_CHOICES,
    _EMOTION_CHOICES,
    _FISH_LATENCY_CHOICES,
    _FISH_MODEL_CHOICES,
    _MODEL_CHOICES,
    _VOICE_ADD_PROVIDER_CHOICES,
    _chunk_lines,
    _provider_tag,
)

log = logging.getLogger("tts_bot")


def register(bot, tts_group, fallback_group, shared: dict) -> dict:
    validate_voice_profile = shared["validate_voice_profile"]
    voice_profile_autocomplete = shared["voice_profile_autocomplete"]
    voice_id_autocomplete = shared["voice_id_autocomplete"]
    _configured_model = shared["_configured_model"]

    @tts_group.command(name="voice-set", description="Сменить озвучку по умолчанию")
    @app_commands.describe(voice="Профиль голоса")
    @app_commands.autocomplete(voice=voice_profile_autocomplete)
    async def slash_tts_voice_set(interaction: discord.Interaction, voice: str) -> None:
        if not await common.require_guild_manager(interaction):
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
        if not await common.require_guild_manager(interaction):
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
        if not await common.require_guild_manager(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        bot.config_store.clear_user_voice(guild.id, user.id)
        await interaction.response.send_message(f"Персональная озвучка сброшена: {user.mention}", ephemeral=True)


    @tts_group.command(name="voice-say-set", description="Всегда озвучивать вместо сообщений пользователя фиксированную фразу")
    @app_commands.describe(user="Пользователь", phrase="Фраза, которая будет озвучиваться вместо его сообщений")
    async def slash_tts_voice_say_set(interaction: discord.Interaction, user: discord.Member, phrase: str) -> None:
        if not await common.require_guild_manager(interaction):
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
        if not await common.require_guild_manager(interaction):
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
            tag = _provider_tag(rec)
            fb = " (fallback)" if name == reg.fallback_profile else ""
            desc = f" — {rec.description}" if rec.description else ""
            lines.append(f"`{name}` [{tag}]{fb} - {rec.label}{desc}")
        blocks = _chunk_lines(lines, 1900) or ["Каталог пуст."]
        await interaction.response.send_message(blocks[0], ephemeral=True)
        for block in blocks[1:]:
            await interaction.followup.send(block, ephemeral=True)


    @tts_group.command(name="voice-add", description="Зарегистрировать голос MiniMax, Gemini или ElevenLabs")
    @app_commands.describe(
        name="Имя профиля (kebab-case: a-z, 0-9, дефис)",
        voice_id="MiniMax/ElevenLabs voice_id или голос Gemini (Kore, Puck…)",
        description="Описание (необязательно)",
        provider="Провайдер голоса (по умолчанию MiniMax)",
    )
    @app_commands.choices(provider=_VOICE_ADD_PROVIDER_CHOICES)
    @app_commands.autocomplete(voice_id=voice_id_autocomplete)
    async def slash_tts_voice_add(
        interaction: discord.Interaction,
        name: str,
        voice_id: str,
        description: str = "",
        provider: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await common.require_bot_owner(bot, interaction):
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
        if provider is not None and provider.value == "elevenlabs":
            if bot.tts_dispatcher.provider_for("elevenlabs") is None:
                await interaction.response.send_message(
                    "ElevenLabs не настроен (нет ELEVENLABS_API_KEY).", ephemeral=True,
                )
                return
            if not re.fullmatch(r"[A-Za-z0-9]{1,64}", voice_id):
                await interaction.response.send_message(
                    "Укажите voice_id ElevenLabs (например `JBFqnCBsd6RMkjVDRZzb`).", ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            titles = dict(await bot.tts_dispatcher.provider_for("elevenlabs").list_voices())
            title = titles.get(voice_id, "").split(" - ")[0] or voice_id
            error = await bot.register_elevenlabs_voice(
                name=name, voice_id=voice_id,
                label=f"{name} (ElevenLabs {title})", description=description.strip(),
            )
            if error:
                await interaction.followup.send(f"Голос не добавлен: {error}.", ephemeral=True)
                return
            await interaction.followup.send(
                f"Добавлен голос `{name}` (ElevenLabs `{voice_id}`). "
                f"Назначьте его через `/voicebot voice-user` или `/voicebot voice-set`.",
                ephemeral=True,
            )
            return
        if provider is not None and provider.value == "gemini":
            if bot.tts_dispatcher.provider_for("gemini") is None:
                await interaction.response.send_message(
                    "Gemini не настроен (нет OPENROUTER_API_KEY).", ephemeral=True,
                )
                return
            # Prebuilt names ("Kore") and Voice Library ids ("en-us-nika").
            if len(voice_id) > 64 or not voice_id.replace("-", "").isalnum():
                await interaction.response.send_message(
                    "Укажите голос Gemini: имя (`Kore`) или id из библиотеки (`en-us-nika`).",
                    ephemeral=True,
                )
                return
            # The probe is a real (billed) request; defer past the 3 s limit.
            await interaction.response.defer(ephemeral=True, thinking=True)
            error = await bot.register_gemini_voice(
                name=name, voice=voice_id,
                label=f"{name} (Gemini {voice_id})", description=description.strip(),
            )
            if error:
                await interaction.followup.send(f"Голос не добавлен: {error}.", ephemeral=True)
                return
            await interaction.followup.send(
                f"Добавлен голос `{name}` (Gemini `{voice_id}`). "
                f"Назначьте его через `/voicebot voice-user` или `/voicebot voice-set`.",
                ephemeral=True,
            )
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
            log.exception("Failed to save the voice catalog after voice-add")
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
        if not await common.require_bot_owner(bot, interaction):
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
        fish = bot.tts_dispatcher.provider_for("fish")
        if fish is None:
            await interaction.response.send_message(
                "Fish Audio не настроен (нет FISH_API_KEY).", ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        error = await bot.register_fish_voice(
            name=name, reference_id=reference_id,
            label=f"{name} (Fish)", description=description.strip(),
        )
        if error:
            await interaction.followup.send(f"Голос не добавлен: {error}.", ephemeral=True)
            return
        await interaction.followup.send(
            f"Добавлен голос `{name}` (Fish reference_id=`{reference_id}`). "
            f"Назначьте его через `/voicebot voice-user`.", ephemeral=True,
        )

    @tts_group.command(name="voice-clone", description="Клонировать голос в Fish Audio из аудиофайла")
    @app_commands.describe(
        name="Имя профиля (kebab-case: a-z, 0-9, дефис)",
        sample="Аудиосэмпл (mp3/m4a/wav/ogg/opus/flac, от 10 сек, до 20 МБ)",
        description="Описание (необязательно)",
    )
    async def slash_tts_voice_clone(
        interaction: discord.Interaction,
        name: str,
        sample: discord.Attachment,
        description: str = "",
    ) -> None:
        if not await common.require_bot_owner(bot, interaction):
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
        ctype = (sample.content_type or "").lower()
        # Trust Discord's audio/* content type; the extensions cover uploads
        # without one. .ogg is what Discord voice messages are saved as.
        if not (ctype.startswith("audio/") or fname.endswith((".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac"))):
            await interaction.response.send_message(
                "Нужен аудиофайл (mp3/m4a/wav/ogg/opus/flac).", ephemeral=True
            )
            return
        if bot.tts_dispatcher.provider_for("fish") is None:
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
        if not await common.require_bot_owner(bot, interaction):
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
            log.exception("Failed to save the voice catalog after voice-describe")
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
        if not await common.require_bot_owner(bot, interaction):
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
            await interaction.response.send_message(f"Не удалось сохранить: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Fish-голос `{name}` настроен: эмоция `{tuned.emotion or 'без метки'}`, "
            f"скорость `{tuned.speed}`, тон `{tuned.pitch}`, громкость `{tuned.volume_db} dB`, "
            f"модель `{tuned.model or _configured_model('fish', 's2.1-pro-free')}`, "
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
        if not await common.require_bot_owner(bot, interaction):
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
        name="voice-tune", description="Настроить голос MiniMax или ElevenLabs"
    )
    @app_commands.describe(
        name="Имя голоса",
        emotion="MiniMax: эмоция (auto = подбор по тексту сообщения)",
        speed="MiniMax: скорость речи 0.5–2.0 (норма 1.0)",
        pitch="MiniMax: высота тона -12..12 (норма 0)",
        vol="MiniMax: громкость 0.1–10 (норма 1.0)",
        model="Модель синтеза (MiniMax или ElevenLabs — по типу голоса)",
        stability="ElevenLabs: стабильность 0–1 (ниже — живее, выше — ровнее)",
        similarity="ElevenLabs: сходство с исходным голосом 0–1",
        reset="ElevenLabs: вернуть stability/similarity к настройкам самого голоса",
    )
    @app_commands.autocomplete(name=voice_profile_autocomplete)
    @app_commands.choices(emotion=_EMOTION_CHOICES, model=_MODEL_CHOICES + _ELEVENLABS_MODEL_CHOICES)
    async def slash_tts_voice_tune(
        interaction: discord.Interaction,
        name: str,
        emotion: app_commands.Choice[str] | None = None,
        speed: app_commands.Range[float, 0.5, 2.0] | None = None,
        pitch: app_commands.Range[int, -12, 12] | None = None,
        vol: app_commands.Range[float, 0.1, 10.0] | None = None,
        model: app_commands.Choice[str] | None = None,
        stability: app_commands.Range[float, 0.0, 1.0] | None = None,
        similarity: app_commands.Range[float, 0.0, 1.0] | None = None,
        reset: bool = False,
    ) -> None:
        if not await common.require_bot_owner(bot, interaction):
            return
        name = name.strip().lower()
        rec = bot.voice_registry.get(name)
        if rec is None:
            await interaction.response.send_message(
                f"Голос `{name}` не найден. Список: `/voicebot voices`.", ephemeral=True
            )
            return
        if rec.is_elevenlabs and rec.elevenlabs is not None:
            await _tune_elevenlabs(
                interaction, rec, name,
                minimax_only=(emotion, speed, pitch, vol),
                model=model, stability=stability, similarity=similarity, reset=reset,
            )
            return
        if not rec.is_minimax or rec.minimax is None:
            await interaction.response.send_message(
                "Настройка доступна только для MiniMax- и ElevenLabs-голосов "
                "(Fish: `/voicebot voice-fish-tune`).", ephemeral=True
            )
            return
        if stability is not None or similarity is not None or reset:
            await interaction.response.send_message(
                "stability / similarity / reset — только для ElevenLabs-голосов.", ephemeral=True
            )
            return
        if model is not None and not model.value.startswith("speech-"):
            await interaction.response.send_message(
                f"`{model.value}` — модель ElevenLabs, а `{name}` — MiniMax-голос.", ephemeral=True
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
            log.exception("Failed to save the voice catalog after voice-tune")
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


    async def _tune_elevenlabs(
        interaction, rec, name: str, *, minimax_only, model, stability, similarity, reset,
    ) -> None:
        if any(value is not None for value in minimax_only):
            await interaction.response.send_message(
                "У ElevenLabs v4 нет emotion / speed / pitch / vol: доступны "
                "stability, similarity, model и reset.", ephemeral=True,
            )
            return
        if model is not None and not model.value.startswith("eleven_"):
            await interaction.response.send_message(
                f"`{model.value}` — модель MiniMax, а `{name}` — ElevenLabs-голос.", ephemeral=True
            )
            return
        if not reset and stability is None and similarity is None and model is None:
            await interaction.response.send_message(
                "Укажите хотя бы один параметр: stability / similarity / model / reset.",
                ephemeral=True,
            )
            return
        old = rec.elevenlabs
        if reset:
            old = replace(old, stability=None, similarity_boost=None)
        tuned = replace(
            old,
            stability=old.stability if stability is None else float(stability),
            similarity_boost=old.similarity_boost if similarity is None else float(similarity),
            model=old.model if model is None else model.value,
        )
        bot.voice_registry.add(replace(rec, elevenlabs=tuned))
        try:
            bot.persist_voice_registry()
        except OSError as exc:
            log.exception("Failed to save the voice catalog after voice-tune")
            await interaction.response.send_message(f"Не удалось сохранить: {exc}", ephemeral=True)
            return

        def shown(value) -> str:
            return "как у голоса" if value is None else f"{value:g}"

        elevenlabs = bot.tts_dispatcher.provider_for("elevenlabs")
        configured = elevenlabs.config.model if elevenlabs is not None else "eleven_v4_turbo"
        await interaction.response.send_message(
            f"Голос `{name}` настроен: stability `{shown(tuned.stability)}`, "
            f"similarity `{shown(tuned.similarity_boost)}`, модель `{tuned.model or configured}`.",
            ephemeral=True,
        )

    return dict(locals())
