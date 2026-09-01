"""Internal score representation and the request/response schemas for the API."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field


@dataclass
class MelodyNote:
    """A single melodic event. `pitch` is None for a rest."""

    pitch: int | None
    offset: float  # quarter lengths from the start of the piece
    duration: float
    lyric: str | None = None
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
    transcription_note: str | None = None
    warnings: list[str] = Field(default_factory=list)


class BarSpec(BaseModel):
    """Per-bar user overrides."""

    index: int
    style: str | None = None
    chord: str | None = Field(
        default=None, description="Override chord symbol, e.g. 'Ab7' or 'F#m7'."
    )


class ArrangeRequest(BaseModel):
    session_id: str
    ensemble: str = "satb"
    default_style: str = "satb_chorale"
    bars: list[BarSpec] = Field(default_factory=list)
    transpose: int = Field(default=0, ge=-12, le=12)
    tempo: float | None = None
    include_lyrics: bool = True
    lyrics: str | None = Field(
        default=None,
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
    warnings: list[str] = Field(default_factory=list)


class CommandRequest(BaseModel):
    session_id: str
    text: str = Field(description="A typed instruction, e.g. 'bars 9-16 barbershop'.")
    tempo: float | None = Field(
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
