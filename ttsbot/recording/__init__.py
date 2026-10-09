"""Voice receive: record every speaker of the bot's voice channel to disk.

``client.RecordingVoiceClient`` receives the UDP voice packets,
``rtp`` removes the transport encryption, the DAVE session removes the
end-to-end encryption, and ``recorder.ChannelRecorder`` writes each
user's Opus packets into Ogg files (``ogg.OggOpusWriter``) without
decoding them. ``chunks.jsonl`` in the session folder lists every
closed file with the wall-clock time of each utterance in it.
"""
