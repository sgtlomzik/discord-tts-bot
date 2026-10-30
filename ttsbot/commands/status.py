"""/voicebot stats, status, test, limit and queue-clear."""

import logging
import time

import discord
from discord import app_commands

from ttsbot import config
from ttsbot.textnorm import (
    build_mention_say_map_from_guild,
    speakable_text,
)
from ttsbot.commands import common
from ttsbot.commands.common import (
    _chunk_lines,
    _fmt_int,
    _fmt_uptime,
    _provider_tag,
)

log = logging.getLogger("tts_bot")


def register(bot, tts_group, fallback_group, shared: dict) -> dict:
    _configured_model = shared["_configured_model"]

    def _build_stats_embed() -> discord.Embed:
        disp = bot.tts_dispatcher
        cache = getattr(disp, "cache", None)
        fish = disp.provider_for("fish")
        gemini = disp.provider_for("gemini")
        elevenlabs = disp.provider_for("elevenlabs")
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
        embed.add_field(name="Очередь", value=str(bot.queue_size()), inline=True)
        def _cb_state(breaker) -> str:
            if breaker is None:
                return "—"
            left = breaker.cooldown_remaining
            kind = f" {breaker.kind.value}" if getattr(breaker, "kind", None) else ""
            return f"`{breaker.state.value}`{kind}" + (f" ({left:.0f} с)" if left else "")

        breakers = [
            f"{disp.breaker_for(name).name} {_cb_state(disp.breaker_for(name))}"
            for name in disp.provider_names()
        ]
        embed.add_field(name="Circuit breaker", value=" · ".join(breakers) or "—", inline=True)
        if fish is not None:
            embed.add_field(
                name="Fish запросы / символы (сессия)",
                value=f"{_fmt_int(fish.session_requests)} / {_fmt_int(fish.session_chars)}",
                inline=True,
            )
        if gemini is not None:
            embed.add_field(
                name="Gemini запросы / символы (сессия)",
                value=f"{_fmt_int(gemini.session_requests)} / {_fmt_int(gemini.session_chars)}",
                inline=True,
            )
        if elevenlabs is not None:
            keys = elevenlabs.key_usage()
            embed.add_field(
                name=f"ElevenLabs ключи ({len(keys)})",
                value="\n".join(
                    f"{'▶' if active else '·'} #{i} `{masked}` — {_fmt_int(credits)} кр."
                    for i, (masked, credits, active) in enumerate(keys, 1)
                ) or "—",
                inline=True,
            )
            embed.add_field(
                name="ElevenLabs запросы / символы / кредиты (сессия)",
                value=(
                    f"{_fmt_int(elevenlabs.session_requests)} / {_fmt_int(elevenlabs.session_chars)}"
                    f" / {_fmt_int(elevenlabs.session_credits)}"
                ),
                inline=True,
            )
        minimax = disp.provider_for("minimax")
        if minimax is not None:
            chars = getattr(minimax, "session_chars", None)
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
        embed.add_field(name="Кэш", value="вкл" if cache_on else "выкл", inline=True)
        embed.add_field(name="Лимит символов", value=str(bot.config_store.max_chars()), inline=True)
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
        embed.add_field(
            name="Fallback", value=" → ".join(f"`{n}`" for n in reg.fallback_names()) or "нет", inline=True,
        )
        embed.add_field(
            name="Алиасов эмодзи", value=str(len(bot.config_store.emoji_aliases)), inline=True
        )
        lines: list[str] = []
        for name in reg.names():
            rec = reg.get(name)
            if rec is None:
                continue
            tag = _provider_tag(rec)
            extra = ""
            if rec.is_minimax and rec.minimax is not None:
                extra = f" · {rec.minimax.model}"
                if rec.minimax.emotion:
                    extra += f" · {rec.minimax.emotion}"
            elif rec.is_fish and rec.fish is not None:
                configured = _configured_model("fish", "s2.1-pro-free")
                extra = f" · {rec.fish.model or configured}"
                if rec.fish.emotion:
                    extra += f" · {rec.fish.emotion}"
            elif rec.is_gemini and rec.gemini is not None:
                extra = f" · {rec.gemini.voice}"
            elif rec.is_elevenlabs and rec.elevenlabs is not None:
                configured = _configured_model("elevenlabs", "eleven_v4_turbo")
                extra = f" · {rec.elevenlabs.model or configured}"
            lines.append(f"`{name}` [{tag}]{extra}")
        if lines:
            for index, block in enumerate(_chunk_lines(lines, 1024)):
                embed.add_field(name="Список" if index == 0 else "​", value=block, inline=False)
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
        if not await common.require_bot_owner(bot, interaction):
            return
        view = StatsView(interaction.guild, author_id=interaction.user.id)
        await interaction.response.send_message(
            embed=_build_stats_embed(), view=view, ephemeral=True
        )
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass


    def _describe_workers(states: dict, guild_id: int) -> str:
        """The global workers and this guild's own (others' ids stay
        private), short enough for one Discord message."""
        own = f":{guild_id}"
        lines = [
            f"`{name}` {'ok' if st.running else 'stopped'}"
            + (f" (restarts {st.restarts}: {(st.last_error or '')[:80]})" if st.restarts else "")
            for name, st in sorted(states.items())
            if ":" not in name or name.endswith(own)
        ]
        shown: list[str] = []
        for line in lines:
            if sum(len(item) + 2 for item in shown) + len(line) > 1500:
                shown.append(f"… +{len(lines) - len(shown)}")
                break
            shown.append(line)
        return ", ".join(shown) or "none"

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
                    f"Queue: `{bot.queue_size(interaction.guild.id)}`",
                    f"Default voice: `{guild_config.default_voice}`",
                    f"Allowed users: `{len(guild_config.allowed_users)}`",
                    "Workers: " + _describe_workers(bot.supervisor.status(), interaction.guild.id),
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
        if not allowed and not common.is_guild_manager(interaction.user):
            await interaction.response.send_message("Вы не добавлены в озвучку.", ephemeral=True)
            return
        target_channel = common.resolve_tts_command_voice_channel(bot, interaction, voice_channel)
        if target_channel is None:
            await interaction.response.send_message(
                "Выберите голосовой канал или зайдите в него сами.",
                ephemeral=True,
            )
            return
        mentions = build_mention_say_map_from_guild(text, interaction.guild)
        # enqueue_tts cleans the text and applies /voicebot limit; this
        # check only answers before the interaction times out.
        if not speakable_text(text, bot.config_store.emoji_say_map(), mentions):
            await interaction.response.send_message("Нет текста для озвучки.", ephemeral=True)
            return
        await interaction.response.send_message("Тестовая фраза добавлена в очередь.", ephemeral=True)
        await bot.enqueue_tts(text, target_channel, interaction.user.id, interaction.channel_id or 0,
                              mentions=mentions)


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
        if not await common.require_bot_owner(bot, interaction):
            return
        if chars is None:
            await interaction.response.send_message(
                f"Текущий лимит: `{bot.config_store.max_chars()}` символов на сообщение.",
                ephemeral=True,
            )
            return
        bot.config_store.set_tts_max_chars(int(chars))
        await interaction.response.send_message(
            f"Лимит длины текста: `{bot.config_store.max_chars()}` символов на сообщение. "
            "Более длинные сообщения будут обрезаться.",
            ephemeral=True,
        )


    @tts_group.command(name="queue-clear", description="Очистить очередь TTS")
    async def slash_tts_queue_clear(interaction: discord.Interaction) -> None:
        if not await common.require_guild_manager(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        cleared = bot.clear_queue_for_guild(guild.id)
        await interaction.response.send_message(f"Очередь очищена: {cleared}.", ephemeral=True)

    return dict(locals())
