"""Using the harmony a file already has, fitting to real voices, and practice tracks."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analysis import analyze  # noqa: E402
from app.export import to_midi_bytes, to_practice_midi  # noqa: E402
from app.fit import find_fit  # noqa: E402
from app.harmony.arranger import build_arrangement  # noqa: E402
from app.harmony.styles import ENSEMBLES, get_ensemble  # noqa: E402
from app.ingest.score import parse_score_file  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MELODY = ROOT / "samples" / "twinkle.musicxml"
CHORALE = ROOT / "samples" / "chorale_satb.musicxml"


@pytest.fixture(scope="module")
def melody_only():
    return parse_score_file(str(MELODY))


@pytest.fixture(scope="module")
def chorale():
    return parse_score_file(str(CHORALE))


# ── Reading harmony the file already states ───────────────────────────────


def test_single_line_has_no_texture_to_read(melody_only):
    assert melody_only.texture == []
    assert melody_only.source_chords == []
    _, _, used = analyze(melody_only)
    assert used == "inferred"


def test_four_part_score_yields_texture(chorale):
    assert len(chorale.texture) > 0
    _, _, used = analyze(chorale)
    assert used == "texture"


def test_texture_weights_cannot_exceed_what_is_sounding(chorale):
    """A tied note counted twice would skew the chord away from the truth."""
    for sonority in chorale.texture:
        assert sum(sonority.weights.values()) <= sonority.duration * 4 + 1e-6
        assert sonority.bass_pc is None or 0 <= sonority.bass_pc < 12


def test_reading_the_parts_beats_guessing_from_the_melody(chorale):
    """The whole point: the file's own harmony should differ from a re-guess."""
    _, from_texture, used_a = analyze(chorale, harmony_source="auto")
    _, inferred, used_b = analyze(chorale, harmony_source="infer")
    assert (used_a, used_b) == ("texture", "inferred")
    assert [b.symbol for b in from_texture] != [b.symbol for b in inferred]


def test_forcing_inference_ignores_the_texture(chorale):
    _, _, used = analyze(chorale, harmony_source="infer")
    assert used == "inferred"


def test_key_detection_uses_the_full_texture(chorale):
    key, _, _ = analyze(chorale)
    assert key.name in ("B minor", "D major")  # bwv84.5 sits here


# ── Harmonic rhythm ───────────────────────────────────────────────────────


@pytest.mark.parametrize("chords_per_bar,expected", [(1, 1), (2, 2), (4, 4)])
def test_four_four_subdivides_as_asked(melody_only, chords_per_bar, expected):
    _, harmony, _ = analyze(melody_only, chords_per_bar=chords_per_bar)
    assert max(len(bar.segments) for bar in harmony) <= expected


def test_triple_metre_never_splits_in_two(tmp_path):
    """Two chords in a 3/4 bar would fall across the beat, not on it."""
    from music21 import converter

    piece = converter.parse("tinyNotation: 3/4 g4 b4 d'4 c'4 b4 a4 b2.")
    path = tmp_path / "waltz.musicxml"
    piece.write("musicxml", fp=str(path))
    source = parse_score_file(str(path))

    for chords_per_bar in (2, 4):
        _, harmony, _ = analyze(source, chords_per_bar=chords_per_bar)
        counts = {len(bar.segments) for bar in harmony}
        assert 2 not in counts, f"3/4 split in two at chords_per_bar={chords_per_bar}"


# ── Fitting to real voices ────────────────────────────────────────────────


@pytest.fixture(scope="module")
def analysed(melody_only):
    key, harmony, _ = analyze(melody_only)
    return melody_only, key, harmony


def test_fit_ranks_every_combination(analysed):
    source, key, harmony = analysed
    ranked = find_fit(source, key, harmony, {}, "satb_chorale", transpose_range=2)
    assert len(ranked) == len(ENSEMBLES) * 5      # 5 transpositions each
    assert ranked == sorted(ranked, key=lambda r: r.score)


def test_fit_prefers_fewer_unsingable_notes(analysed):
    source, key, harmony = analysed
    ranked = find_fit(source, key, harmony, {}, "satb_chorale", transpose_range=3)
    best = ranked[0]
    assert best.out_of_range <= min(r.out_of_range for r in ranked)


def test_fit_can_be_held_to_the_original_key(analysed):
    source, key, harmony = analysed
    ranked = find_fit(source, key, harmony, {}, "satb_chorale", keep_key=True)
    assert {r.transpose for r in ranked} == {0}


def test_fit_breaks_ties_toward_not_moving_the_music(analysed):
    """Between two equally comfortable settings, the smaller move should win.

    Comfort still leads — a materially easier key beats staying put — but a
    negligible gain should not reprint everyone's music in a new key.
    """
    source, key, harmony = analysed
    ranked = find_fit(source, key, harmony, {}, "satb_chorale", ensembles=["satb"])
    by_shift = {r.transpose: r for r in ranked}

    for shift in (1, 2, 3):
        here, there = by_shift[0], by_shift.get(shift)
        if there is None:
            continue
        if there.out_of_range == here.out_of_range and abs(there.strain - here.strain) < 1e-4:
            assert here.score < there.score, f"+{shift} should not beat an equal home key"


