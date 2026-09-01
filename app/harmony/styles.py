"""Ensemble definitions and the catalogue of per-bar harmony styles.

A style is a bundle of decisions an arranger makes: which colour tones to add to
the bar's chord, how far apart to spread the voices, what rhythm the backing
sings, and which syllable it sings on. Each bar of a piece can use a different
one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from ..theory import ChordSpec, KeyContext


@dataclass(frozen=True)
class Voice:
    name: str
    abbreviation: str
    lo: int  # lowest comfortable MIDI pitch
    hi: int  # highest comfortable MIDI pitch
    clef: str  # "treble" | "bass" | "treble8vb"

    @property
    def centre(self) -> float:
        return (self.lo + self.hi) / 2


@dataclass(frozen=True)
class Ensemble:
    id: str
    name: str
    voices: tuple[Voice, ...]
    melody_index: int = 0

    @property
    def size(self) -> int:
        return len(self.voices)


SOPRANO = Voice("Soprano", "S", 60, 79, "treble")
ALTO = Voice("Alto", "A", 55, 74, "treble")
TENOR = Voice("Tenor", "T", 48, 67, "treble8vb")
BASS = Voice("Bass", "B", 40, 60, "bass")

ENSEMBLES: dict[str, Ensemble] = {
    "satb": Ensemble("satb", "SATB (mixed choir)", (SOPRANO, ALTO, TENOR, BASS), 0),
    "sab": Ensemble("sab", "SAB (three-part mixed)", (SOPRANO, ALTO, BASS), 0),
    "ssaa": Ensemble(
        "ssaa",
        "SSAA (upper voices)",
        (
            Voice("Soprano 1", "S1", 60, 81, "treble"),
            Voice("Soprano 2", "S2", 57, 77, "treble"),
            Voice("Alto 1", "A1", 55, 74, "treble"),
            Voice("Alto 2", "A2", 52, 70, "treble"),
        ),
        0,
    ),
    "ttbb": Ensemble(
        "ttbb",
        "TTBB (lower voices)",
        (
            Voice("Tenor 1", "T1", 50, 71, "treble8vb"),
            Voice("Tenor 2", "T2", 47, 67, "treble8vb"),
            Voice("Baritone", "Bar", 43, 64, "bass"),
            Voice("Bass", "B", 38, 58, "bass"),
        ),
        0,
    ),
    "barbershop": Ensemble(
        "barbershop",
        "Barbershop quartet (lead on 2nd voice)",
        (
            Voice("Tenor", "Tn", 57, 76, "treble8vb"),
            Voice("Lead", "Ld", 48, 69, "treble8vb"),
            Voice("Baritone", "Bar", 45, 65, "bass"),
            Voice("Bass", "Bs", 40, 60, "bass"),
        ),
        1,
    ),
    "ssatb": Ensemble(
        "ssatb",
        "SSATB (five parts, richer voicings)",
        (
            Voice("Soprano 1", "S1", 62, 81, "treble"),
            Voice("Soprano 2", "S2", 58, 76, "treble"),
            Voice("Alto", "A", 55, 74, "treble"),
            Voice("Tenor", "T", 48, 67, "treble8vb"),
            Voice("Bass", "B", 40, 60, "bass"),
        ),
        0,
    ),
}


@dataclass(frozen=True)
class VoicingWeights:
    """Cost coefficients steering the voicing search."""

    motion: float = 1.0
    leap: float = 0.7
    spacing: float = 1.2
    max_upper_gap: int = 12
    min_gap: int = 1
    ideal_gap_hi: int = 9
    crossing: float = 80.0
    missing_tone: float = 7.0
    doubled_colour: float = 7.0
    doubled_fifth: float = 1.5
    parallel: float = 16.0
    tessitura: float = 0.8
    close_bias: float = 0.0
    open_bias: float = 0.0


# --------------------------------------------------------------------------
# Chord enrichment: how a style colours the bar's underlying harmony
# --------------------------------------------------------------------------


def _requality(base: ChordSpec, quality: str) -> ChordSpec:
    return replace(base, quality=quality)


def enrich_plain(base: ChordSpec, key: KeyContext) -> ChordSpec:
    return base


def enrich_chorale(base: ChordSpec, key: KeyContext) -> ChordSpec:
    """Triads throughout, but let the dominant carry its seventh."""
    dominant_pc = (key.tonic_pc + 7) % 12
    if base.quality == "maj" and base.root_pc == dominant_pc:
        return _requality(base, "dom7")
    if base.quality == "dim":
        return _requality(base, "dim") if key.mode == "major" else _requality(base, "dim7")
    return base


def enrich_barbershop(base: ChordSpec, key: KeyContext) -> ChordSpec:
    """Barbershop lives on the dominant seventh; the tonic gets a sixth."""
    if base.quality == "maj":
        if base.root_pc == key.tonic_pc:
            return _requality(base, "maj6")
        return _requality(base, "dom7")
    if base.quality == "min":
        return _requality(base, "min7")
    if base.quality in ("dim", "m7b5"):
        return _requality(base, "dim7")
    return base


def enrich_jazz(base: ChordSpec, key: KeyContext) -> ChordSpec:
    mapping = {
        "maj": "maj7",
        "min": "min7",
        "dim": "m7b5",
        "dom7": "dom9",
        "maj7": "maj9",
        "min7": "min9",
    }
    return _requality(base, mapping.get(base.quality, base.quality))


def enrich_gospel(base: ChordSpec, key: KeyContext) -> ChordSpec:
    mapping = {
        "maj": "maj69",
        "maj7": "maj69",
        "min": "min9",
        "min7": "min9",
        "dom7": "dom13",
        "dim": "m7b5",
    }
    return _requality(base, mapping.get(base.quality, base.quality))


def enrich_cluster(base: ChordSpec, key: KeyContext) -> ChordSpec:
    mapping = {"maj": "maj9", "min": "min9", "dom7": "dom9", "maj7": "maj9", "dim": "m7b5"}
    return _requality(base, mapping.get(base.quality, base.quality))


def enrich_doowop(base: ChordSpec, key: KeyContext) -> ChordSpec:
    if base.quality == "maj" and base.root_pc == key.tonic_pc:
        return _requality(base, "maj6")
    if base.quality in ("maj7", "maj9", "maj69"):
        return _requality(base, "maj")
    if base.quality in ("min7", "min9"):
        return _requality(base, "min")
    return base


def enrich_sus(base: ChordSpec, key: KeyContext) -> ChordSpec:
    if base.quality in ("maj", "maj7", "maj9", "dom7"):
        return _requality(base, "sus2")
    return base


@dataclass(frozen=True)
class Style:
    id: str
    name: str
    description: str
    syllable: str
    rhythm: str  # follow | sustain | pulse_half | pulse_quarter | pulse_eighth
    generator: str = "voiced"  # voiced | parallel | drone | unison
    enrich: Callable[[ChordSpec, KeyContext], ChordSpec] = enrich_plain
    bass_root: bool = True
    allow_parallels: bool = False
    min_voices: int = 3
    intervals: tuple[int, ...] = ()  # for the "parallel" generator
    parallel_snap: str = "chord"  # none | chord | scale
    weights: VoicingWeights = field(default_factory=VoicingWeights)


STYLES: dict[str, Style] = {
    "satb_chorale": Style(
        id="satb_chorale",
        name="Chorale (SATB)",
        description=(
            "Classical four-part writing. Voices move with the melody's rhythm, "
            "parallel fifths and octaves are avoided, and spacing follows "
            "traditional close/open rules."
        ),
        syllable="Ah",
        rhythm="follow",
        enrich=enrich_chorale,
        weights=VoicingWeights(motion=1.2, parallel=22.0, missing_tone=9.0),
    ),
    "hymn_open": Style(
        id="hymn_open",
        name="Open hymn pad",
        description=(
            "Wide, sustained four-part chords held under the tune. Warm and "
            "church-like; good for slow sections."
        ),
        syllable="Ah",
        rhythm="sustain",
        enrich=enrich_chorale,
        weights=VoicingWeights(open_bias=1.6, max_upper_gap=14, motion=0.8),
    ),
    "barbershop": Style(
        id="barbershop",
        name="Barbershop",
        description=(
            "Tight close harmony packed under the lead, leaning on barbershop "
            "sevenths and sixths. Bass anchors the root."
        ),
        syllable="Ooh",
        rhythm="follow",
        enrich=enrich_barbershop,
        allow_parallels=False,
        min_voices=4,
        weights=VoicingWeights(
            close_bias=2.2, max_upper_gap=9, ideal_gap_hi=5, missing_tone=12.0, motion=1.4
        ),
    ),
    "jazz_close": Style(
        id="jazz_close",
        name="Jazz close harmony",
        description=(
            "Sevenths and ninths voiced tightly, with smooth chromatic voice "
            "leading. Sung on 'doo'."
        ),
        syllable="Doo",
        rhythm="pulse_half",
        enrich=enrich_jazz,
        weights=VoicingWeights(close_bias=1.4, motion=1.6, missing_tone=5.0, doubled_colour=9.0),
    ),
    "gospel_pad": Style(
        id="gospel_pad",
        name="Gospel 6/9 pad",
        description=(
            "Thick 6/9 and 13th voicings held across the bar. Lush and open, "
            "with the bass on the root."
        ),
        syllable="Oh",
        rhythm="sustain",
        enrich=enrich_gospel,
        weights=VoicingWeights(open_bias=1.0, max_upper_gap=13, missing_tone=4.0, motion=0.9),
    ),
    "doo_wop": Style(
        id="doo_wop",
        name="Doo-wop",
        description=(
            "Simple triads punched on every beat under the melody, 1950s style. "
            "Sung on 'doo'."
        ),
        syllable="Doo",
        rhythm="pulse_quarter",
        enrich=enrich_doowop,
        weights=VoicingWeights(close_bias=1.2, motion=1.1),
    ),
    "rhythmic_vamp": Style(
        id="rhythmic_vamp",
        name="Rhythmic vamp",
        description=(
            "Eighth-note backing chords for driving, up-tempo sections. Sung on "
            "'bm'."
        ),
        syllable="Bm",
        rhythm="pulse_eighth",
        enrich=enrich_doowop,
        weights=VoicingWeights(close_bias=1.4, motion=1.0),
    ),
    "cluster": Style(
        id="cluster",
        name="Contemporary cluster",
        description=(
            "Added ninths and seconds held close together for a modern, slightly "
            "astringent choral sound. Hummed."
        ),
        syllable="Mm",
        rhythm="sustain",
        enrich=enrich_cluster,
        weights=VoicingWeights(
            close_bias=2.6, max_upper_gap=8, ideal_gap_hi=4, min_gap=1,
            missing_tone=3.0, doubled_colour=3.0, motion=0.7,
        ),
    ),
    "sus_air": Style(
        id="sus_air",
        name="Suspended / airy",
        description=(
            "Sus2 colours with no third, floating and ambiguous. Good for "
            "intros and quiet passages."
        ),
        syllable="Oo",
        rhythm="sustain",
        enrich=enrich_sus,
        weights=VoicingWeights(open_bias=1.2, missing_tone=3.0, motion=0.7),
    ),
    "open_fifths": Style(
        id="open_fifths",
        name="Open fifths (organum)",
        description=(
            "Bare parallel fifths and octaves moving with the tune — medieval, "
            "stark, and deliberately hollow."
        ),
        syllable="Oo",
        rhythm="follow",
        generator="parallel",
        intervals=(-7, -12, -19),
        parallel_snap="none",
        allow_parallels=True,
        min_voices=2,
    ),
    "pop_stack": Style(
        id="pop_stack",
        name="Pop thirds stack",
        description=(
            "Diatonic thirds and sixths stacked straight below the melody. The "
            "simplest backing that still sounds intentional."
        ),
        syllable="Ah",
        rhythm="follow",
        generator="parallel",
        intervals=(-3, -7, -12),
        parallel_snap="chord",
        allow_parallels=True,
        min_voices=2,
    ),
    "drone": Style(
        id="drone",
        name="Pedal drone",
        description=(
            "A held root-and-fifth pedal under the melody. Ignores the bar's "
            "chord changes on purpose."
        ),
        syllable="Oo",
        rhythm="sustain",
        generator="drone",
        min_voices=2,
    ),
    "unison": Style(
        id="unison",
        name="Unison / octaves",
        description=(
            "No harmony: every voice sings the melody in its own octave. Use it "
            "to clear space before a big entrance."
        ),
        syllable="Ah",
        rhythm="follow",
        generator="unison",
        min_voices=1,
    ),
}

DEFAULT_STYLE = "satb_chorale"


def get_style(style_id: str | None) -> Style:
    return STYLES.get(style_id or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])


def get_ensemble(ensemble_id: str | None) -> Ensemble:
    return ENSEMBLES.get(ensemble_id or "satb", ENSEMBLES["satb"])
