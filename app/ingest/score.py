"""Read notated input (MusicXML, MIDI, ABC, humdrum) into a SourceScore."""

from __future__ import annotations

import math
from fractions import Fraction

from music21 import chord, converter, meter, note, stream, tempo

from ..models import Bar, MelodyNote, SourceScore
from .harmony_source import has_multiple_parts, read_chord_symbols, read_texture

SCORE_SUFFIXES = {".xml", ".musicxml", ".mxl", ".mid", ".midi", ".abc", ".krn"}

# Past this the UI is unusable and every arrange takes many seconds, so it is
# better to refuse clearly than to appear to hang.
MAX_BARS = 1000


def _to_float(value) -> float:
    if isinstance(value, Fraction):
        return float(value)
    return float(value)


def _pick_melody_part(score: stream.Score) -> stream.Part:
    """Choose the part carrying the tune: the highest-sitting one with content.

    Real scores put the melody on top far more often than not, and using median
    pitch rather than a hard "part 0" rule survives scores whose first part is a
    percussion or chord-symbol staff.
    """
    parts = list(score.parts) if hasattr(score, "parts") else []
    if not parts:
        flat = score.flatten()
        holder = stream.Part()
        for element in flat.notesAndRests:
            holder.insert(element.offset, element)
        return holder

    best_part = None
    best_score = -1.0
    for part in parts:
        pitches = []
        for element in part.recurse().notes:
            if isinstance(element, chord.Chord):
                pitches.append(max(p.midi for p in element.pitches))
            elif isinstance(element, note.Note):
                pitches.append(element.pitch.midi)
        if not pitches:
            continue
        pitches.sort()
        median = pitches[len(pitches) // 2]
        # Weight by note count so a two-note cue staff cannot win on pitch alone.
        rank = median + min(len(pitches), 40) * 0.15
        if rank > best_score:
            best_score = rank
            best_part = part

    return best_part if best_part is not None else parts[0]


def _time_signature(part: stream.Part, score: stream.Score) -> meter.TimeSignature:
    for source in (part, score):
        found = source.recurse().getElementsByClass(meter.TimeSignature)
        if found:
            return found[0]
    return meter.TimeSignature("4/4")


def _tempo_bpm(score: stream.Score) -> float:
    marks = score.recurse().getElementsByClass(tempo.MetronomeMark)
    for mark in marks:
        if mark.number:
            return float(mark.number)
    return 96.0


def parse_score_file(path: str, title_hint: str = "") -> SourceScore:
    parsed = converter.parse(path)
    score = parsed if isinstance(parsed, stream.Score) else stream.Score([parsed])

    melody_part = _pick_melody_part(score)
    melody_part = melody_part.stripTies()

    time_sig = _time_signature(melody_part, score)
    bar_quarters = _to_float(time_sig.barDuration.quarterLength)
    bpm = _tempo_bpm(score)

    flat = melody_part.flatten()
    events: list[MelodyNote] = []
    for element in flat.notes:
        if isinstance(element, chord.Chord):
            pitch = max(p.midi for p in element.pitches)
        elif isinstance(element, note.Note):
            pitch = element.pitch.midi
        else:
            continue
        duration = _to_float(element.duration.quarterLength)
        if duration <= 0:
            continue
        # Take the first verse only. A score with several verses stacks a
        # Lyric per verse on each note, and music21's `.lyric` joins them with
        # newlines -- which would interleave the verses into nonsense.
        lyric = None
        syllabic = None
        lines = getattr(element, "lyrics", None) or []
        if lines:
            lyric = (lines[0].text or "").strip() or None
            syllabic = getattr(lines[0], "syllabic", None)
        events.append(
            MelodyNote(
                pitch=pitch,
                offset=_to_float(element.offset),
                duration=duration,
                lyric=lyric,
                syllabic=syllabic,
            )
        )

    events.sort(key=lambda n: (n.offset, -(n.pitch or 0)))
    events = _make_monophonic(events)

    title = ""
    if score.metadata is not None and score.metadata.title:
        title = str(score.metadata.title)
    title = title or title_hint or "Untitled"

    # Prefer the barlines the file already has: they carry pickups and any
    # mid-piece metre change, both of which are lost by re-barring everything
    # to whatever time signature happened to come first.
    pickup = _detect_pickup(melody_part, bar_quarters)
    bars = bars_from_measures(melody_part, events)
    if bars is None:
        bars = layout_bars(events, bar_quarters, time_sig.numerator, time_sig.denominator, pickup)

    # What the file says about its own harmony, in order of how much it is worth
    # believing: written chord symbols first, then the real multi-part texture.
    source_chords = read_chord_symbols(score)
    texture = [] if source_chords else read_texture(score, bar_quarters / 2)

    return SourceScore(
        bars=bars,
        source_chords=source_chords,
        texture=texture,
        tempo=bpm,
        title=title,
        source_kind="midi" if path.lower().endswith((".mid", ".midi")) else "score",
        pickup_quarters=pickup,
    )


def bars_from_measures(part: stream.Part, events: list[MelodyNote]) -> list[Bar] | None:
    """Use the score's own measures, so a metre change survives ingest.

    Returns None when the part has no usable measures, leaving the caller to
    fall back to slicing at a fixed bar length.
    """
    measures = [m for m in part.getElementsByClass(stream.Measure)]
    if len(measures) < 1:
        return None

    bounds: list[tuple[float, float, int, int]] = []
    for measure in measures:
        start = _to_float(measure.offset)
        length = _to_float(measure.duration.quarterLength)
        if length <= 0:
            continue
        signature = measure.timeSignature or measure.getContextByClass(meter.TimeSignature)
        beats = signature.numerator if signature else 4
        beat_type = signature.denominator if signature else 4
        bounds.append((start, length, beats, beat_type))

    if not bounds:
        return None
    if len(bounds) > MAX_BARS:
        raise ValueError(
            f"This piece has more than {MAX_BARS} bars, which is beyond what the "
            "arranger handles. Try a shorter excerpt."
        )

    bars = [
        Bar(index=i, offset=start, length=length, beats=beats, beat_type=beat_type)
        for i, (start, length, beats, beat_type) in enumerate(bounds)
    ]

    # Anything sounding past the final barline (music21 occasionally reports a
    # short last measure) still needs somewhere to live.
    last = bars[-1]
    total = max((e.end for e in events), default=0.0)
    if total > last.offset + last.length + 1e-6:
        last.length = total - last.offset

    _place_notes(bars, events)
    return bars


def _place_notes(bars: list[Bar], events: list[MelodyNote]) -> None:
    """Drop each note into its bar, splitting anything crossing a barline."""
    for event in events:
        if event.pitch is None:
            continue
        start = event.offset
        remaining = event.duration
        is_continuation = False
        while remaining > 1e-6:
            bar = _bar_at(bars, start)
            if bar is None:
                break
            bar_end = bar.offset + bar.length
            span = min(remaining, bar_end - start)
            if span <= 1e-6:
                break
            bar.notes.append(
                MelodyNote(
                    pitch=event.pitch,
                    offset=start,
                    duration=round(span, 6),
                    lyric=event.lyric if not is_continuation else None,
                    syllabic=event.syllabic if not is_continuation else None,
                    tied_from_previous=is_continuation,
                )
            )
            is_continuation = True
            start += span
            remaining -= span

    for bar in bars:
        bar.notes.sort(key=lambda n: n.offset)


def _detect_pickup(part: stream.Part, bar_quarters: float) -> float:
    """Length of an incomplete opening bar, in quarters (0 when there is none)."""
    measures = list(part.getElementsByClass(stream.Measure))
    if not measures:
        return 0.0
    first = measures[0]
    length = _to_float(first.duration.quarterLength)
    if 0 < length < bar_quarters - 1e-6:
        return length
    return 0.0


def _make_monophonic(events: list[MelodyNote]) -> list[MelodyNote]:
    """Collapse overlaps by keeping the top voice and truncating what it covers."""
    result: list[MelodyNote] = []
    for event in events:
        if not result:
            result.append(event)
            continue
        previous = result[-1]
        if event.offset < previous.end - 1e-6:
            if event.pitch is not None and previous.pitch is not None and event.pitch <= previous.pitch:
                # Lower simultaneous note: it is accompaniment, not the tune.
                continue
            previous.duration = max(0.0, event.offset - previous.offset)
            if previous.duration <= 1e-6:
                result.pop()
        result.append(event)
    return [event for event in result if event.duration > 1e-6]


def layout_bars(
    events: list[MelodyNote],
    bar_quarters: float,
    beats: int,
    beat_type: int,
    pickup: float = 0.0,
) -> list[Bar]:
    """Slice a flat note list into bars, splitting notes that straddle barlines."""
    # A non-positive bar length would make the boundary walk below never
    # advance, appending forever until the process dies. Callers are validated,
    # but this is the loop that would hang, so it defends itself.
    if not bar_quarters or bar_quarters <= 0:
        bar_quarters = 4.0
        beats, beat_type = 4, 4

    if not events:
        return [Bar(index=0, offset=0.0, length=bar_quarters, beats=beats, beat_type=beat_type)]

    total = max(event.end for event in events)
    boundaries = [0.0]
    if pickup > 0:
        boundaries.append(pickup)
    # Bound the walk as well: a corrupt file reporting an enormous duration
    # should be refused, not turned into millions of empty bars.
    while boundaries[-1] < total - 1e-6:
        boundaries.append(boundaries[-1] + bar_quarters)
        if len(boundaries) > MAX_BARS + 2:
            raise ValueError(
                f"This piece would need more than {MAX_BARS} bars, which is beyond "
                "what the arranger handles. Try a shorter excerpt."
            )

    bars: list[Bar] = []
    for index in range(len(boundaries) - 1 if len(boundaries) > 1 else 1):
        start = boundaries[index]
        end = boundaries[index + 1] if index + 1 < len(boundaries) else start + bar_quarters
        bars.append(
            Bar(
                index=index,
                offset=start,
                length=round(end - start, 6),
                beats=beats,
                beat_type=beat_type,
            )
        )

    for event in events:
        if event.pitch is None:
            continue
        start = event.offset
        remaining = event.duration
        is_continuation = False
        while remaining > 1e-6:
            bar = _bar_at(bars, start)
            if bar is None:
                break
            bar_end = bar.offset + bar.length
            span = min(remaining, bar_end - start)
            if span > 1e-6:
                bar.notes.append(
                    MelodyNote(
                        pitch=event.pitch,
                        offset=start,
                        duration=round(span, 6),
                        lyric=event.lyric if not is_continuation else None,
                        syllabic=event.syllabic if not is_continuation else None,
                        tied_from_previous=is_continuation,
                    )
                )
                is_continuation = True
            start += span
            remaining -= span

    for bar in bars:
        bar.notes.sort(key=lambda n: n.offset)
    return bars


def _bar_at(bars: list[Bar], offset: float) -> Bar | None:
    for bar in bars:
        if bar.offset - 1e-6 <= offset < bar.offset + bar.length - 1e-6:
            return bar
    return bars[-1] if bars and offset < bars[-1].offset + bars[-1].length + 1e-6 else None


def quantize(value: float, grid: float = 0.25) -> float:
    return round(round(value / grid) * grid, 6)


def snap_duration(value: float, grid: float = 0.25) -> float:
    return max(grid, quantize(value, grid))


def is_score_file(filename: str) -> bool:
    lowered = filename.lower()
    return any(lowered.endswith(suffix) for suffix in SCORE_SUFFIXES)


__all__ = [
    "parse_score_file",
    "layout_bars",
    "quantize",
    "snap_duration",
    "is_score_file",
    "SCORE_SUFFIXES",
    "math",
]
