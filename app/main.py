"""FastAPI application: upload, analyse, arrange, export."""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .analysis import BarHarmony, Segment, analyze, infer_harmony, parse_chord_symbol
from .export import to_midi_bytes, to_musicxml
from .harmony.arranger import build_arrangement
from .harmony.styles import DEFAULT_STYLE, ENSEMBLES, STYLES, get_ensemble
from .ingest.audio import AUDIO_SUFFIXES, is_audio_file, transcribe_audio
from .ingest.score import SCORE_SUFFIXES, is_score_file, parse_score_file
from .models import (
    AnalysisResponse,
    ArrangeRequest,
    ArrangeResponse,
    BarAnalysis,
    EnsembleInfo,
    SourceScore,
    StyleInfo,
)
from .theory import KeyContext

_log = logging.getLogger("acappella")

MAX_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_SESSIONS = 40
MAX_ARRANGEMENTS = 80

STATIC_DIR = Path(__file__).parent / "static"
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"

app = FastAPI(title="A Cappella Arranger", version="1.0")


@dataclass
class Session:
    source: SourceScore
    key: KeyContext
    harmony: list[BarHarmony]
    chords_per_bar: int


class _Store(OrderedDict):
    """Bounded LRU so a long-running local server cannot grow without limit."""

    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit

    def put(self, key: str, value) -> None:
        self[key] = value
        self.move_to_end(key)
        while len(self) > self.limit:
            self.popitem(last=False)

    def fetch(self, key: str):
        if key not in self:
            return None
        self.move_to_end(key)
        return self[key]


SESSIONS: _Store = _Store(MAX_SESSIONS)
ARRANGEMENTS: _Store = _Store(MAX_ARRANGEMENTS)


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------


@app.get("/api/catalog")
def catalog() -> dict:
    return {
        "styles": [
            StyleInfo(
                id=style.id,
                name=style.name,
                description=style.description,
                min_voices=style.min_voices,
                syllable=style.syllable,
            ).model_dump()
            for style in STYLES.values()
        ],
        "ensembles": [
            EnsembleInfo(
                id=ensemble.id,
                name=ensemble.name,
                voices=[voice.name for voice in ensemble.voices],
                melody_voice=ensemble.voices[ensemble.melody_index].name,
            ).model_dump()
            for ensemble in ENSEMBLES.values()
        ],
        "default_style": DEFAULT_STYLE,
        "accepted_score": sorted(SCORE_SUFFIXES),
        "accepted_audio": sorted(AUDIO_SUFFIXES),
    }


# --------------------------------------------------------------------------
# Upload and analysis
# --------------------------------------------------------------------------


def _analysis_response(session_id: str, session: Session) -> AnalysisResponse:
    source = session.source
    bars = []
    for bar, bar_harmony in zip(source.bars, session.harmony):
        primary = bar_harmony.segments[0]
        bars.append(
            BarAnalysis(
                index=bar.index,
                beats=bar.beats,
                beat_type=bar.beat_type,
                melody=[
                    [note.pitch, round(note.offset - bar.offset, 4), round(note.duration, 4)]
                    for note in bar.notes
                ],
                chord=bar_harmony.symbol,
                roman=bar_harmony.roman,
                root_pc=primary.chord.root_pc,
                quality=primary.chord.quality,
                style=DEFAULT_STYLE,
            )
        )

    first = source.bars[0] if source.bars else None
    return AnalysisResponse(
        session_id=session_id,
        title=source.title,
        source_kind=source.source_kind,
        key=session.key.name,
        key_confidence=session.key.confidence,
        tempo=source.tempo,
        time_signature=f"{first.beats}/{first.beat_type}" if first else "4/4",
        bar_count=len(source.bars),
        bars=bars,
        transcription_note=source.transcription_note,
    )


@app.post("/api/upload", response_model=AnalysisResponse)
async def upload(
    file: UploadFile = File(...),
    beats: int = Form(4),
    beat_type: int = Form(4),
    chords_per_bar: int = Form(2),
    merge_repeats: bool = Form(True),
) -> AnalysisResponse:
    filename = file.filename or "upload"
    payload = await file.read()
    if not payload:
        raise HTTPException(400, "The uploaded file is empty.")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")

    suffix = Path(filename).suffix.lower()
    if not (is_score_file(filename) or is_audio_file(filename)):
        raise HTTPException(
            415,
            f"Unsupported file type '{suffix or filename}'. Upload a score "
            f"({', '.join(sorted(SCORE_SUFFIXES))}) or audio "
            f"({', '.join(sorted(AUDIO_SUFFIXES))}).",
        )

    handle, temp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(handle, "wb") as target:
            target.write(payload)

        title_hint = Path(filename).stem.replace("_", " ").replace("-", " ").strip()
        try:
            if is_audio_file(filename):
                source = transcribe_audio(
                    temp_path,
                    title_hint=title_hint,
                    beats=beats,
                    beat_type=beat_type,
                    merge_repeats=merge_repeats,
                )
            else:
                source = parse_score_file(temp_path, title_hint=title_hint)
        except HTTPException:
            raise
        except Exception as error:
            _log.exception("failed to read %s", filename)
            raise HTTPException(422, f"Could not read this file: {error}") from error
    finally:
        Path(temp_path).unlink(missing_ok=True)

    if not source.all_notes:
        raise HTTPException(422, "No melody notes were found in this file.")

    key, harmony = analyze(source, chords_per_bar=chords_per_bar)
    session_id = uuid.uuid4().hex[:12]
    SESSIONS.put(
        session_id,
        Session(source=source, key=key, harmony=harmony, chords_per_bar=chords_per_bar),
    )
    stored: Session = SESSIONS.fetch(session_id)
    return _analysis_response(session_id, stored)


