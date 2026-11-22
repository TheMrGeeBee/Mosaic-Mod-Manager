"""Nexus links in a loadout: parsing, which file gets chosen, and the plan split."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from Modhold.loadout_manifest import parse_loadout, plan_install
from Modhold.nexus_links import (
    NexusRef,
    choose_file,
    parse_nexus_url,
    resolve_nexus_picks,
)


@pytest.mark.parametrize("url,expected", [
    ("https://www.nexusmods.com/skyrimspecialedition/mods/6817",
     NexusRef("skyrimspecialedition", 6817)),
    ("https://www.nexusmods.com/skyrimspecialedition/mods/98556?tab=description",
     NexusRef("skyrimspecialedition", 98556)),            # a real loadout link
    ("https://www.nexusmods.com/skyrimspecialedition/mods/271/",
     NexusRef("skyrimspecialedition", 271)),
    ("https://www.nexusmods.com/skyrimspecialedition/mods/12604?tab=files&file_id=555",
     NexusRef("skyrimspecialedition", 12604, 555)),
    ("https://nexusmods.com/Fallout4/mods/7#files", NexusRef("fallout4", 7)),
    ("  https://next.nexusmods.com/skyrimspecialedition/mods/5  ",
     NexusRef("skyrimspecialedition", 5)),
])
def test_nexus_mod_links_are_recognised(url, expected):
    assert parse_nexus_url(url) == expected


@pytest.mark.parametrize("url", [
    "https://vectorplexis.com/files/file/283-high-poly-head/",     # a real non-Nexus item
    "https://modholdmods.com/g/skyrim-se/mods/12/x",
    "https://evil.example/www.nexusmods.com/skyrimspecialedition/mods/1",
    "https://www.nexusmods.com.evil.example/skyrimspecialedition/mods/1",
    "https://www.nexusmods.com/skyrimspecialedition/collections/abc",
    "https://www.nexusmods.com/skyrimspecialedition/mods/notanumber",
    "https://www.nexusmods.com/", "", None, "not a url", "nxm://skyrimspecialedition/mods/1",
])
def test_everything_else_is_not_a_nexus_mod_link(url):
    assert parse_nexus_url(url) is None


def _f(fid, cat="MAIN", primary=False, ts=0, name=None, desc=""):
    return SimpleNamespace(file_id=fid, category_name=cat, is_primary=primary,
                           uploaded_timestamp=ts, file_name=name or f"f{fid}.7z",
                           name=name or f"File {fid}", version="1", size_in_bytes=10,
                           description=desc)


def test_the_file_named_in_the_link_wins_even_if_it_is_not_a_main_file():
    files = [_f(1, "MAIN", True, 100), _f(2, "OPTIONAL", ts=50)]
    assert choose_file(files, wanted_file_id=2).file.file_id == 2
    gone = choose_file(files, wanted_file_id=99)
    assert gone.file is None and "no longer exists" in gone.reason


def test_the_primary_main_file_is_preferred_over_a_newer_one():
    files = [_f(1, "MAIN", primary=True, ts=100), _f(2, "MAIN", ts=900), _f(3, "OPTIONAL", ts=999)]
    c = choose_file(files)
    assert c.file.file_id == 1 and not c.ambiguous


def test_without_a_primary_the_newest_main_is_chosen_and_flagged_when_there_were_several():
    several = choose_file([_f(1, "MAIN", ts=100), _f(2, "MAIN", ts=300), _f(3, "MAIN", ts=200)])
    assert several.file.file_id == 2 and several.ambiguous
    single = choose_file([_f(1, "MAIN", ts=100), _f(2, "UPDATE", ts=500), _f(3, "OLD_VERSION", ts=9)])
    assert single.file.file_id == 1 and not single.ambiguous


def test_optional_old_and_misc_files_are_never_picked_automatically():
    c = choose_file([_f(1, "OPTIONAL"), _f(2, "OLD_VERSION"), _f(3, "MISCELLANEOUS"), _f(4, "UPDATE")])
    assert c.file is None and "no main file" in c.reason
    assert choose_file([]).file is None


def test_category_names_are_matched_case_insensitively():
    assert choose_file([_f(1, "main")]).file.file_id == 1


# ------------------------------------------------------------------ picks via the API

def _item(order_index, name, ref):
    return SimpleNamespace(order_index=order_index, name=name, nexus_ref=ref)


class _Api:
    def __init__(self, listings, errors=None):
        self.listings, self.errors, self.calls = listings, errors or {}, []

    def get_mod_files(self, domain, mod_id):
        self.calls.append((domain, mod_id))
        if mod_id in self.errors:
            raise self.errors[mod_id]
        return SimpleNamespace(files=self.listings[mod_id])


def test_picks_cover_every_nexus_item_and_one_failure_does_not_stop_the_rest():
    api = _Api({1: [_f(10, ts=5)], 3: [_f(30, "OPTIONAL")]}, errors={2: RuntimeError("hidden mod")})
    items = [_item(0, "A", NexusRef("skyrimspecialedition", 1)),
             _item(1, "B", NexusRef("skyrimspecialedition", 2)),
             _item(2, "C", NexusRef("skyrimspecialedition", 3)),
             _item(3, "No link", None)]
    logs = []
    picks = resolve_nexus_picks(api, items, log=logs.append)
    assert set(picks) == {0, 1, 2}
    assert picks[0].usable and picks[0].file.file_id == 10
    assert not picks[1].usable and "hidden mod" in picks[1].reason
    assert not picks[2].usable and "no main file" in picks[2].reason
    assert any("hidden mod" in m for m in logs)
    assert sorted(api.calls) == [("skyrimspecialedition", 1), ("skyrimspecialedition", 2),
                                 ("skyrimspecialedition", 3)]


def test_no_nexus_items_means_no_calls():
    api = _Api({})
    assert resolve_nexus_picks(api, [_item(0, "x", None)]) == {} and api.calls == []


# ----------------------------------------------------------------- plan split

def _ext(pos, name, url):
    return {"position": pos, "optional": False, "note": "",
            "mod": {"id": None, "name": name, "page": url}, "locked": None, "files": [], "nexus": url}


def _hosted(pos, mod_id, fid):
    return {"position": pos, "optional": False, "note": "",
            "mod": {"id": mod_id, "name": f"Mod {mod_id}", "version": "1", "author": "A",
                    "page": f"https://modholdmods.com/m/{mod_id}"}, "locked": None,
            "files": [{"id": fid, "name": f"f{fid}.zip", "version": "1", "size": 10,
                       "sha256": "ab" * 32}], "nexus": None}


def _loadout():
    return parse_loadout({"format": "modhold-loadout", "version": 1, "id": 3, "name": "L", "by": "S",
                          "game": {"slug": "skyrim-se"}, "items": [
        _ext(1, "SkyUI", "https://www.nexusmods.com/skyrimspecialedition/mods/12604"),
        _hosted(2, 10, 100),
        _ext(3, "High Poly Head", "https://vectorplexis.com/files/file/283-high-poly-head/"),
        _ext(4, "SKSE", "https://www.nexusmods.com/skyrimspecialedition/mods/30379?tab=files"),
        _ext(5, "SkyUI again", "https://www.nexusmods.com/skyrimspecialedition/mods/12604")]})


def test_items_carry_their_nexus_reference():
    lo = _loadout()
    refs = {i.name: i.nexus_ref for i in lo.items}
    assert refs["SkyUI"] == NexusRef("skyrimspecialedition", 12604)
    assert refs["Mod 10"] is None and refs["High Poly Head"] is None


def test_by_default_nothing_is_fetched_from_nexus():
    plan = plan_install(_loadout())
    assert plan.nexus == []
    assert [i.name for i in plan.external] == ["SkyUI", "High Poly Head", "SKSE"]


def test_chosen_nexus_items_move_out_of_the_manual_list_and_keep_their_order():
    lo = _loadout()
    nexus_idx = {i.order_index for i in lo.items if i.nexus_ref}
    plan = plan_install(lo, nexus_items=nexus_idx)
    assert [i.name for i in plan.nexus] == ["SkyUI", "SKSE"]          # the duplicate is dropped
    assert [i.name for i in plan.external] == ["High Poly Head"]      # a non-Nexus link stays manual
    assert [i.name for i in plan.ordered()] == ["SkyUI", "Mod 10", "SKSE"]   # loadout order, mixed sources


def test_unticking_a_nexus_item_leaves_it_on_the_manual_list():
    lo = _loadout()
    only_skse = {i.order_index for i in lo.items if i.name == "SKSE"}
    plan = plan_install(lo, nexus_items=only_skse)
    assert [i.name for i in plan.nexus] == ["SKSE"]
    assert [i.name for i in plan.external] == ["SkyUI", "High Poly Head"]


def test_modhold_hosted_items_are_unaffected_by_the_nexus_choice():
    lo = _loadout()
    assert [i.mod_id for i in plan_install(lo, nexus_items=set()).installable] == [10]


# ------------------------------------------------- files that name the game version

def _skse():
    """Shaped like the real SKSE page: the primary main file is for the newest game, an
    older build is kept under 'old versions' and says which game version it suits."""
    return [_f(1, "MAIN", True, 900, "SKSE 2.3.1", desc="Skyrim Special Edition 1.7.104 build"),
            _f(2, "OLD_VERSION", False, 500, "SKSE 2.2.8",
               desc="Compatible with Skyrim Special Edition 1.6.1170 from Steam"),
            _f(3, "OLD_VERSION", False, 300, "SKSE 2.2.6", desc="Compatible with 1.6.1130")]


def test_an_older_file_that_names_the_game_version_beats_the_newest_main():
    c = choose_file(_skse(), game_version="1.6.1170")
    assert c.file.file_id == 2 and c.version_matched and not c.ambiguous


def test_the_current_game_version_keeps_the_current_file():
    c = choose_file(_skse(), game_version="1.7.104")
    assert c.file.file_id == 1 and c.version_matched
    assert choose_file(_skse(), game_version="1.7.104.0").file.file_id == 1   # trailing .0


def test_without_a_matching_file_or_a_version_the_old_rule_applies():
    for gv in ("1.5.97", ""):
        c = choose_file(_skse(), game_version=gv)
        assert c.file.file_id == 1 and not c.version_matched


def test_the_file_named_in_the_link_still_wins_over_the_game_version():
    assert choose_file(_skse(), wanted_file_id=3, game_version="1.6.1170").file.file_id == 3


@pytest.mark.parametrize("desc", [
    "for 11.6.11700", "built for 0.1.6.1170", "1.6.11705", "not compatible with 1.6.1170",
    "No longer works with 1.6.1170", "does not support Skyrim 1.6.1170", "x1.6.11700.1",
])
def test_a_version_inside_another_number_or_a_negation_does_not_count(desc):
    from Modhold.nexus_links import names_version
    assert not names_version(_f(9, "MAIN", desc=desc), "1.6.1170")


def test_the_version_is_found_in_the_file_name_too():
    from Modhold.nexus_links import names_version
    assert names_version(_f(9, "MAIN", name="AddressLibrary 1.6.1170 v12.7z"), "1.6.1170")


def test_miscellaneous_files_are_never_picked_for_a_version():
    files = [_f(1, "MAIN", True, 9), _f(2, "MISCELLANEOUS", False, 99, desc="tool for 1.6.1170")]
    assert choose_file(files, game_version="1.6.1170").file.file_id == 1


def test_among_several_matches_main_then_primary_then_newest_wins():
    files = [_f(1, "OLD_VERSION", desc="1.6.1170", ts=900), _f(2, "OPTIONAL", True, 800, desc="1.6.1170"),
             _f(3, "MAIN", False, 100, desc="1.6.1170"), _f(4, "MAIN", False, 200, desc="1.6.1170")]
    assert choose_file(files, game_version="1.6.1170").file.file_id == 4
    files = [f for f in files if f.file_id != 3 and f.file_id != 4]
    assert choose_file(files, game_version="1.6.1170").file.file_id == 2      # primary over newest


def test_picks_carry_the_version_and_say_why(monkeypatch):
    api = _Api({1: _skse()})
    items = [_item(0, "SKSE", NexusRef("skyrimspecialedition", 1))]
    pick = resolve_nexus_picks(api, items, game_version="1.6.1170")[0]
    assert pick.file.file_id == 2 and pick.version_matched
    other = resolve_nexus_picks(api, items)[0]                       # no version: unchanged
    assert other.file.file_id == 1 and not other.version_matched
