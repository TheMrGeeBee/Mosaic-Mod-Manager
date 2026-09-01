"""Tests for the collection.json -> CollectionSchemaIndex derivation.

``_build_schema_index`` is pure: a fold over ``collection_schema["mods"]`` with
no I/O, network or game access, so these tests need no fixtures beyond plain
dicts and no mocks. The helpers below read off the same index.
"""
from __future__ import annotations

import pytest

from Utils.collections.collection_install import (
    CollectionSchemaIndex,
    _build_schema_index,
    _manual_domain,
    _manual_url,
    _match_existing,
    _name_candidates,
    _preferred_name,
    _schema_sort_key,
)


def mod_entry(file_id, *, name="", logical=None, mod_id=None, size=None,
              md5=None, domain=None, phase=None, details=None, choices=None,
              hashes=None):
    """One ``collection.json`` ``mods[]`` entry. Omitted keys stay absent, which
    is the case the real manifests exercise most."""
    source: dict = {}
    if file_id is not None:
        source["fileId"] = file_id
    if logical is not None:
        source["logicalFilename"] = logical
    if mod_id is not None:
        source["modId"] = mod_id
    if size is not None:
        source["fileSize"] = size
    if md5 is not None:
        source["md5"] = md5
    entry: dict = {"source": source, "name": name}
    if domain is not None:
        entry["domainName"] = domain
    if phase is not None:
        entry["phase"] = phase
    if details is not None:
        entry["details"] = details
    if choices is not None:
        entry["choices"] = choices
    if hashes is not None:
        entry["hashes"] = hashes
    return entry


class FakeMod:
    """Stand-in for the mod objects the pipeline passes around."""

    def __init__(self, file_id, mod_name="", mod_id=0, domain_name=""):
        self.file_id = file_id
        self.mod_name = mod_name
        self.mod_id = mod_id
        self.domain_name = domain_name


# --------------------------------------------------------------------------
# _build_schema_index
# --------------------------------------------------------------------------
def test_empty_manifest_yields_empty_maps():
    idx = _build_schema_index({"mods": []})
    assert isinstance(idx, CollectionSchemaIndex)
    assert idx.mods == []
    assert idx.file_id_to_pos == {}
    assert idx.file_id_to_suffix == {}
    assert idx.fomod_by_file_id == {}


def test_manifest_with_no_mods_key_does_not_raise():
    idx = _build_schema_index({})
    assert idx.mods == []
    assert idx.file_id_to_mod_id == {}


def test_entries_without_a_file_id_are_skipped():
    idx = _build_schema_index({"mods": [
        mod_entry(None, name="No file id"),
        mod_entry(7, name="Real", mod_id=3),
    ]})
    assert list(idx.file_id_to_mod_id) == [7]
    # the skipped entry still occupies its slot in the raw array
    assert len(idx.mods) == 2


def test_arrayidx_is_manifest_order_not_priority_order():
    """file_id_to_arrayidx is the plain mods[] index; file_id_to_pos is the
    resolved priority rank. Manual mode prompts in array order, so these two
    must stay distinguishable."""
    idx = _build_schema_index({"mods": [
        mod_entry(10, name="First"),
        mod_entry(20, name="Second"),
        mod_entry(30, name="Third"),
    ]})
    assert idx.file_id_to_arrayidx == {10: 0, 20: 1, 30: 2}


def test_sort_key_orders_by_resolved_priority_and_unlisted_sorts_last():
    idx = _build_schema_index({"mods": [
        mod_entry(10, name="A"), mod_entry(20, name="B"), mod_entry(30, name="C"),
    ]})
    known = [FakeMod(30), FakeMod(10), FakeMod(20)]
    ordered = sorted(known, key=lambda m: _schema_sort_key(idx, m))
    assert [m.file_id for m in ordered] == sorted(
        [10, 20, 30], key=lambda f: idx.file_id_to_pos[f])

    unlisted = FakeMod(999)
    assert _schema_sort_key(idx, unlisted) == len(idx.mods)
    assert _schema_sort_key(idx, unlisted) >= max(
        _schema_sort_key(idx, m) for m in known)


# --------------------------------------------------------------------------
# numeric / string coercion
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [(0, 0), (2, 2), ("3", 3), (None, 0)])
def test_phase_is_coerced_to_int_with_zero_default(raw, expected):
    idx = _build_schema_index({"mods": [mod_entry(1, phase=raw)]})
    assert idx.file_id_to_phase[1] == expected


