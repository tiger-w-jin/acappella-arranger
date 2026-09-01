"""Lyric parsing and how words land on the melody."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analysis import analyze  # noqa: E402
from app.export import to_musicxml  # noqa: E402
from app.harmony.arranger import build_arrangement  # noqa: E402
from app.harmony.styles import get_ensemble  # noqa: E402
from app.ingest.score import parse_score_file  # noqa: E402
from app.lyrics import BEGIN, END, MIDDLE, SINGLE, fit, parse_lyrics  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "twinkle.musicxml"


def texts(syllables):
    return [s.text for s in syllables]


def kinds(syllables):
    return [s.syllabic for s in syllables]


# ── Parsing ───────────────────────────────────────────────────────────────


def test_plain_words_are_single_syllables():
    parsed = parse_lyrics("how sweet the sound")
    assert texts(parsed) == ["how", "sweet", "the", "sound"]
    assert kinds(parsed) == [SINGLE] * 4


def test_hyphens_split_a_word_across_notes():
    parsed = parse_lyrics("A-ma-zing grace")
    assert texts(parsed) == ["A", "ma", "zing", "grace"]
    assert kinds(parsed) == [BEGIN, MIDDLE, END, SINGLE]


def test_two_part_word_is_begin_then_end():
    assert kinds(parse_lyrics("Twin-kle")) == [BEGIN, END]


def test_underscore_holds_a_note_without_printing():
    parsed = parse_lyrics("sound _ _")
    assert texts(parsed) == ["sound", "", ""]


def test_messy_input_is_tolerated():
    assert texts(parse_lyrics("  a--b   c-  ")) == ["a", "b", "c"]


def test_empty_lyrics():
    assert parse_lyrics("") == []
    assert parse_lyrics("   ") == []


# ── Fitting to a melody ───────────────────────────────────────────────────


def test_fit_pads_when_there_are_more_notes_than_syllables():
    assigned, warnings = fit("one two", 5)
    assert [s.text if s else None for s in assigned] == ["one", "two", None, None, None]
    assert warnings and "5" in warnings[0]


def test_fit_reports_when_lyrics_overrun_the_melody():
    assigned, warnings = fit("a b c d", 2)
    assert len(assigned) == 2
    assert warnings and "left off" in warnings[0]


def test_fit_is_silent_when_the_counts_match():
    assigned, warnings = fit("one two three", 3)
    assert warnings == []
    assert [s.text for s in assigned] == ["one", "two", "three"]


def test_underscore_leaves_a_note_blank():
    assigned, _ = fit("hold _", 2)
    assert assigned[0].text == "hold"
    assert assigned[1] is None


# ── Through the arranger and into MusicXML ────────────────────────────────


@pytest.fixture(scope="module")
def analysed():
    source = parse_score_file(str(SAMPLE))
    return (source, *analyze(source))


def test_words_reach_the_melody_part(analysed):
    source, key, harmony = analysed
    arrangement = build_arrangement(
        source, key, harmony, get_ensemble("satb"), {},
        lyrics="Twin-kle twin-kle lit-tle star",
    )
    melody = arrangement.parts[0]
    assert [(e.lyric, e.syllabic) for e in melody[:4]] == [
        ("Twin", BEGIN), ("kle", END), ("twin", BEGIN), ("kle", END),
    ]


def test_backing_parts_keep_their_syllable_by_default(analysed):
    source, key, harmony = analysed
    arrangement = build_arrangement(
        source, key, harmony, get_ensemble("satb"), {}, lyrics="Twin-kle twin-kle",
    )
    words = {e.lyric for part in arrangement.parts[1:] for e in part if e.lyric}
    assert "Twin" not in words
    assert "Ah" in words  # the chorale's own syllable


def test_all_voices_option_puts_words_under_every_part(analysed):
    source, key, harmony = analysed
    arrangement = build_arrangement(
        source, key, harmony, get_ensemble("satb"), {},
        lyrics="Twin-kle twin-kle lit-tle star",
        lyrics_all_voices=True,
    )
    alto = [e.lyric for e in arrangement.parts[1] if e.lyric]
    assert alto[:2] == ["Twin", "kle"]


def test_musicxml_carries_syllabic_so_hyphens_render(analysed):
    source, key, harmony = analysed
    arrangement = build_arrangement(
        source, key, harmony, get_ensemble("satb"), {}, lyrics="Twin-kle twin-kle",
    )
    xml = to_musicxml(arrangement)
    assert "<syllabic>begin</syllabic>" in xml
    assert "<syllabic>end</syllabic>" in xml
    assert "<text>Twin</text>" in re.sub(r"\s+", "", xml).replace("><", ">\n<") or "Twin" in xml


def test_mismatched_lyrics_surface_as_a_warning(analysed):
    source, key, harmony = analysed
    arrangement = build_arrangement(
        source, key, harmony, get_ensemble("satb"), {}, lyrics="just three words here",
    )
    assert any("syllable" in w for w in arrangement.warnings)


def test_no_lyrics_leaves_the_arrangement_untouched(analysed):
    source, key, harmony = analysed
    without = build_arrangement(source, key, harmony, get_ensemble("satb"), {})
    assert not any(e.syllabic for e in without.parts[0])