@app.post("/api/reanalyze", response_model=AnalysisResponse)
def reanalyze(session_id: str = Form(...), chords_per_bar: int = Form(2)) -> AnalysisResponse:
    """Redo the chord analysis at a different harmonic rhythm."""
    session: Session | None = SESSIONS.fetch(session_id)
    if session is None:
        raise HTTPException(404, "Session expired. Please upload the file again.")

    session.harmony = infer_harmony(session.source, session.key, chords_per_bar)
    session.chords_per_bar = chords_per_bar
    return _analysis_response(session_id, session)


# --------------------------------------------------------------------------
# Arranging
# --------------------------------------------------------------------------


@app.post("/api/arrange", response_model=ArrangeResponse)
def arrange(request: ArrangeRequest) -> ArrangeResponse:
    session: Session | None = SESSIONS.fetch(request.session_id)
    if session is None:
        raise HTTPException(404, "Session expired. Please upload the file again.")

    if request.default_style not in STYLES:
        raise HTTPException(400, f"Unknown style '{request.default_style}'.")
    if request.ensemble not in ENSEMBLES:
        raise HTTPException(400, f"Unknown ensemble '{request.ensemble}'.")

    warnings: list[str] = []
    bar_styles: dict[int, str] = {}
    harmony = [
        BarHarmony(index=bar.index, segments=list(bar.segments)) for bar in session.harmony
    ]

    for spec in request.bars:
        if not 0 <= spec.index < len(harmony):
            continue
        if spec.style:
            if spec.style not in STYLES:
                raise HTTPException(400, f"Unknown style '{spec.style}' on bar {spec.index + 1}.")
            bar_styles[spec.index] = spec.style
        if spec.chord:
            parsed = parse_chord_symbol(spec.chord)
            if parsed is None:
                warnings.append(
                    f"Bar {spec.index + 1}: could not read the chord '{spec.chord}', "
                    "so the detected chord was kept."
                )
                continue
            bar = session.source.bars[spec.index]
            harmony[spec.index].segments = [
                Segment(
                    bar_index=spec.index,
                    start=bar.offset,
                    duration=bar.length,
                    chord=parsed,
                    roman="",
                    symbol=parsed.symbol(session.key.prefer_flats),
                )
            ]

    ensemble = get_ensemble(request.ensemble)
    source = session.source
    if request.tempo:
        source = replace(source, tempo=max(20.0, min(300.0, request.tempo)))

    arrangement = build_arrangement(
        source,
        session.key,
        harmony,
        ensemble,
        bar_styles,
        default_style=request.default_style,
        transpose=request.transpose,
        include_lyrics=request.include_lyrics,
    )

    try:
        musicxml = to_musicxml(arrangement)
        midi = to_midi_bytes(arrangement)
    except Exception as error:
        _log.exception("export failed")
        raise HTTPException(500, f"Could not render the arrangement: {error}") from error

    arrangement_id = uuid.uuid4().hex[:12]
    stem = "".join(
        char if char.isalnum() or char in " -_" else "_" for char in arrangement.title
    ).strip() or "arrangement"
    ARRANGEMENTS.put(
        arrangement_id,
        {
            "musicxml": musicxml,
            "midi": midi,
            "stem": f"{stem} ({ensemble.id})",
        },
    )

    return ArrangeResponse(
        session_id=request.session_id,
        arrangement_id=arrangement_id,
        musicxml=musicxml,
        voices=[voice.name for voice in ensemble.voices],
        ensemble=ensemble.id,
        key=arrangement.key.name,
        warnings=warnings + arrangement.warnings,
    )


@app.get("/api/arrangement/{arrangement_id}.mid")
def download_midi(arrangement_id: str) -> Response:
    record = ARRANGEMENTS.fetch(arrangement_id)
    if record is None:
        raise HTTPException(404, "That arrangement is no longer available.")
    return Response(
        content=record["midi"],
        media_type="audio/midi",
        headers={"Content-Disposition": f'attachment; filename="{record["stem"]}.mid"'},
    )


@app.get("/api/arrangement/{arrangement_id}.musicxml")
def download_musicxml(arrangement_id: str) -> Response:
    record = ARRANGEMENTS.fetch(arrangement_id)
    if record is None:
        raise HTTPException(404, "That arrangement is no longer available.")
    return Response(
        content=record["musicxml"],
        media_type="application/vnd.recordare.musicxml+xml",
        headers={"Content-Disposition": f'attachment; filename="{record["stem"]}.musicxml"'},
    )


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "sessions": len(SESSIONS), "arrangements": len(ARRANGEMENTS)}


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if SAMPLES_DIR.is_dir():
    app.mount("/samples", StaticFiles(directory=SAMPLES_DIR), name="samples")
