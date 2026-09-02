"""Degenerate, hostile and merely unusual inputs.

Every case here corresponds to a defect that was found by throwing bad input at
the running code, not by reading it. The comments say what actually broke.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analysis import analyze, parse_chord_symbol  # noqa: E402
from app.export import to_midi_bytes, to_musicxml  # noqa: E402
from app.harmony.arranger import build_arrangement  # noqa: E402
from app.harmony.styles import ENSEMBLES, get_ensemble  # noqa: E402
from app.ingest.score import MAX_BARS, layout_bars, parse_score_file  # noqa: E402
from app.models import MelodyNote  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def write_score(stream_obj) -> str:
    handle, path = tempfile.mkstemp(suffix=".musicxml")
    Path(path).unlink(missing_ok=True)
    stream_obj.write("musicxml", fp=path)
    return path


def full_pipeline(path: str, **kwargs):
    source = parse_score_file(path)
    key, harmony, _ = analyze(source)
    arrangement = build_arrangement(source, key, harmony, get_ensemble("satb"), {}, **kwargs)
    return source, arrangement, to_musicxml(arrangement), to_midi_bytes(arrangement)


# ── The one that could take the server down ───────────────────────────────


def test_zero_bar_length_does_not_hang():
    """`beats=0` made the bar walk never advance, appending until the process died.

    One crafted upload was enough to take the server down, so the loop now
    defends itself rather than trusting its caller.
    """
    bars = layout_bars([MelodyNote(pitch=60, offset=0.0, duration=1.0)], 0.0, 0, 4)
    assert len(bars) == 1
    assert bars[0].length > 0
    assert (bars[0].beats, bars[0].beat_type) == (4, 4)


@pytest.mark.parametrize("bad", [0.0, -1.0, -0.0])
def test_non_positive_bar_length_is_survivable(bad):
    bars = layout_bars([MelodyNote(pitch=60, offset=0.0, duration=4.0)], bad, 4, 4)
    assert bars and all(b.length > 0 for b in bars)


def test_absurd_duration_is_refused_not_expanded():
    """A corrupt file claiming a huge duration must not become a million bars."""
    with pytest.raises(ValueError, match="bars"):
        layout_bars([MelodyNote(pitch=60, offset=0.0, duration=1e6)], 4.0, 4, 4)


# ── Degenerate scores ─────────────────────────────────────────────────────


def test_empty_score(tmp_path):
    from music21 import stream

    score = stream.Score()
    score.insert(0, stream.Part())
    source, arrangement, xml, midi = full_pipeline(write_score(score))
    assert source.all_notes == []
    assert xml.startswith("<?xml") and len(midi) > 0


def test_score_of_only_rests():
    from music21 import converter

    source, _, xml, _ = full_pipeline(write_score(converter.parse("tinyNotation: 4/4 r1 r1")))
    assert source.all_notes == []
    assert xml.startswith("<?xml")


def test_single_note():
    from music21 import converter

    source, _, xml, _ = full_pipeline(write_score(converter.parse("tinyNotation: 4/4 c1")))
    assert len(source.all_notes) == 1
    assert xml.startswith("<?xml")


def test_every_note_the_same_pitch():
    """Key detection has nothing to correlate against; it must still produce one."""
    from music21 import converter

    source = parse_score_file(write_score(converter.parse("tinyNotation: 4/4 " + "c4 " * 16)))
    key, harmony, _ = analyze(source)
    assert key.name and len(harmony) == len(source.bars)


def test_fully_chromatic_melody():
    from music21 import converter

    tune = "tinyNotation: 4/4 c4 c#4 d4 d#4 e4 f4 f#4 g4 g#4 a4 a#4 b4"
    _, _, xml, _ = full_pipeline(write_score(converter.parse(tune)))
    assert xml.startswith("<?xml")


def test_tuplets_survive_export():
    from music21 import converter

    tune = "tinyNotation: 4/4 trip{c8 d8 e8} trip{f8 g8 a8} c2"
    _, _, xml, midi = full_pipeline(write_score(converter.parse(tune)))
    assert xml.startswith("<?xml") and len(midi) > 0


# ── Output must be valid MIDI whatever went in ────────────────────────────


def _extreme_pitch_score():
    from music21 import meter, note, stream

    score = stream.Score()
    part = stream.Part()
    part.append(meter.TimeSignature("4/4"))
    for midi in (0, 2, 125, 127):
        pitched = note.Note()
        pitched.pitch.midi = midi
        pitched.quarterLength = 1
        part.append(pitched)
    score.insert(0, part)
    return write_score(score)


@pytest.mark.parametrize("transpose", [-12, 0, 12])
@pytest.mark.parametrize("ensemble_id", ["satb", "ttbb"])
def test_output_pitches_are_valid_midi(transpose, ensemble_id):
    """A source with MIDI 0 drove backing voices to -48: unsingable and invalid."""
    path = _extreme_pitch_score()
    source = parse_score_file(path)
    key, harmony, _ = analyze(source)
    arrangement = build_arrangement(
        source, key, harmony, ENSEMBLES[ensemble_id], {}, transpose=transpose
    )
    pitches = [e.pitch for part in arrangement.parts for e in part if e.pitch is not None]
    assert pitches
    assert all(0 <= p <= 127 for p in pitches), f"outside MIDI: {[p for p in pitches if not 0 <= p <= 127]}"


def test_clamping_is_reported_not_silent():
    path = _extreme_pitch_score()
    source = parse_score_file(path)
    key, harmony, _ = analyze(source)
    arrangement = build_arrangement(source, key, harmony, get_ensemble("satb"), {})
    assert any("MIDI range" in w for w in arrangement.warnings)


# ── Metre ─────────────────────────────────────────────────────────────────


def test_mid_piece_metre_change_is_kept():
    """4/4 then 3/4 used to come out as two 4/4 bars, re-barring the music wrongly."""
    from music21 import meter, note, stream

    score = stream.Score()
    part = stream.Part()
    part.append(meter.TimeSignature("4/4"))
    for _ in range(4):
        part.append(note.Note("C4", quarterLength=1))
    part.append(meter.TimeSignature("3/4"))
    for _ in range(3):
        part.append(note.Note("D4", quarterLength=1))
    part.append(meter.TimeSignature("7/8"))
    for _ in range(7):
        part.append(note.Note("E4", quarterLength=0.5))
    score.insert(0, part)

    source = parse_score_file(write_score(score))
    signatures = [(b.beats, b.beat_type) for b in source.bars]
    assert (4, 4) in signatures and (3, 4) in signatures and (7, 8) in signatures
    assert [len(b.notes) for b in source.bars] == [4, 3, 7]


def test_pickup_is_still_detected():
    source = parse_score_file(str(ROOT / "samples" / "chorale_satb.musicxml"))
    assert source.bars[0].length < source.bars[1].length


@pytest.mark.parametrize("signature,beats", [("5/8", 5), ("7/8", 7), ("12/8", 12), ("2/2", 2)])
def test_unusual_metres_parse(signature, beats):
    from music21 import converter

    count = beats if "8" in signature else 2
    unit = "8" if "8" in signature else "2"
    tune = f"tinyNotation: {signature} " + " ".join([f"c{unit}"] * count)
    source = parse_score_file(write_score(converter.parse(tune)))
    assert source.bars[0].beats == beats


# ── Chord symbol parsing ──────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "", "   ", "H", "Cmaj9999", "C" * 500, "<script>alert(1)</script>",
    "\x00\x01", "C/", "/G", "---", "🎵", "C#b#b", "N.C.",
])
def test_hostile_chord_symbols_never_raise(text):
    result = parse_chord_symbol(text)
    assert result is None or hasattr(result, "root_pc")


# ── Size limits ───────────────────────────────────────────────────────────


def test_max_bars_is_enforced_somewhere_sane():
    assert 100 <= MAX_BARS <= 5000


# ── The API surface under hostile input ───────────────────────────────────


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def session(client):
    with open(ROOT / "samples" / "twinkle.musicxml", "rb") as handle:
        return client.post(
            "/api/upload", files={"file": ("t.musicxml", handle.read(), "application/xml")}
        ).json()["session_id"]


@pytest.mark.parametrize("beats", [0, -1, 999, 33])
def test_upload_rejects_impossible_metres(client, beats):
    """`beats=0` previously hung the server rather than returning anything."""
    with open(ROOT / "samples" / "twinkle.musicxml", "rb") as handle:
        response = client.post(
            "/api/upload",
            files={"file": ("t.musicxml", handle.read(), "application/xml")},
            data={"beats": str(beats)},
        )
    assert response.status_code == 422


@pytest.mark.parametrize("body_key,value", [
    ("transpose", 99), ("transpose", -99), ("tempo", 0), ("tempo", 100000), ("tempo", -5),
])
def test_arrange_rejects_out_of_range_settings(client, session, body_key, value):
    response = client.post("/api/arrange", json={"session_id": session, body_key: value})
    assert response.status_code == 422


@pytest.mark.parametrize("raw", ["Infinity", "NaN", "1e400", "-Infinity"])
def test_non_finite_numbers_are_rejected_cleanly(client, session, raw):
    """These reached the JSON encoder and came back as a 500 with a stack trace."""
    response = client.post(
        "/api/arrange",
        content='{"session_id":"%s","tempo":%s}' % (session, raw),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "Traceback" not in response.text


def test_oversized_text_is_refused(client, session):
    assert client.post(
        "/api/arrange", json={"session_id": session, "lyrics": "la " * 350_000}
    ).status_code == 422
    assert client.post(
        "/api/command", json={"session_id": session, "text": "a " * 50_000}
    ).status_code == 422


def test_bar_indices_must_exist(client, session):
    for index in (-1, 99_999):
        response = client.post(
            "/api/arrange", json={"session_id": session, "bars": [{"index": index}]}
        )
        assert response.status_code == 422


def test_restore_refuses_an_oversized_project(client):
    bar = {"beats": 4, "beat_type": 4, "length": 4.0,
           "melody": [[60, 0, 1]], "chord": "C", "roman": "I"}
    assert client.post("/api/restore", json={"bars": [bar] * 5000}).status_code == 422


def test_restore_refuses_zero_length_bars(client):
    """Zero-length bars would stack every later bar on the same offset."""
    bar = {"beats": 4, "beat_type": 4, "length": 0,
           "melody": [[60, 0, 1]], "chord": "C", "roman": "I"}
    assert client.post("/api/restore", json={"bars": [bar]}).status_code == 422


@pytest.mark.parametrize("payload", [
    b"", b"not xml at all", b"<score-partwise><unclosed",
    b"\x00\x01\x02\x03", b"%PDF-1.4 fake pdf",
])
def test_corrupt_uploads_fail_cleanly(client, payload):
    response = client.post(
        "/api/upload", files={"file": ("x.musicxml", payload, "application/xml")}
    )
    assert response.status_code in (400, 415, 422)
    assert "Traceback" not in response.text
    assert "/usr/local" not in response.text


def test_errors_never_leak_paths_or_traces(client):
    """Error bodies must not reflect internals back to the caller."""
    responses = [
        client.post("/api/arrange", json={"session_id": "nope"}),
        client.post("/api/fit", json={"session_id": "nope"}),
        client.post("/api/command", json={"session_id": "nope", "text": "all gospel"}),
        client.get("/api/arrangement/nope.mid"),
        client.get("/api/arrangement/nope/practice/0.mid"),
        client.post("/api/upload", files={"file": ("x.txt", b"hi", "text/plain")}),
    ]
    for response in responses:
        assert response.status_code >= 400
        assert "Traceback" not in response.text
        assert "/usr/local" not in response.text


def test_an_evicted_session_fails_gracefully(client):
    """Sessions are capped, so a busy server can drop one mid-edit."""
    from app.main import MAX_SESSIONS

    with open(ROOT / "samples" / "twinkle.musicxml", "rb") as handle:
        data = handle.read()
    mine = client.post(
        "/api/upload", files={"file": ("mine.musicxml", data, "application/xml")}
    ).json()["session_id"]

    for index in range(MAX_SESSIONS + 2):
        client.post("/api/upload", files={"file": (f"o{index}.musicxml", data, "application/xml")})

    response = client.post("/api/arrange", json={"session_id": mine})
    assert response.status_code == 404
    assert "upload" in response.json()["detail"].lower()


def test_a_hostile_title_is_never_reflected_as_markup(client):
    """Titles come from uploaded files, so they are caller-controlled text."""
    from music21 import converter, metadata

    piece = converter.parse("tinyNotation: 4/4 c1")
    piece.insert(0, metadata.Metadata())
    piece.metadata.title = "<script>alert(1)</script>"
    path = write_score(piece)

    with open(path, "rb") as handle:
        analysis = client.post(
            "/api/upload", files={"file": ("evil.musicxml", handle.read(), "application/xml")}
        ).json()

    # The API may carry the text, but it must arrive as data, not as markup in
    # an HTML response. The front-end renders it with textContent.
    assert "<script>" not in client.get("/").text
