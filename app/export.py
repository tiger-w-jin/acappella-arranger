"""Render an Arrangement to a music21 score, then to MusicXML and MIDI.

Measures are built explicitly rather than via makeNotation() so the exported
barlines line up exactly with the bars the user saw and styled in the UI,
including an anacrusis.
"""

from __future__ import annotations

import io
from fractions import Fraction

from music21 import (
    bar as m21bar,
    clef,
    expressions,
    harmony,
    instrument,
    key as m21key,
    layout,
    metadata,
    meter,
    note,
    stream,
    tempo,
    tie,
)

from .harmony.arranger import Arrangement, VoiceEvent
from .theory import ChordSpec, KeyContext, pitch_name, spell_pitch

_CLEFS = {
    "treble": clef.TrebleClef,
    "bass": clef.BassClef,
    "treble8vb": clef.Treble8vbClef,
}


def _make_note(pitch: int, duration: float, key: KeyContext) -> note.Note:
    step, alter, octave = spell_pitch(pitch, key)
    accidental = {1: "#", -1: "-"}.get(alter, "")
    item = note.Note(f"{step}{accidental}{octave}")
    item.duration.quarterLength = Fraction(duration).limit_denominator(48)
    return item


def _key_signature(arrangement: Arrangement) -> m21key.Key:
    tonic = pitch_name(60 + arrangement.key.tonic_pc, arrangement.key.prefer_flats)
    name = tonic[0] + ("#" if tonic[1] == 1 else "-" if tonic[1] == -1 else "")
    return m21key.Key(name, arrangement.key.mode)


# Velocities for a practice track: loud enough to pick your line out, quiet
# enough to still hear how it fits against the others.
PRACTICE_FOREGROUND = 112
PRACTICE_BACKGROUND = 42


def build_music21_score(
    arrangement: Arrangement,
    with_chord_symbols: bool = True,
    lyrics_on_melody_only: bool = False,
    practice_voice: int | None = None,
) -> stream.Score:
    score = stream.Score()
    score.insert(0, metadata.Metadata())
    score.metadata.title = arrangement.title
    # music21 mirrors the title into movement-title when it is unset, which
    # notation programs then print a second time as a subtitle.
    score.metadata.movementName = ""
    score.metadata.composer = f"A cappella arrangement \u00b7 {arrangement.ensemble.name}"

    time_signature_text = f"{arrangement.beats}/{arrangement.beat_type}"
    full_bar = arrangement.beats * (4.0 / arrangement.beat_type)

    for voice_index, voice in enumerate(arrangement.ensemble.voices):
        part = stream.Part(id=voice.abbreviation)
        part.partName = voice.name
        part.partAbbreviation = voice.abbreviation
        vocal = instrument.Vocalist()
        vocal.midiProgram = 52  # Choir Aahs, so MIDI previews sound like voices
        part.insert(0, vocal)

        events = arrangement.parts[voice_index]
        is_melody = voice_index == arrangement.ensemble.melody_index
        velocity = None
        if practice_voice is not None:
            velocity = (
                PRACTICE_FOREGROUND if voice_index == practice_voice else PRACTICE_BACKGROUND
            )

        for bar_index, (bar_start, bar_length) in enumerate(arrangement.bar_bounds):
            measure = stream.Measure(number=bar_index + 1)
            if bar_index == 0:
                measure.insert(0, _CLEFS.get(voice.clef, clef.TrebleClef)())
                measure.insert(0, _key_signature(arrangement))
                measure.insert(0, meter.TimeSignature(time_signature_text))
                if voice_index == 0:
                    measure.insert(0, tempo.MetronomeMark(number=round(arrangement.tempo)))
                if bar_length < full_bar - 1e-6:
                    measure.paddingLeft = full_bar - bar_length

            if with_chord_symbols and is_melody and bar_index < len(arrangement.bar_chords):
                for offset, chord_spec in arrangement.bar_chords[bar_index]:
                    _insert_chord_symbol(measure, chord_spec, offset, arrangement.key)

            _fill_measure(
                measure,
                [e for e in events if bar_start - 1e-6 <= e.offset < bar_start + bar_length - 1e-6],
                bar_start,
                bar_length,
                arrangement.key,
                with_lyrics=not lyrics_on_melody_only or is_melody,
                velocity=velocity,
            )

            if bar_index == len(arrangement.bar_bounds) - 1:
                measure.rightBarline = m21bar.Barline("final")
            part.append(measure)

        score.insert(0, part)

    _apply_ties(score, arrangement)
    try:
        score.makeAccidentals(inPlace=True)
    except Exception:  # pragma: no cover - cosmetic only
        pass

    staff_group = layout.StaffGroup(
        list(score.parts), name=arrangement.ensemble.name, symbol="bracket", barTogether=True
    )
    score.insert(0, staff_group)
    return score


