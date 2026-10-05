"""Recordings website: HTTP routes, errors, auth, CORS, static frontend."""

import base64
import shutil
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from ttsbot.archive.media import MediaCache
from ttsbot.archive.retention import RetentionPolicy
from ttsbot.archive.service import ArchiveService
from ttsbot.archive.store import RecordingStore
from ttsbot.web.app import create_app

from tests.test_archive import GUILD, NAME, SessionFixture

SESSION = f"/api/v1/sessions/{GUILD}/{NAME}"


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = self.dir / "recordings"
        self.root.mkdir()
        SessionFixture(self.root)
        (self.root / GUILD / NAME / "transcript.md").write_bytes("# Запись\n".encode())

    async def client(self, **kw) -> TestClient:
        service = ArchiveService(RecordingStore(self.root), MediaCache(self.dir / "cache", max_bytes=10**8),
                                 RetentionPolicy(audio_days=90))
        client = TestClient(TestServer(create_app(service, **kw)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def test_api_documents(self):
        client = await self.client()
        info = await (await client.get("/api/v1/info")).json()
        self.assertEqual(info["api_version"], 1)
        listing = await client.get("/api/v1/sessions?limit=10")
        self.assertEqual(listing.status, 200)
        self.assertEqual(listing.headers["Cache-Control"], "no-store")
        items = (await listing.json())["items"]
        self.assertEqual(items[0]["links"]["self"], SESSION)
        doc = await (await client.get(SESSION)).json()
        self.assertEqual([line["position"] for line in doc["lines"]], [0.0, 1.6, 1.7, 2.6])
        guilds = await (await client.get("/api/v1/guilds")).json()
        self.assertEqual(guilds["items"][0]["id"], GUILD)
        health = await client.get("/api/v1/health")
        self.assertEqual((await health.json())["status"], "ok")

    async def test_errors_are_json(self):
        client = await self.client()
        cases = [
            (f"/api/v1/sessions/{GUILD}/20990101-000000Z-1", 404, "session_not_found"),
            ("/api/v1/sessions/1/..%2F..%2Fetc", 404, "not_found"),
            ("/api/v1/nothing", 404, "not_found"),
            ("/api/v1/sessions?limit=x", 400, "bad_parameter"),
            ("/api/v1/sessions?user=abc", 400, "bad_parameter"),
        ]
        for url, status, code in cases:
            response = await client.get(url)
            self.assertEqual(response.status, status, url)
            self.assertEqual((await response.json())["error"]["code"], code, url)

    async def test_audio_supports_ranges(self):
        client = await self.client()
        full = await client.get(f"{SESSION}/audio")
        self.assertEqual(full.status, 200)
        self.assertEqual(full.headers["Content-Type"], "audio/ogg")
        body = await full.read()
        self.assertEqual(body[:4], b"OggS")
        part = await client.get(f"{SESSION}/audio", headers={"Range": "bytes=10-19"})
        self.assertEqual(part.status, 206)
        self.assertEqual(await part.read(), body[10:20])
        download = await client.get(f"{SESSION}/audio?download=1")
        self.assertIn("attachment", download.headers["Content-Disposition"])

    async def test_audio_gone(self):
        for ogg in (self.root / GUILD / NAME).glob("*/*.ogg"):
            ogg.unlink()
        client = await self.client()
        response = await client.get(f"{SESSION}/audio")
        self.assertEqual(response.status, 404)
        self.assertEqual((await response.json())["error"]["code"], "audio_unavailable")

    async def test_transcript_markdown(self):
        client = await self.client()
        response = await client.get(f"{SESSION}/transcript.md")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), "# Запись\n")

    async def test_basic_auth(self):
        client = await self.client(username="me", password="secret")
        denied = await client.get("/api/v1/sessions")
        self.assertEqual(denied.status, 401)
        self.assertIn("Basic", denied.headers["WWW-Authenticate"])
        wrong = base64.b64encode(b"me:nope").decode()
        self.assertEqual((await client.get("/api/v1/sessions", headers={"Authorization": f"Basic {wrong}"})).status, 401)
        good = base64.b64encode(b"me:secret").decode()
        self.assertEqual((await client.get("/api/v1/sessions", headers={"Authorization": f"Basic {good}"})).status, 200)
        self.assertEqual((await client.get("/api/v1/health")).status, 200)  # for the container healthcheck

    async def test_cors_for_listed_origins_only(self):
        client = await self.client(cors_origins=("http://localhost:5173",), password="secret")
        preflight = await client.options("/api/v1/sessions", headers={
            "Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
        self.assertEqual(preflight.status, 204)
        self.assertEqual(preflight.headers["Access-Control-Allow-Origin"], "http://localhost:5173")
        denied = await client.get("/api/v1/sessions", headers={"Origin": "http://localhost:5173"})
        self.assertEqual(denied.status, 401)
        self.assertEqual(denied.headers["Access-Control-Allow-Origin"], "http://localhost:5173")
        other = await client.get("/api/v1/health", headers={"Origin": "http://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", other.headers)

    async def test_static_frontend_with_page_fallback(self):
        static = self.dir / "static"
        (static / "assets").mkdir(parents=True)
        (static / "index.html").write_text("<html>app</html>", encoding="utf-8")
        (static / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
        (self.dir / "secret.txt").write_text("no", encoding="utf-8")
        client = await self.client(static_dir=static)
        self.assertEqual(await (await client.get("/")).text(), "<html>app</html>")
        self.assertEqual(await (await client.get(f"/sessions/{GUILD}/{NAME}")).text(), "<html>app</html>")
        self.assertEqual(await (await client.get("/assets/app.js")).text(), "console.log(1)")
        self.assertEqual(await (await client.get("/../secret.txt")).text(), "<html>app</html>")
        api_miss = await client.get("/api/v1/nothing")
        self.assertEqual(api_miss.status, 404)


if __name__ == "__main__":
    unittest.main()
