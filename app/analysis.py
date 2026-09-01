"""Work out what the uploaded melody is doing harmonically.

Key detection is Krumhansl-Schmuckler over the whole piece. Chords are then
chosen with a Viterbi pass across the bars: each candidate chord is scored on
how well it fits that bar's melody notes, and consecutive choices are scored on
how idiomatic the progression is. Doing it globally rather than bar-by-bar is
what stops the analysis from picking a locally plausible chord that wrecks the
cadence two bars later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Bar, SourceScore
from .theory import (
    CHORD_QUALITIES,
    FLAT_NAMES,
    SHARP_NAMES,
    ChordSpec,
    KeyContext,
    detect_key,
)

# Function classes used by the transition model.
TONIC, SUBDOMINANT, DOMINANT, OTHER = "T", "S", "D", "O"

_FUNCTION_BONUS = {
    (DOMINANT, TONIC): 0.9,
    (SUBDOMINANT, DOMINANT): 0.8,
    (TONIC, SUBDOMINANT): 0.5,
    (TONIC, DOMINANT): 0.4,
    (SUBDOMINANT, TONIC): 0.25,
    (DOMINANT, SUBDOMINANT): -0.8,
    (TONIC, TONIC): 0.0,
}

# How well a melody note sitting on a given chord interval supports that chord.
# The third defines the chord's quality so it counts for most; root and fifth
# are near-equal, which stops the analyser from simply chasing the melody note
# and calling it the root.
_TONE_FIT: dict[int, float] = {
    0: 1.05,   # root
    2: 0.95,   # sus second
    3: 1.15,   # minor third
    4: 1.15,   # major third
    5: 1.0,    # sus fourth
    6: 1.05,   # diminished fifth
    7: 1.0,    # perfect fifth
    8: 1.0,    # augmented fifth
    9: 0.9,    # sixth / diminished seventh
    10: 0.9,   # minor seventh
    11: 0.85,  # major seventh
    13: 0.7,   # b9
    14: 0.8,   # 9
    18: 0.7,   # #11
    21: 0.75,  # 13
}


@dataclass
class Segment:
    """One span of constant harmony. A bar holds one or two of them."""

    bar_index: int
    start: float
    duration: float
    chord: ChordSpec
    roman: str
    symbol: str


@dataclass
class BarHarmony:
    index: int
    segments: list[Segment]

    @property
    def chord(self) -> ChordSpec:
        return self.segments[0].chord

    @property
    def symbol(self) -> str:
        return " | ".join(segment.symbol for segment in self.segments)

    @property
    def roman(self) -> str:
        return " | ".join(segment.roman for segment in self.segments)


def pitch_class_weights(score: SourceScore) -> dict[int, float]:
    weights: dict[int, float] = {}
    for note in score.all_notes:
        if note.pitch is None:
            continue
        pc = note.pitch % 12
        weights[pc] = weights.get(pc, 0.0) + note.duration
    return weights


def _degree_name(semitones: int, mode: str) -> str:
    """Roman numeral stem for a root that many semitones above the tonic."""
    major_map = {0: "I", 2: "II", 4: "III", 5: "IV", 7: "V", 9: "VI", 11: "VII"}
    minor_map = {0: "I", 2: "II", 3: "III", 5: "IV", 7: "V", 8: "VI", 10: "VII"}
    table = major_map if mode == "major" else minor_map
    if semitones in table:
        return table[semitones]
    # Chromatic root: name it as a flattened version of the degree above.
    above = {1: "II", 3: "III", 6: "V", 8: "VI", 10: "VII"}
    if semitones in above:
        return "b" + above[semitones]
    return {4: "bV", 9: "bVII"}.get(semitones, "?")


def roman_for(chord: ChordSpec, key: KeyContext) -> str:
    semitones = (chord.root_pc - key.tonic_pc) % 12

    # A dominant seventh that is not the key's own V reads as a secondary
    # dominant, so name it by the degree it tonicises.
    if chord.quality in ("dom7", "dom9", "dom13") and semitones != 7:
        target = (semitones + 5) % 12
        target_stem = _degree_name(target, key.mode)
        if target_stem != "?":
            minor_targets = {2, 4, 9} if key.mode == "major" else {5, 10}
            if target in minor_targets:
                target_stem = target_stem.lower()
            return f"V7/{target_stem}"

    stem = _degree_name(semitones, key.mode)
    prefix = ""
    if stem.startswith("b"):
        prefix, stem = "b", stem[1:]

    minorish = chord.quality in ("min", "min7", "min9", "min6", "minmaj7", "dim", "dim7", "m7b5")
    text = prefix + (stem.lower() if minorish else stem)

    if chord.quality in ("dim", "dim7"):
        text += "\u00b0"
    elif chord.quality == "m7b5":
        text += "\u00f8"
    elif chord.quality == "aug":
        text += "+"

    if chord.quality in ("dom7", "min7", "maj7", "minmaj7", "dim7", "m7b5", "dom7sus4"):
        text += "7"
    elif chord.quality in ("maj9", "dom9", "min9"):
        text += "9"
    elif chord.quality in ("maj6", "min6", "maj69"):
        text += "6"
    elif chord.quality == "dom13":
        text += "13"
    elif chord.quality.startswith("sus"):
        text += chord.quality[3:]
    return text


def _function_of(chord: ChordSpec, key: KeyContext) -> str:
    semitones = (chord.root_pc - key.tonic_pc) % 12
    if key.mode == "major":
        if semitones in (0, 9):
            return TONIC
        if semitones == 4:
            return TONIC
        if semitones in (5, 2):
            return SUBDOMINANT
        if semitones in (7, 11):
            return DOMINANT
    else:
        if semitones in (0, 3):
            return TONIC
        if semitones in (5, 2, 8):
            return SUBDOMINANT
        if semitones in (7, 11):
            return DOMINANT
    return OTHER


def candidate_chords(key: KeyContext) -> list[tuple[ChordSpec, float]]:
    """Chords the analyser will consider, with a prior favouring the common ones."""
    tonic = key.tonic_pc
    candidates: list[tuple[ChordSpec, float]] = []

    def add(offset: int, quality: str, prior: float) -> None:
        candidates.append((ChordSpec((tonic + offset) % 12, quality), prior))

    if key.mode == "major":
        add(0, "maj", 1.0)
        add(2, "min", 0.6)
        add(4, "min", 0.35)
        add(5, "maj", 0.85)
        add(7, "maj", 0.9)
        add(7, "dom7", 0.8)
        add(9, "min", 0.7)
        add(11, "dim", 0.3)
        add(2, "min7", 0.4)
        add(0, "maj7", 0.3)
        add(10, "maj", 0.25)  # bVII, borrowed
        add(5, "min", 0.2)    # iv, borrowed
        add(8, "maj", 0.15)   # bVI, borrowed
        for offset in (2, 4, 9, 11):  # secondary dominants
            add(offset, "dom7", 0.12)
    else:
        add(0, "min", 1.0)
        add(2, "dim", 0.3)
        add(3, "maj", 0.7)
        add(5, "min", 0.8)
        add(7, "min", 0.5)
        add(7, "maj", 0.75)
        add(7, "dom7", 0.7)
        add(8, "maj", 0.7)
        add(10, "maj", 0.6)
        add(0, "min7", 0.35)
        add(5, "min7", 0.3)
        add(11, "dim7", 0.25)
        add(3, "maj7", 0.2)

    return candidates


def _metric_weight(bar: Bar, offset_in_bar: float) -> float:
    """Beat 1 matters most, the mid-bar beat next, offbeats least."""
    if offset_in_bar < 1e-6:
        return 2.0
    half = bar.length / 2
    if abs(offset_in_bar - half) < 1e-6:
        return 1.5
    if abs(offset_in_bar - round(offset_in_bar)) < 1e-6:
        return 1.0
    return 0.5


@dataclass
class _Span:
    """A candidate span of constant harmony, with its melody content."""

    bar_index: int
    start: float
    duration: float
    bar: Bar


def _build_spans(score: SourceScore, chords_per_bar: int) -> list[_Span]:
    spans: list[_Span] = []
    for bar in score.bars:
        # Never subdivide a short bar (3/4, 6/8, or an anacrusis).
        divisions = chords_per_bar if bar.length >= 4.0 - 1e-6 else 1
        length = bar.length / divisions
        for step in range(divisions):
            spans.append(
                _Span(
                    bar_index=bar.index,
                    start=bar.offset + step * length,
                    duration=length,
                    bar=bar,
                )
            )
    return spans


def _emission(span: _Span, chord: ChordSpec, prior: float) -> float:
    """How well a chord explains the melody sounding during this span.

    Notes are weighted by metric strength and by how much of them actually
    falls inside the span, so a note held over a chord change counts for both.
    """
    span_end = span.start + span.duration
    total = 0.0
    weighted_duration = 0.0
    for note in span.bar.notes:
        if note.pitch is None:
            continue
        overlap = min(note.end, span_end) - max(note.offset, span.start)
        if overlap <= 1e-6:
            continue
        weight = _metric_weight(span.bar, note.offset - span.bar.offset) * overlap
        weighted_duration += weight
        interval = chord.tone_for_pc(note.pitch % 12)
        if interval is None:
            total -= weight * 1.25
        else:
            total += weight * _TONE_FIT.get(interval, 0.85)

    if weighted_duration <= 0:
        return prior * 0.5
    return 2.2 * (total / weighted_duration) + prior * 1.0


def _transition(
    previous: ChordSpec, nxt: ChordSpec, key: KeyContext, within_bar: bool = False
) -> float:
    """Score a chord change. `within_bar` marks a mid-bar boundary, where real
    arrangements change harmony far less often than they do across a barline."""
    if previous.root_pc == nxt.root_pc and previous.quality == nxt.quality:
        return 0.45 if within_bar else 0.0

    score = -0.25 if within_bar else 0.0
    motion = (previous.root_pc - nxt.root_pc) % 12
    if motion == 7:      # down a fifth
        score += 0.7
    elif motion == 10:   # up a step
        score += 0.35
    elif motion in (3, 4):  # down a third
        score += 0.35
    elif motion == 5:    # up a fifth
        score += 0.1

    if previous.quality in ("dom7", "dim7"):
        resolves = (previous.root_pc + 5) % 12 == nxt.root_pc
        score += 0.7 if resolves else -0.7

    score += _FUNCTION_BONUS.get((_function_of(previous, key), _function_of(nxt, key)), 0.0)
    return score


def infer_harmony(
    score: SourceScore, key: KeyContext, chords_per_bar: int = 2
) -> list[BarHarmony]:
    """Viterbi across harmonic spans to pick the most idiomatic progression."""
    if not score.bars:
        return []

    spans = _build_spans(score, max(1, min(2, chords_per_bar)))
    candidates = candidate_chords(key)
    n_states = len(candidates)
    transition_weight = 0.35

    scores = [
        _emission(spans[0], chord, prior) + (0.6 if chord.root_pc == key.tonic_pc else 0.0)
        for chord, prior in candidates
    ]
    backpointers: list[list[int]] = []

    for position, span in enumerate(spans[1:], start=1):
        within_bar = span.bar_index == spans[position - 1].bar_index
        emissions = [_emission(span, chord, prior) for chord, prior in candidates]
        transitions = [
            [
                transition_weight
                * _transition(candidates[i][0], candidates[j][0], key, within_bar)
                for i in range(n_states)
            ]
            for j in range(n_states)
        ]
        new_scores = [float("-inf")] * n_states
        pointers = [0] * n_states
        for j in range(n_states):
            row = transitions[j]
            best_index = max(range(n_states), key=lambda i: scores[i] + row[i])
            new_scores[j] = scores[best_index] + row[best_index] + emissions[j]
            pointers[j] = best_index
        scores = new_scores
        backpointers.append(pointers)

    # Cadence: land on the tonic, or on the dominant for a half close.
    dominant_pc = (key.tonic_pc + 7) % 12
    final = []
    for index, value in enumerate(scores):
        root = candidates[index][0].root_pc
        bonus = 0.8 if root == key.tonic_pc else (0.5 if root == dominant_pc else 0.0)
        final.append(value + bonus)
    state = max(range(n_states), key=lambda i: final[i])

    path = [state]
    for pointers in reversed(backpointers):
        state = pointers[state]
        path.append(state)
    path.reverse()

    bars: dict[int, BarHarmony] = {
        bar.index: BarHarmony(index=bar.index, segments=[]) for bar in score.bars
    }
    for span, state_index in zip(spans, path):
        chord = candidates[state_index][0]
        bars[span.bar_index].segments.append(
            Segment(
                bar_index=span.bar_index,
                start=span.start,
                duration=span.duration,
                chord=chord,
                roman=roman_for(chord, key),
                symbol=chord.symbol(key.prefer_flats),
            )
        )

    result = [bars[bar.index] for bar in score.bars]
    return _merge_repeats(result)


def _merge_repeats(bars: list[BarHarmony]) -> list[BarHarmony]:
    """Collapse a bar's two spans into one when they chose the same chord."""
    for bar in bars:
        if len(bar.segments) == 2:
            first, second = bar.segments
            if first.chord == second.chord:
                bar.segments = [
                    Segment(
                        bar_index=first.bar_index,
                        start=first.start,
                        duration=first.duration + second.duration,
                        chord=first.chord,
                        roman=first.roman,
                        symbol=first.symbol,
                    )
                ]
    return bars


