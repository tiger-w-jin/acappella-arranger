"""Fit a text of lyrics onto a melody, one syllable per note.

The input convention is the one every notation program uses, so it is already
in most people's fingers: a space starts a new word, a hyphen splits a word
across notes.

    A-ma-zing grace how sweet the sound

Each syllable is tagged so MusicXML can draw the hyphens that join a word split
over several notes, which is what makes a vocal score readable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Syllabic values MusicXML understands.
SINGLE, BEGIN, MIDDLE, END = "single", "begin", "middle", "end"

_EXTEND = "_"


@dataclass(frozen=True)
class Syllable:
    text: str
    syllabic: str = SINGLE


def parse_lyrics(text: str) -> list[Syllable]:
    """Split written lyrics into one Syllable per note to be sung.

    An underscore on its own is a melisma: it holds the previous syllable over
    another note, so it consumes a note without printing anything.
    """
    if not text or not text.strip():
        return []

    syllables: list[Syllable] = []
    for word in text.split():
        if word == _EXTEND:
            syllables.append(Syllable("", SINGLE))
            continue

        # Keep empty pieces out so "a--b" and a trailing "-" behave sanely.
        pieces = [p for p in re.split(r"-+", word) if p]
        if not pieces:
            continue
        if len(pieces) == 1:
            syllables.append(Syllable(pieces[0], SINGLE))
            continue
        for index, piece in enumerate(pieces):
            if index == 0:
                syllabic = BEGIN
            elif index == len(pieces) - 1:
                syllabic = END
            else:
                syllabic = MIDDLE
            syllables.append(Syllable(piece, syllabic))

    return syllables


def fit(text: str, note_count: int) -> tuple[list[Syllable | None], list[str]]:
    """Line syllables up with notes, reporting any mismatch rather than hiding it."""
    syllables = parse_lyrics(text)
    warnings: list[str] = []
    if not syllables:
        return [None] * note_count, warnings

    if len(syllables) > note_count:
        warnings.append(
            f"Lyrics have {len(syllables)} syllables but the melody has {note_count} "
            f"notes — the last {len(syllables) - note_count} were left off. Use hyphens "
            "to split a word over several notes."
        )
    elif len(syllables) < note_count:
        warnings.append(
            f"Lyrics have {len(syllables)} syllables for {note_count} melody notes — "
            f"the last {note_count - len(syllables)} notes have no word under them."
        )

    assigned: list[Syllable | None] = []
    for index in range(note_count):
        if index >= len(syllables):
            assigned.append(None)
            continue
        syllable = syllables[index]
        assigned.append(None if syllable.text == "" else syllable)
    return assigned, warnings
