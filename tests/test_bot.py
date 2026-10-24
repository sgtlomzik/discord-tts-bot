import asyncio
import contextlib
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


from support import load_bot_module  # noqa: E402,F401  (re-exported for other tests)


def temp_config_store(bot_mod, fallback_users=None):
    tmp_dir = tempfile.TemporaryDirectory()
    store = bot_mod.BotConfigStore(Path(tmp_dir.name) / "config.json", set(fallback_users or []))
    store._tmp_dir = tmp_dir
    return store


class TTSBotTests(unittest.TestCase):
    def test_parse_user_ids_ignores_invalid_entries(self):
        bot = load_bot_module()

        self.assertEqual(bot.parse_user_ids("1, bad; 2,,3"), {1, 2, 3})

    def test_analyze_message_animated_custom_emoji_not_spoken_with_raw_id(self):
        bot = load_bot_module()
        self.assertNotIn("1035577866370416721", bot.speakable_text("<a:pepe_sad:1035577866370416721>"))

    def test_analyze_message_mention_only_not_spoken_with_raw_id(self):
        bot = load_bot_module()
        self.assertNotIn("123456789", bot.speakable_text("<@123456789>"))

    def test_analyze_message_url_only_dropped_by_default(self):
        bot = load_bot_module()
        self.assertEqual(bot.speakable_text("https://example.com/aaaa"), "")

    def test_runtime_has_ruslan_and_irina_piper_voices(self):
        bot = load_bot_module()

        self.assertEqual(bot.config.DEFAULT_VOICE_PROFILE, "piper-ruslan")
        self.assertEqual(
            list(bot.VOICE_PROFILES),
            ["piper-ruslan", "piper-irina"],
        )
        for voice_name in ("ruslan", "irina"):
            profile = bot.VOICE_PROFILES[f"piper-{voice_name}"]
            self.assertEqual(
                profile.piper_model_path,
                f"/app/models/ru_RU-{voice_name}-medium.onnx",
            )
            self.assertEqual(
                profile.piper_config_path,
                f"/app/models/ru_RU-{voice_name}-medium.onnx.json",
            )
        self.assertFalse(hasattr(bot, "RHVOICE_URL"))
        self.assertFalse(hasattr(bot, "ESPEAK_CMD"))

    def test_config_store_persists_guild_settings(self):
        bot = load_bot_module()

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.json"
            store = bot.BotConfigStore(path, {111})
            config = store.get_guild(10)

            self.assertTrue(config.enabled)
            self.assertEqual(config.allowed_users, set())  # env users are not copied in
            self.assertTrue(store.is_allowed(10, 111))

            store.add_user(10, 222)
            store.set_enabled(10, False)
            store.set_default_voice(10, "piper-ruslan")
            store.set_user_voice(10, 222, "piper-ruslan")

            loaded = bot.BotConfigStore(path, set())
            loaded_config = loaded.get_guild(10)

            self.assertFalse(loaded_config.enabled)
            self.assertEqual(loaded_config.allowed_users, {222})
            self.assertEqual(loaded_config.default_voice, "piper-ruslan")
            self.assertEqual(loaded.voice_for_user(10, 222), "piper-ruslan")

    def test_validate_voice_profile_rejects_unknown_voice(self):
        bot = load_bot_module()

        self.assertEqual(bot.validate_voice_profile("PIPER-RUSLAN"), "piper-ruslan")
        self.assertIsNone(bot.validate_voice_profile("missing"))

    def test_resolve_tts_command_voice_channel_prefers_requested_channel(self):
        bot = load_bot_module()

        class FakeVoiceChannel:
            pass

        requested = FakeVoiceChannel()
        with patch.object(bot.discord, "VoiceChannel", FakeVoiceChannel):
            self.assertIs(bot.resolve_tts_command_voice_channel(None, None, requested), requested)

    def test_resolve_tts_command_voice_channel_uses_connected_bot_channel_before_user_channel(self):
        bot = load_bot_module()

        class FakeVoiceChannel:
            pass

        guild = types.SimpleNamespace(id=10)
        bot_channel = FakeVoiceChannel()
        user_channel = FakeVoiceChannel()
        voice_client = types.SimpleNamespace(
            channel=bot_channel,
            is_connected=MagicMock(return_value=True),
        )
        interaction = types.SimpleNamespace(
            guild=guild,
            user=types.SimpleNamespace(voice=types.SimpleNamespace(channel=user_channel)),
        )

        with (
            patch.object(bot.discord, "VoiceChannel", FakeVoiceChannel),
            patch.object(bot.discord.utils, "get", MagicMock(return_value=voice_client)),
        ):
            self.assertIs(bot.resolve_tts_command_voice_channel(bot.bot, interaction), bot_channel)

    def test_resolve_tts_command_voice_channel_falls_back_to_user_channel(self):
        bot = load_bot_module()

        class FakeVoiceChannel:
            pass

        user_channel = FakeVoiceChannel()
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=10),
            user=types.SimpleNamespace(voice=types.SimpleNamespace(channel=user_channel)),
        )

        with (
            patch.object(bot.discord, "VoiceChannel", FakeVoiceChannel),
            patch.object(bot.discord.utils, "get", MagicMock(return_value=None)),
        ):
            self.assertIs(bot.resolve_tts_command_voice_channel(bot.bot, interaction), user_channel)

    def test_slash_group_uses_non_reserved_name(self):
        bot = load_bot_module()

        self.assertEqual(bot.tts_group.name, "voicebot")

    def test_slash_group_exposes_voice_management_commands(self):
        bot = load_bot_module()

        command_names = {command.name for command in bot.tts_group.commands}
        self.assertTrue(
            {"voice-set", "voice-user", "voice-clear", "voice-say-set", "voice-say-clear", "voices"}
            <= command_names
        )

    def test_config_store_removes_user_voice_when_user_denied(self):
        bot = load_bot_module()

        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot.BotConfigStore(Path(tmp_dir) / "config.json", set())
            store.add_user(10, 222)
            store.set_user_voice(10, 222, "piper-ruslan")
            store.remove_user(10, 222)

            config = store.get_guild(10)
            self.assertNotIn(222, config.allowed_users)
            self.assertNotIn(222, config.user_voices)

    def test_config_store_persists_user_fixed_phrase(self):
        bot = load_bot_module()

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.json"
            store = bot.BotConfigStore(path, {111})

            self.assertIsNone(store.fixed_phrase_for_user(10, 222))

            store.set_user_fixed_phrase(10, 222, "Бип-боп, это робот")

            loaded = bot.BotConfigStore(path, set())
            self.assertEqual(loaded.fixed_phrase_for_user(10, 222), "Бип-боп, это робот")

            loaded.clear_user_fixed_phrase(10, 222)
            self.assertIsNone(loaded.fixed_phrase_for_user(10, 222))

    def test_config_store_removes_user_fixed_phrase_when_user_denied(self):
        bot = load_bot_module()

        with tempfile.TemporaryDirectory() as tmp_dir:
            store = bot.BotConfigStore(Path(tmp_dir) / "config.json", set())
            store.add_user(10, 222)
            store.set_user_fixed_phrase(10, 222, "Заглушка")
            store.remove_user(10, 222)

            config = store.get_guild(10)
            self.assertNotIn(222, config.user_fixed_phrases)

    def test_config_store_ignores_blank_or_invalid_saved_fixed_phrases(self):
        bot = load_bot_module()

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.json"
            path.write_text(
                '{"guilds":{"10":{"user_fixed_phrases":{"222":"   ","333":"ok","abc":"bad"}}}}',
                encoding="utf-8",
            )

            store = bot.BotConfigStore(path, set())
            config = store.get_guild(10)

            self.assertEqual(config.user_fixed_phrases, {333: "ok"})

    def test_config_store_ignores_unknown_saved_voice_profiles(self):
        bot = load_bot_module()

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.json"
            path.write_text(
                '{"guilds":{"10":{"default_voice":"missing","user_voices":{"222":"missing"}}}}',
                encoding="utf-8",
            )

            store = bot.BotConfigStore(path, set())
            config = store.get_guild(10)

            self.assertEqual(config.default_voice, bot.config.DEFAULT_VOICE_PROFILE)
            self.assertEqual(config.user_voices, {})

    def test_tts_pcm_command_outputs_48khz_stereo_s16le(self):
        bot = load_bot_module()

        command = bot.build_tts_pcm_command(Path("/tmp/in.wav"))

        self.assertIn("-f", command)
        self.assertIn("s16le", command)
        self.assertIn("-ar", command)
        self.assertIn("48000", command)
        self.assertIn("-ac", command)
        self.assertIn("2", command)
        self.assertEqual(command[-1], "pipe:1")

    def test_split_pcm_frames_pads_last_frame_and_adds_tail(self):
        bot = load_bot_module()

        frames = bot.split_pcm_frames(b"\x01" * (bot.PCM_FRAME_BYTES + 10), tail_ms=40)

        self.assertEqual(len(frames), 4)
        self.assertEqual(len(frames[0]), bot.PCM_FRAME_BYTES)
        self.assertEqual(len(frames[1]), bot.PCM_FRAME_BYTES)
        self.assertEqual(frames[1][:10], b"\x01" * 10)
        self.assertEqual(frames[1][10:], b"\x00" * (bot.PCM_FRAME_BYTES - 10))

    def test_continuous_audio_source_reads_idle_and_queued_frames(self):
        bot = load_bot_module()
        idle = b"\x00" * bot.PCM_FRAME_BYTES
        speech = b"\x01" * bot.PCM_FRAME_BYTES
        source = bot.ContinuousTTSAudioSource(idle)

        self.assertEqual(source.read(), idle)
        source.enqueue_frames([speech])
        self.assertEqual(source.read(), speech)
        self.assertEqual(source.read(), idle)
        self.assertFalse(source.is_opus())

    def test_continuous_audio_source_stop_returns_empty_bytes(self):
        bot = load_bot_module()
        source = bot.ContinuousTTSAudioSource(b"\x00" * bot.PCM_FRAME_BYTES)

        source.stop()

        self.assertEqual(source.read(), b"")

    def test_build_idle_pcm_frame_can_generate_comfort_noise_or_silence(self):
        bot = load_bot_module()

        silence = bot.build_idle_pcm_frame("silence", -60)
        noise = bot.build_idle_pcm_frame("comfort_noise", -60)

        self.assertEqual(len(silence), bot.PCM_FRAME_BYTES)
        self.assertEqual(len(noise), bot.PCM_FRAME_BYTES)
        self.assertEqual(silence, b"\x00" * bot.PCM_FRAME_BYTES)
        self.assertNotEqual(noise, silence)


class TTSBotWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_voice_sets_cooldown_on_connect_error(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()

        guild = types.SimpleNamespace(id=77)
        voice_channel = types.SimpleNamespace(
            id=88,
            guild=guild,
            connect=AsyncMock(side_effect=RuntimeError("connect failed")),
        )

        with self.assertRaises(RuntimeError):
            await tts_bot.ensure_voice(voice_channel)

        self.assertGreater(tts_bot.voice_connect_cooldown_remaining(guild.id), 0.0)

    async def test_ensure_voice_recovers_from_stale_voice_client(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()

        # Zombie: registered on the guild but its socket is dead. connect()
        # would raise "Already connected" if we didn't clean it up first.
        stale_vc = types.SimpleNamespace(
            is_connected=MagicMock(return_value=False),
            disconnect=AsyncMock(),
        )
        guild = types.SimpleNamespace(id=77, voice_client=stale_vc)
        new_vc = types.SimpleNamespace(is_connected=MagicMock(return_value=True))
        voice_channel = types.SimpleNamespace(
            id=88,
            guild=guild,
            connect=AsyncMock(return_value=new_vc),
        )

        result = await tts_bot.ensure_voice(voice_channel)

        self.assertIs(result, new_vc)
        stale_vc.disconnect.assert_awaited_once_with(force=True)
        voice_channel.connect.assert_awaited_once()
        self.assertEqual(tts_bot.voice_connect_cooldown_remaining(guild.id), 0.0)

    async def test_auto_connect_skips_during_cooldown(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.config_store = temp_config_store(bot_mod, {441612025286885397})
        tts_bot.ensure_voice = AsyncMock()

        guild = types.SimpleNamespace(id=90)
        member = types.SimpleNamespace(id=441612025286885397)
        channel = types.SimpleNamespace(id=91, guild=guild)
        tts_bot.voice_connect_cooldown_until[guild.id] = bot_mod.time.monotonic() + 30.0

        await tts_bot.auto_connect_for_member(member, channel)

        tts_bot.ensure_voice.assert_not_awaited()

    async def test_auto_connect_skips_after_recent_idle_disconnect(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.config_store = temp_config_store(bot_mod, {441612025286885397})
        tts_bot.ensure_voice = AsyncMock()

        guild = types.SimpleNamespace(id=90)
        member = types.SimpleNamespace(id=441612025286885397)
        channel = types.SimpleNamespace(id=91, guild=guild)
        tts_bot.suppress_auto_connect_until[guild.id] = bot_mod.time.monotonic() + 30.0

        await tts_bot.auto_connect_for_member(member, channel)

        tts_bot.ensure_voice.assert_not_awaited()

    async def test_auto_connect_does_not_follow_member_out_of_busy_channel(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.config_store = temp_config_store(bot_mod, {441612025286885397, 449137238698098688})
        tts_bot.ensure_voice = AsyncMock()

        guild = types.SimpleNamespace(id=90)
        mover = types.SimpleNamespace(id=441612025286885397, bot=False)
        stayer = types.SimpleNamespace(id=449137238698098688, bot=False)
        current = types.SimpleNamespace(id=91, guild=guild, members=[stayer])
        target = types.SimpleNamespace(id=92, guild=guild, members=[mover])
        vc = types.SimpleNamespace(
            channel=current,
            is_connected=MagicMock(return_value=True),
            is_playing=MagicMock(return_value=False),
            is_paused=MagicMock(return_value=False),
        )

        with patch.object(bot_mod.discord.utils, "get", MagicMock(return_value=vc)):
            await tts_bot.auto_connect_for_member(mover, target)
            tts_bot.ensure_voice.assert_not_awaited()

            # Nobody whitelisted left behind: follow as before.
            current.members = []
            await tts_bot.auto_connect_for_member(mover, target)
        tts_bot.ensure_voice.assert_awaited_once_with(target)

    async def test_auto_connect_ignores_state_changes_in_current_channel(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.config_store = temp_config_store(bot_mod, {441612025286885397})
        tts_bot.ensure_voice = AsyncMock()

        guild = types.SimpleNamespace(id=90)
        member = types.SimpleNamespace(id=441612025286885397, bot=False)
        channel = types.SimpleNamespace(id=91, guild=guild, members=[member])
        vc = types.SimpleNamespace(channel=channel, is_connected=MagicMock(return_value=True))

        with patch.object(bot_mod.discord.utils, "get", MagicMock(return_value=vc)):
            await tts_bot.auto_connect_for_member(member, channel)
        tts_bot.ensure_voice.assert_not_awaited()

    async def test_auto_connect_skips_when_guild_disabled(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.config_store = temp_config_store(bot_mod)
        tts_bot.ensure_voice = AsyncMock()

        guild = types.SimpleNamespace(id=91)
        member = types.SimpleNamespace(id=441612025286885397)
        channel = types.SimpleNamespace(id=92, guild=guild)
        tts_bot.config_store.add_user(guild.id, member.id)
        tts_bot.config_store.set_enabled(guild.id, False)

        await tts_bot.auto_connect_for_member(member, channel)

        tts_bot.ensure_voice.assert_not_awaited()

    async def test_auto_connect_skips_when_disabled(self):
        bot_mod = load_bot_module()
        bot_mod.config.TTS_AUTO_CONNECT_ENABLED = False
        tts_bot = bot_mod.TTSBot()
        tts_bot.config_store = temp_config_store(bot_mod, {441612025286885397})
        tts_bot.ensure_voice = AsyncMock()

        guild = types.SimpleNamespace(id=91)
        member = types.SimpleNamespace(id=441612025286885397)
        channel = types.SimpleNamespace(id=92, guild=guild)

        await tts_bot.auto_connect_for_member(member, channel)

        tts_bot.ensure_voice.assert_not_awaited()

    async def test_voice_state_schedules_idle_disconnect_when_continuous_stream_is_idle(self):
        bot_mod = load_bot_module()

        class FakeVoiceChannel:
            def __init__(self, members=None):
                self.members = members or []

        guild = types.SimpleNamespace(id=123)
        member = types.SimpleNamespace(id=441612025286885397, bot=False, guild=guild)
        vc = types.SimpleNamespace(
            channel=FakeVoiceChannel(members=[]),
            is_connected=MagicMock(return_value=True),
            is_playing=MagicMock(return_value=True),
            is_paused=MagicMock(return_value=False),
        )
        bot_mod.bot.config_store = temp_config_store(bot_mod, {member.id})
        bot_mod.bot.continuous_sources[guild.id] = bot_mod.ContinuousTTSAudioSource(
            b"\x00" * bot_mod.PCM_FRAME_BYTES
        )
        bot_mod.bot.schedule_idle_disconnect = MagicMock()

        with (
            patch.object(bot_mod.discord, "VoiceChannel", FakeVoiceChannel),
            patch.object(bot_mod.discord.utils, "get", MagicMock(return_value=vc)),
        ):
            await bot_mod.on_voice_state_update(
                member,
                types.SimpleNamespace(channel=FakeVoiceChannel(members=[member])),
                types.SimpleNamespace(channel=None),
            )

        bot_mod.bot.schedule_idle_disconnect.assert_called_once_with(guild)

    async def test_ensure_voice_lock_is_reused_per_guild(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()

        first = tts_bot.get_voice_connect_lock(5)
        second = tts_bot.get_voice_connect_lock(5)

        self.assertIs(first, second)

    async def test_clear_queue_for_guild_keeps_other_guild_jobs(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()

        guild_one = types.SimpleNamespace(id=1)
        guild_two = types.SimpleNamespace(id=2)
        channel_one = types.SimpleNamespace(id=10, guild=guild_one)
        channel_two = types.SimpleNamespace(id=20, guild=guild_two)
        await tts_bot.session_for(1).queue.put(
            bot_mod.TTSJob("one", channel_one, 0.0, 100, 1, 1000, "piper-ruslan")
        )
        await tts_bot.session_for(2).queue.put(
            bot_mod.TTSJob("two", channel_two, 0.0, 200, 2, 2000, "piper-ruslan")
        )

        removed = tts_bot.clear_queue_for_guild(1)

        self.assertEqual(removed, 1)
        self.assertEqual(tts_bot.queue_size(1), 0)
        self.assertEqual((await tts_bot.session_for(2).queue.get()).guild_id, 2)

    async def test_enqueue_tts_uses_piper_ruslan_profile(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.config_store = temp_config_store(bot_mod)

        guild = types.SimpleNamespace(id=100)
        voice_channel = types.SimpleNamespace(id=200, guild=guild)
        tts_bot.config_store.set_user_voice(guild.id, 300, "piper-ruslan")

        await tts_bot.enqueue_tts("hello", voice_channel, 300, 400)

        job = await tts_bot.session_for(guild.id).queue.get()
        self.assertEqual(job.voice_profile, "piper-ruslan")

    async def test_slash_tts_test_responds_before_enqueueing_audio(self):
        bot_mod = load_bot_module()

        class FakeMember:
            pass

        call_order = []
        target_channel = types.SimpleNamespace(id=200, guild=types.SimpleNamespace(id=100))
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=100),
            user=FakeMember(),
            channel_id=300,
            response=types.SimpleNamespace(
                send_message=AsyncMock(side_effect=lambda *args, **kwargs: call_order.append("response")),
            ),
        )
        interaction.user.id = 400
        bot_mod.bot.config_store.is_allowed = MagicMock(return_value=True)
        bot_mod.bot.enqueue_tts = AsyncMock(side_effect=lambda *args, **kwargs: call_order.append("enqueue"))

        with (
            patch.object(bot_mod.discord, "Member", FakeMember),
            patch.object(bot_mod.tts_commands, "resolve_tts_command_voice_channel", MagicMock(return_value=target_channel)),
        ):
            await bot_mod.slash_tts_test.callback(interaction, "проверка")

        self.assertEqual(call_order, ["response", "enqueue"])

    async def test_idle_disconnect_runs_even_when_allowed_user_is_present(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        guild = types.SimpleNamespace(id=123)
        allowed_member = types.SimpleNamespace(id=441612025286885397, bot=False)
        channel = types.SimpleNamespace(members=[allowed_member])
        vc = types.SimpleNamespace(
            channel=channel,
            is_connected=MagicMock(return_value=True),
            is_playing=MagicMock(return_value=False),
            is_paused=MagicMock(return_value=False),
            disconnect=AsyncMock(),
        )
        tts_bot.config_store = temp_config_store(bot_mod, {allowed_member.id})

        with (
            patch.object(bot_mod.asyncio, "sleep", AsyncMock()),
            patch.object(bot_mod.discord.utils, "get", MagicMock(return_value=vc)),
        ):
            await tts_bot._idle_disconnect_after_timeout(guild)

        vc.disconnect.assert_awaited_once_with(force=True)
        self.assertGreater(tts_bot.suppress_auto_connect_remaining(guild.id), 0.0)

    async def test_idle_disconnect_runs_when_only_continuous_idle_stream_is_playing(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        guild = types.SimpleNamespace(id=123)
        source = bot_mod.ContinuousTTSAudioSource(b"\x00" * bot_mod.PCM_FRAME_BYTES)
        vc = types.SimpleNamespace(
            is_connected=MagicMock(return_value=True),
            is_playing=MagicMock(return_value=True),
            is_paused=MagicMock(return_value=False),
            disconnect=AsyncMock(),
            stop=MagicMock(),
            channel=None,  # _idle_disconnect_after_timeout reads vc.channel
        )
        tts_bot.continuous_sources[guild.id] = source

        with (
            patch.object(bot_mod.asyncio, "sleep", AsyncMock()),
            patch.object(bot_mod.discord.utils, "get", MagicMock(return_value=vc)),
        ):
            await tts_bot._idle_disconnect_after_timeout(guild)

        self.assertTrue(source.stopped)
        self.assertNotIn(guild.id, tts_bot.continuous_sources)
        vc.stop.assert_called_once()
        vc.disconnect.assert_awaited_once_with(force=True)

    async def test_prepare_tts_pcm_frames_raises_on_ffmpeg_error(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()

        class FailedProc:
            returncode = 1

            async def communicate(self):
                return b"", b"bad wav"

        with tempfile.TemporaryDirectory() as tmp_dir:
            source = Path(tmp_dir) / "source.wav"
            source.write_bytes(b"fake")
            with patch.object(bot_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=FailedProc())):
                with self.assertRaisesRegex(RuntimeError, "ffmpeg PCM preparation failed"):
                    await tts_bot.prepare_tts_pcm_frames(source)

    async def test_prepare_tts_pcm_frames_returns_padded_frames(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()

        class OkProc:
            returncode = 0

            async def communicate(self):
                return b"\x01" * (bot_mod.PCM_FRAME_BYTES + 10), b""

        with tempfile.TemporaryDirectory() as tmp_dir:
            source = Path(tmp_dir) / "source.wav"
            source.write_bytes(b"fake")
            with patch.object(bot_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=OkProc())):
                frames = await tts_bot.prepare_tts_pcm_frames(source)

            self.assertGreaterEqual(len(frames), 2)
            self.assertTrue(all(len(frame) == bot_mod.PCM_FRAME_BYTES for frame in frames))

    async def test_ensure_continuous_player_stops_previous_source_when_replacing(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        vc = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=123),
            is_playing=MagicMock(return_value=True),
            is_paused=MagicMock(return_value=False),
            stop=MagicMock(),
            play=MagicMock(),
        )

        source = tts_bot.ensure_continuous_player(vc)

        self.assertIs(tts_bot.continuous_sources[123], source)
        vc.stop.assert_called_once()
        vc.play.assert_called_once_with(source)

    async def test_continuous_idle_stop_stops_drained_source_and_voice(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        guild = types.SimpleNamespace(id=123)
        source = bot_mod.ContinuousTTSAudioSource(b"\x00" * bot_mod.PCM_FRAME_BYTES)
        vc = types.SimpleNamespace(
            is_connected=MagicMock(return_value=True),
            is_playing=MagicMock(return_value=True),
            is_paused=MagicMock(return_value=False),
            stop=MagicMock(),
        )
        tts_bot.continuous_sources[guild.id] = source

        with (
            patch.object(bot_mod.asyncio, "sleep", AsyncMock()),
            patch.object(bot_mod.discord.utils, "get", MagicMock(return_value=vc)),
        ):
            await tts_bot._continuous_idle_stop_after_timeout(guild)

        self.assertTrue(source.stopped)
        self.assertNotIn(guild.id, tts_bot.continuous_sources)
        vc.stop.assert_called_once()

    async def test_continuous_idle_stop_keeps_source_when_speech_pending(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        guild = types.SimpleNamespace(id=123)
        source = bot_mod.ContinuousTTSAudioSource(b"\x00" * bot_mod.PCM_FRAME_BYTES)
        source.enqueue_frames([b"\x01" * bot_mod.PCM_FRAME_BYTES])
        tts_bot.continuous_sources[guild.id] = source

        with patch.object(bot_mod.asyncio, "sleep", AsyncMock()):
            await tts_bot._continuous_idle_stop_after_timeout(guild)

        self.assertFalse(source.stopped)
        self.assertIs(tts_bot.continuous_sources[guild.id], source)


if __name__ == "__main__":
    unittest.main()
