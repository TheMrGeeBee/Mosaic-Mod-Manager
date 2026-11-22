"""Qt-free helpers behind the Modhold browse tab."""
from __future__ import annotations

import pytest

from Modhold.browse_util import (
    default_file_selection,
    format_price,
    format_size,
    is_gated,
    meta_from_detail,
    mod_badges,
    needs_chooser,
    numbers_line,
    page_count,
)

FILES = [
    {"id": 1508, "kind": "main", "label": "Main file", "name": "Shop 1.0.7.zip",
     "version": "1.0.7", "size": 3058441, "sha256": "AB" * 32},
    {"id": 587, "kind": "optional", "label": "SkyrimNet Add-on", "name": "Add-on.zip",
     "version": "1.0.1", "size": 969, "sha256": "cd" * 32},
]


@pytest.mark.parametrize("n,expected", [
    (0, "0 B"), (969, "969 B"), (2048, "2.0 KB"),
    (3058441, "2.9 MB"), (4910442966, "4.6 GB"), (None, "0 B"),
])
def test_format_size(n, expected):
    assert format_size(n) == expected


def test_format_price():
    assert format_price(400) == "$4.00"
    assert format_price(0) == "$0.00"


def test_free_mod_has_no_badges_and_is_not_gated():
    mod = {"access": "free", "price_cents": 0, "beta": False, "adult": False,
           "supporters_until": None}
    assert mod_badges(mod) == [] and not is_gated(mod)


def test_beta_paid_and_adult_badges():
    mod = {"beta": True, "price_cents": 400, "access": "paid", "adult": True}
    assert mod_badges(mod) == ["Beta", "$4.00", "Adult"]
    assert is_gated(mod)


def test_non_free_access_without_a_price_shows_the_access_name():
    assert mod_badges({"access": "pass_only"}) == ["Pass Only"]
    assert is_gated({"access": "pass_only"})


def test_supporter_window_is_a_gate():
    mod = {"access": "free", "supporters_until": "2026-12-01 00:00:00"}
    assert is_gated(mod) and "Supporters early access" in mod_badges(mod)


def test_numbers_line_omits_missing_score():
    assert numbers_line({"numbers": {"downloads": 2370, "score": None, "scores": 0,
                                     "vouches": 0}}) == "2,370 downloads"
    line = numbers_line({"numbers": {"downloads": 5, "score": 9.2, "scores": 14,
                                     "vouches": 12}})
    assert line == "5 downloads · score 9.2/10 (14) · 12 vouches"
    assert numbers_line({}) == "0 downloads"


def test_default_selection_is_the_main_files():
    assert [f["id"] for f in default_file_selection(FILES)] == [1508]


def test_default_selection_falls_back_to_the_first_file():
    only_optional = [dict(FILES[1]), dict(FILES[1], id=9)]
    assert [f["id"] for f in default_file_selection(only_optional)] == [587]
    assert default_file_selection([]) == []


def test_chooser_only_when_there_is_a_choice():
    assert needs_chooser(FILES)
    assert not needs_chooser(FILES[:1])
    assert not needs_chooser([])


def test_meta_from_detail():
    detail = {"id": 12, "name": "Falkreath Shop", "version": "1.0.7", "beta": False,
              "page": "https://modholdmods.com/g/skyrim-se/mods/12/x",
              "author": {"handle": "SethSaidThat", "name": "Seth"}}
    meta = meta_from_detail(detail, FILES[0], "skyrim-se", loadout="3")
    assert (meta.mod_id, meta.file_id, meta.version) == (12, 1508, "1.0.7")
    assert meta.sha256 == "ab" * 32            # lower-cased
    assert (meta.uploader, meta.game, meta.loadout) == ("Seth", "skyrim-se", "3")
    assert meta.locked == ""


def test_meta_marks_beta_and_prefers_the_files_version():
    detail = {"id": 197, "name": "Skywent", "version": "0.2.9", "beta": True,
              "author": {"handle": "SethSaidThat"}}
    meta = meta_from_detail(detail, {"id": 1828, "version": "0.2.9 beta"}, "skyrim-se")
    assert meta.locked == "beta" and meta.version == "0.2.9 beta"
    assert meta.uploader == "SethSaidThat"


@pytest.mark.parametrize("total,per,expected", [
    (0, 50, 1), (1, 50, 1), (50, 50, 1), (51, 50, 2), (177, 50, 4), (10, 0, 1),
])
def test_page_count(total, per, expected):
    assert page_count(total, per) == expected
