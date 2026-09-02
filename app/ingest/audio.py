"""Audio -> melody transcription using Spotify's basic-pitch (ONNX backend).

The raw model output is polyphonic and noisy, so this module does three jobs:
transcribe, clean up the note soup into a single singable line, and place that
line on a beat grid so it can be barred like notated input.
"""

from __future__ import annotations

import logging
import os
import tempfile
import warnings
from pathlib import Path

import numpy as np

from ..models import MelodyNote, SourceScore
from ..theory import detect_key
from .score import layout_bars

AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".aiff", ".aif", ".wma"}

# Video containers people actually have a tune inside: a phone recording, a
# screen capture, a rehearsal clip. libsndfile cannot open any of them, so the
# audio track is extracted with ffmpeg first and the rest of the pipeline never
# knows the difference.
VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".flv", ".wmv", ".3gp"}

FFMPEG_TIMEOUT_SECONDS = 120

# Intervals above a louder simultaneous note that mark a detection as an
# overtone of that note rather than a real one.
_OVERTONE_INTERVALS = {12, 19, 24, 28, 31, 34, 36}

_log = logging.getLogger(__name__)


def is_audio_file(filename: str) -> bool:
    lowered = filename.lower()
    return any(lowered.endswith(s) for s in AUDIO_SUFFIXES | VIDEO_SUFFIXES)


def is_video_file(filename: str) -> bool:
    lowered = filename.lower()
    return any(lowered.endswith(suffix) for suffix in VIDEO_SUFFIXES)


