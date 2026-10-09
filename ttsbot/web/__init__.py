"""HTTP front of the recordings archive (``python -m ttsbot.web``).

``app.create_app`` serves ``ttsbot.archive.service.ArchiveService`` as a
JSON API under ``/api/v1`` and, when ``WEB_STATIC_DIR`` is set, a single
page frontend from that folder. See docs/RECORDINGS_WEB.md.
"""
