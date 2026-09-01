"""Turn an analysed melody plus per-bar style choices into a full arrangement."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..analysis import BarHarmony, Segment
from ..lyrics import Syllable, fit as fit_lyrics
from ..models import MelodyNote, SourceScore
from ..theory import ChordSpec, KeyContext
from .styles import DEFAULT_STYLE, Ensemble, Style, get_style
from .voicing import drone_voicing, parallel_voicing, unison_voicing, voice_chord

_PULSE_LENGTHS = {
    "pulse_half": 2.0,
    "pulse_quarter": 1.0,
    "pulse_eighth": 0.5,
}


@dataclass
class VoiceEvent:
    offset: float
    duration: float
    pitch: int | None  # None means a rest
    lyric: str | None = None
    syllabic: str | None = None  # MusicXML hyphenation for split words
    tied_from_previous: bool = False


@dataclass
class Arrangement:
    parts: list[list[VoiceEvent]]
    ensemble: Ensemble
    key: KeyContext
    tempo: float
    title: str
    beats: int
    beat_type: int
    bar_styles: list[str]
    bar_symbols: list[str]
    bar_bounds: list[tuple[float, float]]
    # Per bar: the chords sounding in it as (offset within the bar, ChordSpec),
    # so a bar that changes harmony mid-way gets both symbols in the right place.
    bar_chords: list[list[tuple[float, ChordSpec]]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Slot:
    start: float
    duration: float
    melody_pitch: int | None

    @property
    def end(self) -> float:
        return self.start + self.duration


def _transpose_chord(chord: ChordSpec, semitones: int) -> ChordSpec:
    if semitones == 0:
        return chord
    return replace(
        chord,
        root_pc=(chord.root_pc + semitones) % 12,
        bass_pc=None if chord.bass_pc is None else (chord.bass_pc + semitones) % 12,
    )


def _melody_octave_shift(notes: list[MelodyNote], ensemble: Ensemble) -> int:
    """Whole-octave shift that best centres the tune in the melody voice."""
    pitches = [n.pitch for n in notes if n.pitch is not None]
    if not pitches:
        return 0
    voice = ensemble.voices[ensemble.melody_index]
    median = sorted(pitches)[len(pitches) // 2]
    best_shift = 0
    best_penalty = float("inf")
    for octaves in range(-3, 4):
        shift = octaves * 12
        outside = sum(1 for p in pitches if not (voice.lo <= p + shift <= voice.hi))
        # Fitting the range dominates; centring only breaks ties, and leaving
        # the melody where the composer put it beats a cosmetic re-centring.
        penalty = outside * 8.0 + abs((median + shift) - voice.centre) * 0.3 + abs(octaves) * 1.5
        if penalty < best_penalty:
            best_penalty = penalty
            best_shift = shift
    return best_shift


def _sounding_pitch(notes: list[MelodyNote], time: float) -> int | None:
    """Melody pitch sounding at `time`, else the next one to start."""
    current: int | None = None
    upcoming: int | None = None
    for note in notes:
        if note.pitch is None:
            continue
        if note.offset - 1e-6 <= time < note.end - 1e-6:
            current = note.pitch
        elif note.offset >= time and upcoming is None:
            upcoming = note.pitch
    return current if current is not None else upcoming


def _lowest_melody_pitch(notes: list[MelodyNote], start: float, end: float) -> int | None:
    """Lowest melody note sounding anywhere in a span.

    Backing voices that are held while the tune moves have to clear the whole
    phrase, not just whatever note happened to be sounding when the chord
    started, or the melody dives underneath its own accompaniment.
    """
    pitches = [
        note.pitch
        for note in notes
        if note.pitch is not None and note.offset < end - 1e-6 and note.end > start + 1e-6
    ]
    if pitches:
        return min(pitches)
    return _sounding_pitch(notes, start)


def _slots_for_bar(bar, style: Style, melody: list[MelodyNote]) -> list[_Slot]:
    start = bar.offset
    end = bar.offset + bar.length

    if style.rhythm == "follow":
        in_bar = [n for n in bar.notes if n.pitch is not None]
        if not in_bar:
            return [_Slot(start, bar.length, _sounding_pitch(melody, start))]
        slots: list[_Slot] = []
        cursor = start
        for note in in_bar:
            if note.offset > cursor + 1e-6:
                slots.append(_Slot(cursor, note.offset - cursor, _sounding_pitch(melody, cursor)))
            slots.append(_Slot(note.offset, note.duration, note.pitch))
            cursor = note.end
        if end - cursor > 1e-6:
            slots.append(_Slot(cursor, end - cursor, _sounding_pitch(melody, cursor)))
        return slots

    if style.rhythm in _PULSE_LENGTHS:
        step = _PULSE_LENGTHS[style.rhythm]
        slots = []
        cursor = start
        while cursor < end - 1e-6:
            span = min(step, end - cursor)
            slots.append(_Slot(cursor, span, _lowest_melody_pitch(melody, cursor, cursor + span)))
            cursor += span
        return slots

    # "sustain"
    return [_Slot(start, bar.length, _lowest_melody_pitch(melody, start, end))]


def _split_on_segments(
    slots: list[_Slot], chords: list[tuple[float, float, ChordSpec]]
) -> list[_Slot]:
    """Cut any slot that straddles a chord change, so no note spans two chords."""
    if len(chords) < 2:
        return slots
    boundaries = [start for start, _, _ in chords[1:]]
    result: list[_Slot] = []
    for slot in slots:
        cursor = slot.start
        for boundary in boundaries:
            if slot.start + 1e-6 < boundary < slot.end - 1e-6:
                result.append(_Slot(cursor, boundary - cursor, slot.melody_pitch))
                cursor = boundary
        result.append(_Slot(cursor, slot.end - cursor, slot.melody_pitch))
    return [item for item in result if item.duration > 1e-6]


def _chord_at(chords: list[tuple[float, float, ChordSpec]], time: float) -> ChordSpec:
    for start, duration, chord in chords:
        if start - 1e-6 <= time < start + duration - 1e-6:
            return chord
    return chords[-1][2]


def build_arrangement(
    score: SourceScore,
    key: KeyContext,
    harmony: list[BarHarmony],
    ensemble: Ensemble,
    bar_styles: dict[int, str],
    default_style: str = DEFAULT_STYLE,
    transpose: int = 0,
    include_lyrics: bool = True,
    lyrics: str | None = None,
    lyrics_all_voices: bool = False,
) -> Arrangement:
    warnings: list[str] = []
    melody_voice = ensemble.voices[ensemble.melody_index]

    melody = [
        MelodyNote(
            pitch=None if n.pitch is None else n.pitch + transpose,
            offset=n.offset,
            duration=n.duration,
            lyric=n.lyric,
            tied_from_previous=n.tied_from_previous,
        )
        for bar in score.bars
        for n in bar.notes
    ]
    melody.sort(key=lambda n: n.offset)

    shift = _melody_octave_shift(melody, ensemble)
    for note in melody:
        if note.pitch is not None:
            note.pitch += shift
    if shift:
        warnings.append(
            f"Melody moved {abs(shift) // 12} octave(s) "
            f"{'up' if shift > 0 else 'down'} to sit in the {melody_voice.name} range."
        )

    out_of_range = sum(
        1 for n in melody if n.pitch is not None and not (melody_voice.lo <= n.pitch <= melody_voice.hi)
    )
    if out_of_range:
        warnings.append(
            f"{out_of_range} melody note(s) fall outside a comfortable {melody_voice.name} "
            "range even after transposition — consider a different ensemble or transpose setting."
        )

    sung: list[Syllable | None] = []
    if lyrics:
        pitched = [n for n in melody if n.pitch is not None]
        sung, lyric_warnings = fit_lyrics(lyrics, len(pitched))
        warnings.extend(lyric_warnings)
        onset_syllable = {
            round(note.offset, 6): syllable
            for note, syllable in zip(pitched, sung)
            if syllable is not None
        }
    else:
        onset_syllable = {}

    key_shifted = replace(key, tonic_pc=(key.tonic_pc + transpose) % 12) if transpose else key

    parts: list[list[VoiceEvent]] = [[] for _ in ensemble.voices]
    previous: list[int] | None = None
    used_styles: list[str] = []
    symbols: list[str] = []
    bar_chords: list[list[tuple[float, ChordSpec]]] = []
    last_style_id: str | None = None

    for bar in score.bars:
        style = get_style(bar_styles.get(bar.index, default_style))
        used_styles.append(style.id)

        if style.min_voices > ensemble.size:
            warnings.append(
                f"Bar {bar.index + 1}: '{style.name}' wants {style.min_voices} voices but "
                f"{ensemble.name} has {ensemble.size}; voicing it as best as possible."
            )

        segments = harmony[bar.index].segments if bar.index < len(harmony) else []
        if not segments:
            segments = [
                Segment(
                    bar_index=bar.index,
                    start=bar.offset,
                    duration=bar.length,
                    chord=ChordSpec(key.tonic_pc, "maj"),
                    roman="I",
                    symbol="",
                )
            ]

        chords: list[tuple[float, float, ChordSpec]] = []
        for segment in segments:
            base = _transpose_chord(segment.chord, transpose)
            enriched = style.enrich(base, key_shifted) if style.generator == "voiced" else base
            chords.append((segment.start, segment.duration, enriched))
        symbols.append(
            " | ".join(chord.symbol(key_shifted.prefer_flats) for _, _, chord in chords)
        )
        bar_chords.append([(start - bar.offset, chord) for start, _, chord in chords])

        slots = _split_on_segments(_slots_for_bar(bar, style, melody), chords)
        announce_syllable = include_lyrics and style.id != last_style_id
        last_style_id = style.id

        for slot_index, slot in enumerate(slots):
            chord = _chord_at(chords, slot.start)
            pitches = _voice_slot(chord, slot.melody_pitch, ensemble, style, previous, key_shifted)
            previous = pitches
            lyric = style.syllable if (announce_syllable and slot_index == 0) else None
            syllabic = None
            if lyrics_all_voices:
                # Only where a backing chord is struck exactly on a melody note
                # can the parts share its word; a held pad cannot.
                word = onset_syllable.get(round(slot.start, 6))
                if word is not None:
                    lyric, syllabic = word.text, word.syllabic
                else:
                    lyric = None
            for voice_index in range(ensemble.size):
                if voice_index == ensemble.melody_index:
                    continue
                parts[voice_index].append(
                    VoiceEvent(
                        offset=slot.start,
                        duration=slot.duration,
                        pitch=pitches[voice_index],
                        lyric=lyric,
                        syllabic=syllabic,
                    )
                )

    parts[ensemble.melody_index] = _melody_events(melody, include_lyrics, sung)

    for index in range(len(parts)):
        parts[index] = _fill_and_merge(parts[index])

    warnings.extend(_range_warnings(parts, ensemble))

    return Arrangement(
        parts=parts,
        ensemble=ensemble,
        key=key_shifted,
        tempo=score.tempo,
        title=score.title,
        beats=score.bars[0].beats if score.bars else 4,
        beat_type=score.bars[0].beat_type if score.bars else 4,
        bar_styles=used_styles,
        bar_symbols=symbols,
        bar_bounds=[(bar.offset, bar.length) for bar in score.bars],
        bar_chords=bar_chords,
        warnings=warnings,
    )


def _voice_slot(
    chord: ChordSpec,
    melody_pitch: int | None,
    ensemble: Ensemble,
    style: Style,
    previous: list[int] | None,
    key: KeyContext,
) -> list[int]:
    if style.generator == "unison":
        reference = melody_pitch if melody_pitch is not None else key.tonic_pc + 60
        return unison_voicing(reference, ensemble)
    if style.generator == "drone":
        return drone_voicing(chord, melody_pitch, ensemble, key)
    if style.generator == "parallel" and melody_pitch is not None:
        return parallel_voicing(chord, melody_pitch, ensemble, style, key)
    return voice_chord(chord, melody_pitch, ensemble, style, previous)


def _melody_events(
    melody: list[MelodyNote],
    include_lyrics: bool,
    sung: list[Syllable | None],
) -> list[VoiceEvent]:
    """The tune itself. Typed lyrics win over any words the source file carried."""
    events: list[VoiceEvent] = []
    index = 0
    for note in melody:
        if note.pitch is None:
            continue
        syllable = sung[index] if index < len(sung) else None
        index += 1
        if syllable is not None:
            lyric, syllabic = syllable.text, syllable.syllabic
        else:
            lyric, syllabic = (note.lyric if include_lyrics else None), None
        events.append(
            VoiceEvent(
                offset=note.offset,
                duration=note.duration,
                pitch=note.pitch,
                lyric=lyric,
                syllabic=syllabic,
                tied_from_previous=note.tied_from_previous,
            )
        )
    return events


def _range_warnings(parts: list[list[VoiceEvent]], ensemble: Ensemble) -> list[str]:
    """Flag notes a real singer in that part would struggle with.

    Some style/ensemble pairings simply do not fit — asking an SSAA group for
    organum fifths below the tune wants notes no soprano has — and it is more
    useful to say so than to silently transpose the effect away.
    """
    messages: list[str] = []
    for index, voice in enumerate(ensemble.voices):
        if index == ensemble.melody_index:
            continue  # already reported against the melody
        outside = [
            event.pitch
            for event in parts[index]
            if event.pitch is not None and not (voice.lo <= event.pitch <= voice.hi)
        ]
        if len(outside) > max(2, len(parts[index]) // 10):
            messages.append(
                f"{voice.name}: {len(outside)} note(s) sit outside a comfortable range "
                f"for this part — this style may not suit {ensemble.name}."
            )
    return messages


def _fill_and_merge(events: list[VoiceEvent]) -> list[VoiceEvent]:
    """Sort, drop overlaps, and merge identical adjacent pitches into one note."""
    events.sort(key=lambda e: e.offset)
    cleaned: list[VoiceEvent] = []
    for event in events:
        if event.duration <= 1e-6:
            continue
        if cleaned:
            previous = cleaned[-1]
            overlap = previous.offset + previous.duration - event.offset
            if overlap > 1e-6:
                previous.duration = round(event.offset - previous.offset, 6)
                if previous.duration <= 1e-6:
                    cleaned.pop()
        cleaned.append(event)
    return cleaned
