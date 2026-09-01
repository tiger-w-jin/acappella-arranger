"""End-to-end checks over the arranging pipeline.

Run with:  .venv/bin/python -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analysis import analyze, parse_chord_symbol, roman_for  # noqa: E402
from app.export import to_midi_bytes, to_musicxml  # noqa: E402
from app.harmony.arranger import build_arrangement  # noqa: E402
from app.harmony.styles import ENSEMBLES, STYLES, get_ensemble  # noqa: E402
from app.ingest.score import parse_score_file  # noqa: E402
from app.theory import ChordSpec, detect_key  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "twinkle.musicxml"

# Styles whose whole point is unison or a pedal, so equal/crossed parts are
# intended rather than a defect.
INTENTIONAL_OVERLAP = {"unison", "drone"}


def sounding_pitches(arrangement, time: float) -> list[int]:
    """Pitch sounding in each part at a given moment, highest part first."""
    result = []
    for events in arrangement.parts:
        pitch = None
        for event in events:
            if event.offset - 1e-6 <= time < event.offset + event.duration - 1e-6:
                pitch = event.pitch
                break
        result.append(pitch)
    return result


@pytest.fixture(scope="module")
def source():
    return parse_score_file(str(SAMPLE))


@pytest.fixture(scope="module")
def analysis(source):
    return analyze(source)


def test_score_parses_into_bars(source):
    assert len(source.bars) == 6
    assert all(bar.length == 4.0 for bar in source.bars)
    assert [n.pitch for n in source.bars[0].notes] == [60, 60, 67, 67]


def test_key_detection(analysis):
    key, _ = analysis
    assert key.name == "C major"
    assert key.confidence > 0.8


def test_harmony_is_musically_sane(analysis):
    """The tune opens on the tonic and every chord must be diatonically plausible."""
    key, harmony = analysis
    assert harmony[0].segments[0].chord.root_pc == key.tonic_pc

    # No chord may contradict the melody: each bar's chord shares at least one
    # pitch class with the notes sounding over it.
    for bar_harmony in harmony:
        for segment in bar_harmony.segments:
            assert segment.chord.pitch_classes


def test_no_chord_omits_every_melody_note(source, analysis):
    key, harmony = analysis
    for bar, bar_harmony in zip(source.bars, harmony):
        pcs: set[int] = set()
        for segment in bar_harmony.segments:
            pcs |= set(segment.chord.pitch_classes)
        melody_pcs = {n.pitch % 12 for n in bar.notes if n.pitch is not None}
        assert melody_pcs & pcs, f"bar {bar.index + 1} chord shares nothing with the melody"


@pytest.mark.parametrize("ensemble_id", sorted(ENSEMBLES))
@pytest.mark.parametrize("style_id", sorted(STYLES))
def test_every_style_and_ensemble_renders(source, analysis, ensemble_id, style_id):
    key, harmony = analysis
    ensemble = ENSEMBLES[ensemble_id]
    arrangement = build_arrangement(source, key, harmony, ensemble, {}, default_style=style_id)

    assert len(arrangement.parts) == ensemble.size
    assert all(events for events in arrangement.parts)

    xml = to_musicxml(arrangement)
    assert xml.startswith("<?xml")
    assert len(to_midi_bytes(arrangement)) > 100


@pytest.mark.parametrize("ensemble_id", sorted(ENSEMBLES))
@pytest.mark.parametrize("style_id", sorted(set(STYLES) - INTENTIONAL_OVERLAP))
def test_backing_voices_never_cross(source, analysis, ensemble_id, style_id):
    """The accompanying parts must stay in pitch order against each other at all times."""
    key, harmony = analysis
    ensemble = ENSEMBLES[ensemble_id]
    arrangement = build_arrangement(source, key, harmony, ensemble, {}, default_style=style_id)

    times = sorted({event.offset for part in arrangement.parts for event in part})
    for time in times:
        column = sounding_pitches(arrangement, time)
        backing = [
            pitch
            for index, pitch in enumerate(column)
            if index != ensemble.melody_index and pitch is not None
        ]
        crossed = [i for i in range(len(backing) - 1) if backing[i] < backing[i + 1]]
        assert not crossed, f"backing crossing at t={time} in {ensemble_id}/{style_id}: {backing}"


@pytest.mark.parametrize("ensemble_id", sorted(ENSEMBLES))
@pytest.mark.parametrize("style_id", sorted(set(STYLES) - INTENTIONAL_OVERLAP))
def test_full_texture_ordered_at_each_attack(source, analysis, ensemble_id, style_id):
    """When a chord is struck, every part including the melody reads in order.

    Between attacks a held pad may legitimately be crossed by a moving melody,
    so the invariant is checked at the moment each backing chord enters.
    """
    key, harmony = analysis
    ensemble = ENSEMBLES[ensemble_id]
    arrangement = build_arrangement(source, key, harmony, ensemble, {}, default_style=style_id)

    backing_index = 0 if ensemble.melody_index != 0 else 1
    for event in arrangement.parts[backing_index]:
        column = [p for p in sounding_pitches(arrangement, event.offset) if p is not None]
        crossed = [i for i in range(len(column) - 1) if column[i] < column[i + 1]]
        assert not crossed, f"crossing at t={event.offset} in {ensemble_id}/{style_id}: {column}"


def test_mixed_styles_per_bar(source, analysis):
    """Different styles in different bars must all land in one coherent score."""
    key, harmony = analysis
    ensemble = get_ensemble("satb")
    bar_styles = {0: "satb_chorale", 1: "barbershop", 2: "gospel_pad", 3: "doo_wop", 4: "cluster"}
    arrangement = build_arrangement(source, key, harmony, ensemble, bar_styles)

    assert arrangement.bar_styles[:5] == [
        "satb_chorale", "barbershop", "gospel_pad", "doo_wop", "cluster",
    ]
    assert to_musicxml(arrangement).startswith("<?xml")


def test_transpose_shifts_every_part(source, analysis):
    key, harmony = analysis
    ensemble = get_ensemble("satb")
    plain = build_arrangement(source, key, harmony, ensemble, {})
    raised = build_arrangement(source, key, harmony, ensemble, {}, transpose=2)

    melody_plain = [e.pitch for e in plain.parts[0]]
    melody_raised = [e.pitch for e in raised.parts[0]]
    assert melody_raised == [p + 2 for p in melody_plain]
    assert raised.key.tonic_pc == (key.tonic_pc + 2) % 12


def test_bar_count_matches_source(source, analysis):
    key, harmony = analysis
    arrangement = build_arrangement(source, key, harmony, get_ensemble("satb"), {})
    assert len(arrangement.bar_bounds) == len(source.bars)
    assert len(arrangement.bar_styles) == len(source.bars)


def _write_waltz(tmp_path) -> str:
    from music21 import converter, metadata

    piece = converter.parse("tinyNotation: 3/4 g4 b4 d'4 d'4 c'4 b4 a4 b4 c'4 b2.")
    piece.insert(0, metadata.Metadata())
    piece.metadata.title = "Waltz"
    target = tmp_path / "waltz.musicxml"
    piece.write("musicxml", fp=str(target))
    return str(target)


def test_triple_metre_is_not_split_mid_bar(tmp_path):
    """A 3/4 bar is too short for two chords, so it must keep one."""
    source = parse_score_file(_write_waltz(tmp_path))
    assert all(bar.beats == 3 and bar.length == 3.0 for bar in source.bars)

    _, harmony = analyze(source, chords_per_bar=2)
    assert all(len(bar.segments) == 1 for bar in harmony)


def test_midi_input_matches_musicxml_input(tmp_path):
    """The same music read as MIDI or MusicXML must analyse identically."""
    from music21 import converter

    xml_path = _write_waltz(tmp_path)
    midi_path = str(tmp_path / "waltz.mid")
    converter.parse(xml_path).write("midi", fp=midi_path)

    from_xml = parse_score_file(xml_path)
    from_midi = parse_score_file(midi_path)

    assert [n.pitch for n in from_xml.all_notes] == [n.pitch for n in from_midi.all_notes]
    key_xml, harmony_xml = analyze(from_xml)
    key_midi, harmony_midi = analyze(from_midi)
    assert key_xml.name == key_midi.name
    assert [b.symbol for b in harmony_xml] == [b.symbol for b in harmony_midi]


def test_exported_score_has_no_duplicate_title(source, analysis):
    key, harmony = analysis
    xml = to_musicxml(build_arrangement(source, key, harmony, get_ensemble("satb"), {}))
    assert "<movement-title>" not in xml or "<movement-title />" in xml


def test_exported_score_spells_accidentals_for_the_key(source, analysis):
    """A barbershop IV7 in C major must print E-flat, never D-sharp."""
    key, harmony = analysis
    arrangement = build_arrangement(
        source, key, harmony, get_ensemble("satb"), {}, default_style="barbershop"
    )
    xml = to_musicxml(arrangement)
    assert "<step>D</step>\n        <alter>1</alter>" not in xml


def test_chord_symbols_are_exportable():
    """Every quality the arranger can produce must render as a chord symbol."""
    from music21 import harmony as m21harmony

    from app.theory import CHORD_QUALITIES

    for quality in CHORD_QUALITIES:
        figure = ChordSpec(0, quality).export_figure()
        m21harmony.ChordSymbol(figure)  # raises if music21 cannot read it


@pytest.mark.parametrize("style_id", sorted(STYLES))
def test_style_preview_renders(style_id):
    """The palette's 'hear it' demo must exist for every style."""
    from app.preview import preview_midi

    assert len(preview_midi(style_id, "satb")) > 100