def analyze(
    score: SourceScore, chords_per_bar: int = 2
) -> tuple[KeyContext, list[BarHarmony]]:
    key = detect_key(pitch_class_weights(score))
    return key, infer_harmony(score, key, chords_per_bar)


# --------------------------------------------------------------------------
# Chord symbol parsing, for user overrides
# --------------------------------------------------------------------------

_SYMBOL_RE = re.compile(r"^([A-Ga-g])([#b\u266f\u266d]?)(.*)$")

_SUFFIX_TO_QUALITY = {
    "": "maj", "maj": "maj", "M": "maj", "major": "maj",
    "m": "min", "min": "min", "-": "min", "minor": "min",
    "dim": "dim", "o": "dim", "\u00b0": "dim",
    "+": "aug", "aug": "aug",
    "sus2": "sus2", "sus4": "sus4", "sus": "sus4",
    "6": "maj6", "m6": "min6", "min6": "min6",
    "7": "dom7", "dom7": "dom7",
    "maj7": "maj7", "M7": "maj7", "\u25b37": "maj7",
    "m7": "min7", "min7": "min7", "-7": "min7",
    "mMaj7": "minmaj7", "mM7": "minmaj7",
    "m7b5": "m7b5", "\u00f8": "m7b5", "halfdim": "m7b5",
    "dim7": "dim7", "o7": "dim7", "\u00b07": "dim7",
    "7sus4": "dom7sus4", "7sus": "dom7sus4",
    "9": "dom9", "maj9": "maj9", "M9": "maj9", "m9": "min9", "min9": "min9",
    "6/9": "maj69", "69": "maj69",
    "13": "dom13", "7b9": "dom7b9", "7#11": "dom7s11",
}


