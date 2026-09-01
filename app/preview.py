"""Short audible demos of each harmony style.

A style's description only goes so far — "close harmony with barbershop
sevenths" means little until you hear one. This renders the same three-bar
ii-V-I in whichever style and ensemble you ask for, so the palette can preview
what a choice actually sounds like before it is applied to a real piece.

The chords are fixed rather than inferred: the point is to isolate the style,
so every preview must differ only by the style.
"""

from __future__ import annotations

from functools import lru_cache

from .analysis import BarHarmony, Segment
from .export import to_midi_bytes
from .harmony.arranger import build_arrangement
from .harmony.styles import get_ensemble, get_style
from .models import Bar, MelodyNote, SourceScore
from .theory import ChordSpec, KeyContext

# A ii-V-I in C major. The melody is deliberately plain so the backing voices
# are what you notice, and it steps down onto the third of the tonic so the
# cadence lands.
_MELODY = [
    # (midi, offset in quarters, duration)
    (69, 0.0, 1.0), (69, 1.0, 1.0), (65, 2.0, 1.0), (65, 3.0, 1.0),   # bar 1, over Dm
    (71, 4.0, 1.0), (69, 5.0, 1.0), (67, 6.0, 1.0), (65, 7.0, 1.0),   # bar 2, over G7
    (64, 8.0, 4.0),                                                    # bar 3, over C
]

_PROGRESSION = [
    ChordSpec(root_pc=2, quality="min7"),   # ii
    ChordSpec(root_pc=7, quality="dom7"),   # V
    ChordSpec(root_pc=0, quality="maj"),    # I
]

_TEMPO = 88.0


def _demo_score() -> SourceScore:
    bars = [
        Bar(index=index, offset=index * 4.0, length=4.0, beats=4, beat_type=4)
        for index in range(3)
    ]
    for pitch, offset, duration in _MELODY:
        bars[int(offset // 4)].notes.append(
            MelodyNote(pitch=pitch, offset=offset, duration=duration)
        )
    return SourceScore(bars=bars, tempo=_TEMPO, title="Style preview", source_kind="score")


def _demo_harmony() -> list[BarHarmony]:
    return [
        BarHarmony(
            index=index,
            segments=[
                Segment(
                    bar_index=index,
                    start=index * 4.0,
                    duration=4.0,
                    chord=chord,
                    roman="",
                    symbol=chord.symbol(),
                )
            ],
        )
        for index, chord in enumerate(_PROGRESSION)
    ]


@lru_cache(maxsize=128)
def preview_midi(style_id: str, ensemble_id: str) -> bytes:
    """MIDI for a three-bar ii-V-I in one style. Cached: the input is tiny and fixed."""
    style = get_style(style_id)
    ensemble = get_ensemble(ensemble_id)
    key = KeyContext(tonic_pc=0, mode="major")

    arrangement = build_arrangement(
        _demo_score(),
        key,
        _demo_harmony(),
        ensemble,
        bar_styles={index: style.id for index in range(3)},
        default_style=style.id,
        include_lyrics=False,
    )
    return to_midi_bytes(arrangement)
