"""Fetching media from a link, and the lead-line MIDI export.

The fetch tests are mostly about what must *not* work. A server that retrieves
any URL a caller supplies is an SSRF tool, and this one listens on a network
interface, so it could otherwise be used to reach loopback services, cloud
metadata endpoints and hosts inside a private network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analysis import analyze  # noqa: E402
from app.export import to_melody_midi  # noqa: E402
from app.ingest.fetch import FetchError, validate_url  # noqa: E402
from app.ingest.score import parse_score_file  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# ── Addresses that must never be reachable ────────────────────────────────


@pytest.mark.parametrize("url", [
    "http://localhost:8000/api/health",
    "http://127.0.0.1/",
    "http://127.0.0.1:22/",
    "http://0.0.0.0/",
    "http://[::1]/",
    "http://10.0.0.5/x.mp3",
    "http://192.168.1.1/x.mp3",
    "http://172.16.0.1/x.mp3",
    "http://169.254.169.254/latest/meta-data/",
    "http://metadata.google.internal/computeMetadata/v1/",
])
def test_private_and_internal_addresses_are_refused(url):
    with pytest.raises(FetchError):
        validate_url(url)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "gopher://host/x", "ftp://host/a.mp3",
    "data:audio/mp3;base64,AAAA", "jar:http://host/a!/b", "//host/a.mp3",
])
def test_only_http_and_https_are_allowed(url):
    with pytest.raises(FetchError):
        validate_url(url)


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=x", "https://youtu.be/x",
    "https://music.youtube.com/watch?v=x", "https://open.spotify.com/track/x",
    "https://music.apple.com/us/song/x/1", "https://soundcloud.com/a/b",
    "https://www.bilibili.com/video/x", "https://y.qq.com/x",
])
def test_streaming_platforms_are_refused_with_a_reason(url):
    """The refusal should explain itself rather than look like a failure."""
    with pytest.raises(FetchError) as caught:
        validate_url(url)
    message = str(caught.value).lower()
    assert "streaming" in message or "terms" in message


@pytest.mark.parametrize("url", ["", "   ", "not a url", "http://", "https://"])
def test_malformed_links_are_refused(url):
    with pytest.raises(FetchError):
        validate_url(url)


def test_an_over_long_link_is_refused():
    with pytest.raises(FetchError, match="too long"):
        validate_url("https://example.com/" + "a" * 3000)


def test_a_public_link_passes_validation():
    assert validate_url("https://upload.wikimedia.org/wikipedia/commons/c/c8/Example.ogg")


# ── Fetching itself, against a local server ───────────────────────────────


@pytest.fixture
def local_server(tmp_path):
    """A throwaway HTTP server. Loopback is blocked by design, so the private-
    address check is stubbed out for these tests only -- what is under test
    here is the streaming, sizing and content handling, not the SSRF guard."""
    import http.server
    import threading

    (tmp_path / "tune.wav").write_bytes(_tiny_wav())
    (tmp_path / "page.html").write_text("<html>not media</html>")
    (tmp_path / "empty.mp3").write_bytes(b"")

    handler = http.server.SimpleHTTPRequestHandler

    class Quiet(handler):
        def log_message(self, *args):
            pass

        def translate_path(self, path):
            return str(tmp_path / Path(path).name)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _tiny_wav() -> bytes:
    import io

    import numpy as np
    import soundfile as sf

    buffer = io.BytesIO()
    sf.write(buffer, np.zeros(2205, dtype="float32"), 22050, format="WAV")
    return buffer.getvalue()


@pytest.fixture
def unguarded(monkeypatch):
    import app.ingest.fetch as fetch_module

    monkeypatch.setattr(fetch_module, "_reject_private", lambda host: None)
    return fetch_module


def test_fetches_a_media_file(local_server, unguarded):
    got = unguarded.fetch_media(f"{local_server}/tune.wav")
    try:
        assert got.filename.endswith(".wav") and got.size > 0
        assert Path(got.path).exists()
    finally:
        Path(got.path).unlink(missing_ok=True)


def test_a_web_page_is_not_media(local_server, unguarded):
    with pytest.raises(FetchError, match="not audio"):
        unguarded.fetch_media(f"{local_server}/page.html")


def test_an_empty_file_is_refused(local_server, unguarded):
    with pytest.raises(FetchError):
        unguarded.fetch_media(f"{local_server}/empty.mp3")


def test_a_missing_file_reports_its_status(local_server, unguarded):
    with pytest.raises(FetchError, match="404"):
        unguarded.fetch_media(f"{local_server}/nope.wav")


def test_oversized_downloads_are_cut_off(local_server, unguarded, monkeypatch):
    """content-length can lie or be absent, so the stream is capped as well."""
    monkeypatch.setattr(unguarded, "MAX_FETCH_BYTES", 64)
    with pytest.raises(FetchError, match="larger than"):
        unguarded.fetch_media(f"{local_server}/tune.wav")


def test_a_redirect_into_private_space_is_refused(tmp_path, monkeypatch):
    """A public URL redirecting inward is the usual way past an SSRF filter.

    The first hop is allowed through so the redirect can happen at all; the
    target is then checked by the real rule, which is the thing under test.
    """
    import http.server
    import threading

    import app.ingest.fetch as fetch_module

    real_reject = fetch_module._reject_private

    def allow_only_the_test_server(host):
        if host in ("127.0.0.1", "localhost"):
            return
        real_reject(host)

    monkeypatch.setattr(fetch_module, "_reject_private", allow_only_the_test_server)

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(FetchError, match="private or internal|link-local"):
            fetch_module.fetch_media(f"http://127.0.0.1:{server.server_port}/a.mp3")
    finally:
        server.shutdown()


# ── The lead line on its own ──────────────────────────────────────────────


def test_lead_midi_holds_one_track_matching_the_melody():
    from music21 import converter

    source = parse_score_file(str(ROOT / "samples" / "twinkle.musicxml"))
    key, _, _ = analyze(source)
    data = to_melody_midi(source, key, "Twinkle")
    assert data.startswith(b"MThd")

    handle = Path("/tmp/_lead_test.mid")
    handle.write_bytes(data)
    try:
        parsed = converter.parse(str(handle))
        pitches = [n.pitch.midi for n in parsed.recurse().notes]
        assert pitches == [n.pitch for n in source.all_notes]
        assert len(parsed.parts) == 1
    finally:
        handle.unlink(missing_ok=True)


def test_lead_midi_survives_a_piece_with_no_notes():
    from music21 import stream

    from app.models import SourceScore
    from app.theory import KeyContext

    empty = SourceScore(bars=[], tempo=96.0, title="Nothing", source_kind="score")
    data = to_melody_midi(empty, KeyContext(tonic_pc=0, mode="major"))
    assert data.startswith(b"MThd")


def test_lead_endpoint(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    with open(ROOT / "samples" / "twinkle.musicxml", "rb") as handle:
        session = client.post(
            "/api/upload", files={"file": ("t.musicxml", handle.read(), "application/xml")}
        ).json()["session_id"]

    good = client.get(f"/api/session/{session}/lead.mid")
    assert good.status_code == 200
    assert good.content.startswith(b"MThd")
    assert client.get("/api/session/nope/lead.mid").status_code == 404


def test_fetch_endpoint_refuses_what_it_should():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    for url in ("http://169.254.169.254/", "https://www.youtube.com/watch?v=x",
                "file:///etc/passwd", "http://localhost:8000/"):
        response = client.post("/api/fetch", json={"url": url})
        assert response.status_code == 400, url
        assert "Traceback" not in response.text