def test_malformed_phase_falls_back_to_zero():
    idx = _build_schema_index({"mods": [mod_entry(1, phase="not-a-number")]})
    assert idx.file_id_to_phase[1] == 0


def test_unparseable_file_size_is_omitted_rather_than_stored_wrong():
    idx = _build_schema_index({"mods": [
        mod_entry(1, size="bogus"), mod_entry(2, size="4096"), mod_entry(3, size=99),
    ]})
    assert 1 not in idx.file_id_to_size
    assert idx.file_id_to_size[2] == 4096
    assert idx.file_id_to_size[3] == 99


def test_md5_is_normalised_to_lowercase_and_stripped():
    idx = _build_schema_index({"mods": [mod_entry(1, md5="  ABCdef01  ")]})
    assert idx.file_id_to_md5[1] == "abcdef01"


def test_blank_md5_and_domain_are_omitted_not_stored_empty():
    idx = _build_schema_index({"mods": [mod_entry(1, md5="   ", domain="  ")]})
    assert 1 not in idx.file_id_to_md5
    assert 1 not in idx.file_id_to_domain


def test_cross_domain_entry_records_manifest_domain_size_and_md5():
    """A Skyrim SE mod referenced by an Enderal SE collection: the GraphQL mod
    list omits these, so the manifest is the only source."""
    idx = _build_schema_index({"mods": [
        mod_entry(1, name="Cross", domain="skyrimspecialedition",
                  size=1234, md5="AABB"),
    ]})
    assert idx.file_id_to_domain[1] == "skyrimspecialedition"
    assert idx.file_id_to_size[1] == 1234
    assert idx.file_id_to_md5[1] == "aabb"


def test_details_type_and_category_are_stripped_and_blanks_dropped():
    idx = _build_schema_index({"mods": [
        mod_entry(1, details={"type": "  dinput  ", "category": "  Patches  "}),
        mod_entry(2, details={"type": "", "category": ""}),
    ]})
    assert idx.file_id_to_install_type[1] == "dinput"
    assert idx.file_id_to_category[1] == "Patches"
    assert 2 not in idx.file_id_to_install_type
    assert 2 not in idx.file_id_to_category


# --------------------------------------------------------------------------
# choices dispatch
# --------------------------------------------------------------------------
def test_fomod_choices_are_converted_to_saved_selections_format():
    idx = _build_schema_index({"mods": [mod_entry(1, choices={
        "type": "fomod",
        "options": [{"groups": [{"name": "Group A", "choices": [
            {"name": "Plugin1"}, {"name": "Plugin2"}]}]}],
    })]})
    assert idx.fomod_by_file_id[1] == {"0": {"Group A": ["Plugin1", "Plugin2"]}}
    assert idx.bain_by_file_id == {}


def test_fomod_selections_are_passed_through_verbatim():
    sel = {"0": {"G": ["P"]}}
    idx = _build_schema_index({"mods": [
        mod_entry(1, choices={"type": "fomod_selections", "selections": sel})]})
    assert idx.fomod_by_file_id[1] == sel


def test_bain_selections_go_to_the_bain_map_not_the_fomod_map():
    idx = _build_schema_index({"mods": [
        mod_entry(1, choices={"type": "bain_selections",
                              "selections": ["00 Core"]})]})
    assert idx.bain_by_file_id[1] == ["00 Core"]
    assert idx.fomod_by_file_id == {}


def test_unknown_choices_type_populates_neither_map():
    idx = _build_schema_index({"mods": [
        mod_entry(1, choices={"type": "something_new", "selections": ["x"]})]})
    assert idx.fomod_by_file_id == {}
    assert idx.bain_by_file_id == {}


# --------------------------------------------------------------------------
# logical-name resolution and collision suffixes
# --------------------------------------------------------------------------
def test_unique_logical_filename_is_used_as_the_logical_name():
    idx = _build_schema_index({"mods": [
        mod_entry(1, name="Display Name", logical="Unique Logical")]})
    assert idx.file_id_to_logical[1] == "Unique Logical"


def test_shared_logical_filename_falls_back_to_the_display_name():
    """Two entries sharing one logicalFilename would install to the same
    folder, so the per-entry display name wins instead."""
    idx = _build_schema_index({"mods": [
        mod_entry(1, name="Mod One", logical="Shared"),
        mod_entry(2, name="Mod Two", logical="Shared"),
    ]})
    assert idx.file_id_to_logical[1] == "Mod One"
    assert idx.file_id_to_logical[2] == "Mod Two"


