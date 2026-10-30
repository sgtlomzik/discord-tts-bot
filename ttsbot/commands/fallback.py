"""/fallback set and status: the fallback chain and provider health."""

import asyncio
import logging

import discord
from discord import app_commands

from dataclasses import replace

from ttsbot.commands import common
from ttsbot.commands.common import (
    _parse_chain,
    _provider_tag,
)

log = logging.getLogger("tts_bot")


def register(bot, tts_group, fallback_group, shared: dict) -> dict:
    validate_voice_profile = shared["validate_voice_profile"]
    voice_profile_autocomplete = shared["voice_profile_autocomplete"]

    def _chain_line(names: list[str]) -> str:
        reg = bot.voice_registry
        parts = []
        for name in names:
            rec = reg.get(name)
            parts.append(f"`{name}` [{_provider_tag(rec)}]" if rec is not None else f"`{name}` (нет такого)")
        return " → ".join(parts) if parts else "без запасных голосов"

    def _describe_fallback(record=None) -> str:
        reg = bot.voice_registry
        if reg.fallback_chain is None:
            common = f"{_chain_line(reg.fallback_names())} (по умолчанию: fallback_profile)"
        else:
            common = _chain_line(reg.fallback_chain)
        lines = [f"**Общая цепочка:** {common}"]
        own = [r for r in (reg.get(n) for n in reg.names()) if r is not None and r.fallback is not None]
        if own:
            lines.append("**Свои цепочки:**")
            lines += [f"• `{r.name}` → {_chain_line(list(r.fallback))}" for r in own]
        if record is not None:
            chain = reg.chain_for(record)
            lines.append(f"**Для `{record.name}` сейчас:** {_chain_line([v.name for v in chain])}")
            if chain and not chain[-1].is_piper:
                lines.append("⚠️ В конце нет локального Piper: если облако недоступно, сообщение не прозвучит.")
        return "\n".join(lines)[:1900]


    @fallback_group.command(name="set", description="Цепочка запасных голосов, если основной недоступен")
    @app_commands.describe(
        chain="Голоса через запятую по порядку; none — без запасных; default — по умолчанию; пусто — показать",
        voice="Голос со своей цепочкой; пусто — общая цепочка для всех голосов",
    )
    @app_commands.autocomplete(voice=voice_profile_autocomplete)
    async def slash_tts_fallback(
        interaction: discord.Interaction, chain: str | None = None, voice: str | None = None,
    ) -> None:
        if not await common.require_bot_owner(bot, interaction):
            return
        reg = bot.voice_registry
        record = None
        if voice:
            name = validate_voice_profile(voice)
            if name is None:
                await interaction.response.send_message(f"Голос `{voice}` не найден.", ephemeral=True)
                return
            record = reg.get(name)
        if chain is None:
            await interaction.response.send_message(_describe_fallback(record), ephemeral=True)
            return
        names = _parse_chain(chain)
        unknown = [n for n in names or [] if n not in reg]
        if unknown:
            await interaction.response.send_message(
                "Нет таких голосов: " + ", ".join(f"`{n}`" for n in unknown) + ". Список: `/voicebot voices`.",
                ephemeral=True,
            )
            return
        if record is not None:
            if names is not None:
                names = [n for n in names if n != record.name]
            record = replace(record, fallback=tuple(names) if names is not None else None)
            reg.add(record)
        else:
            reg.fallback_chain = names
        try:
            bot.persist_voice_registry()
        except OSError as exc:
            log.exception("Failed to save the voice catalog after changing the fallback chain")
            await interaction.response.send_message(f"Не удалось сохранить: {exc}", ephemeral=True)
            return
        log.info("Fallback chain changed voice=%s chain=%s", record.name if record else "*", names)
        await interaction.response.send_message("Сохранено.\n" + _describe_fallback(record), ephemeral=True)


    _PROVIDER_CHOICES = [
        app_commands.Choice(name=label, value=name)
        for name, label in (("elevenlabs", "ElevenLabs"), ("fish", "Fish Audio"),
                            ("gemini", "Gemini"), ("minimax", "MiniMax"))
    ]

    @fallback_group.command(name="status", description="Состояние TTS-провайдеров: паузы, квоты, цепочка")
    @app_commands.describe(reset="Снять паузу с провайдера (после пополнения баланса или смены ключа)")
    @app_commands.choices(reset=_PROVIDER_CHOICES)
    async def slash_tts_providers(
        interaction: discord.Interaction, reset: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await common.require_guild_manager(interaction):
            return
        if reset is not None and not await common.require_bot_owner(bot, interaction):
            return  # a reset affects every guild
        await interaction.response.defer(ephemeral=True, thinking=True)
        disp = bot.tts_dispatcher
        notes: list[str] = []
        if reset is not None:
            breaker = disp.breaker_for(reset.value)
            if breaker is not None and disp.provider_for(reset.value) is not None:
                breaker.reset()
                log.info("Provider %s health reset by %s", reset.value, interaction.user.id)
                notes.append(f"Пауза {breaker.name} снята.")
            else:
                notes.append(f"{reset.name} не настроен.")
        lines: list[str] = []
        for name in disp.provider_names():
            breaker = disp.breaker_for(name)
            provider = disp.provider_for(name)
            line = f"**{breaker.name}** — {breaker.describe()}"
            if breaker.state.value != "closed" and breaker.last_error:
                line += f"\n  └ {breaker.last_error[:160]}"
            checker = getattr(provider, "check_quota", None)
            if callable(checker):
                try:
                    report = await asyncio.wait_for(checker(), 20)
                except Exception as exc:
                    report = None
                    line += f"\n  └ квота: не удалось проверить ({type(exc).__name__})"
                if report is not None:
                    line += f"\n  └ квота: {report.detail or ('есть' if report.available else 'нет')}"
                    if report.available and breaker.kind is not None and breaker.kind.value == "quota":
                        breaker.record_success(confirmed=False)
                        line += "\n  └ квота снова есть — пауза снята"
            lines.append(line)
        lines.append("**Piper** — локальный, работает без сети")
        embed = discord.Embed(title="🔀 TTS • Провайдеры", color=0xFEE75C,
                              description="\n".join(notes + lines)[:4000])
        embed.add_field(name="Fallback", value=_describe_fallback()[:1024], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    return dict(locals())
