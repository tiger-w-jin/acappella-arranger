"""Pitch, chord and key primitives shared by the analysis and harmony layers.

Everything here works in MIDI note numbers and pitch classes (0 == C) so the
harmony search never has to touch music21 objects, which are far too slow to
put inside an inner loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLAT_NAMES = ["C", "D-", "D", "E-", "E", "F", "G-", "G", "A-", "A", "B-", "B"]

# Interval structure of every chord quality the arranger can build, as
# semitones above the root. Values above 11 are true upper extensions and are
# voiced an octave up when there is room.
CHORD_QUALITIES: dict[str, tuple[int, ...]] = {
    "maj": (0, 4, 7),
    "min": (0, 3, 7),
    "dim": (0, 3, 6),
    "aug": (0, 4, 8),
    "sus2": (0, 2, 7),
    "sus4": (0, 5, 7),
    "maj6": (0, 4, 7, 9),
    "min6": (0, 3, 7, 9),
    "dom7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11),
    "min7": (0, 3, 7, 10),
    "minmaj7": (0, 3, 7, 11),
    "m7b5": (0, 3, 6, 10),
    "dim7": (0, 3, 6, 9),
    "dom7sus4": (0, 5, 7, 10),
    "maj9": (0, 4, 7, 11, 14),
    "dom9": (0, 4, 7, 10, 14),
    "min9": (0, 3, 7, 10, 14),
    "maj69": (0, 4, 7, 9, 14),
    "dom13": (0, 4, 7, 10, 14, 21),
    "dom7b9": (0, 4, 7, 10, 13),
    "dom7s11": (0, 4, 7, 10, 18),
}

# Human-facing chord symbol suffixes.
QUALITY_SUFFIX: dict[str, str] = {
    "maj": "",
    "min": "m",
    "dim": "dim",
    "aug": "+",
    "sus2": "sus2",
    "sus4": "sus4",
    "maj6": "6",
    "min6": "m6",
    "dom7": "7",
    "maj7": "maj7",
    "min7": "m7",
    "minmaj7": "mMaj7",
    "m7b5": "m7b5",
    "dim7": "dim7",
    "dom7sus4": "7sus4",
    "maj9": "maj9",
    "dom9": "9",
    "min9": "m9",
    "maj69": "6/9",
    "dom13": "13",
    "dom7b9": "7b9",
    "dom7s11": "7#11",
}

# music21 rejects a few of the suffixes above when building a <harmony> element,
# so these spellings are substituted on export only. The UI keeps the readable
# form.
_EXPORT_SUFFIX_OVERRIDE = {"minmaj7": "mM7", "maj9": "M9", "maj69": "6add9"}

# How badly a chord tone can be left out. Root and third define the chord's
# identity, the fifth is the first thing a real arranger drops.
TONE_IMPORTANCE: dict[int, float] = {
    0: 3.0,   # root
    3: 3.0,   # minor third
    4: 3.0,   # major third
    5: 2.0,   # sus fourth
    2: 1.5,   # sus second
    6: 2.5,   # diminished fifth (identity-bearing)
    7: 1.0,   # perfect fifth
    8: 2.0,   # augmented fifth
    9: 1.5,   # sixth / dim seventh
    10: 2.5,  # minor seventh
    11: 2.5,  # major seventh
    13: 1.0,  # b9
    14: 1.0,  # 9
    18: 1.0,  # #11
    21: 1.0,  # 13
}

MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


@dataclass(frozen=True)
class ChordSpec:
    """A concrete chord: a root pitch class plus a quality."""

    root_pc: int
    quality: str
    bass_pc: int | None = None

    @property
    def intervals(self) -> tuple[int, ...]:
        return CHORD_QUALITIES[self.quality]

    @property
    def pitch_classes(self) -> tuple[int, ...]:
        return tuple(sorted({(self.root_pc + i) % 12 for i in self.intervals}))

    def symbol(self, prefer_flats: bool = False) -> str:
        names = FLAT_NAMES if prefer_flats else SHARP_NAMES
        text = names[self.root_pc].replace("-", "b") + QUALITY_SUFFIX[self.quality]
        if self.bass_pc is not None and self.bass_pc != self.root_pc:
            text += "/" + names[self.bass_pc].replace("-", "b")
        return text

    def export_figure(self, prefer_flats: bool = False) -> str:
        """Chord symbol in a spelling music21 can turn into a <harmony> element."""
        names = FLAT_NAMES if prefer_flats else SHARP_NAMES
        suffix = _EXPORT_SUFFIX_OVERRIDE.get(self.quality, QUALITY_SUFFIX[self.quality])
        text = names[self.root_pc].replace("-", "b") + suffix
        if self.bass_pc is not None and self.bass_pc != self.root_pc:
            text += "/" + names[self.bass_pc].replace("-", "b")
        return text

    def tone_for_pc(self, pc: int) -> int | None:
        """Return the interval (mod 12 matched) this pitch class plays, if any."""
        for i in self.intervals:
            if (self.root_pc + i) % 12 == pc % 12:
                return i
        return None


@dataclass
class KeyContext:
    """Detected key, plus the spelling preference that follows from it."""

    tonic_pc: int
    mode: str  # "major" or "minor"
    confidence: float = 0.0
    prefer_flats: bool = field(default=False)

    @property
    def name(self) -> str:
        names = FLAT_NAMES if self.prefer_flats else SHARP_NAMES
        return f"{names[self.tonic_pc].replace('-', 'b')} {self.mode}"

    @property
    def scale_pcs(self) -> tuple[int, ...]:
        steps = (0, 2, 4, 5, 7, 9, 11) if self.mode == "major" else (0, 2, 3, 5, 7, 8, 10)
        return tuple((self.tonic_pc + s) % 12 for s in steps)


# Keys conventionally written with flats, as (tonic_pc, mode) pairs.
_FLAT_KEYS = {
    (5, "major"), (10, "major"), (3, "major"), (8, "major"), (1, "major"), (6, "major"),
    (2, "minor"), (7, "minor"), (0, "minor"), (5, "minor"), (10, "minor"), (3, "minor"),
}


def detect_key(weights: dict[int, float]) -> KeyContext:
    """Krumhansl-Schmuckler key finding over duration-weighted pitch classes."""
    total = sum(weights.values()) or 1.0
    observed = [weights.get(pc, 0.0) / total for pc in range(12)]
    mean_obs = sum(observed) / 12

    best: tuple[float, int, str] = (-2.0, 0, "major")
    for mode, profile in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
        mean_prof = sum(profile) / 12
        for tonic in range(12):
            num = 0.0
            den_o = 0.0
            den_p = 0.0
            for i in range(12):
                do = observed[(tonic + i) % 12] - mean_obs
                dp = profile[i] - mean_prof
                num += do * dp
                den_o += do * do
                den_p += dp * dp
            corr = num / ((den_o * den_p) ** 0.5) if den_o > 0 and den_p > 0 else 0.0
            if corr > best[0]:
                best = (corr, tonic, mode)

    corr, tonic, mode = best
    return KeyContext(
        tonic_pc=tonic,
        mode=mode,
        confidence=round(corr, 3),
        prefer_flats=(tonic, mode) in _FLAT_KEYS,
    )


def pitch_name(midi: int, prefer_flats: bool = False) -> tuple[str, int, int]:
    """Split a MIDI number into (step letter, alteration, octave) for MusicXML."""
    names = FLAT_NAMES if prefer_flats else SHARP_NAMES
    raw = names[midi % 12]
    step = raw[0]
    alter = 1 if raw.endswith("#") else (-1 if raw.endswith("-") else 0)
    octave = midi // 12 - 1
    # A flat spelling borrows its letter from the note above, which never
    # changes the octave here because B-flat and C-flat are the only wrap
    # candidates and we never spell C-flat.
    return step, alter, octave


# Conventional spelling for notes outside the key, keyed by semitones above the
# tonic. True means write it as a flat. Chromatic notes are spelled toward the
# nearer side of the circle of fifths, which is what a reader expects: E-flat in
# C major, never D-sharp.
_CHROMATIC_IS_FLAT_MAJOR = {1: True, 3: True, 6: False, 8: True, 10: True}
_CHROMATIC_IS_FLAT_MINOR = {1: True, 4: False, 6: False, 9: False, 11: False}


def spell_pitch(midi: int, key: KeyContext) -> tuple[str, int, int]:
    """Split a MIDI number into (step, alteration, octave), spelled for the key."""
    degree = (midi - key.tonic_pc) % 12
    diatonic = (0, 2, 4, 5, 7, 9, 11) if key.mode == "major" else (0, 2, 3, 5, 7, 8, 10)
    if degree in diatonic:
        use_flats = key.prefer_flats
    else:
        table = _CHROMATIC_IS_FLAT_MAJOR if key.mode == "major" else _CHROMATIC_IS_FLAT_MINOR
        use_flats = table.get(degree, key.prefer_flats)
    return pitch_name(midi, use_flats)


def nearest_pitch_with_pc(target: int, pc: int, lo: int, hi: int) -> int | None:
    """Pitch in [lo, hi] with the given pitch class that sits closest to target."""
    best: int | None = None
    base = (target // 12) * 12 + (pc % 12)
    for candidate in (base - 24, base - 12, base, base + 12, base + 24):
        if lo <= candidate <= hi:
            if best is None or abs(candidate - target) < abs(best - target):
                best = candidate
    return best


def pitches_in_range(pc: int, lo: int, hi: int) -> list[int]:
    """Every pitch of a given pitch class inside an inclusive MIDI range."""
    start = lo + ((pc - lo) % 12)
    return list(range(start, hi + 1, 12))
