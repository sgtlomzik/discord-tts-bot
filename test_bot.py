import asyncio
import contextlib
import importlib.util
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


def load_bot_module():
    module_path = Path(__file__).with_name("bot.py")
    spec = importlib.util.spec_from_file_location("tts_bot_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def temp_config_store(bot_mod, fallback_users=None):
    tmp_dir = tempfile.TemporaryDirectory()
    store = bot_mod.BotConfigStore(Path(tmp_dir.name) / "config.json", set(fallback_users or []))
    store._tmp_dir = tmp_dir
    return store


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

    def test_process_text_replaces_newlines(self):
        bot = load_bot_module()

        self.assertEqual(bot.process_text("hello\nworld"), "hello. world")

    def test_runtime_is_piper_ruslan_only(self):
        bot = load_bot_module()

        self.assertEqual(bot.DEFAULT_VOICE_PROFILE, "piper-ruslan")
        self.assertEqual(list(bot.VOICE_PROFILES), ["piper-ruslan"])
        self.assertFalse(hasattr(bot, "RHVOICE_URL"))
        self.assertFalse(hasattr(bot, "ESPEAK_CMD"))

    def test_config_store_persists_guild_settings(self):
        bot = load_bot_module()

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.json"
            store = bot.BotConfigStore(path, {111})
            config = store.get_guild(10)

            self.assertTrue(config.enabled)
            self.assertEqual(config.allowed_users, {111})

            store.add_user(10, 222)
            store.set_enabled(10, False)
            store.set_default_voice(10, "piper-ruslan")
            store.set_user_voice(10, 222, "piper-ruslan")

            loaded = bot.BotConfigStore(path, set())
            loaded_config = loaded.get_guild(10)

            self.assertFalse(loaded_config.enabled)
            self.assertEqual(loaded_config.allowed_users, {111, 222})
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
            self.assertIs(bot.resolve_tts_command_voice_channel(None, requested), requested)

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
            self.assertIs(bot.resolve_tts_command_voice_channel(interaction), bot_channel)

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
            self.assertIs(bot.resolve_tts_command_voice_channel(interaction), user_channel)

    def test_slash_group_uses_non_reserved_name(self):
        bot = load_bot_module()

        self.assertEqual(bot.tts_group.name, "voicebot")

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

            self.assertEqual(config.default_voice, bot.DEFAULT_VOICE_PROFILE)
            self.assertEqual(config.user_voices, {})

    def test_playback_prepare_command_adds_preroll_after_speech_trim(self):
        bot = load_bot_module()

        command = bot.build_playback_prepare_command(Path("/tmp/in.wav"), Path("/tmp/out.wav"))
        filter_complex = command[command.index("-filter_complex") + 1]

        self.assertNotIn("adelay", filter_complex)
        self.assertIn("silenceremove", filter_complex)
        self.assertLess(filter_complex.index("silenceremove"), filter_complex.index("concat=n=3"))
        self.assertIn("[primer][speech][tail]concat=n=3:v=0:a=1[out]", filter_complex)
        self.assertTrue(any("anullsrc" in part for part in command))
        self.assertEqual(command[-1], "/tmp/out.wav")

    def test_default_preroll_and_idle_frames_are_silent(self):
        bot = load_bot_module()

        self.assertEqual(bot.TTS_PREROLL_MODE, "silence")
        self.assertEqual(bot.TTS_IDLE_FRAME_MODE, "silence")
        self.assertIn("anullsrc", bot.build_preroll_lavfi_source(bot.TTS_PREROLL_MODE, "0.2"))

    def test_preroll_lavfi_source_supports_sine_noise_and_silence(self):
        bot = load_bot_module()

        self.assertIn("anoisesrc", bot.build_preroll_lavfi_source("noise", "0.5"))
        self.assertIn("sine=frequency=180", bot.build_preroll_lavfi_source("sine", "0.5"))
        self.assertIn("anullsrc", bot.build_preroll_lavfi_source("silence", "0.5"))

    def test_playback_filter_can_disable_speech_trim_without_removing_primer(self):
        bot = load_bot_module()

        filter_complex = bot.build_playback_filter_complex(False, -42)

        self.assertNotIn("silenceremove", filter_complex)
        self.assertIn("[primer][speech][tail]concat=n=3:v=0:a=1[out]", filter_complex)
        self.assertIn("volume=-42dB", filter_complex)

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
    async def test_tts_worker_does_not_disconnect_on_tts_generation_error(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()

        tts_bot.wait_until_ready = AsyncMock()
        tts_bot.warmup_tts = AsyncMock()
        tts_bot.ensure_voice = AsyncMock(return_value=object())
        tts_bot.generate_tts_file = AsyncMock(side_effect=RuntimeError("tts failed"))
        tts_bot.play_file = AsyncMock()
        tts_bot.disconnect_guild_voice = AsyncMock()
        tts_bot.is_closed = MagicMock(side_effect=[False, True])

        guild = types.SimpleNamespace(id=1)
        voice_channel = types.SimpleNamespace(id=2, guild=guild)
        await tts_bot.message_queue.put(
            bot_mod.TTSJob(
                text="hello",
                voice_channel=voice_channel,
                queued_at=0.0,
                author_id=441612025286885397,
                guild_id=guild.id,
                text_channel_id=3,
                voice_profile="piper-ruslan",
            )
        )

        await asyncio.wait_for(tts_bot.tts_worker(), timeout=1.0)

        tts_bot.disconnect_guild_voice.assert_not_awaited()
        tts_bot.play_file.assert_not_awaited()

    async def test_tts_worker_uses_continuous_stream_when_enabled(self):
        bot_mod = load_bot_module()
        bot_mod.TTS_CONTINUOUS_STREAM = True
        tts_bot = bot_mod.TTSBot()

        source = types.SimpleNamespace(enqueue_frames=MagicMock(), wait_until_drained=AsyncMock(return_value=True))
        tts_bot.wait_until_ready = AsyncMock()
        tts_bot.warmup_tts = AsyncMock()
        tts_bot.ensure_voice = AsyncMock(return_value=types.SimpleNamespace(guild=types.SimpleNamespace(id=1)))
        tts_bot.generate_tts_file = AsyncMock(return_value=None)
        tts_bot.prepare_tts_pcm_frames = AsyncMock(return_value=[b"\x00" * bot_mod.PCM_FRAME_BYTES])
        tts_bot.ensure_continuous_player = MagicMock(return_value=source)
        tts_bot.play_file = AsyncMock()
        tts_bot.schedule_idle_disconnect = MagicMock()
        tts_bot.schedule_continuous_idle_stop = MagicMock()
        tts_bot.is_closed = MagicMock(side_effect=[False, True])

        guild = types.SimpleNamespace(id=1)
        voice_channel = types.SimpleNamespace(id=2, guild=guild)
        await tts_bot.message_queue.put(
            bot_mod.TTSJob("hello", voice_channel, 0.0, 100, 1, 1000, "piper-ruslan")
        )

        await asyncio.wait_for(tts_bot.tts_worker(), timeout=1.0)

        tts_bot.prepare_tts_pcm_frames.assert_awaited_once()
        source.enqueue_frames.assert_called_once()
        source.wait_until_drained.assert_awaited_once()
        tts_bot.play_file.assert_not_awaited()
        tts_bot.schedule_continuous_idle_stop.assert_called_once_with(guild)

    async def test_tts_worker_uses_play_file_when_continuous_stream_disabled(self):
        bot_mod = load_bot_module()
        bot_mod.TTS_CONTINUOUS_STREAM = False
        tts_bot = bot_mod.TTSBot()

        tts_bot.wait_until_ready = AsyncMock()
        tts_bot.warmup_tts = AsyncMock()
        tts_bot.ensure_voice = AsyncMock(return_value=object())
        tts_bot.generate_tts_file = AsyncMock(return_value=None)
        tts_bot.prepare_tts_pcm_frames = AsyncMock()
        tts_bot.play_file = AsyncMock()
        tts_bot.schedule_idle_disconnect = MagicMock()
        tts_bot.schedule_continuous_idle_stop = MagicMock()
        tts_bot.is_closed = MagicMock(side_effect=[False, True])

        guild = types.SimpleNamespace(id=1)
        voice_channel = types.SimpleNamespace(id=2, guild=guild)
        await tts_bot.message_queue.put(
            bot_mod.TTSJob("hello", voice_channel, 0.0, 100, 1, 1000, "piper-ruslan")
        )

        await asyncio.wait_for(tts_bot.tts_worker(), timeout=1.0)

        tts_bot.play_file.assert_awaited_once()
        tts_bot.prepare_tts_pcm_frames.assert_not_awaited()
        tts_bot.schedule_continuous_idle_stop.assert_called_once_with(guild)

    async def test_tts_worker_disconnects_on_voice_connect_error(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()

        tts_bot.wait_until_ready = AsyncMock()
        tts_bot.warmup_tts = AsyncMock()
        tts_bot.ensure_voice = AsyncMock(side_effect=RuntimeError("connect failed"))
        tts_bot.generate_tts_file = AsyncMock(return_value=None)
        tts_bot.play_file = AsyncMock()
        tts_bot.disconnect_guild_voice = AsyncMock()
        tts_bot.is_closed = MagicMock(side_effect=[False, True])

        guild = types.SimpleNamespace(id=10)
        voice_channel = types.SimpleNamespace(id=20, guild=guild)
        await tts_bot.message_queue.put(
            bot_mod.TTSJob(
                text="hello",
                voice_channel=voice_channel,
                queued_at=0.0,
                author_id=441612025286885397,
                guild_id=guild.id,
                text_channel_id=30,
                voice_profile="piper-ruslan",
            )
        )

        await asyncio.wait_for(tts_bot.tts_worker(), timeout=1.0)

        tts_bot.disconnect_guild_voice.assert_not_awaited()
        tts_bot.play_file.assert_not_awaited()

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

    async def test_ensure_voice_lock_is_reused_per_guild(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()

        first = tts_bot.get_voice_connect_lock(5)
        second = tts_bot.get_voice_connect_lock(5)

        self.assertIs(first, second)

    async def test_generate_tts_file_uses_piper_only(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.generate_piper_file = AsyncMock(return_value=None)

        filename = Path("/tmp/test_piper_only.wav")
        await tts_bot.generate_tts_file("hello", filename)

        tts_bot.generate_piper_file.assert_awaited_once()
        profile = tts_bot.generate_piper_file.await_args.args[2]
        self.assertEqual(profile.name, "piper-ruslan")

    async def test_clear_queue_for_guild_keeps_other_guild_jobs(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()

        guild_one = types.SimpleNamespace(id=1)
        guild_two = types.SimpleNamespace(id=2)
        channel_one = types.SimpleNamespace(id=10, guild=guild_one)
        channel_two = types.SimpleNamespace(id=20, guild=guild_two)
        await tts_bot.message_queue.put(
            bot_mod.TTSJob("one", channel_one, 0.0, 100, 1, 1000, "piper-ruslan")
        )
        await tts_bot.message_queue.put(
            bot_mod.TTSJob("two", channel_two, 0.0, 200, 2, 2000, "piper-ruslan")
        )

        removed = tts_bot.clear_queue_for_guild(1)
        kept = await tts_bot.message_queue.get()

        self.assertEqual(removed, 1)
        self.assertEqual(kept.guild_id, 2)

    async def test_clear_merge_buffers_cancels_only_target_guild(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()

        guild_one = types.SimpleNamespace(id=1)
        guild_two = types.SimpleNamespace(id=2)
        channel_one = types.SimpleNamespace(id=10, guild=guild_one)
        channel_two = types.SimpleNamespace(id=20, guild=guild_two)
        task_one = asyncio.create_task(asyncio.sleep(10))
        task_two = asyncio.create_task(asyncio.sleep(10))
        tts_bot.merge_buffers[(100, 10)] = ["a"]
        tts_bot.merge_buffers[(200, 20)] = ["b"]
        tts_bot.merge_targets[(100, 10)] = (channel_one, 100, 1000)
        tts_bot.merge_targets[(200, 20)] = (channel_two, 200, 2000)
        tts_bot.merge_tasks[(100, 10)] = task_one
        tts_bot.merge_tasks[(200, 20)] = task_two

        tts_bot.clear_merge_buffers(1)

        self.assertNotIn((100, 10), tts_bot.merge_buffers)
        self.assertIn((200, 20), tts_bot.merge_buffers)
        self.assertTrue(task_one.cancelled() or task_one.cancelling())
        self.assertFalse(task_two.cancelled())
        task_two.cancel()

    async def test_queue_or_merge_short_messages_combines_text(self):
        bot_mod = load_bot_module()
        bot_mod.TTS_MERGE_WINDOW_MS = 100000
        tts_bot = bot_mod.TTSBot()
        tts_bot.config_store = temp_config_store(bot_mod, {300})

        guild = types.SimpleNamespace(id=100)
        voice_channel = types.SimpleNamespace(id=200, guild=guild)

        await tts_bot.queue_or_merge_message("one", voice_channel, 300, 400)
        await tts_bot.queue_or_merge_message("two", voice_channel, 300, 400)

        task = tts_bot.merge_tasks[(300, 200)]
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        bot_mod.TTS_MERGE_WINDOW_MS = 0
        await tts_bot._flush_merge_after_delay((300, 200))
        job = await tts_bot.message_queue.get()

        self.assertEqual(job.text, "one. two")

    async def test_enqueue_tts_uses_piper_ruslan_profile(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.config_store = temp_config_store(bot_mod)

        guild = types.SimpleNamespace(id=100)
        voice_channel = types.SimpleNamespace(id=200, guild=guild)
        tts_bot.config_store.set_user_voice(guild.id, 300, "piper-ruslan")

        await tts_bot.enqueue_tts("hello", voice_channel, 300, 400)

        job = await tts_bot.message_queue.get()
        self.assertEqual(job.voice_profile, "piper-ruslan")

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

    async def test_prepare_playback_file_raises_on_ffmpeg_error(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()

        class FailedProc:
            returncode = 1

            async def communicate(self):
                return b"stdout", b"stderr"

        with tempfile.TemporaryDirectory() as tmp_dir:
            source = Path(tmp_dir) / "source.wav"
            prepared = Path(tmp_dir) / "prepared.wav"
            source.write_bytes(b"fake")
            with patch.object(bot_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=FailedProc())):
                with self.assertRaisesRegex(RuntimeError, "ffmpeg playback preparation failed"):
                    await tts_bot.prepare_playback_file(source, prepared)

    async def test_prepare_playback_file_returns_prepared_output(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()

        class OkProc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        with tempfile.TemporaryDirectory() as tmp_dir:
            source = Path(tmp_dir) / "source.wav"
            prepared = Path(tmp_dir) / "prepared.wav"
            source.write_bytes(b"fake-source")

            async def fake_exec(*args, **kwargs):
                prepared.write_bytes(b"fake-prepared")
                return OkProc()

            with patch.object(bot_mod.asyncio, "create_subprocess_exec", fake_exec):
                result = await tts_bot.prepare_playback_file(source, prepared)

            self.assertEqual(result, prepared)
            self.assertEqual(prepared.read_bytes(), b"fake-prepared")

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
