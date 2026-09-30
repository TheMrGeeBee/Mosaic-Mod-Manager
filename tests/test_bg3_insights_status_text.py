"""Regression test for the Load Order Insights status label shown when a
freshly-accepted author-note rule doesn't match the current mod order yet.

Reported bug: accept_author_note() only records a "winner beats loser" rule
-- it never reorders the modlist (unlike apply_winner()) -- so a rule that
disagrees with the current order is immediately flagged rule_violated=True.
The generic "Your rule is broken" wording implies something that used to
work has since drifted stale, which is wrong and confusing for a rule that
was never applied in the first place. author_note findings get a distinct,
accurate message instead.

``_status_text`` only touches ``self.tr(...)`` and the passed-in Finding, so
it's tested against a bare stand-in instead of a fully constructed Qt
wizard view (which would spin up a real background scan in __init__)."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from Utils.mods.bg3_pak_index import Finding
from wizards_qt.bg3_insights_view import BG3InsightsView


class _FakeSelf:
    def tr(self, text):
        return text


def _status_text(f, ignored=False):
    return BG3InsightsView._status_text(_FakeSelf(), f, ignored)


def test_freshly_accepted_author_note_does_not_say_broken():
    f = Finding(kind="author_note", mods=["Better Dyeing Lite",
                                          "Better Various Widgets All-In-One"],
               keys=[], winner="Better Various Widgets All-In-One",
               rule_violated=True)
    text = _status_text(f)
    assert text != "Your rule is broken"
    assert "reorder" in text.lower()


def test_stale_manual_rule_still_says_broken():
    f = Finding(kind="same_file", mods=["Mod A", "Mod B"], keys=["x"],
               winner="Mod A", rule_violated=True)
    assert _status_text(f) == "Your rule is broken"


def test_author_note_not_violated_takes_other_branches():
    # rule_violated only wins the branch when set; an author_note finding
    # that's merely pending still shows the normal accept prompt.
    f = Finding(kind="author_note", mods=["A", "B"], keys=[], winner="B")
    assert _status_text(f) == "Author says — accept?"
