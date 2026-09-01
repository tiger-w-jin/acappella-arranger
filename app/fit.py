"""Find a key and ensemble the singers in front of you can actually sing.

The arranger already warns that notes fall outside a part's comfortable range,
which tells you there is a problem and leaves you to solve it by trial and
error. This searches the transpositions and ensembles for you and says which
one fits, using the same voice ranges the voicing engine works to.
"""

from __future__ import annotations

from dataclasses import dataclass

from .analysis import BarHarmony
from .harmony.arranger import build_arrangement
from .harmony.styles import ENSEMBLES, Ensemble
from .models import SourceScore
from .theory import KeyContext


@dataclass
class FitResult:
    ensemble: str
    ensemble_name: str
    transpose: int
    out_of_range: int
    strain: float
    score: float
    key: str
    summary: str

    def to_dict(self) -> dict:
        return {
            "ensemble": self.ensemble,
            "ensemble_name": self.ensemble_name,
            "transpose": self.transpose,
            "out_of_range": self.out_of_range,
            "strain": round(self.strain, 2),
            "score": round(self.score, 2),
            "key": self.key,
            "summary": self.summary,
        }


def _measure(arrangement, ensemble: Ensemble) -> tuple[int, float]:
    """Count unsingable notes, and how hard the singable ones push.

    Notes outside a part's range are counted outright. Everything else
    contributes "strain": how close it sits to the edge of that voice, so a
    setting that is technically singable but sits everyone at the top of their
    range loses to one that sits comfortably.
    """
    outside = 0
    strain = 0.0
    total = 0

    for index, voice in enumerate(ensemble.voices):
        span = max(1, voice.hi - voice.lo)
        comfortable_low = voice.lo + span * 0.15
        comfortable_high = voice.hi - span * 0.15
        for event in arrangement.parts[index]:
            if event.pitch is None:
                continue
            total += 1
            if event.pitch < voice.lo or event.pitch > voice.hi:
                outside += 1
                strain += 1.0
            elif event.pitch < comfortable_low:
                strain += (comfortable_low - event.pitch) / span
            elif event.pitch > comfortable_high:
                strain += (event.pitch - comfortable_high) / span

    return outside, (strain / total if total else 0.0)


def find_fit(
    score: SourceScore,
    key: KeyContext,
    harmony: list[BarHarmony],
    bar_styles: dict[int, str],
    default_style: str,
    ensembles: list[str] | None = None,
    transpose_range: int = 6,
    keep_key: bool = False,
) -> list[FitResult]:
    """Rank ensemble and transposition combinations by how singable they are."""
    candidates = [e for e in (ensembles or list(ENSEMBLES)) if e in ENSEMBLES]
    shifts = [0] if keep_key else list(range(-transpose_range, transpose_range + 1))

    results: list[FitResult] = []
    for ensemble_id in candidates:
        ensemble = ENSEMBLES[ensemble_id]
        for shift in shifts:
            arrangement = build_arrangement(
                score, key, harmony, ensemble, bar_styles,
                default_style=default_style, transpose=shift, include_lyrics=False,
            )
            outside, strain = _measure(arrangement, ensemble)

            # Unsingable notes dominate; strain breaks ties; a nudge toward the
            # original key so a marginal gain does not move everyone's music.
            total = outside * 10.0 + strain * 20.0 + abs(shift) * 0.35
            results.append(
                FitResult(
                    ensemble=ensemble_id,
                    ensemble_name=ensemble.name,
                    transpose=shift,
                    out_of_range=outside,
                    strain=strain,
                    score=total,
                    key=arrangement.key.name,
                    summary=_describe(ensemble, shift, outside),
                )
            )

    results.sort(key=lambda r: r.score)
    return results


def _describe(ensemble: Ensemble, shift: int, outside: int) -> str:
    where = ensemble.name.split(" (")[0]
    if shift == 0:
        move = "in the original key"
    else:
        move = f"transposed {'up' if shift > 0 else 'down'} {abs(shift)} semitone{'s' if abs(shift) != 1 else ''}"
    if outside == 0:
        return f"{where} {move} — every note inside a comfortable range"
    return f"{where} {move} — {outside} note{'s' if outside != 1 else ''} still outside a comfortable range"