def test_missing_logical_filename_falls_back_to_display_name():
    idx = _build_schema_index({"mods": [mod_entry(1, name="Only A Name")]})
    assert idx.file_id_to_logical[1] == "Only A Name"


def test_non_colliding_entries_get_an_empty_suffix():
    idx = _build_schema_index({"mods": [
        mod_entry(1, name="Alpha", mod_id=1),
        mod_entry(2, name="Beta", mod_id=2),
    ]})
    assert idx.file_id_to_suffix.get(1, "") == ""
    assert idx.file_id_to_suffix.get(2, "") == ""


def test_colliding_entries_from_different_mod_pages_get_distinct_folders():
    """Same resolved name from two different mod pages must not install into
    one folder -- _preferred_name has to return different values."""
    idx = _build_schema_index({"mods": [
        mod_entry(1, name="Same Name", mod_id=100),
        mod_entry(2, name="Same Name", mod_id=200),
    ]})
    a = _preferred_name(idx, FakeMod(1, mod_name="Same Name"))
    b = _preferred_name(idx, FakeMod(2, mod_name="Same Name"))
    assert a != b


# --------------------------------------------------------------------------
# hoisted per-mod helpers
# --------------------------------------------------------------------------
def test_preferred_name_prefers_logical_then_schema_then_mod_name():
    idx = _build_schema_index({"mods": [
        mod_entry(1, name="Schema Name", logical="Logical Name"),
        mod_entry(2, name="Schema Only"),
    ]})
    assert _preferred_name(idx, FakeMod(1, mod_name="Obj")) == "Logical Name"
    assert _preferred_name(idx, FakeMod(2, mod_name="Obj")) == "Schema Only"
    # unknown file_id: nothing in the manifest, fall back to the mod object
    assert _preferred_name(idx, FakeMod(404, mod_name="Obj")) == "Obj"


def test_name_candidates_falls_back_to_mod_name_for_unknown_file_id():
    idx = _build_schema_index({"mods": []})
    assert _name_candidates(idx, FakeMod(1, mod_name="Some Mod")) != []


def test_name_candidates_is_deduplicated_and_ordered():
    idx = _build_schema_index({"mods": [mod_entry(1, name="Dup", logical="Dup")]})
    got = _name_candidates(idx, FakeMod(1, mod_name="Dup"))
    assert len(got) == len(set(got))


def test_match_existing_prefers_the_mod_id_plus_file_id_pair():
    idx = _build_schema_index({"mods": [mod_entry(5, name="M", mod_id=42)]})
    by_ids = {(42, 5): "Folder From Ids"}
    by_fid = {5: "Folder From Fid"}
    assert _match_existing(idx, by_ids, by_fid, FakeMod(5)) == "Folder From Ids"


def test_match_existing_falls_back_to_file_id_then_to_empty():
    idx = _build_schema_index({"mods": [mod_entry(5, name="M", mod_id=42)]})
    assert _match_existing(idx, {}, {5: "By Fid"}, FakeMod(5)) == "By Fid"
    assert _match_existing(idx, {}, {}, FakeMod(5)) == ""


def test_manual_domain_precedence_mod_object_then_manifest_then_game():
    idx = _build_schema_index({"mods": [mod_entry(1, name="M", domain="manifestdom")]})
    assert _manual_domain(idx, "gamedom", FakeMod(1, domain_name="objdom")) == "objdom"
    assert _manual_domain(idx, "gamedom", FakeMod(1)) == "manifestdom"
    assert _manual_domain(idx, "gamedom", FakeMod(2)) == "gamedom"


def test_manual_url_uses_manifest_mod_id_and_domain_for_cross_domain_entries():
    idx = _build_schema_index({"mods": [
        mod_entry(7, name="Cross", mod_id=555, domain="skyrimspecialedition")]})
    url = _manual_url(idx, "enderalse", FakeMod(7, mod_id=1))
    assert url == ("https://www.nexusmods.com/skyrimspecialedition/mods/555"
                   "?tab=files&file_id=7")


def test_manual_url_falls_back_to_the_mod_objects_own_id_and_game_domain():
    idx = _build_schema_index({"mods": []})
    url = _manual_url(idx, "gamedom", FakeMod(9, mod_id=88))
    assert url == "https://www.nexusmods.com/gamedom/mods/88?tab=files&file_id=9"


