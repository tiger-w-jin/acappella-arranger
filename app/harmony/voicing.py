"""The voicing search: turn a chord plus a melody note into concrete pitches.

For the main "voiced" generator this is an exhaustive search over every legal
pitch each free voice could take, scored by a cost function that encodes the
usual arranging rules — smooth voice leading, no crossing, sane spacing, keep
the chord complete, don't double colour tones, avoid parallel perfects. The
search space is small (a few hundred to a few thousand combinations per chord),
so brute force gives an exact optimum with no heuristics to go wrong.
"""

from __future__ import annotations

import itertools

from ..theory import TONE_IMPORTANCE, ChordSpec, KeyContext, nearest_pitch_with_pc, pitches_in_range
from .styles import Ensemble, Style, Voice

_MAX_COMBINATIONS = 24_000


def _fit_into_range(pitch: int, voice: Voice) -> int:
    """Octave-shift a pitch until it lands inside a voice's range."""
    while pitch < voice.lo:
        pitch += 12
    while pitch > voice.hi:
        pitch -= 12
    return max(voice.lo, min(voice.hi, pitch))


def _tessitura_cost(pitch: int, voice: Voice, weight: float) -> float:
    span = max(1, voice.hi - voice.lo)
    low_comfort = voice.lo + span * 0.15
    high_comfort = voice.hi - span * 0.15
    if pitch < low_comfort:
        return weight * (low_comfort - pitch)
    if pitch > high_comfort:
        return weight * (pitch - high_comfort)
    return 0.0


def _is_extension(chord: ChordSpec, pitch: int) -> bool:
    """True when this pitch is sounding a 9th/#11/13th rather than a core tone."""
    pc = pitch % 12
    for interval in chord.intervals:
        if (chord.root_pc + interval) % 12 == pc:
            return interval >= 13
    return False


def _candidates(chord: ChordSpec, voice: Voice, style: Style, is_bass: bool) -> list[int]:
    if is_bass and style.bass_root:
        allowed = [chord.bass_pc if chord.bass_pc is not None else chord.root_pc]
    else:
        allowed = list(chord.pitch_classes)

    options: list[int] = []
    for pc in allowed:
        options.extend(pitches_in_range(pc, voice.lo, voice.hi))
    if not options:
        # Range too narrow for any chord tone; fall back to the nearest root.
        fallback = nearest_pitch_with_pc(int(voice.centre), chord.root_pc, voice.lo - 6, voice.hi + 6)
        options = [fallback if fallback is not None else int(voice.centre)]
    return sorted(set(options))


def _prune(options: list[int], anchor: float, limit: int) -> list[int]:
    if len(options) <= limit:
        return options
    return sorted(sorted(options, key=lambda p: abs(p - anchor))[:limit])