def test_fit_still_moves_the_key_when_it_genuinely_helps(analysed):
    """A setting with unsingable notes must lose to one without them."""
    source, key, harmony = analysed
    ranked = find_fit(source, key, harmony, {}, "satb_chorale", ensembles=["ssaa"])
    clean = [r for r in ranked if r.out_of_range == 0]
    dirty = [r for r in ranked if r.out_of_range > 0]
    if clean and dirty:
        assert min(r.score for r in clean) < min(r.score for r in dirty)


def test_fit_summary_is_readable(analysed):
    source, key, harmony = analysed
    best = find_fit(source, key, harmony, {}, "satb_chorale", transpose_range=2)[0]
    assert best.ensemble_name
    assert "range" in best.summary


# ── Practice tracks ───────────────────────────────────────────────────────


def test_each_part_gets_a_different_practice_mix(analysed):
    source, key, harmony = analysed
    ensemble = get_ensemble("satb")
    arrangement = build_arrangement(source, key, harmony, ensemble, {})

    plain = to_midi_bytes(arrangement)
    tracks = [to_practice_midi(arrangement, i) for i in range(ensemble.size)]

    assert all(track != plain for track in tracks)
    assert len(set(tracks)) == ensemble.size, "two parts produced identical mixes"


def test_practice_track_keeps_every_part_audible(analysed):
    """Part-dominant, not solo: you need to hear how your line sits."""
    from app.export import PRACTICE_BACKGROUND, PRACTICE_FOREGROUND, build_music21_score

    source, key, harmony = analysed
    ensemble = get_ensemble("satb")
    arrangement = build_arrangement(source, key, harmony, ensemble, {})
    score = build_music21_score(arrangement, with_chord_symbols=False, practice_voice=1)

    velocities = [
        {n.volume.velocity for n in part.recurse().notes} for part in score.parts
    ]
    assert velocities[1] == {PRACTICE_FOREGROUND}
    for index, found in enumerate(velocities):
        if index != 1:
            assert found == {PRACTICE_BACKGROUND}
            assert PRACTICE_BACKGROUND > 0, "other parts must stay audible"


# ── Projects: work must survive a reload ──────────────────────────────────


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _project_from(analysis: dict) -> dict:
    return {
        "title": analysis["title"],
        "source_kind": analysis["source_kind"],
        "tempo": analysis["tempo"],
        "bars": [
            {
                "beats": bar["beats"],
                "beat_type": bar["beat_type"],
                "length": bar["beats"] * (4 / bar["beat_type"]),
                "melody": bar["melody"],
                "chord": bar["chord"],
                "roman": bar["roman"],
            }
            for bar in analysis["bars"]
        ],
    }


def test_a_project_round_trips_without_the_original_file(client):
    """The whole point: rebuild a session from saved state, no upload needed."""
    with open(MELODY, "rb") as handle:
        uploaded = client.post(
            "/api/upload", files={"file": ("twinkle.musicxml", handle, "application/xml")}
        ).json()

    restored = client.post("/api/restore", json=_project_from(uploaded)).json()

    assert restored["session_id"] != uploaded["session_id"]
    assert restored["bar_count"] == uploaded["bar_count"]
    assert restored["key"] == uploaded["key"]
    assert [b["melody"] for b in restored["bars"]] == [b["melody"] for b in uploaded["bars"]]


def test_a_restored_session_can_be_arranged(client):
    with open(MELODY, "rb") as handle:
        uploaded = client.post(
            "/api/upload", files={"file": ("twinkle.musicxml", handle, "application/xml")}
        ).json()
    restored = client.post("/api/restore", json=_project_from(uploaded)).json()

    arranged = client.post(
        "/api/arrange", json={"session_id": restored["session_id"], "ensemble": "satb"}
    )
    assert arranged.status_code == 200
    assert arranged.json()["musicxml"].startswith("<?xml")


def test_restore_rejects_an_empty_project(client):
    assert client.post("/api/restore", json={"bars": []}).status_code == 400


def test_restore_rejects_a_project_with_no_notes(client):
    payload = {
        "bars": [{"beats": 4, "beat_type": 4, "length": 4.0, "melody": [], "chord": "C", "roman": "I"}]
    }
    assert client.post("/api/restore", json=payload).status_code == 422


def test_fit_endpoint_needs_a_live_session(client):
    assert client.post("/api/fit", json={"session_id": "nope"}).status_code == 404


def test_practice_endpoint_rejects_an_unknown_voice(client):
    with open(MELODY, "rb") as handle:
        uploaded = client.post(
            "/api/upload", files={"file": ("twinkle.musicxml", handle, "application/xml")}
        ).json()
    arranged = client.post(
        "/api/arrange", json={"session_id": uploaded["session_id"], "ensemble": "satb"}
    ).json()
    aid = arranged["arrangement_id"]

    assert client.get(f"/api/arrangement/{aid}/practice/0.mid").status_code == 200
    assert client.get(f"/api/arrangement/{aid}/practice/99.mid").status_code == 404
    assert client.get("/api/arrangement/nope/practice/0.mid").status_code == 404


def test_harmony_source_is_reported_to_the_client(client):
    with open(CHORALE, "rb") as handle:
        chorale = client.post(
            "/api/upload", files={"file": ("chorale.musicxml", handle, "application/xml")}
        ).json()
    assert chorale["harmony_source"] == "texture"

    with open(MELODY, "rb") as handle:
        melody = client.post(
            "/api/upload", files={"file": ("twinkle.musicxml", handle, "application/xml")}
        ).json()
    assert melody["harmony_source"] == "inferred"
