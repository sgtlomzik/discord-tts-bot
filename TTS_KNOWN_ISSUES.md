# TTS Known Issues

Current problems observed in logs and manual checks:

1. Voice connect sometimes races with itself.
   - `Already connected to a voice channel`
   - Seen when a new TTS request arrives while the bot is still in the middle of connecting.

2. Startup and playback latency are still high.
   - Short messages usually start in about 4.8s to 7.5s after queueing.
   - Longer messages can take 18s to 24s end-to-end.
   - The biggest cost is still TTS generation plus voice connect / handshake.

3. Auto-connect can time out.
   - `Auto-connect failed ... TimeoutError`
   - This appears after repeated voice handshake retries.

4. Long messages are expensive.
   - In logs, a 75kB temp audio file took noticeably longer to generate and play.
   - This makes the latency problem much worse for longer inputs.

5. Need a clean post-beta comparison.
   - Stable `master` works, but the beta branch has different voice behavior.
   - Future fixes should be tested against both branches before merging.

Notes:
- This file is only a problem tracker.
- No code changes are included here.