def parse_chord_symbol(text: str) -> ChordSpec | None:
    """Turn a symbol like 'F#m7' or 'Ab7/C' into a ChordSpec, or None."""
    if not text:
        return None
    cleaned = text.strip().replace(" ", "")
    bass_pc: int | None = None
    if "/" in cleaned:
        cleaned, bass_text = cleaned.split("/", 1)
        # "6/9" is a quality, not a slash chord.
        if bass_text and bass_text[0].upper() in "ABCDEFG" and not bass_text[0].isdigit():
            bass = _root_of(bass_text)
            bass_pc = bass[0] if bass else None
        else:
            cleaned = f"{cleaned}/{bass_text}"

    parsed = _root_of(cleaned)
    if parsed is None:
        return None
    root_pc, suffix = parsed
    quality = _SUFFIX_TO_QUALITY.get(suffix)
    if quality is None:
        quality = _SUFFIX_TO_QUALITY.get(suffix.lower())
    if quality is None or quality not in CHORD_QUALITIES:
        return None
    return ChordSpec(root_pc=root_pc, quality=quality, bass_pc=bass_pc)


def _root_of(text: str) -> tuple[int, str] | None:
    match = _SYMBOL_RE.match(text)
    if not match:
        return None
    letter, accidental, rest = match.groups()
    base = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[letter.upper()]
    if accidental in ("#", "\u266f"):
        base += 1
    elif accidental in ("b", "\u266d"):
        base -= 1
    return base % 12, rest


def format_symbol(chord: ChordSpec, prefer_flats: bool) -> str:
    return chord.symbol(prefer_flats)


__all__ = [
    "analyze",
    "infer_harmony",
    "roman_for",
    "parse_chord_symbol",
    "format_symbol",
    "BarHarmony",
    "SHARP_NAMES",
    "FLAT_NAMES",
]
