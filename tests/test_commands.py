"""Tests for the typed-command parser.

The parser is the primary engine for the command box, so its behaviour on real
phrasing is pinned down here rather than left to an LLM's discretion.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.commands import parse_command  # noqa: E402

BARS = 32


def styles_of(plan):
    """{style_id: sorted 1-based bar numbers} for the plan's style actions."""
    out = {}
    for action in plan.actions:
        if action.type == "style":
            out.setdefault(action.style, []).extend(b + 1 for b in action.bars)
    return {k: sorted(v) for k, v in out.items()}


def settings_of(plan):
    return {a.type: a.value for a in plan.actions if a.type != "style"}


# ── Bar selectors ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    ("bars 9-16 barbershop", list(range(9, 17))),
    ("bars 9 to 16 barbershop", list(range(9, 17))),
    ("bars 9 through 16 barbershop", list(range(9, 17))),
    ("bar 5 barbershop", [5]),
    ("bars 1, 3 and 5 barbershop", [1, 3, 5]),
    ("first 8 bars barbershop", list(range(1, 9))),
    ("last 4 bars barbershop", [29, 30, 31, 32]),
    ("first four bars barbershop", [1, 2, 3, 4]),
    ("make it all barbershop", list(range(1, 33))),
    ("barbershop everywhere", list(range(1, 33))),
])
def test_bar_selectors(text, expected):
    plan = parse_command(text, BARS)
    assert plan.understood, text
    assert styles_of(plan) == {"barbershop": expected}, text


def test_bare_style_with_no_selector_applies_everywhere():
    plan = parse_command("gospel", BARS)
    assert styles_of(plan) == {"gospel_pad": list(range(1, 33))}


def test_ranges_are_clamped_to_the_piece():
    plan = parse_command("bars 28-99 jazz", BARS)
    assert styles_of(plan) == {"jazz_close": [28, 29, 30, 31, 32]}


def test_reversed_range_is_accepted():
    plan = parse_command("bars 16-9 barbershop", BARS)
    assert styles_of(plan) == {"barbershop": list(range(9, 17))}


def test_except_subtracts():
    plan = parse_command("everything except the last 4 doo-wop", BARS)
    assert styles_of(plan) == {"doo_wop": list(range(1, 29))}


def test_the_rest_covers_what_is_left():
    plan = parse_command("first 8 chorale, the rest gospel", BARS)
    assert styles_of(plan) == {
        "satb_chorale": list(range(1, 9)),
        "gospel_pad": list(range(9, 33)),
    }


def test_multiple_clauses_in_order():
    plan = parse_command("bars 1-8 chorale; bars 9-16 barbershop; bars 17-32 jazz", BARS)
    assert styles_of(plan) == {
        "satb_chorale": list(range(1, 9)),
        "barbershop": list(range(9, 17)),
        "jazz_close": list(range(17, 33)),
    }


# ── Style vocabulary ──────────────────────────────────────────────────────


@pytest.mark.parametrize("text,style_id", [
    ("all chorale", "satb_chorale"),
    ("all bach", "satb_chorale"),
    ("open hymn pad everywhere", "hymn_open"),
    ("make it barbershop", "barbershop"),
    ("barber shop please", "barbershop"),
    ("jazzy", "jazz_close"),
    ("close harmony", "jazz_close"),
    ("gospel", "gospel_pad"),
    ("doowop", "doo_wop"),
    ("fifties style", "doo_wop"),
    ("rhythmic vamp", "rhythmic_vamp"),
    ("contemporary cluster", "cluster"),
    ("crunchy", "cluster"),
    ("suspended", "sus_air"),
    ("organum", "open_fifths"),
    ("open fifths", "open_fifths"),
    ("medieval", "open_fifths"),
    ("pop thirds stack", "pop_stack"),
    ("pedal drone", "drone"),
    ("in unison", "unison"),
    ("no harmony", "unison"),
])
def test_style_aliases(text, style_id):
    plan = parse_command(text, BARS)
    assert plan.understood, text
    assert style_id in styles_of(plan), f"{text!r} -> {styles_of(plan)}"


def test_longest_alias_wins():
    """'open fifths' must not be read as the shorter 'fifths' of another style."""
    plan = parse_command("open fifths for the intro", BARS)
    assert "open_fifths" in styles_of(plan)


# ── Ensembles ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,ensemble", [
    ("switch to ttbb", "ttbb"),
    ("use ssaa", "ssaa"),
    ("make it a mixed choir", "satb"),
    ("sing it as a barbershop quartet", "barbershop"),
    ("five part", "ssatb"),
    ("lower voices", "ttbb"),
])
def test_ensembles(text, ensemble):
    plan = parse_command(text, BARS)
    assert settings_of(plan).get("ensemble") == ensemble, text


def test_bare_barbershop_is_a_style_not_an_ensemble():
    """The commonest word in this app is ambiguous; style is the likelier intent."""
    plan = parse_command("make it barbershop", BARS)
    assert "barbershop" in styles_of(plan)
    assert "ensemble" not in settings_of(plan)


def test_barbershop_quartet_is_an_ensemble():
    plan = parse_command("barbershop quartet", BARS)
    assert settings_of(plan).get("ensemble") == "barbershop"


