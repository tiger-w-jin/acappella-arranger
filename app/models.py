"""Internal score representation and the request/response schemas for the API."""

from __future__ import annotations

from dataclasses import dataclass, field

from typing import Annotated

from pydantic import BaseModel, Field

DEFAULT_STYLE_ID = "satb_chorale"

# Ceilings on anything a caller controls. Without them a single request can
# make the server do unbounded work: a 1 MB lyric, a 5000-bar project, or a
# tempo of infinity that only failed at JSON encoding time, as a 500.
MAX_BARS = 1000
MAX_TEXT = 20_000
MAX_COMMAND = 2_000


@dataclass
class MelodyNote:
    """A single melodic event. `pitch` is None for a rest."""

    pitch: int | None
    offset: float  # quarter lengths from the start of the piece
    duration: float
    lyric: str | None = None
    syllabic: str | None = None  # begin/middle/end/single, for rebuilding hyphens
    tied_from_previous: bool = False  # continuation of a note split at a barline

    @property
    def end(self) -> float:
        return self.offset + self.duration


@dataclass
class Bar:
    index: int
    offset: float
    length: float  # quarter lengths
    beats: int
    beat_type: int
    notes: list[MelodyNote] = field(default_factory=list)


@dataclass
class SourceScore:
    """Normalized input, whatever the upload actually was."""

    bars: list[Bar]
    tempo: float
    title: str
    source_kind: str  # "score" | "midi" | "audio"
    pickup_quarters: float = 0.0
    notes_dropped: int = 0
    transcription_note: str | None = None
    # Harmony the file stated outright, rather than something inferred from it.
    source_chords: list = field(default_factory=list)   # list[SourceChord]
    texture: list = field(default_factory=list)         # list[Sonority]

    @property
    def all_notes(self) -> list[MelodyNote]:
        return [n for bar in self.bars for n in bar.notes if n.pitch is not None]


# --------------------------------------------------------------------------
# API schemas
# --------------------------------------------------------------------------


class BarAnalysis(BaseModel):
    index: int
    beats: int
    beat_type: int
    melody: list[list[float | None]] = Field(
        description="Each entry is [midi_or_null, offset_in_bar, duration]."
    )
    chord: str = Field(description="Inferred chord symbol, e.g. 'Cmaj7'.")
    roman: str = Field(description="Roman numeral of the chord in the detected key.")
    root_pc: int
    quality: str
    style: str = Field(description="Style id currently assigned to this bar.")


class AnalysisResponse(BaseModel):
    session_id: str
    title: str
    source_kind: str
    key: str
    key_confidence: float
    tempo: float
    time_signature: str
    bar_count: int
    bars: list[BarAnalysis]
    harmony_source: str = Field(
        default="inferred",
        description="Where the chords came from: 'symbols' (the file's own chord "
                    "symbols), 'texture' (read off its parts), or 'inferred'.",
    )
    source_lyrics: str | None = Field(
        default=None, description="Lyrics the uploaded file already carried, ready to edit."
    )
    transcription_note: str | None = None
    warnings: list[str] = Field(default_factory=list)


class BarSpec(BaseModel):
    """Per-bar user overrides."""

    index: int = Field(ge=0, lt=MAX_BARS)
    style: str | None = None
    chord: str | None = Field(
        default=None, max_length=32,
        description="Override chord symbol, e.g. 'Ab7' or 'F#m7'.",
    )


class ArrangeRequest(BaseModel):
    session_id: str
    ensemble: str = "satb"
    default_style: str = "satb_chorale"
    bars: list[BarSpec] = Field(default_factory=list, max_length=MAX_BARS)
    transpose: int = Field(default=0, ge=-12, le=12)
    # allow_inf_nan=False: an infinite tempo otherwise reached JSON encoding and
    # failed there with a 500 rather than a clean rejection.
    tempo: Annotated[float, Field(gt=0, le=400, allow_inf_nan=False)] | None = None
    include_lyrics: bool = True
    lyrics: str | None = Field(
        default=None,
        max_length=MAX_TEXT,
        description="Words to sing. Space separates words, hyphen splits a word "
                    "across notes: 'A-ma-zing grace how sweet the sound'.",
    )
    lyrics_all_voices: bool = Field(
        default=False,
        description="Put the words under every part, not just the melody. Only "
                    "applies where a backing chord lands on a melody note.",
    )


class ArrangeResponse(BaseModel):
    session_id: str
    arrangement_id: str
    musicxml: str
    voices: list[str]
    ensemble: str
    key: str
    lyric_layout: list[list] = Field(
        default_factory=list,
        description="One [bar_index, syllable_or_null] per melody note, for the "
                    "alignment strip. Derived from the arrangement itself, so it "
                    "cannot disagree with the score.",
    )
    warnings: list[str] = Field(default_factory=list)


class CommandRequest(BaseModel):
    session_id: str
    text: str = Field(
        max_length=MAX_COMMAND,
        description="A typed instruction, e.g. 'bars 9-16 barbershop'.",
    )
    tempo: Annotated[float, Field(gt=0, le=400, allow_inf_nan=False)] | None = Field(
        default=None, description="Current tempo, so 'faster' has something to work from."
    )


class CommandResponse(BaseModel):
    understood: bool
    source: str = Field(description="'rules' or 'llm' — which engine produced the plan.")
    summary: str = Field(description="Human-readable account of what was applied.")
    actions: list[dict] = Field(
        default_factory=list, description="Edits to apply: style/ensemble/transpose/tempo."
    )
    unparsed: list[str] = Field(default_factory=list)
    message: str | None = None


class FitRequest(BaseModel):
    session_id: str
    ensembles: list[str] | None = Field(
        default=None, description="Ensembles to consider; all of them when omitted."
    )
    keep_key: bool = Field(default=False, description="Only consider the original key.")
    default_style: str = DEFAULT_STYLE_ID


class FitOption(BaseModel):
    ensemble: str
    ensemble_name: str
    transpose: int
    out_of_range: int
    strain: float
    score: float
    key: str
    summary: str


class FitResponse(BaseModel):
    best: FitOption | None
    options: list[FitOption] = Field(default_factory=list)
    current: FitOption | None = None


class ProjectBar(BaseModel):
    beats: int = Field(ge=1, le=32)
    beat_type: int = Field(ge=1, le=64)
    # A zero-length bar would place every later bar on top of it.
    length: Annotated[float, Field(gt=0, le=64, allow_inf_nan=False)]
    melody: list[list[float | None]] = Field(max_length=512)
    chord: str = Field(default="", max_length=32)
    roman: str = Field(default="", max_length=32)


class RestoreRequest(BaseModel):
    """Enough of a project to rebuild a session without the original file."""

    title: str = Field(default="Restored project", max_length=200)
    source_kind: str = Field(default="score", max_length=16)
    tempo: Annotated[float, Field(gt=0, le=400, allow_inf_nan=False)] = 96.0
    key: str | None = Field(default=None, max_length=32)
    bars: list[ProjectBar] = Field(max_length=MAX_BARS)


class HyphenateRequest(BaseModel):
    text: str = Field(max_length=MAX_TEXT)
    lang: str = Field(default="en", max_length=16)


class HyphenateResponse(BaseModel):
    text: str


class StyleInfo(BaseModel):
    id: str
    name: str
    description: str
    min_voices: int
    syllable: str


class EnsembleInfo(BaseModel):
    id: str
    name: str
    voices: list[str]
    melody_voice: str
