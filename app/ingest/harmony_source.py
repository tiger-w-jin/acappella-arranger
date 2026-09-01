"""Read the harmony a file already contains, instead of guessing it back.

A lead sheet carries chord symbols. A hymn or chorale carries four real parts.
Both state the harmony outright, and inferring it from the melody alone throws
that away and gets a different answer — on Bach's BWV 66.6 the opening is A
major and F# minor, while melody-only inference reads it as C#7 and F#m.

So there are three grades of evidence, used in this order:

1. explicit chord symbols  — believe them
2. two or more parts       — read the chords off the actual texture
3. a single line           — infer, which is all you can do for audio

This module produces the first two. `analysis.py` owns the third.
"""

from __future__ import annotations

from dataclasses import dataclass

from music21 import chord as m21chord, harmony, note as m21note, stream

from ..theory import CHORD_QUALITIES, ChordSpec


@dataclass
class SourceChord:
    """A chord the file stated, in quarter-length offsets from the start."""

    offset: float
    duration: float
    chord: ChordSpec


@dataclass
class Sonority:
    """What is actually sounding over a span, taken from every part at once."""

    offset: float
    duration: float
    weights: dict[int, float]  # pitch class -> duration sounding
    bass_pc: int | None


def _to_float(value) -> float:
    return float(value)


# --------------------------------------------------------------------------
# 1. Explicit chord symbols
# --------------------------------------------------------------------------

# music21 names chord kinds in its own vocabulary; map the ones we can voice.
_KIND_TO_QUALITY = {
    "major": "maj", "minor": "min", "augmented": "aug", "diminished": "dim",
    "dominant": "dom7", "dominant-seventh": "dom7",
    "major-seventh": "maj7", "minor-seventh": "min7",
    "diminished-seventh": "dim7", "half-diminished": "m7b5",
    "major-sixth": "maj6", "minor-sixth": "min6",
    "major-ninth": "maj9", "dominant-ninth": "dom9", "minor-ninth": "min9",
    "suspended-fourth": "sus4", "suspended-second": "sus2",
    "major-minor": "minmaj7", "dominant-13th": "dom13",
    "power": "maj",
}


def _quality_from_symbol(symbol: harmony.ChordSymbol) -> str | None:
    kind = (getattr(symbol, "chordKind", "") or "").strip()
    if kind in _KIND_TO_QUALITY:
        return _KIND_TO_QUALITY[kind]

    # Fall back to matching the actual pitch classes against what we can voice,
    # which covers spellings music21 labels in ways this table does not.
    try:
        pcs = {p.pitchClass for p in symbol.pitches}
        root = symbol.root().pitchClass
    except Exception:
        return None
    wanted = frozenset((pc - root) % 12 for pc in pcs)
    for quality, intervals in CHORD_QUALITIES.items():
        if frozenset(i % 12 for i in intervals) == wanted:
            return quality
    return None


def read_chord_symbols(score: stream.Score) -> list[SourceChord]:
    """Chord symbols written in the file, in order. Empty when there are none."""
    found: list[SourceChord] = []
    for symbol in score.flatten().getElementsByClass(harmony.ChordSymbol):
        if isinstance(symbol, harmony.NoChord):
            continue
        quality = _quality_from_symbol(symbol)
        if quality is None:
            continue
        try:
            root = symbol.root().pitchClass
        except Exception:
            continue
        bass = None
        try:
            if symbol.bass() is not None and symbol.bass().pitchClass != root:
                bass = symbol.bass().pitchClass
        except Exception:
            bass = None
        found.append(
            SourceChord(
                offset=_to_float(symbol.offset),   # absolute in the flat stream
                duration=0.0,  # filled in below, from the gap to the next symbol
                chord=ChordSpec(root_pc=root, quality=quality, bass_pc=bass),
            )
        )

    found.sort(key=lambda c: c.offset)
    for index, item in enumerate(found):
        end = found[index + 1].offset if index + 1 < len(found) else item.offset + 4.0
        item.duration = max(0.25, end - item.offset)
    return found


# --------------------------------------------------------------------------
# 2. The real texture of a multi-part score
# --------------------------------------------------------------------------


def read_texture(score: stream.Score, span: float) -> list[Sonority]:
    """Duration-weighted pitch classes per span, taken from all parts together.

    Weighting by how long each pitch actually sounds is what keeps passing
    notes from outvoting chord tones, and the lowest pitch is tracked
    separately because the bass is the strongest single clue to a chord's root.
    """
    parts = list(score.parts) if hasattr(score, "parts") else []
    if len(parts) < 2:
        return []

    # Ties first, then flatten: a tied note counted twice would double its
    # weight and skew the chord away from what is really sounding.
    try:
        flat = score.stripTies().flatten()
    except Exception:
        flat = score.flatten()

    events: list[tuple[float, float, int]] = []  # (start, end, midi)
    for element in flat.notes:
        start = _to_float(element.offset)   # already absolute in a flat stream
        length = _to_float(element.duration.quarterLength)
        if length <= 0:
            continue
        pitches = (
            [p.midi for p in element.pitches]
            if isinstance(element, m21chord.Chord)
            else [element.pitch.midi] if isinstance(element, m21note.Note)
            else []
        )
        for midi in pitches:
            events.append((start, start + length, midi))

    if not events:
        return []

    total = max(end for _, end, _ in events)
    sonorities: list[Sonority] = []
    steps = int(total / span) + 1
    for index in range(steps):
        start = index * span
        end = start + span
        weights: dict[int, float] = {}
        lowest: int | None = None
        for note_start, note_end, midi in events:
            overlap = min(note_end, end) - max(note_start, start)
            if overlap <= 1e-6:
                continue
            weights[midi % 12] = weights.get(midi % 12, 0.0) + overlap
            if lowest is None or midi < lowest:
                lowest = midi
        if weights:
            sonorities.append(
                Sonority(
                    offset=start,
                    duration=span,
                    weights=weights,
                    bass_pc=None if lowest is None else lowest % 12,
                )
            )
    return sonorities


def has_multiple_parts(score: stream.Score) -> bool:
    parts = list(score.parts) if hasattr(score, "parts") else []
    return sum(1 for p in parts if any(True for _ in p.recurse().notes)) >= 2
