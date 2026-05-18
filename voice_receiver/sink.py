try:
    from discord.ext import voice_recv
except Exception:  # pragma: no cover - optional runtime dependency
    voice_recv = None

from .models import RawAudioFrame


BaseAudioSink = voice_recv.AudioSink if voice_recv is not None else object


class QueueingVoiceSink(BaseAudioSink):
    def __init__(self, session) -> None:
        if voice_recv is not None:
            super().__init__()
        self.session = session

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data) -> None:
        self.session.handle_frame(
            RawAudioFrame(
                user_id=getattr(user, "id", None),
                username_snapshot=getattr(user, "display_name", None) or getattr(user, "name", None),
                pcm=data.pcm,
            )
        )

    def cleanup(self) -> None:
        return None
