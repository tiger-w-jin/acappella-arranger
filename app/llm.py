"""Optional Gemini fallback for commands the grammar cannot parse.

This is deliberately secondary. The grammar in `commands.py` handles the shapes
people actually type, instantly and offline; a model is worth calling only for
phrasing it rejects — "build towards the end", "make the chorus bigger".

Three rules keep it from being a liability:

* It is off unless `GEMINI_API_KEY` is set, so a clone with no credentials
  still has a fully working command box.
* Its output is validated back into the same `Action` objects, with unknown
  styles and out-of-range bars discarded. A hallucinated style id cannot reach
  the arranger.
* Any failure — no key, network error, bad JSON, timeout — returns None and the
  caller reports that the command was not understood. It never raises into a
  request.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from .commands import Action, Plan
from .harmony.styles import ENSEMBLES, STYLES

_log = logging.getLogger("acappella.llm")

_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
_DEFAULT_MODEL = "gemini-2.0-flash"
_TIMEOUT_SECONDS = 12

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["style", "ensemble", "transpose", "tempo"]},
                    "style": {"type": "string"},
                    "from": {"type": "integer"},
                    "to": {"type": "integer"},
                    "value": {"type": "string"},
                },
                "required": ["type"],
            },
        },
    },
    "required": ["actions", "summary"],
}


def is_available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def _prompt(text: str, bar_count: int, current_tempo: float) -> str:
    styles = "\n".join(f"  {sid}: {s.name} — {s.description}" for sid, s in STYLES.items())
    ensembles = "\n".join(f"  {eid}: {e.name}" for eid, e in ENSEMBLES.items())
    return f"""You translate a musician's instruction into edits to an a cappella arrangement.

The piece has {bar_count} bars, numbered 1 to {bar_count}. Current tempo is {round(current_tempo)} BPM.

Available styles (use the id on the left):
{styles}

Available ensembles (use the id on the left):
{ensembles}

Emit actions:
- {{"type":"style","style":"<style id>","from":<bar>,"to":<bar>}}  inclusive, 1-based
- {{"type":"ensemble","value":"<ensemble id>"}}
- {{"type":"transpose","value":"<-12..12>"}}   semitones, as a string
- {{"type":"tempo","value":"<20..300>"}}       BPM, as a string

Rules:
- Use only the ids listed above. Never invent one.
- Cover a whole piece with from=1 to={bar_count}.
- Emit several style actions for discontiguous or differing sections.
- If the instruction is musical but vague ("build towards the end"), choose a
  sensible progression of styles and say what you chose in the summary.
- If it is not about arranging this piece, return an empty actions list.
- summary: one short sentence, past tense, describing what you did.

Instruction: {text}"""


def interpret(text: str, bar_count: int, current_tempo: float = 96.0) -> Plan | None:
    """Ask Gemini for a plan. Returns None whenever anything at all goes wrong."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key or bar_count <= 0 or not text.strip():
        return None

    body = json.dumps({
        "contents": [{"parts": [{"text": _prompt(text, bar_count, current_tempo)}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseSchema": _RESPONSE_SCHEMA,
        },
    }).encode()

    url = _ENDPOINT.format(model=os.environ.get("GEMINI_MODEL", _DEFAULT_MODEL), key=key)
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read())
        raw = payload["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as error:
        _log.warning("Gemini fallback unavailable: %s", error)
        return None

    return _to_plan(parsed, bar_count)


def _to_plan(parsed: dict, bar_count: int) -> Plan | None:
    """Validate the model's JSON into actions, dropping anything unrecognised."""
    plan = Plan(source="llm")
    for item in parsed.get("actions") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")

        if kind == "style":
            style = item.get("style")
            if style not in STYLES:
                continue
            start = _as_int(item.get("from"), 1)
            end = _as_int(item.get("to"), bar_count)
            if start is None or end is None:
                continue
            if start > end:
                start, end = end, start
            bars = [b for b in range(start - 1, end) if 0 <= b < bar_count]
            if bars:
                plan.actions.append(Action(type="style", style=style, bars=bars))

        elif kind == "ensemble":
            value = item.get("value")
            if value in ENSEMBLES:
                plan.actions.append(Action(type="ensemble", value=value))

        elif kind == "transpose":
            value = _as_int(item.get("value"))
            if value is not None:
                plan.actions.append(
                    Action(type="transpose", value=max(-12, min(12, value)))
                )

        elif kind == "tempo":
            value = _as_int(item.get("value"))
            if value is not None:
                plan.actions.append(Action(type="tempo", value=max(20, min(300, value))))

    if not plan.actions:
        return None

    summary = str(parsed.get("summary") or "").strip()
    plan.summary.append(summary or "Applied the requested changes.")
    return plan


def _as_int(value, default=None) -> int | None:
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
