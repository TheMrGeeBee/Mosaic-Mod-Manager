"""Tests for the manual-download queue's tab budgeting.

Pure: ``pages_to_open`` is deliberately kept free of Qt so the rule that
decides how many browser tabs to open can be tested without a widget.
"""
from __future__ import annotations

from dataclasses import dataclass

from gui_qt.wabbajack.manual_download_queue import pages_to_open


@dataclass
class _A:
    hash: str


A, B, C, D = _A("a"), _A("b"), _A("c"), _A("d")


def test_one_tab_at_a_time_is_the_old_behaviour():
    assert pages_to_open([A, B, C], set(), 1) == [A]


def test_fills_the_budget_in_order():
    assert pages_to_open([A, B, C, D], set(), 3) == [A, B, C]


def test_open_pages_hold_their_slots():
    assert pages_to_open([A, B, C, D], {"a"}, 3) == [B, C]


def test_no_free_slots_opens_nothing():
    assert pages_to_open([A, B, C], {"a", "b", "c"}, 3) == []
    assert pages_to_open([A, B], {"a"}, 1) == []


def test_never_reopens_a_page_already_open():
    """The budget frees as files arrive; a page already open must not be
    opened a second time when the list is rescanned."""
    assert A not in pages_to_open([A, B, C], {"a"}, 2)


def test_budget_larger_than_the_queue():
    assert pages_to_open([A, B], set(), 10) == [A, B]


def test_empty_queue():
    assert pages_to_open([], set(), 5) == []


def test_more_open_than_the_budget_allows():
    """Lowering the spinner while pages are open must not open more."""
    assert pages_to_open([A, B, C], {"a", "b"}, 1) == []
