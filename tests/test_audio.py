"""Audio transcription tests.

These run the real basic-pitch model over a synthesised melody, so they are the
slowest tests in the suite, but they are the only ones covering the audio half
of the pipeline.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analysis import analyze  # noqa: E402
from app.ingest.audio import _clean_line, is_audio_file, transcribe_audio  # noqa: E402

# "Twinkle, Twinkle, Little Star", the same tune the notated tests use.
MELODY = [60, 60, 67, 67, 69, 69, 67, None, 65, 65, 64, 64, 62, 62, 60, None]
TEMPO = 100


@pytest.fixture(scope="module")
def sung_wav(tmp_path_factory) -> str:
    """Synthesise a sung-sounding melody: harmonics, vibrato and an envelope."""
    sample_rate = 22050
    seconds_per_beat = 60 / TEMPO
    chunks = []
    for pitch in MELODY:
        t = np.linspace(0, seconds_per_beat, int(sample_rate * seconds_per_beat), endpoint=False)
        if pitch is None:
            chunks.append(np.zeros_like(t))
            continue
        frequency = 440 * 2 ** ((pitch - 69) / 12)
        vibrato = 1 + 0.004 * np.sin(2 * np.pi * 5 * t)
        wave = sum(
            np.sin(2 * np.pi * frequency * harmonic * t * vibrato) * amplitude
            for harmonic, amplitude in [(1, 1.0), (2, 0.35), (3, 0.15), (4, 0.07)]
        )
        envelope = np.clip(np.minimum(t / 0.03, (seconds_per_beat - t) / 0.06), 0, 1)
        chunks.append(wave * envelope * 0.2)

    audio = np.concatenate(chunks)
    audio += np.random.default_rng(0).normal(0, 0.001, len(audio))
    path = tmp_path_factory.mktemp("audio") / "sung.wav"
    sf.write(str(path), audio.astype(np.float32), sample_rate)
    return str(path)


def test_recognises_audio_extensions():
    assert is_audio_file("song.mp3") and is_audio_file("SONG.WAV")
    assert not is_audio_file("score.musicxml")


def test_transcription_recovers_the_melody(sung_wav):
    source = transcribe_audio(sung_wav, title_hint="Twinkle")

    assert source.source_kind == "audio"
    assert abs(source.tempo - TEMPO) < 8, f"tempo drifted: {source.tempo}"
    assert source.transcription_note

    # Every distinct pitch of the tune, in order, ignoring repeats and rests.
    expected: list[int] = []
    for pitch in MELODY:
        if pitch is not None and (not expected or expected[-1] != pitch):
            expected.append(pitch)

    got: list[int] = []
    for note in source.all_notes:
        if note.pitch is not None and (not got or got[-1] != note.pitch):
            got.append(note.pitch)

    assert got == expected, f"expected {expected}, transcribed {got}"


def test_transcribed_audio_analyses_to_the_right_key(sung_wav):
    source = transcribe_audio(sung_wav)
    key, harmony, _ = analyze(source)

    assert key.name == "C major"
    assert harmony[0].segments[0].chord.root_pc == 0  # opens on the tonic
    assert len(harmony) == len(source.bars)


def test_cleanup_merges_fragments_and_drops_wobble():
    """One note split into fragments with a vibrato blip must come back as one."""
    events = [
        (0.00, 0.60, 60, 0.85),  # note, first fragment
        (0.60, 0.90, 60, 0.80),  # same note, re-triggered mid-note
        (0.90, 1.15, 61, 0.35),  # quiet vibrato blip a semitone up
        (1.20, 1.80, 67, 0.85),  # genuine leap to a new note
    ]
    cleaned = _clean_line(events, merge_repeats=True)
    assert [event[2] for event in cleaned] == [60, 67]
    assert cleaned[0][0] == 0.0 and cleaned[0][1] == pytest.approx(1.15)


def test_cleanup_keeps_a_real_melodic_step():
    """Two notes sung at a similar level are melody, not wobble, even a step apart."""
    events = [(0.0, 0.6, 60, 0.8), (0.6, 1.2, 62, 0.8)]
    cleaned = _clean_line(events, merge_repeats=True)
    assert [event[2] for event in cleaned] == [60, 62]


def test_cleanup_keeps_a_short_loud_note_after_a_long_one():
    """A note the model clipped short must survive if it was sung at full voice.

    This is the failure that matters most: a real note truncated by the model
    looks brief next to the long note before it, and only its loudness marks it
    out as something the singer meant.
    """
    events = [(0.0, 1.2, 62, 0.81), (1.22, 1.47, 60, 0.86)]
    cleaned = _clean_line(events, merge_repeats=True)
    assert [event[2] for event in cleaned] == [62, 60]


def test_merge_repeats_can_be_disabled():
    events = [(0.0, 0.5, 60, 0.8), (0.5, 1.0, 60, 0.8)]
    assert len(_clean_line(events, merge_repeats=True)) == 1
    assert len(_clean_line(events, merge_repeats=False)) == 2
