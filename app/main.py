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

from . import llm
from .analysis import BarHarmony, Segment, analyze, infer_harmony, parse_chord_symbol
from .commands import examples as command_examples, parse_command
from .export import to_midi_bytes, to_musicxml, to_practice_midi
from .harmony.arranger import build_arrangement
from .harmony.styles import DEFAULT_STYLE, ENSEMBLES, STYLES, get_ensemble
from .ingest.audio import AUDIO_SUFFIXES, is_audio_file, transcribe_audio
from .ingest.score import SCORE_SUFFIXES, is_score_file, parse_score_file
from .models import (
    AnalysisResponse,
    ArrangeRequest,
    ArrangeResponse,
    BarAnalysis,
    CommandRequest,
    CommandResponse,
    FitOption,
    FitRequest,
    FitResponse,
    HyphenateRequest,
    HyphenateResponse,
    RestoreRequest,
    Bar,
    EnsembleInfo,
    MelodyNote,
    SourceScore,
    StyleInfo,
)
from .fit import find_fit
from .lyrics import auto_hyphenate, available_languages, rebuild as rebuild_lyrics
from .preview import preview_midi
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
    harmony_source: str = "inferred"


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
        "command_examples": command_examples(),
        "lyric_languages": available_languages(),
        "llm_enabled": llm.is_available(),
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

    # Words the file already carried, rebuilt with their hyphens so they can be
    # edited rather than retyped.
    written = rebuild_lyrics([
        (note.lyric or "", note.syllabic)
        for bar in source.bars for note in bar.notes
        if note.pitch is not None and note.lyric
    ])

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
        harmony_source=session.harmony_source,
        source_lyrics=written or None,
        transcription_note=source.transcription_note,
    )


@app.post("/api/upload", response_model=AnalysisResponse)
async def upload(
    file: UploadFile = File(...),
    beats: int = Form(4),
    beat_type: int = Form(4),
    chords_per_bar: int = Form(2),
    merge_repeats: bool = Form(True),
    harmony_source: str = Form("auto"),
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

    requested_harmony = harmony_source if harmony_source in ("auto", "infer") else "auto"

    key, harmony, harmony_source = analyze(
        source, chords_per_bar=chords_per_bar, harmony_source=requested_harmony
    )
    session_id = uuid.uuid4().hex[:12]
    SESSIONS.put(
        session_id,
        Session(
            source=source, key=key, harmony=harmony,
            chords_per_bar=chords_per_bar, harmony_source=harmony_source,
        ),
    )
    stored: Session = SESSIONS.fetch(session_id)
    return _analysis_response(session_id, stored)


@app.post("/api/reanalyze", response_model=AnalysisResponse)
def reanalyze(
    session_id: str = Form(...),
    chords_per_bar: int = Form(2),
    harmony_source: str = Form("auto"),
) -> AnalysisResponse:
    """Redo the chord analysis at a different harmonic rhythm."""
    session: Session | None = SESSIONS.fetch(session_id)
    if session is None:
        raise HTTPException(404, "Session expired. Please upload the file again.")

    wanted = harmony_source if harmony_source in ("auto", "infer") else "auto"
    _, session.harmony, session.harmony_source = analyze(
        session.source, chords_per_bar=chords_per_bar, harmony_source=wanted
    )
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
        lyrics=request.lyrics,
        lyrics_all_voices=request.lyrics_all_voices,
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
            "arrangement": arrangement,
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
        lyric_layout=[[bar, text] for bar, text in arrangement.lyric_layout],
        warnings=warnings + arrangement.warnings,
    )


@app.post("/api/command", response_model=CommandResponse)
def command(request: CommandRequest) -> CommandResponse:
    """Interpret a typed instruction into a plan of edits the UI can apply.

    The grammar answers first because it is exact and instant. Gemini is only
    consulted for phrasing the grammar rejects, and only when a key is set.
    """
    session: Session | None = SESSIONS.fetch(request.session_id)
    if session is None:
        raise HTTPException(404, "Session expired. Please upload the file again.")

    bar_count = len(session.source.bars)
    tempo = request.tempo or session.source.tempo

    plan = parse_command(request.text, bar_count, tempo)

    if not plan.understood and llm.is_available():
        fallback = llm.interpret(request.text, bar_count, tempo)
        if fallback is not None:
            plan = fallback

    payload = plan.to_dict()
    if not plan.understood:
        payload["message"] = (
            "I could not read that. Try something like \u201cbars 9-16 barbershop\u201d "
            "or \u201cfirst 8 chorale, the rest gospel\u201d."
            + ("" if llm.is_available() else " Set GEMINI_API_KEY to allow freer phrasing.")
        )
    return CommandResponse(**payload)