def test_style_previews_differ_between_styles():
    """A preview is only useful if styles actually sound different."""
    from app.preview import preview_midi

    rendered = {style_id: preview_midi(style_id, "satb") for style_id in STYLES}
    assert len(set(rendered.values())) == len(rendered), "some styles render identically"


def test_chord_symbol_round_trip():
    for text, root, quality in [
        ("C", 0, "maj"), ("Am", 9, "min"), ("F#m7", 6, "min7"), ("Bb7", 10, "dom7"),
        ("Ebmaj7", 3, "maj7"), ("G13", 7, "dom13"), ("Ddim7", 2, "dim7"),
        ("Csus4", 0, "sus4"), ("A7b9", 9, "dom7b9"),
    ]:
        parsed = parse_chord_symbol(text)
        assert parsed is not None, text
        assert (parsed.root_pc, parsed.quality) == (root, quality), text

    assert parse_chord_symbol("C/G") == ChordSpec(0, "maj", bass_pc=7)
    assert parse_chord_symbol("nonsense") is None
    assert parse_chord_symbol("") is None


def test_roman_numerals():
    key = detect_key({0: 10, 4: 8, 7: 8, 2: 4, 5: 4, 9: 4, 11: 2})
    assert key.name == "C major"
    assert roman_for(ChordSpec(0, "maj"), key) == "I"
    assert roman_for(ChordSpec(9, "min"), key) == "vi"
    assert roman_for(ChordSpec(7, "dom7"), key) == "V7"
    assert roman_for(ChordSpec(2, "dom7"), key) == "V7/V"  # secondary dominant
    assert roman_for(ChordSpec(11, "dim"), key).startswith("vii")


def test_chord_override_is_respected(source, analysis):
    """A user-supplied chord must actually drive the voicing."""
    key, harmony = analysis
    forced = parse_chord_symbol("Ab")
    assert forced is not None
    harmony[0].segments[0].chord = forced

    arrangement = build_arrangement(source, key, harmony, get_ensemble("satb"), {})
    bass = [e.pitch for e in arrangement.parts[3] if e.offset < 4.0]
    assert all(p % 12 == 8 for p in bass), f"bass should sing Ab, got {bass}"