# ── Transpose and tempo ───────────────────────────────────────────────────


@pytest.mark.parametrize("text,semitones", [
    ("transpose up 2", 2),
    ("transpose down 3", -3),
    ("up a tone", 2),
    ("down a semitone", -1),
    ("up an octave", 12),
    ("transpose down a fifth", -7),
])
def test_transpose(text, semitones):
    plan = parse_command(text, BARS)
    assert settings_of(plan).get("transpose") == semitones, text


def test_transpose_is_clamped():
    assert settings_of(parse_command("up 3 octaves", BARS))["transpose"] == 12


@pytest.mark.parametrize("text,bpm", [
    ("tempo 120", 120),
    ("set tempo to 72", 72),
    ("140 bpm", 140),
])
def test_absolute_tempo(text, bpm):
    assert settings_of(parse_command(text, BARS)).get("tempo") == bpm, text


def test_relative_tempo_uses_the_current_value():
    faster = settings_of(parse_command("faster", BARS, current_tempo=100))
    slower = settings_of(parse_command("slow down", BARS, current_tempo=100))
    assert faster["tempo"] > 100 and slower["tempo"] < 100


# ── Combinations and failure ──────────────────────────────────────────────


def test_combined_command():
    plan = parse_command(
        "bars 1-8 chorale, bars 9-16 barbershop, switch to ttbb, transpose up 2, tempo 108",
        BARS,
    )
    assert styles_of(plan) == {
        "satb_chorale": list(range(1, 9)),
        "barbershop": list(range(9, 17)),
    }
    assert settings_of(plan) == {"ensemble": "ttbb", "transpose": 2, "tempo": 108}


def test_summary_is_human_readable():
    plan = parse_command("bars 9-16 barbershop", BARS)
    assert "Barbershop" in plan.to_dict()["summary"]
    assert "9\u201316" in plan.to_dict()["summary"]


def test_single_bar_summary_reads_singular():
    plan = parse_command("bar 3 gospel", BARS)
    assert "bar 3" in plan.to_dict()["summary"]


def test_nonsense_is_reported_not_guessed():
    plan = parse_command("make it sound like a helicopter", BARS)
    assert not plan.understood
    assert plan.unparsed


def test_empty_input():
    assert not parse_command("", BARS).understood
    assert not parse_command("   ", BARS).understood


def test_no_bars_yet():
    assert not parse_command("all gospel", 0).understood


def test_plan_serialises():
    payload = parse_command("first 8 gospel", BARS).to_dict()
    assert payload["understood"] is True
    assert payload["source"] == "rules"
    assert payload["actions"][0]["type"] == "style"
    assert payload["actions"][0]["bars"] == list(range(8))


# ── LLM fallback validation ───────────────────────────────────────────────
#
# The model's output is never trusted directly. These exercise the validation
# layer with no network call, which is where a hallucinated style id or an
# out-of-range bar has to be caught.

from app import llm  # noqa: E402


def test_llm_is_off_without_a_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert not llm.is_available()
    assert llm.interpret("anything", BARS) is None


def test_llm_rejects_unknown_style():
    parsed = {"summary": "x", "actions": [{"type": "style", "style": "reggaeton", "from": 1, "to": 4}]}
    assert llm._to_plan(parsed, BARS) is None


def test_llm_rejects_unknown_ensemble():
    parsed = {"summary": "x", "actions": [{"type": "ensemble", "value": "sextet"}]}
    assert llm._to_plan(parsed, BARS) is None


def test_llm_clamps_bars_to_the_piece():
    parsed = {"summary": "x", "actions": [{"type": "style", "style": "gospel_pad", "from": 20, "to": 999}]}
    plan = llm._to_plan(parsed, BARS)
    assert plan is not None
    assert [b + 1 for b in plan.actions[0].bars] == list(range(20, 33))


def test_llm_clamps_transpose_and_tempo():
    parsed = {"summary": "x", "actions": [
        {"type": "transpose", "value": "99"},
        {"type": "tempo", "value": "5"},
    ]}
    plan = llm._to_plan(parsed, BARS)
    values = {a.type: a.value for a in plan.actions}
    assert values == {"transpose": 12, "tempo": 20}


def test_llm_handles_reversed_range_and_junk_entries():
    parsed = {"summary": "x", "actions": [
        "not a dict",
        {"type": "nonsense"},
        {"type": "style", "style": "cluster", "from": 8, "to": 3},
    ]}
    plan = llm._to_plan(parsed, BARS)
    assert len(plan.actions) == 1
    assert [b + 1 for b in plan.actions[0].bars] == [3, 4, 5, 6, 7, 8]


def test_llm_plan_is_marked_as_llm_sourced():
    parsed = {"summary": "Built to a climax.", "actions": [
        {"type": "style", "style": "unison", "from": 1, "to": 4},
    ]}
    plan = llm._to_plan(parsed, BARS)
    assert plan.to_dict()["source"] == "llm"
    assert plan.to_dict()["summary"] == "Built to a climax."


def test_llm_empty_actions_is_not_a_plan():
    assert llm._to_plan({"summary": "nothing to do", "actions": []}, BARS) is None
