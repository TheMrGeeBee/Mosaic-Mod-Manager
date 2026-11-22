"""Loadout parsing and install planning, including the oddities seen in a real loadout."""
from __future__ import annotations

import copy

import pytest

from Modhold.loadout_manifest import (
    LoadoutFormatError,
    parse_loadout,
    plan_install,
)

SHA = "AB" * 32


def _file(fid, name=None, size=100):
    return {"id": fid, "name": name or f"f{fid}.zip", "version": "1.0", "size": size,
            "sha256": SHA}


def _hosted(pos, mod_id, files, optional=False, locked=None, note="", nexus=None):
    return {"position": pos, "optional": optional, "note": note,
            "mod": {"id": mod_id, "name": f"Mod {mod_id}", "version": "1.0",
                    "author": "Seth", "author_handle": "SethSaidThat",
                    "page": f"https://modholdmods.com/g/skyrim-se/mods/{mod_id}/x"},
            "locked": locked, "files": files, "nexus": nexus}


def _external(pos, name, url, optional=False, note=""):
    return {"position": pos, "optional": optional, "note": note,
            "mod": {"id": None, "name": name, "version": None, "author": None, "page": url},
            "locked": None, "files": [], "nexus": url}


def _payload(items, **kw):
    base = {"format": "modhold-loadout", "version": 1, "id": 3, "name": "Estates",
            "summary": "s", "by": "SethSaidThat",
            "game": {"slug": "skyrim-se", "name": "Skyrim SE", "short": "SSE"},
            "updated": "2026-11-20 16:04:56", "page": "https://modholdmods.com/loadouts/3",
            "signed_in": True, "items": items}
    base.update(kw)
    return base


def test_parses_the_header_and_items():
    lo = parse_loadout(_payload([_hosted(1, 786, [_file(1182)])]))
    assert (lo.id, lo.name, lo.by, lo.game_slug) == (3, "Estates", "SethSaidThat", "skyrim-se")
    assert lo.signed_in and len(lo.items) == 1
    item = lo.items[0]
    assert item.hosted and item.mod_id == 786 and item.files[0].sha256 == SHA.lower()
    assert item.author == "Seth" and item.locked == ""


@pytest.mark.parametrize("bad", [None, [], {}, {"format": "something-else", "version": 1}])
def test_not_a_loadout_is_rejected(bad):
    with pytest.raises(LoadoutFormatError):
        parse_loadout(bad)


def test_a_newer_format_is_refused_with_a_clear_message():
    with pytest.raises(LoadoutFormatError, match="Update Mosaic"):
        parse_loadout(_payload([], version=2))


def test_repeated_positions_keep_payload_order_and_unique_indexes():
    """Real data repeats positions (two items at 54, 55, 56, 57, 58...)."""
    items = [_hosted(54, 1, [_file(10)]), _external(54, "Island Lord", "https://n/1"),
             _hosted(53, 2, [_file(20)]), _hosted(54, 3, [_file(30)])]
    lo = parse_loadout(_payload(items))
    assert [i.name for i in lo.items] == ["Mod 2", "Mod 1", "Island Lord", "Mod 3"]
    assert len({i.order_index for i in lo.items}) == 4


def test_unhosted_items_keep_their_link_and_hosted_ones_do_not_need_it():
    lo = parse_loadout(_payload([
        _external(1, "SkyUI", "https://www.nexusmods.com/skyrimspecialedition/mods/12604"),
        _hosted(2, 786, [_file(1182)], nexus="https://www.nexusmods.com/x/mods/53000")]))
    ext, hosted = lo.items
    assert not ext.hosted and ext.external_url.endswith("12604") and ext.link.endswith("12604")
    assert hosted.hosted and hosted.external_url == ""            # its Nexus origin is not needed
    assert hosted.link == hosted.page


def test_a_mod_with_an_id_but_no_files_is_not_hosted():
    lo = parse_loadout(_payload([_hosted(1, 5, [], nexus="https://n/5")]))
    assert not lo.items[0].hosted and lo.items[0].external_url == "https://n/5"