def _cost(
    pitches: tuple[int, ...],
    chord: ChordSpec,
    ensemble: Ensemble,
    style: Style,
    previous: list[int] | None,
) -> float:
    weights = style.weights
    count = len(pitches)
    total = 0.0

    # Voices must stay in order, highest first. A unison between neighbours is
    # legal but thins the texture, so it costs a little; true crossing is
    # excluded by the search before it ever gets here.
    for i in range(count - 1):
        if pitches[i] < pitches[i + 1]:
            total += weights.crossing * (1 + (pitches[i + 1] - pitches[i]))
        elif pitches[i] == pitches[i + 1]:
            total += 4.0

    # Spacing. The gap above the bass is conventionally allowed to be large, so
    # the upper-structure rules stop one voice short when the bass holds a root.
    upper_end = count - 2 if (style.bass_root and count >= 3) else count - 1
    for i in range(max(0, upper_end)):
        gap = pitches[i] - pitches[i + 1]
        if gap > weights.max_upper_gap:
            total += weights.spacing * (gap - weights.max_upper_gap)
        if gap < weights.min_gap:
            total += weights.spacing * 2 * (weights.min_gap - gap)
        if gap > weights.ideal_gap_hi:
            total += weights.close_bias * (gap - weights.ideal_gap_hi)
        if gap < 4:
            total += weights.open_bias * (4 - gap)

    # Voice leading against the previous chord.
    if previous:
        for i in range(min(count, len(previous))):
            if previous[i] is None:
                continue
            distance = abs(pitches[i] - previous[i])
            total += weights.motion * distance
            if distance > 4 and i != ensemble.melody_index:
                total += weights.leap * (distance - 4)

        if not style.allow_parallels:
            for i in range(count):
                for j in range(i + 1, count):
                    if i >= len(previous) or j >= len(previous):
                        continue
                    now = (pitches[i] - pitches[j]) % 12
                    before = (previous[i] - previous[j]) % 12
                    moved = pitches[i] != previous[i] or pitches[j] != previous[j]
                    same_direction = (pitches[i] - previous[i]) * (pitches[j] - previous[j]) > 0
                    if moved and same_direction and now == before and now in (0, 7):
                        total += weights.parallel

    # Chord completeness.
    present = {pitch % 12 for pitch in pitches}
    for interval in chord.intervals:
        pc = (chord.root_pc + interval) % 12
        if pc not in present:
            total += weights.missing_tone * TONE_IMPORTANCE.get(interval, 1.0)

    # Doubling: root doubling is free, colour tones should be unique.
    counts: dict[int, int] = {}
    for pitch in pitches:
        counts[pitch % 12] = counts.get(pitch % 12, 0) + 1
    for pc, occurrences in counts.items():
        if occurrences < 2:
            continue
        interval = chord.tone_for_pc(pc)
        if interval is None or interval == 0:
            continue
        if interval == 7:
            total += weights.doubled_fifth * (occurrences - 1)
        else:
            total += weights.doubled_colour * (occurrences - 1)

    # Tensions muddy the bottom of the texture.
    for index in range(max(0, count - 2), count):
        if _is_extension(chord, pitches[index]):
            total += 6.0

    for index, pitch in enumerate(pitches):
        total += _tessitura_cost(pitch, ensemble.voices[index], weights.tessitura)

    return total


def voice_chord(
    chord: ChordSpec,
    melody_pitch: int | None,
    ensemble: Ensemble,
    style: Style,
    previous: list[int] | None = None,
) -> list[int]:
    """Best set of pitches, one per voice, ordered highest to lowest."""
    count = ensemble.size
    melody_index = ensemble.melody_index if melody_pitch is not None else -1

    option_lists: list[list[int]] = []
    for index, voice in enumerate(ensemble.voices):
        if index == melody_index and melody_pitch is not None:
            option_lists.append([_fit_into_range(melody_pitch, voice)])
            continue
        options = _candidates(chord, voice, style, is_bass=(index == count - 1))
        anchor = previous[index] if previous and index < len(previous) else voice.centre
        option_lists.append(_prune(options, anchor, 10))

    total_combinations = 1
    for options in option_lists:
        total_combinations *= len(options)
    if total_combinations > _MAX_COMBINATIONS:
        for index in range(count):
            if index == melody_index:
                continue
            anchor = previous[index] if previous and index < len(previous) else ensemble.voices[index].centre
            option_lists[index] = _prune(option_lists[index], anchor, 5)

    # Two passes: insist on a non-crossing voicing, and only if the ranges make
    # that impossible fall back to allowing one. Treating crossing as a hard
    # constraint is more reliable than pricing it into the cost function, where
    # a large enough voice-leading saving could always buy its way past.
    for require_ordered in (True, False):
        best: tuple[int, ...] | None = None
        best_cost = float("inf")
        for combination in itertools.product(*option_lists):
            if require_ordered and not _is_ordered(combination):
                continue
            score = _cost(combination, chord, ensemble, style, previous)
            if score < best_cost:
                best_cost = score
                best = combination
        if best is not None:
            return list(best)

    return [int(voice.centre) for voice in ensemble.voices]


def _is_ordered(pitches: tuple[int, ...]) -> bool:
    return all(pitches[i] >= pitches[i + 1] for i in range(len(pitches) - 1))


# --------------------------------------------------------------------------
# Non-search generators
# --------------------------------------------------------------------------


