from .config import VoiceRecorderConfig


class WebRtcVad:
    def __init__(self, config: VoiceRecorderConfig) -> None:
        import webrtcvad

        self._vad = webrtcvad.Vad(config.vad_mode)
        self._sample_rate = config.sample_rate

    def is_speech(self, pcm: bytes) -> bool:
        return self._vad.is_speech(pcm, self._sample_rate)