def test_beta_is_marked_and_files_without_ids_are_dropped():
    lo = parse_loadout(_payload([_hosted(1, 9, [_file(7), {"id": 0}, {"name": "x"}],
                                         locked="beta")]))
    item = lo.items[0]
    assert item.locked == "beta" and [f.id for f in item.files] == [7]


def test_plan_splits_hosted_external_optional_and_gated():
    lo = parse_loadout(_payload([
        _hosted(1, 1, [_file(10, size=100)]),
        _external(2, "Nexus thing", "https://n/2"),
        _hosted(3, 3, [_file(30, size=50)], optional=True),
        _hosted(4, 4, [_file(40, size=25)], locked="beta"),
        _external(5, "Optional ext", "https://n/5", optional=True),
    ]))
    everything = plan_install(lo)
    assert [i.mod_id for i in everything.installable] == [1, 3, 4]
    assert [i.name for i in everything.external] == ["Nexus thing", "Optional ext"]
    assert everything.skipped_optional == []
    assert [i.mod_id for i in everything.gated] == [4]
    assert (everything.file_count, everything.download_bytes) == (3, 175)

    optional_idx = {i.order_index for i in lo.items if i.optional}
    none = plan_install(lo, include_optional=set())
    assert [i.mod_id for i in none.installable] == [1, 4]
    assert [i.name for i in none.external] == ["Nexus thing"]
    assert {i.order_index for i in none.skipped_optional} == optional_idx

    only_hosted = plan_install(lo, include_optional={i.order_index for i in lo.items
                                                     if i.optional and i.hosted})
    assert [i.mod_id for i in only_hosted.installable] == [1, 3, 4]
    assert [i.name for i in only_hosted.external] == ["Nexus thing"]


def test_required_items_ignore_the_optional_selection():
    lo = parse_loadout(_payload([_hosted(1, 1, [_file(10)]), _hosted(2, 2, [_file(20)])]))
    assert len(plan_install(lo, include_optional=set()).installable) == 2


def test_a_file_listed_twice_installs_once():
    lo = parse_loadout(_payload([_hosted(1, 1, [_file(10)]), _hosted(2, 1, [_file(10)]),
                                 _hosted(3, 1, [_file(10), _file(11)])]))
    plan = plan_install(lo)
    assert [[f.id for f in i.files] for i in plan.installable] == [[10], [11]]
    assert plan.file_count == 2


def test_the_same_external_link_is_listed_once():
    lo = parse_loadout(_payload([_external(1, "A", "https://n/1"),
                                 _external(2, "A again", "https://n/1")]))
    assert len(plan_install(lo).external) == 1


def test_planning_does_not_mutate_the_loadout():
    lo = parse_loadout(_payload([_hosted(1, 1, [_file(10)]), _hosted(2, 1, [_file(10), _file(11)])]))
    before = copy.deepcopy([i.files for i in lo.items])
    plan_install(lo)
    assert [i.files for i in lo.items] == before


def test_a_real_world_shape_with_a_null_nexus_hosted_item():
    """Items only on Modhold (e.g. Amber) have ``nexus: null`` and must stay hosted."""
    lo = parse_loadout(_payload([_hosted(48, 198, [_file(1154)], nexus=None)]))
    assert lo.items[0].hosted and lo.items[0].external_url == ""


# ------------------------------------------------------- game_version / works_on

def test_game_version_and_works_on_are_read():
    lo = parse_loadout(_payload([], game_version=" 1.6.1170 ", works_on=["1.6.1179", " 1.7.104 "]))
    assert lo.game_version == "1.6.1170" and lo.works_on == ["1.6.1179", "1.7.104"]


@pytest.mark.parametrize("extra", [
    {}, {"game_version": None, "works_on": None}, {"game_version": 1.6, "works_on": "1.7.104"},
    {"game_version": ["1.6.1170"], "works_on": {"a": 1}}, {"game_version": "  ", "works_on": []},
])
def test_missing_or_wrongly_typed_version_fields_are_empty(extra):
    lo = parse_loadout(_payload([], **extra))
    assert lo.game_version == "" and lo.works_on == []


def test_works_on_keeps_only_text_entries():
    lo = parse_loadout(_payload([], works_on=["1.6.1179", None, 5, "", "  ", "1.7.104"]))
    assert lo.works_on == ["1.6.1179", "1.7.104"]