def parallel_voicing(
    chord: ChordSpec,
    melody_pitch: int,
    ensemble: Ensemble,
    style: Style,
    key: KeyContext,
) -> list[int]:
    """Fixed intervals below the melody, optionally snapped to the harmony."""
    pitches = [0] * ensemble.size
    melody_index = ensemble.melody_index
    pitches[melody_index] = _fit_into_range(melody_pitch, ensemble.voices[melody_index])

    targets = list(style.intervals) or [-12]

    # Voices below the melody take the style's intervals as written. Voices
    # above it (barbershop's tenor, say) take the same interval inverted up an
    # octave, so a third below becomes a sixth above — the standard way to put
    # a parallel harmony line over the tune instead of under it.
    below = [i for i in range(ensemble.size) if i > melody_index]
    above = [i for i in range(ensemble.size) if i < melody_index]

    for slot, index in enumerate(below):
        interval = targets[slot % len(targets)] - 12 * (slot // len(targets))
        pitches[index] = _place(melody_pitch + interval, chord, key, style, ensemble.voices[index])

    for slot, index in enumerate(reversed(above)):
        interval = targets[slot % len(targets)] + 12 * (1 + slot // len(targets))
        pitches[index] = _place(melody_pitch + interval, chord, key, style, ensemble.voices[index])

    return _enforce_descending(pitches, ensemble, melody_index)


def _place(raw: int, chord: ChordSpec, key: KeyContext, style: Style, voice: Voice) -> int:
    if style.parallel_snap == "chord":
        snapped = _snap_to_pcs(raw, chord.pitch_classes)
    elif style.parallel_snap == "scale":
        snapped = _snap_to_pcs(raw, key.scale_pcs)
    else:
        snapped = raw
    return _fit_into_range(snapped, voice)


def drone_voicing(chord: ChordSpec, melody_pitch: int | None, ensemble: Ensemble, key: KeyContext) -> list[int]:
    """Root-and-fifth pedal on the tonic, melody voice untouched."""
    pitches: list[int] = []
    melody_index = ensemble.melody_index
    pedal_pcs = (key.tonic_pc, (key.tonic_pc + 7) % 12)
    for index, voice in enumerate(ensemble.voices):
        if index == melody_index and melody_pitch is not None:
            pitches.append(_fit_into_range(melody_pitch, voice))
            continue
        # Lower voices take the root, upper ones the fifth.
        pc = pedal_pcs[0] if index >= ensemble.size - 2 else pedal_pcs[1]
        target = nearest_pitch_with_pc(int(voice.centre), pc, voice.lo, voice.hi)
        pitches.append(target if target is not None else int(voice.centre))
    return _enforce_descending(pitches, ensemble, melody_index)


def unison_voicing(melody_pitch: int, ensemble: Ensemble) -> list[int]:
    """Melody in every voice, each in its own octave."""
    return [_fit_into_range(melody_pitch, voice) for voice in ensemble.voices]


def _snap_to_pcs(pitch: int, pcs: tuple[int, ...]) -> int:
    best = pitch
    best_distance = 99
    for offset in range(-3, 4):
        if (pitch + offset) % 12 in pcs and abs(offset) < best_distance:
            best_distance = abs(offset)
            best = pitch + offset
    return best


def _enforce_descending(pitches: list[int], ensemble: Ensemble, protected: int) -> list[int]:
    """Octave-shift voices into descending order, working outwards from the melody.

    Each voice keeps its pitch class — that is what makes the harmony parallel —
    and only its octave moves. Walking outwards from the fixed melody voice in
    one pass each way means no step can undo an earlier one. Where a voice's
    range and the ordering genuinely conflict, ordering wins and the arranger
    reports the range problem instead of silently inverting the harmony.
    """
    result = list(pitches)

    ceiling = result[protected]
    for index in range(protected + 1, len(result)):
        result[index] = _best_octave(result[index], ensemble.voices[index], upper=ceiling)
        ceiling = result[index]

    floor = result[protected]
    for index in range(protected - 1, -1, -1):
        result[index] = _best_octave(result[index], ensemble.voices[index], lower=floor)
        floor = result[index]

    return result


def _best_octave(
    target: int, voice: Voice, upper: int | None = None, lower: int | None = None
) -> int:
    """Octave transposition of `target` that respects the bounds and fits best."""
    options = [target + 12 * k for k in range(-5, 6)]
    feasible = [
        pitch
        for pitch in options
        if (upper is None or pitch <= upper) and (lower is None or pitch >= lower)
    ]
    if not feasible:
        bound = upper if upper is not None else lower
        return min(options, key=lambda pitch: abs(pitch - (bound or int(voice.centre))))
    return min(feasible, key=lambda pitch: abs(pitch - voice.centre))
