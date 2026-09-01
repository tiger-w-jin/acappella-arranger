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


# ── Rebuilding written lyrics from a score ────────────────────────────────


def rebuild(syllables: list[tuple[str, str | None]]) -> str:
    """Turn per-note (text, syllabic) pairs back into editable written lyrics.

    The inverse of `parse_lyrics`, used to show the words an uploaded file
    already carried. `begin` and `middle` are mid-word, so they keep the hyphen
    that rejoins them; everything else ends a word.
    """
    out: list[str] = []
    for text, syllabic in syllables:
        if not text:
            continue
        out.append(text + ("-" if syllabic in (BEGIN, MIDDLE) else " "))
    return "".join(out).strip()


# ── Automatic hyphenation ─────────────────────────────────────────────────

_VOWELS = frozenset("aeiouy")

# Hyphenation dictionaries are built for line-breaking, not singing: they leave
# "twinkle", "little", "above" and "mercy" whole because a typesetter would
# rather not break them, while a singer needs every one of them split. So
# English uses the rules below, and pyphen covers the other languages, where it
# is far better than anything short enough to write here.
ENGLISH = "en"


def _vowel_groups(word: str) -> list[tuple[int, int]]:
    groups: list[tuple[int, int]] = []
    index = 0
    while index < len(word):
        if word[index] in _VOWELS:
            end = index
            while end + 1 < len(word) and word[end + 1] in _VOWELS:
                end += 1
            groups.append((index, end))
            index = end + 1
        else:
            index += 1
    return groups


def syllabify_word(word: str) -> list[str]:
    """Split one English word into sung syllables."""
    lowered = word.lower()
    if len(lowered) < 3 or not any(c in _VOWELS for c in lowered):
        return [word]

    groups = _vowel_groups(lowered)

    # A final "e" is silent ("grace"), except after a consonant + "le", where it
    # carries its own syllable ("lit-tle").
    consonant_le = lowered.endswith("le") and lowered[-3] not in _VOWELS
    if (
        len(groups) > 1
        and groups[-1] == (len(lowered) - 1, len(lowered) - 1)
        and lowered.endswith("e")
        and not consonant_le
    ):
        groups = groups[:-1]

    # Past-tense "-ed" is silent unless the stem already ends in t or d
    # ("saved" is one syllable, "want-ed" is two).
    if (
        len(groups) > 1
        and lowered.endswith("ed")
        and len(lowered) >= 4
        and lowered[-3] not in _VOWELS
        and lowered[-3] not in "td"
        and groups[-1][0] == len(lowered) - 2
    ):
        groups = groups[:-1]

    if len(groups) < 2:
        return [word]

    cuts: list[int] = []
    for left, right in zip(groups, groups[1:]):
        between = right[0] - left[1] - 1
        if between <= 0:
            cuts.append(right[0])          # two vowel groups meeting
        elif between == 1:
            cuts.append(left[1] + 1)       # V|CV — "a-bove"
        else:
            cuts.append(left[1] + 2)       # VC|CV — "won-der"
    if consonant_le and cuts:
        cuts[-1] = len(lowered) - 3        # "twin-kle", not "twink-le"

    pieces: list[str] = []
    previous = 0
    for cut in cuts:
        if previous < cut < len(word):
            pieces.append(word[previous:cut])
            previous = cut
    pieces.append(word[previous:])
    return [p for p in pieces if p]


def _pyphen_word(word: str, lang: str) -> list[str]:
    try:
        import pyphen
    except ImportError:
        return [word]
    try:
        dictionary = pyphen.Pyphen(lang=lang)
    except Exception:
        return [word]
    return dictionary.inserted(word, hyphen="\x00").split("\x00")


def auto_hyphenate(text: str, lang: str = ENGLISH) -> str:
    """Insert syllable hyphens into plain prose, leaving the writer in charge.

    A token is left exactly as typed when it already contains a hyphen, so a
    manual split is never second-guessed. Punctuation stays attached to the
    piece it was written against.
    """
    if not text or not text.strip():
        return text

    out: list[str] = []
    for token in text.split():
        if "-" in token or token == _EXTEND:
            out.append(token)
            continue

        prefix = ""
        suffix = ""
        core = token
        while core and not core[0].isalpha():
            prefix, core = prefix + core[0], core[1:]
        while core and not core[-1].isalpha():
            core, suffix = core[:-1], core[-1] + suffix
        if not core:
            out.append(token)
            continue

        pieces = syllabify_word(core) if lang.startswith(ENGLISH) else _pyphen_word(core, lang)
        out.append(prefix + "-".join(pieces) + suffix)

    return " ".join(out)


def available_languages() -> list[dict]:
    """Languages the hyphenator offers. English is built in; the rest need pyphen."""
    languages = [{"id": ENGLISH, "name": "English"}]
    try:
        import pyphen
    except ImportError:
        return languages

    friendly = {
        "de_DE": "German", "fr": "French", "it_IT": "Italian", "es": "Spanish",
        "la": "Latin", "nl_NL": "Dutch", "pt_PT": "Portuguese", "sv": "Swedish",
        "pl_PL": "Polish", "cs_CZ": "Czech", "ru_RU": "Russian", "hu_HU": "Hungarian",
    }
    for code, name in friendly.items():
        if code in pyphen.LANGUAGES:
            languages.append({"id": code, "name": name})
    return languages