def _insert_chord_symbol(
    measure: stream.Measure, chord_spec: ChordSpec, offset: float, key: KeyContext
) -> None:
    """Put a chord symbol over the staff, falling back to plain text if needed."""
    prefer_flats = key.prefer_flats
    try:
        chord_symbol = harmony.ChordSymbol(chord_spec.export_figure(prefer_flats))
        chord_symbol.writeAsChord = False
        measure.insert(offset, chord_symbol)
    except Exception:
        measure.insert(offset, expressions.TextExpression(chord_spec.symbol(prefer_flats)))


def _fill_measure(
    measure: stream.Measure,
    events: list[VoiceEvent],
    bar_start: float,
    bar_length: float,
    key: KeyContext,
    with_lyrics: bool = True,
    velocity: int | None = None,
) -> None:
    cursor = bar_start
    for event in sorted(events, key=lambda e: e.offset):
        if event.pitch is None:
            continue
        if event.offset > cursor + 1e-6:
            measure.insert(cursor - bar_start, note.Rest(quarterLength=event.offset - cursor))
            cursor = event.offset
        duration = min(event.duration, bar_start + bar_length - event.offset)
        if duration <= 1e-6:
            continue
        item = _make_note(event.pitch, duration, key)
        if event.lyric and with_lyrics:
            # syllabic drives the hyphens joining a word split across notes.
            item.lyrics = [note.Lyric(text=event.lyric, syllabic=event.syllabic or "single")]
        if velocity is not None:
            item.volume.velocity = velocity
        if event.tied_from_previous:
            item.tie = tie.Tie("stop")
        measure.insert(event.offset - bar_start, item)
        cursor = event.offset + duration

    remaining = bar_start + bar_length - cursor
    if remaining > 1e-6:
        measure.insert(cursor - bar_start, note.Rest(quarterLength=remaining))


def _apply_ties(score: stream.Score, arrangement: Arrangement) -> None:
    """Give every 'stop' tie a matching 'start' on the note before it."""
    for part in score.parts:
        previous: note.Note | None = None
        for item in part.recurse().notes:
            if not isinstance(item, note.Note):
                previous = None
                continue
            if item.tie is not None and item.tie.type == "stop" and previous is not None:
                if previous.pitch.midi == item.pitch.midi:
                    if previous.tie is not None and previous.tie.type == "stop":
                        previous.tie = tie.Tie("continue")
                    else:
                        previous.tie = tie.Tie("start")
                else:
                    item.tie = None
            previous = item


def to_musicxml(arrangement: Arrangement) -> str:
    score = build_music21_score(arrangement, with_chord_symbols=True)
    from music21.musicxml.m21ToXml import GeneralObjectExporter

    raw = GeneralObjectExporter().parse(score)
    return raw.decode("utf-8")


def to_practice_midi(arrangement: Arrangement, voice_index: int) -> bytes:
    """MIDI with one part brought forward, for learning that line.

    Choirs learn from part-dominant recordings, so the other voices stay
    audible rather than being muted -- you need to hear how your line sits.
    """
    from music21.midi.translate import streamToMidiFile

    score = build_music21_score(
        arrangement,
        with_chord_symbols=False,
        lyrics_on_melody_only=True,
        practice_voice=voice_index,
    )
    return streamToMidiFile(score).writestr()


def to_midi_bytes(arrangement: Arrangement) -> bytes:
    """MIDI, whose lyric track is conventionally the sung text and nothing else.

    Backing syllables belong in the score but would mislead a karaoke player or
    a DAW lyric view, so they are left out here even when the score shows words
    on every part.
    """
    score = build_music21_score(
        arrangement, with_chord_symbols=False, lyrics_on_melody_only=True
    )
    from music21.midi.translate import streamToMidiFile

    midi_file = streamToMidiFile(score)
    buffer = io.BytesIO()
    buffer.write(midi_file.writestr())
    return buffer.getvalue()