@app.post("/api/fit", response_model=FitResponse)
def fit(request: FitRequest) -> FitResponse:
    """Rank ensembles and keys by how singable the result is."""
    session: Session | None = SESSIONS.fetch(request.session_id)
    if session is None:
        raise HTTPException(404, "Session expired. Please upload the file again.")

    ranked = find_fit(
        session.source, session.key, session.harmony, {},
        default_style=request.default_style,
        ensembles=request.ensembles,
        keep_key=request.keep_key,
    )
    if not ranked:
        return FitResponse(best=None)

    options = [FitOption(**item.to_dict()) for item in ranked[:8]]
    current = next(
        (FitOption(**i.to_dict()) for i in ranked if i.transpose == 0 and i.ensemble == "satb"),
        None,
    )
    return FitResponse(best=options[0], options=options, current=current)


@app.get("/api/arrangement/{arrangement_id}/practice/{voice_index}.mid")
def practice_track(arrangement_id: str, voice_index: int) -> Response:
    """One part brought forward against the others, for learning that line."""
    record = ARRANGEMENTS.fetch(arrangement_id)
    if record is None:
        raise HTTPException(404, "That arrangement is no longer available.")
    arrangement = record.get("arrangement")
    if arrangement is None:
        raise HTTPException(409, "This arrangement predates practice tracks; re-arrange it.")
    if not 0 <= voice_index < arrangement.ensemble.size:
        raise HTTPException(404, f"No voice {voice_index} in {arrangement.ensemble.name}.")

    voice = arrangement.ensemble.voices[voice_index]
    return Response(
        content=to_practice_midi(arrangement, voice_index),
        media_type="audio/midi",
        headers={
            "Content-Disposition":
                f'attachment; filename="{record["stem"]} - {voice.name} practice.mid"'
        },
    )


@app.post("/api/restore", response_model=AnalysisResponse)
def restore(request: RestoreRequest) -> AnalysisResponse:
    """Rebuild a session from a saved project, so work survives a reload.

    Sessions live in this process's memory, so a refresh would otherwise mean
    re-uploading the file and redoing every per-bar choice.
    """
    if not request.bars:
        raise HTTPException(400, "That project has no bars in it.")

    bars: list[Bar] = []
    offset = 0.0
    for index, saved in enumerate(request.bars):
        bar = Bar(
            index=index,
            offset=offset,
            length=saved.length,
            beats=saved.beats,
            beat_type=saved.beat_type,
        )
        for entry in saved.melody:
            if not entry or entry[0] is None:
                continue
            bar.notes.append(
                MelodyNote(
                    pitch=int(entry[0]),
                    offset=offset + float(entry[1] or 0.0),
                    duration=float(entry[2] or 1.0),
                )
            )
        bars.append(bar)
        offset += saved.length

    source = SourceScore(
        bars=bars,
        tempo=request.tempo,
        title=request.title,
        source_kind=request.source_kind,
    )
    if not source.all_notes:
        raise HTTPException(422, "That project has no melody notes in it.")

    key, harmony, used = analyze(source)
    session_id = uuid.uuid4().hex[:12]
    SESSIONS.put(
        session_id,
        Session(source=source, key=key, harmony=harmony, chords_per_bar=2, harmony_source=used),
    )
    return _analysis_response(session_id, SESSIONS.fetch(session_id))


@app.post("/api/hyphenate", response_model=HyphenateResponse)
def hyphenate(request: HyphenateRequest) -> HyphenateResponse:
    """Split plain prose into sung syllables, leaving manual hyphens alone."""
    return HyphenateResponse(text=auto_hyphenate(request.text, request.lang))


@app.get("/api/preview/{style_id}.mid")
def preview(style_id: str, ensemble: str = "satb") -> Response:
    """A short ii-V-I rendered in one style, so the palette can audition it."""
    if style_id not in STYLES:
        raise HTTPException(404, f"Unknown style '{style_id}'.")
    if ensemble not in ENSEMBLES:
        raise HTTPException(400, f"Unknown ensemble '{ensemble}'.")
    return Response(
        content=preview_midi(style_id, ensemble),
        media_type="audio/midi",
        headers={"Cache-Control": "public, max-age=3600"},
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
