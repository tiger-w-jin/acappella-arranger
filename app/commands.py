"""Turn a typed instruction into concrete edits.

"bars 9-16 barbershop, the rest chorale" is a small, well-shaped language: a
bar selector plus a style, repeated. A grammar parses that exactly, offline, in
under a millisecond, and can be unit-tested — none of which is true of a model.
So the grammar is the primary engine and an LLM is only consulted for phrasing
it rejects (see `llm.py`), and even then its output is validated back through
the same Action types.

The parser never edits anything itself. It returns a plan the caller applies,
so a misread command can be shown to the user and undone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .harmony.styles import ENSEMBLES, STYLES

# ── Vocabulary ────────────────────────────────────────────────────────────

# Extra words people actually use for each style, beyond its own name.
STYLE_ALIASES: dict[str, tuple[str, ...]] = {
    "satb_chorale": ("chorale", "choral", "bach", "classical", "hymnal", "four part", "4 part"),
    "hymn_open": ("hymn", "open hymn", "church", "open pad", "sustained pad", "warm pad"),
    "barbershop": ("barbershop", "barber shop", "barbers hop", "bbs"),
    "jazz_close": ("jazz", "jazzy", "close harmony", "swing", "sevenths", "ninths"),
    "gospel_pad": ("gospel", "soul", "soulful", "6/9", "six nine", "69 pad"),
    "doo_wop": ("doo wop", "doowop", "doo-wop", "fifties", "50s", "1950s", "fifty s"),
    "rhythmic_vamp": ("vamp", "rhythmic", "driving", "eighth note", "upbeat", "up tempo", "punchy"),
    "cluster": ("cluster", "contemporary", "modern", "dissonant", "crunchy", "seconds", "tight modern"),
    "sus_air": ("sus", "sus2", "suspended", "airy", "floating", "ambient", "ethereal", "open air"),
    "open_fifths": ("open fifths", "fifths", "organum", "medieval", "hollow", "bare", "parallel fifths"),
    "pop_stack": ("pop", "thirds", "stacked thirds", "simple harmony", "pop stack"),
    "drone": ("drone", "pedal", "pedal tone", "held note", "sustain under"),
    "unison": ("unison", "octaves", "no harmony", "in unison", "monophonic", "melody only"),
}

# Ensemble words. "barbershop" alone means the style, so the quartet only
# matches when the phrasing makes an ensemble explicit.
ENSEMBLE_ALIASES: dict[str, tuple[str, ...]] = {
    "satb": ("satb", "mixed choir", "mixed chorus", "full choir", "standard choir"),
    "sab": ("sab", "three part", "3 part", "three-part"),
    "ssaa": ("ssaa", "upper voices", "women", "womens", "female", "treble voices", "high voices"),
    "ttbb": ("ttbb", "lower voices", "men", "mens", "male", "low voices"),
    "barbershop": ("barbershop quartet", "barbershop group", "quartet"),
    "ssatb": ("ssatb", "five part", "5 part", "five-part"),
}

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "a": 1, "an": 1, "half": 0,
}

_INTERVAL_WORDS = {
    "semitone": 1, "half step": 1, "halfstep": 1,
    "tone": 2, "whole step": 2, "wholestep": 2, "step": 2,
    "minor third": 3, "third": 4, "major third": 4,
    "fourth": 5, "fifth": 7, "octave": 12,
}

_CLAUSE_SPLIT = re.compile(r"\s*(?:;|,\s*(?:and\s+)?|\.\s+|\band then\b|\bthen\b)\s*", re.I)

# Clauses are comma-separated, but so are bar lists ("bars 1, 3 and 5"), so the
# commas inside a list are masked before splitting and restored afterwards.
_BAR_LIST_RE = re.compile(r"\bbars?\s+\d+(?:\s*(?:,|and)\s*\d+)+", re.I)
_COMMA_SENTINEL = "\x00"


def _mask_bar_lists(text: str) -> str:
    return _BAR_LIST_RE.sub(lambda m: m.group(0).replace(",", _COMMA_SENTINEL), text)


# ── Plan ──────────────────────────────────────────────────────────────────


@dataclass
class Action:
    """One edit. `bars` holds zero-based indices and is only used by "style"."""

    type: str  # style | ensemble | transpose | tempo
    style: str | None = None
    bars: list[int] | None = None
    value: object = None

    def to_dict(self) -> dict:
        payload: dict = {"type": self.type}
        if self.style is not None:
            payload["style"] = self.style
        if self.bars is not None:
            payload["bars"] = self.bars
        if self.value is not None:
            payload["value"] = self.value
        return payload


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    source: str = "rules"

    @property
    def understood(self) -> bool:
        return bool(self.actions)

    def to_dict(self) -> dict:
        return {
            "understood": self.understood,
            "source": self.source,
            "summary": "; ".join(self.summary),
            "actions": [a.to_dict() for a in self.actions],
            "unparsed": self.unparsed,
        }


# ── Matching helpers ──────────────────────────────────────────────────────


def _normalise(text: str) -> str:
    text = text.lower().replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'")
    return re.sub(r"\s+", " ", text).strip()


def _match_style(text: str) -> tuple[str | None, int]:
    """Longest alias wins, so 'open fifths' beats 'fifths' and 'pop'."""
    best_id, best_len = None, 0
    for style_id, style in STYLES.items():
        candidates = list(STYLE_ALIASES.get(style_id, ()))
        candidates.append(style.name.lower())
        candidates.append(style_id.replace("_", " "))
        for candidate in candidates:
            if len(candidate) > best_len and re.search(rf"\b{re.escape(candidate)}\b", text):
                best_id, best_len = style_id, len(candidate)
    return best_id, best_len


def _match_ensemble(text: str) -> tuple[str | None, int]:
    best_id, best_len = None, 0
    for ensemble_id in ENSEMBLES:
        for candidate in ENSEMBLE_ALIASES.get(ensemble_id, ()):
            if len(candidate) > best_len and re.search(rf"\b{re.escape(candidate)}\b", text):
                best_id, best_len = ensemble_id, len(candidate)
    return best_id, best_len


def _to_int(token: str) -> int | None:
    token = token.strip()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


# ── Bar selectors ─────────────────────────────────────────────────────────

_RANGE_RE = re.compile(
    r"\bbars?\s+(\d+)\s*(?:-|to|through|thru|until|till)\s*(\d+)", re.I)
_BARE_RANGE_RE = re.compile(r"\b(\d+)\s*-\s*(\d+)\b")
_LIST_RE = re.compile(r"\bbars?\s+((?:\d+\s*(?:,|and)\s*)+\d+)", re.I)
_SINGLE_RE = re.compile(r"\bbars?\s+(\d+)\b", re.I)
_FIRST_LAST_RE = re.compile(
    r"\b(first|opening|last|final|closing)\s+(\d+|[a-z]+)?\s*(?:bars?)?", re.I)
_ALL_RE = re.compile(r"\b(all|every bar|everything|whole (?:piece|thing|song)|entire|throughout)\b", re.I)
_REST_RE = re.compile(r"\b(rest|remaining|everywhere else|the others?|otherwise)\b", re.I)
_EXCEPT_RE = re.compile(r"\b(except|but not|apart from|other than|excluding)\b", re.I)


def _select_bars(text: str, bar_count: int, already: set[int]) -> tuple[set[int] | None, bool]:
    """Bars a clause refers to.

    Returns (indices, is_rest). `already` holds bars this command has assigned
    so far, so "the rest" means everything untouched.
    """
    if _REST_RE.search(text):
        return {i for i in range(bar_count) if i not in already}, True

    head, exclude = text, set()
    match = _EXCEPT_RE.search(text)
    if match:
        head, tail = text[: match.start()], text[match.end():]
        found, _ = _select_bars(tail, bar_count, already)
        exclude = found or set()

    picked: set[int] = set()

    for lo, hi in _RANGE_RE.findall(head):
        picked |= _range(int(lo), int(hi), bar_count)
    if not picked:
        for lo, hi in _BARE_RANGE_RE.findall(head):
            picked |= _range(int(lo), int(hi), bar_count)

    for group in _LIST_RE.findall(head):
        for token in re.split(r"\s*(?:,|and)\s*", group):
            number = _to_int(token)
            if number:
                picked |= _range(number, number, bar_count)

    if not picked:
        for token in _SINGLE_RE.findall(head):
            picked |= _range(int(token), int(token), bar_count)

    for which, amount in _FIRST_LAST_RE.findall(head):
        count = _to_int(amount or "") or 1
        count = max(1, min(count, bar_count))
        if which.lower() in ("first", "opening"):
            picked |= set(range(count))
        else:
            picked |= set(range(max(0, bar_count - count), bar_count))

    if not picked and _ALL_RE.search(head):
        picked = set(range(bar_count))

    picked -= exclude
    return (picked or None), False


def _range(lo: int, hi: int, bar_count: int) -> set[int]:
    """Inclusive, 1-based, clamped to the piece."""
    if lo > hi:
        lo, hi = hi, lo
    lo = max(1, lo)
    hi = min(bar_count, hi)
    return set(range(lo - 1, hi))


def _describe(indices: set[int], bar_count: int) -> str:
    if len(indices) == bar_count:
        return "every bar"
    ordered = sorted(indices)
    groups, start, previous = [], ordered[0], ordered[0]
    for index in ordered[1:]:
        if index == previous + 1:
            previous = index
            continue
        groups.append((start, previous))
        start = previous = index
    groups.append((start, previous))
    parts = [f"{a + 1}" if a == b else f"{a + 1}\u2013{b + 1}" for a, b in groups]
    return ("bar " if len(ordered) == 1 else "bars ") + ", ".join(parts)


# ── Settings ──────────────────────────────────────────────────────────────

_TRANSPOSE_RE = re.compile(
    r"\b(transpose|shift|move|take it|put it)?\s*(up|down|higher|lower)\s*"
    r"(?:by\s+)?(\d+|[a-z ]+?)?\s*(semitones?|semi tones?|half steps?|tones?|whole steps?|steps?|octaves?|fifths?|fourths?|thirds?)?\b",
    re.I)
_TEMPO_RE = re.compile(r"\b(?:tempo|bpm|speed)\s*(?:of|to|=|at)?\s*(\d{2,3})\b|\b(\d{2,3})\s*bpm\b", re.I)
_TEMPO_WORD_RE = re.compile(r"\b(faster|quicker|slower|speed up|slow down)\b", re.I)


def _parse_transpose(text: str) -> int | None:
    match = _TRANSPOSE_RE.search(text)
    if not match:
        explicit = re.search(r"\btranspose\s*(?:to|by)?\s*([+-]\d+)\b", text, re.I)
        return int(explicit.group(1)) if explicit else None

    direction = -1 if match.group(2).lower() in ("down", "lower") else 1
    amount_text = (match.group(3) or "").strip()
    unit = (match.group(4) or "").strip().rstrip("s")

    if unit:
        singular = unit.replace("semi tone", "semitone").replace("whole step", "tone")
        per = _INTERVAL_WORDS.get(singular) or _INTERVAL_WORDS.get(unit) or 1
        count = _to_int(amount_text) if amount_text else 1
        return direction * per * (count if count else 1)

    amount = _to_int(amount_text) if amount_text else None
    return direction * amount if amount else None


# ── Entry point ───────────────────────────────────────────────────────────


def parse_command(text: str, bar_count: int, current_tempo: float = 96.0) -> Plan:
    """Parse an instruction into a plan of edits."""
    plan = Plan()
    if not text or not text.strip():
        return plan
    if bar_count <= 0:
        return plan

    assigned: set[int] = set()

    for raw_clause in _CLAUSE_SPLIT.split(_mask_bar_lists(_normalise(text))):
        clause = raw_clause.replace(_COMMA_SENTINEL, ",").strip()
        if not clause:
            continue
        if _handle_clause(clause, bar_count, current_tempo, assigned, plan):
            continue
        plan.unparsed.append(clause)

    return plan


def _handle_clause(
    clause: str, bar_count: int, current_tempo: float, assigned: set[int], plan: Plan
) -> bool:
    matched = False

    # Ensemble. Checked before style so "barbershop quartet" is not read as the
    # barbershop style with a stray word.
    ensemble_id, ensemble_len = _match_ensemble(clause)
    style_id, style_len = _match_style(clause)
    if ensemble_id and ensemble_len >= style_len:
        plan.actions.append(Action(type="ensemble", value=ensemble_id))
        plan.summary.append(f"ensemble \u2192 {ENSEMBLES[ensemble_id].name}")
        matched = True
        style_id = None if style_len <= ensemble_len else style_id

    if style_id:
        selection, _ = _select_bars(clause, bar_count, assigned)
        if selection is None:
            selection = set(range(bar_count))
        assigned |= selection
        plan.actions.append(
            Action(type="style", style=style_id, bars=sorted(selection))
        )
        plan.summary.append(
            f"{STYLES[style_id].name} on {_describe(selection, bar_count)}"
        )
        matched = True

    semitones = _parse_transpose(clause)
    if semitones:
        semitones = max(-12, min(12, semitones))
        plan.actions.append(Action(type="transpose", value=semitones))
        plan.summary.append(f"transpose {semitones:+d} semitones")
        matched = True

    tempo_match = _TEMPO_RE.search(clause)
    relative = _TEMPO_WORD_RE.search(clause)
    if tempo_match:
        bpm = max(20, min(300, int(tempo_match.group(1) or tempo_match.group(2))))
        plan.actions.append(Action(type="tempo", value=bpm))
        plan.summary.append(f"tempo {bpm} BPM")
        matched = True
    elif relative:
        word = relative.group(1).lower()
        factor = 1.15 if word in ("faster", "quicker", "speed up") else 0.87
        bpm = max(20, min(300, round(current_tempo * factor)))
        plan.actions.append(Action(type="tempo", value=bpm))
        plan.summary.append(f"tempo {bpm} BPM")
        matched = True

    return matched


def examples() -> list[str]:
    """Suggestions shown under the command box."""
    return [
        "bars 9-16 barbershop",
        "first 8 chorale, the rest gospel",
        "make it all jazz",
        "everything except the last 4 doo-wop",
        "sing it as a barbershop quartet",
        "transpose up a tone and set tempo 120",
    ]
