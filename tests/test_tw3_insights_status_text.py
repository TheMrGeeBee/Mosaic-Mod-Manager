"""TW3InsightsView._status_text -- pure string logic, tested against a bare
stand-in instead of a fully constructed Qt wizard view (which would spin
up a real background scan in __init__), same technique as
test_bg3_insights_status_text.py.
"""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from Utils.mods.tw3_load_index import Finding, Insights
from wizards_qt.tw3_insights_view import TW3InsightsView


class _FakeSelf:
    def __init__(self, insights=None):
        self._insights = insights

    def tr(self, text):
        return text


def _status_text(f, ignored=False, insights=None):
    return TW3InsightsView._status_text(_FakeSelf(insights), f, ignored)


def test_ignored_takes_priority():
    f = Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner="A")
    assert _status_text(f, ignored=True) == "Ignored"


def test_identical_is_harmless():
    f = Finding(kind="identical", mods=["A", "B"], keys=["k"], winner="A")
    assert _status_text(f) == "Harmless"


def test_never_together_marking():
    f = Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner="A",
               never_together=True)
    assert _status_text(f) == "Marked — never together"


def test_rule_violated_says_broken():
    f = Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner="A",
               rule_violated=True)
    assert _status_text(f) == "Your rule is broken"


def test_intended_says_collection_order():
    f = Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner="A",
               intended=True, intended_by="collection order")
    assert _status_text(f) == "Intended (collection's order)"


def test_resolved_by_rule_says_decided():
    f = Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner="A",
               resolved_by_rule=True)
    assert _status_text(f) == "Decided"


def test_unranked_mod_says_not_in_mods_settings():
    f = Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner=None)
    insights = Insights(load_rank={"A": 1})   # B is missing
    assert _status_text(f, insights=insights) == "Can't be decided — not in mods.settings"


def test_default_needs_a_decision():
    f = Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner="A")
    insights = Insights(load_rank={"A": 1, "B": 2})
    assert _status_text(f, insights=insights) == "Needs a decision"


def test_no_insights_at_all_still_falls_through_to_needs_a_decision():
    f = Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner="A")
    assert _status_text(f, insights=None) == "Needs a decision"