# --------------------------------------------------------------------------
# _fetch_extra_meta (background thread target)
# --------------------------------------------------------------------------
class _FakeAPI:
    def __init__(self, result=None, boom=False):
        self.seen = None
        self._result = result or {}
        self._boom = boom

    def graphql_mod_update_info_batch(self, pairs):
        self.seen = pairs
        if self._boom:
            raise RuntimeError("network down")
        return self._result


def test_fetch_extra_meta_batches_deduplicated_mod_ids():
    """Three manifest entries, two distinct modIds -- the batch must not ask
    for the same mod twice."""
    import threading

    from Utils.collections.collection_install import _fetch_extra_meta

    idx = _build_schema_index({"mods": [
        mod_entry(1, name="A", mod_id=100),
        mod_entry(2, name="B", mod_id=200),
        mod_entry(3, name="C", mod_id=100),
    ]})
    api = _FakeAPI({100: "meta-100", 200: "meta-200"})
    extra, ready, logs = {}, threading.Event(), []

    _fetch_extra_meta(api, "skyrimspecialedition", idx, extra, ready, logs.append)

    assert api.seen == [("skyrimspecialedition", 100), ("skyrimspecialedition", 200)]
    assert extra == {100: "meta-100", 200: "meta-200"}
    assert ready.is_set()
    assert logs == []


def test_fetch_extra_meta_sets_ready_even_when_the_api_raises():
    """Step 5 waits on this event. If a failure left it unset the install
    would block forever instead of just missing categories."""
    import threading

    from Utils.collections.collection_install import _fetch_extra_meta

    idx = _build_schema_index({"mods": [mod_entry(1, name="A", mod_id=100)]})
    extra, ready, logs = {}, threading.Event(), []

    _fetch_extra_meta(_FakeAPI(boom=True), "d", idx, extra, ready, logs.append)

    assert ready.is_set()
    assert extra == {}
    assert logs and "background metadata fetch failed" in logs[0]


def test_fetch_extra_meta_skips_the_api_call_when_no_mod_ids_are_known():
    import threading

    from Utils.collections.collection_install import _fetch_extra_meta

    idx = _build_schema_index({"mods": [mod_entry(1, name="No mod id")]})
    api = _FakeAPI()
    ready = threading.Event()

    _fetch_extra_meta(api, "d", idx, {}, ready, lambda _m: None)

    assert api.seen is None
    assert ready.is_set()


# --------------------------------------------------------------------------
# file_id_to_hashes — the author's recorded file set, used when a FOMOD has
# no replayable `choices`
# --------------------------------------------------------------------------
def test_hashes_populate_the_index():
    idx = _build_schema_index({"mods": [mod_entry(1, name="M", hashes=[
        {"path": "A.esp", "md5": "aa"}, {"path": "meshes\\b.nif", "md5": "bb"}])]})
    assert idx.file_id_to_hashes[1] == [
        {"path": "A.esp", "md5": "aa"}, {"path": "meshes\\b.nif", "md5": "bb"}]


def test_absent_or_empty_hashes_yield_no_entry():
    idx = _build_schema_index({"mods": [
        mod_entry(1, name="none"), mod_entry(2, name="empty", hashes=[])]})
    assert 1 not in idx.file_id_to_hashes
    assert 2 not in idx.file_id_to_hashes


def test_entries_missing_path_or_md5_are_dropped():
    """A partial list would resolve to a partial install, so incomplete
    entries must not reach the resolver."""
    idx = _build_schema_index({"mods": [mod_entry(1, name="M", hashes=[
        {"path": "Good.esp", "md5": "aa"},
        {"path": "NoMd5.esp"},
        {"md5": "cc"},
        {"path": "", "md5": "dd"},
        {"path": "Blank.esp", "md5": ""},
    ])]})
    assert idx.file_id_to_hashes[1] == [{"path": "Good.esp", "md5": "aa"}]


def test_a_mod_can_carry_both_choices_and_hashes():
    """Vortex emits one or the other in practice, but the format permits both
    and the index must not lose either."""
    idx = _build_schema_index({"mods": [mod_entry(1, name="M",
        choices={"type": "fomod_selections", "selections": {"a": [1]}},
        hashes=[{"path": "A.esp", "md5": "aa"}])]})
    assert idx.fomod_by_file_id[1] == {"a": [1]}
    assert idx.file_id_to_hashes[1] == [{"path": "A.esp", "md5": "aa"}]