def extract_audio_track(path: str) -> str:
    """Pull the audio out of a video container into a temp WAV, via ffmpeg.

    The arguments are passed as a list, never through a shell, so a filename
    cannot smuggle in anything. The caller owns the returned file.
    """
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError(
            "This looks like a video file, and extracting its audio needs ffmpeg, "
            "which is not installed. Convert it to WAV or MP3 first."
        )

    handle, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(handle)
    try:
        result = subprocess.run(
            [ffmpeg, "-v", "error", "-y", "-i", path,
             "-vn", "-ac", "1", "-ar", "22050", "-f", "wav", wav_path],
            capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        Path(wav_path).unlink(missing_ok=True)
        raise ValueError("Extracting audio from that video took too long; try a shorter clip.")

    if result.returncode != 0 or not os.path.getsize(wav_path):
        Path(wav_path).unlink(missing_ok=True)
        detail = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        hint = detail[-1] if detail else "no audio track found"
        raise ValueError(f"Could not read audio from that video: {hint}")

    return wav_path


def _predict(path: str):
    """Run basic-pitch, keeping its import noise out of the server log."""
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        previous = logging.root.manager.disable
        logging.disable(logging.WARNING)
        try:
            from basic_pitch import ICASSP_2022_MODEL_PATH
            from basic_pitch.inference import predict

            # A high onset threshold and a long minimum note matter a lot here:
            # on sustained, vibrato-heavy singing the model otherwise re-triggers
            # mid-note and shreds one long note into a run of short repeats.
            _, _, note_events = predict(
                path,
                ICASSP_2022_MODEL_PATH,
                onset_threshold=0.7,
                frame_threshold=0.3,
                minimum_note_length=180.0,
                melodia_trick=True,
            )
        finally:
            logging.disable(previous)
    return note_events


def _overlap(a: tuple, b: tuple) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def _strip_overtones(events: list[tuple]) -> list[tuple]:
    """Drop detections that are quiet overtones of a louder concurrent note."""
    keep: list[tuple] = []
    for event in events:
        start, end, pitch, amp = event[0], event[1], event[2], event[3]
        span = end - start
        shadowed = False
        for other in events:
            if other is event:
                continue
            o_amp, o_pitch = other[3], other[2]
            if o_amp <= amp * 1.15:
                continue
            interval = pitch - o_pitch
            if interval not in _OVERTONE_INTERVALS:
                continue
            if span > 0 and _overlap((start, end), (other[0], other[1])) / span > 0.5:
                shadowed = True
                break
        if not shadowed:
            keep.append(event)
    return keep


def _skyline(events: list[tuple]) -> list[tuple]:
    """Reduce polyphony to the top line, truncating notes a higher one covers."""
    ordered = sorted(events, key=lambda e: (e[0], -e[2]))
    line: list[list] = []
    for start, end, pitch, amp in ((e[0], e[1], e[2], e[3]) for e in ordered):
        if not line:
            line.append([start, end, pitch, amp])
            continue
        previous = line[-1]
        if start < previous[1] - 1e-6:
            if pitch <= previous[2]:
                continue  # lower simultaneous note: accompaniment
            previous[1] = start
            if previous[1] - previous[0] <= 0.04:
                line.pop()
        line.append([start, end, pitch, amp])
    return [tuple(item) for item in line if item[1] - item[0] > 0.04]


# A wobble has to be this short in absolute terms, and this small a fraction of
# the note it hangs off, before it is treated as noise rather than melody.
_WOBBLE_MAX_SECONDS = 0.30
_WOBBLE_MAX_SEMITONES = 2
# A wobble is also markedly quieter than the note it came off, which separates
# it from a genuine melodic step where both notes are sung at a similar level.
_WOBBLE_MAX_AMPLITUDE_RATIO = 0.65


_CONTIGUOUS_GAP = 0.08


def _merge_same_pitch(events: list[tuple]) -> tuple[list[tuple], bool]:
    """Join back-to-back detections of the same pitch into one note."""
    result: list[tuple] = []
    changed = False
    for event in events:
        if result:
            previous = result[-1]
            if previous[2] == event[2] and event[0] - previous[1] < _CONTIGUOUS_GAP:
                result[-1] = (previous[0], event[1], previous[2], max(previous[3], event[3]))
                changed = True
                continue
        result.append(event)
    return result, changed


def _absorb_wobble(events: list[tuple]) -> tuple[list[tuple], bool]:
    """Drop short detections a semitone or two off a much longer neighbour."""
    result = list(events)
    changed = False

    index = 1
    while index < len(result) - 1:
        before, blip, after = result[index - 1], result[index], result[index + 1]
        length = blip[1] - blip[0]
        if (
            before[2] == after[2]
            and abs(blip[2] - before[2]) <= _WOBBLE_MAX_SEMITONES
            and length < (before[1] - before[0])
            and length < (after[1] - after[0])
        ):
            result[index - 1 : index + 2] = [
                (before[0], after[1], before[2], max(before[3], after[3]))
            ]
            changed = True
            index = max(1, index - 1)
            continue
        index += 1

    index = 1
    while index < len(result):
        previous, blip = result[index - 1], result[index]
        previous_length = previous[1] - previous[0]
        length = blip[1] - blip[0]
        # Being quiet is the decisive test. Brevity alone is not enough: the
        # model often clips a real note short, and a genuine note that follows
        # a long one would otherwise be swallowed by it.
        if (
            abs(blip[2] - previous[2]) <= _WOBBLE_MAX_SEMITONES
            and length <= _WOBBLE_MAX_SECONDS
            and blip[3] <= previous[3] * _WOBBLE_MAX_AMPLITUDE_RATIO
            and blip[0] - previous[1] < _CONTIGUOUS_GAP
        ):
            result[index - 1 : index + 1] = [(previous[0], blip[1], previous[2], previous[3])]
            changed = True
            continue
        index += 1

    return result, changed


def _clean_line(events: list[tuple], merge_repeats: bool) -> list[tuple]:
    """Repair the two ways the model mangles a sustained sung note.

    It re-triggers mid-note, splitting one note into a run of short repeats, and
    it wanders a semitone during vibrato or a note's release. Fixing either one
    exposes more of the other -- a wobble only looks short enough to drop once
    the fragments it hangs off have been joined -- so both run to a fixed point.
    """
    result = [tuple(event) for event in events]
    for _ in range(8):
        changed = False
        if merge_repeats:
            result, merged = _merge_same_pitch(result)
            changed |= merged
        result, absorbed = _absorb_wobble(result)
        changed |= absorbed
        if not changed:
            break
    return result


def _beat_grid(path: str) -> tuple[float, np.ndarray]:
    import librosa

    y, sr = librosa.load(path, sr=22050, mono=True)
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    bpm = float(np.atleast_1d(tempo)[0])
    if not np.isfinite(bpm) or bpm <= 0:
        bpm = 100.0
    return bpm, np.asarray(beat_times, dtype=float)


def _seconds_to_beats(times: np.ndarray, bpm: float, beat_times: np.ndarray) -> np.ndarray:
    """Map seconds onto a beat axis, following the detected beats where possible.

    Using the real beat positions rather than a single tempo keeps a human
    performance's rubato from smearing the quantization.
    """
    if beat_times.size < 2:
        return times * bpm / 60.0
    indices = np.arange(beat_times.size, dtype=float)
    seconds_per_beat = float(np.median(np.diff(beat_times))) or 60.0 / bpm
    return np.interp(
        times,
        beat_times,
        indices,
        left=(times[0] - beat_times[0]) / seconds_per_beat if times.size else 0.0,
        right=indices[-1] + (times[-1] - beat_times[-1]) / seconds_per_beat if times.size else 0.0,
    ) if times.size else times


def _snap_chromatic_slips(notes: list[MelodyNote], grid: float) -> list[MelodyNote]:
    """Remove short out-of-key notes that sit a semitone off their neighbour.

    Once the line is quantized its key is clear, and the leftover release-smear
    artefacts share a very specific signature: short, chromatic, and a semitone
    or two from the note they trail. Real accidentals are either long enough to
    matter or land on a strong beat, so both survive this.
    """
    pitched = [note for note in notes if note.pitch is not None]
    if len(pitched) < 4:
        return notes

    weights: dict[int, float] = {}
    for note in pitched:
        assert note.pitch is not None
        weights[note.pitch % 12] = weights.get(note.pitch % 12, 0.0) + note.duration
    scale = set(detect_key(weights).scale_pcs)

    result: list[MelodyNote] = []
    for note in notes:
        if (
            note.pitch is not None
            and result
            and result[-1].pitch is not None
            and note.pitch % 12 not in scale
            and note.duration <= grid
            and abs(note.pitch - result[-1].pitch) <= 2
            and abs(note.offset - result[-1].end) < 1e-6
        ):
            result[-1].duration += note.duration
            continue
        result.append(note)
    return result


def transcribe_audio(
    path: str,
    title_hint: str = "",
    beats: int = 4,
    beat_type: int = 4,
    grid: float = 0.5,
    merge_repeats: bool = True,
) -> SourceScore:
    """Transcribe an audio file into a barred, quantized single-line melody."""
    # A video container has to be unwrapped before anything can read it.
    extracted: str | None = None
    if is_video_file(path):
        extracted = extract_audio_track(path)
        path = extracted

    try:
        return _transcribe(
            path, title_hint, beats, beat_type, grid, merge_repeats
        )
    finally:
        if extracted:
            Path(extracted).unlink(missing_ok=True)


def _transcribe(
    path: str,
    title_hint: str,
    beats: int,
    beat_type: int,
    grid: float,
    merge_repeats: bool,
) -> SourceScore:
    raw = _predict(path)
    total_detected = len(raw)
    if not raw:
        raise ValueError(
            "No pitched notes were detected in this audio. Try a cleaner recording "
            "with a clear lead melody."
        )

    amplitudes = sorted(event[3] for event in raw)
    median_amp = amplitudes[len(amplitudes) // 2]
    floor = max(0.10, median_amp * 0.35)
    events = [event for event in raw if event[3] >= floor and event[1] - event[0] >= 0.06]
    events = _strip_overtones(events)
    events = _skyline(events)
    events = _clean_line(events, merge_repeats)

    if not events:
        raise ValueError("Detected notes were all filtered out as noise or overtones.")

    bpm, beat_times = _beat_grid(path)

    starts = np.array([event[0] for event in events], dtype=float)
    ends = np.array([event[1] for event in events], dtype=float)
    start_beats = _seconds_to_beats(starts, bpm, beat_times)
    end_beats = _seconds_to_beats(ends, bpm, beat_times)

    origin = start_beats[0]
    start_beats = start_beats - origin
    end_beats = end_beats - origin

    notes: list[MelodyNote] = []
    for index, event in enumerate(events):
        onset = round(round(start_beats[index] / grid) * grid, 6)
        release = round(round(end_beats[index] / grid) * grid, 6)
        duration = max(grid, release - onset)
        notes.append(MelodyNote(pitch=int(event[2]), offset=onset, duration=duration))

    notes.sort(key=lambda n: n.offset)
    merged: list[MelodyNote] = []
    for item in notes:
        if merged and item.offset < merged[-1].end - 1e-6:
            merged[-1].duration = max(grid, item.offset - merged[-1].offset)
        if merged and abs(item.offset - merged[-1].offset) < 1e-6:
            continue  # two notes quantized onto the same slot; keep the first
        merged.append(item)

    merged = _snap_chromatic_slips(merged, grid)

    bar_quarters = beats * (4.0 / beat_type)
    bars = layout_bars(merged, bar_quarters, beats, beat_type)

    dropped = total_detected - len(merged)
    return SourceScore(
        bars=bars,
        tempo=round(bpm, 1),
        title=title_hint or "Transcribed audio",
        source_kind="audio",
        notes_dropped=dropped,
        transcription_note=(
            f"Transcribed {len(merged)} melody notes at ~{round(bpm)} BPM, assuming "
            f"{beats}/{beat_type} ({dropped} detections dropped as overtones, noise, "
            "vibrato or inner voices)."
            + (
                " Repeated notes on the same pitch were merged into one — turn that "
                "off if your melody genuinely repeats notes."
                if merge_repeats
                else ""
            )
            + " Check the melody and chords before arranging."
        ),
    )
