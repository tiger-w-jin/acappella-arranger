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
    key, harmony, _ = analyze(source)
    return (source, key, harmony)


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


# ── Rebuilding written lyrics (D) ─────────────────────────────────────────


def test_rebuild_restores_hyphens():
    from app.lyrics import rebuild
    assert rebuild([("A", BEGIN), ("maz", MIDDLE), ("ing", END), ("grace", SINGLE)]) \
        == "A-maz-ing grace"


def test_rebuild_round_trips_through_musicxml(analysed):
    """Words in an uploaded file must come back editable, hyphens intact."""
    from music21 import converter, note as m21note

    from app.lyrics import rebuild

    source, key, harmony = analysed
    written = "A-maz-ing grace how sweet"
    xml = to_musicxml(
        build_arrangement(source, key, harmony, get_ensemble("satb"), {}, lyrics=written)
    )
    reparsed = converter.parse(xml, format="musicxml")
    pairs = [
        (n.lyrics[0].text, n.lyrics[0].syllabic)
        for n in reparsed.parts[0].recurse().notes
        if isinstance(n, m21note.Note) and n.lyrics
    ]
    assert rebuild(pairs).startswith(written)


def test_ingest_captures_syllabic(tmp_path, analysed):
    from app.ingest.score import parse_score_file

    source, key, harmony = analysed
    xml = to_musicxml(
        build_arrangement(source, key, harmony, get_ensemble("satb"), {},
                          lyrics="A-maz-ing grace")
    )
    path = tmp_path / "with-lyrics.musicxml"
    path.write_text(xml)
    reloaded = parse_score_file(str(path))
    carried = [(n.lyric, n.syllabic) for n in reloaded.all_notes if n.lyric]
    assert carried[:3] == [("A", BEGIN), ("maz", MIDDLE), ("ing", END)]


# ── Automatic hyphenation (A) ─────────────────────────────────────────────


@pytest.mark.parametrize("word,expected", [
    ("amazing", "a-ma-zing"), ("twinkle", "twin-kle"), ("little", "lit-tle"),
    ("above", "a-bove"), ("mercy", "mer-cy"), ("only", "on-ly"),
    ("wonder", "won-der"), ("salvation", "sal-va-tion"),
    ("hallelujah", "hal-le-lu-jah"), ("gentle", "gen-tle"), ("table", "ta-ble"),
    # Single-syllable words and silent endings must be left alone.
    ("grace", "grace"), ("sweet", "sweet"), ("sound", "sound"),
    ("saved", "saved"), ("wretch", "wretch"),
])
def test_english_syllabification(word, expected):
    from app.lyrics import syllabify_word
    assert "-".join(syllabify_word(word)) == expected


def test_auto_hyphenate_respects_manual_hyphens():
    from app.lyrics import auto_hyphenate
    assert auto_hyphenate("Twin-kle twinkle") == "Twin-kle twin-kle"


def test_auto_hyphenate_leaves_the_melisma_marker_alone():
    from app.lyrics import auto_hyphenate
    assert auto_hyphenate("sound _ _") == "sound _ _"


def test_auto_hyphenate_keeps_punctuation_and_case():
    from app.lyrics import auto_hyphenate
    assert auto_hyphenate('"Amazing grace," wonderful!') == '"A-ma-zing grace," won-der-ful!'


def test_auto_hyphenate_handles_empty_input():
    from app.lyrics import auto_hyphenate
    assert auto_hyphenate("") == ""
    assert auto_hyphenate("   ").strip() == ""


def test_english_never_needs_pyphen(monkeypatch):
    """English must work with the dependency absent, so a lean clone still runs."""
    import builtins

    from app.lyrics import auto_hyphenate

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "pyphen":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert auto_hyphenate("amazing grace") == "a-ma-zing grace"


def test_available_languages_always_offers_english():
    from app.lyrics import available_languages
    assert available_languages()[0]["id"] == "en"


# ── Alignment layout (B) ──────────────────────────────────────────────────


def test_lyric_layout_has_one_entry_per_melody_note(analysed):
    source, key, harmony = analysed
    arrangement = build_arrangement(
        source, key, harmony, get_ensemble("satb"), {}, lyrics="Twin-kle twin-kle",
    )
    melody = [e for e in arrangement.parts[0] if e.pitch is not None]
    assert len(arrangement.lyric_layout) == len(melody)


def test_lyric_layout_matches_the_melody_part(analysed):
    """The strip is only trustworthy if it mirrors what is actually in the score."""
    source, key, harmony = analysed
    arrangement = build_arrangement(
        source, key, harmony, get_ensemble("satb"), {}, lyrics="Twin-kle twin-kle lit-tle star",
    )
    from_part = [e.lyric for e in arrangement.parts[0] if e.pitch is not None]
    from_layout = [text for _, text in arrangement.lyric_layout]
    assert from_layout == from_part
    assert [bar for bar, _ in arrangement.lyric_layout][:4] == [0, 0, 0, 0]


# ── MIDI lyric track (J) ──────────────────────────────────────────────────


@pytest.mark.parametrize("all_voices", [False, True])
def test_midi_lyric_track_holds_only_the_melody_words(analysed, all_voices):
    from app.export import to_midi_bytes

    source, key, harmony = analysed
    arrangement = build_arrangement(
        source, key, harmony, get_ensemble("satb"), {},
        lyrics="Twin-kle twin-kle lit-tle star",
        lyrics_all_voices=all_voices,
    )
    midi = to_midi_bytes(arrangement)
    assert midi.count(b"\xff\x05") == 7  # the sung syllables, not the backing "Ah"
    assert b"Ah" not in midi


def test_score_still_shows_backing_syllables(analysed):
    """Trimming the MIDI lyric track must not strip the score's own syllables."""
    source, key, harmony = analysed
    xml = to_musicxml(build_arrangement(
        source, key, harmony, get_ensemble("satb"), {}, lyrics="Twin-kle twin-kle",
    ))
    assert "<text>Ah</text>" in xml


def test_a_multi_verse_score_yields_one_readable_verse():
    """Several verses must not be interleaved into a single mangled line.

    music21 stacks one Lyric per verse on each note and its `.lyric` shortcut
    joins them with newlines, which silently produced "Wer / den nur / wird
    den" — verse one and verse two zipped together.
    """
    from app.ingest.score import parse_score_file
    from app.lyrics import rebuild

    chorale = Path(__file__).resolve().parents[1] / "samples" / "chorale_satb.musicxml"
    source = parse_score_file(str(chorale))
    words = rebuild([(n.lyric or "", n.syllabic) for n in source.all_notes if n.lyric])

    assert "\n" not in words
    assert words.startswith("Wer nur den lie-ben Gott")
    # Every syllable belongs to exactly one verse.
    assert all("\n" not in (n.lyric or "") for n in source.all_notes)
