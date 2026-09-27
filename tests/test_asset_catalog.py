"""AssetCatalog: the layered override view (base game → mods in load order)."""
from __future__ import annotations

import pytest

from Utils.archives.bsa_writer import write_bsa
from Utils.nif.asset_catalog import (
    BASE, AssetCatalog, is_viewable, norm_key,
)

MESH = "meshes/armor/iron/cuirass.nif"
TEX = "textures/armor/iron/iron_d.dds"
ONLY_BASE = "meshes/clutter/pot.nif"
NEW_IN_MOD = "meshes/new/thing.nif"


def _put(root, files):
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _bsa(tmp_path, name, files):
    src = tmp_path / f"src_{name}"
    _put(src, files)
    out = tmp_path / name
    write_bsa(out, src, version=105)
    return out


@pytest.fixture
def world(tmp_path):
    """Base game BSA + modA (loose MESH, BSA TEX) + modB (BSA MESH+NEW, loose TEX).

    Load order low→high: modA, modB. Engine rules: loose beats BSA, so
    MESH → modA's loose copy, TEX → modB's loose copy."""
    base = _bsa(tmp_path, "Skyrim - Meshes0.bsa", {
        MESH: b"base-mesh", ONLY_BASE: b"base-pot", TEX: b"base-tex",
        "sounds/x.wav": b"ignored"})
    mods = tmp_path / "mods"
    _put(mods / "modA", {MESH: b"A-loose-mesh"})
    mods.joinpath("modA", "a.bsa").write_bytes(_bsa(tmp_path, "a.bsa", {TEX: b"A-bsa-tex"}).read_bytes())
    mods.joinpath("modB").mkdir(parents=True, exist_ok=True)
    mods.joinpath("modB", "b.bsa").write_bytes(
        _bsa(tmp_path, "b.bsa", {MESH: b"B-bsa-mesh", NEW_IN_MOD: b"B-new"}).read_bytes())
    _put(mods / "modB", {TEX: b"B-loose-tex"})
    cat = AssetCatalog(
        base_name="Game", base_archives=[base], mod_order=["modA", "modB", "empty"],
        loose={"modA": {MESH: MESH}, "modB": {TEX: TEX}},
        bsas={"modA": [("a.bsa", [TEX])], "modB": [("b.bsa", [MESH, NEW_IN_MOD])]},
        loose_winner={MESH: "modA", TEX: "modB"},
        bsa_winner={TEX: "modA", MESH: "modB", NEW_IN_MOD: "modB"},
        mod_dir_for=lambda m: mods / m)
    yield cat
    cat.close()


def test_helpers():
    assert norm_key("\\Meshes\\A\\B.NIF") == "meshes/a/b.nif"
    assert is_viewable("meshes/a.nif") and is_viewable("textures/a.dds")
    assert not is_viewable("meshes/a.dds") and not is_viewable("sounds/a.nif")
    assert not is_viewable("interface/a.dds")


def test_mods_lists_only_mods_with_assets_in_load_order(world):
    assert world.mods() == ["modA", "modB"]


def test_loose_beats_bsa_whatever_the_mod_order(world):
    win = world.resolve(MESH)                    # modB's BSA is later, but modA is loose
    assert (win.mod, win.kind) == ("modA", "loose")
    win = world.resolve(TEX)                     # modB loose beats modA's BSA
    assert (win.mod, win.kind) == ("modB", "loose")


def test_bsa_only_paths_use_the_bsa_winner_and_base_is_the_fallback(world):
    assert (world.resolve(NEW_IN_MOD).mod, world.resolve(NEW_IN_MOD).archive) == ("modB", "b.bsa")
    base = world.resolve(ONLY_BASE)
    assert base.mod == BASE and base.archive == "Skyrim - Meshes0.bsa"
    assert world.resolve("meshes/none.nif") is None


def test_winner_flags_per_mod_copy(world):
    a = {(e.path, e.kind): e.is_winner for e in world.mod_entries("modA")}
    b = {(e.path, e.kind): e.is_winner for e in world.mod_entries("modB")}
    assert a == {(MESH, "loose"): True, (TEX, "bsa"): False}
    assert b == {(TEX, "loose"): True, (MESH, "bsa"): False, (NEW_IN_MOD, "bsa"): True}


def test_base_entries_flag_overridden_files(world):
    base = {e.path: e.is_winner for e in world.base_entries()}
    assert set(base) == {MESH, ONLY_BASE, TEX}          # wav filtered out
    assert base == {MESH: False, TEX: False, ONLY_BASE: True}


def test_read_from_loose_mod_bsa_and_base(world):
    assert world.read(world.resolve(MESH)) == b"A-loose-mesh"          # loose
    assert world.read(world.resolve(NEW_IN_MOD)) == b"B-new"           # mod BSA
    assert world.read(world.resolve(ONLY_BASE)) == b"base-pot"         # base BSA
    overridden = next(e for e in world.base_entries() if e.path == MESH)
    assert world.read(overridden) == b"base-mesh"                      # losing copy still readable


def test_stale_winner_maps_fall_back_to_load_order(tmp_path):
    cat = AssetCatalog(
        base_name="G", base_archives=[], mod_order=["low", "high"],
        loose={"low": {MESH: MESH}, "high": {MESH: MESH}}, bsas={},
        loose_winner={}, bsa_winner={}, mod_dir_for=lambda m: tmp_path / m)
    assert cat.resolve(MESH).mod == "high"


def test_missing_loose_file_and_unreadable_archive_raise(tmp_path):
    cat = AssetCatalog(
        base_name="G", base_archives=[tmp_path / "nope.bsa"], mod_order=["m"],
        loose={"m": {MESH: MESH}}, bsas={}, loose_winner={MESH: "m"}, bsa_winner={},
        mod_dir_for=lambda m: tmp_path / m)
    with pytest.raises(OSError):
        cat.read(cat.resolve(MESH))
    assert cat.base_entries() == []                     # unreadable base archive ignored


def test_resolve_readable_falls_back_past_a_winner_whose_file_is_not_really_there(tmp_path):
    # Real bug: a mod's index claimed a path (e.g. an un-stripped Data/
    # wrapper folder its installer never flattened), but the file isn't
    # actually there on disk at the path the index promises. resolve()
    # still returns that broken winner; resolve_readable() must skip it and
    # fall through to the next lower-priority provider, exactly like the
    # game engine does for a loose override that isn't really on disk.
    base = _bsa(tmp_path, "base.bsa", {TEX: b"base-tex"})
    mods = tmp_path / "mods"
    # "broken" claims to ship TEX loose, but nothing is written to disk for it.
    (mods / "broken").mkdir(parents=True, exist_ok=True)
    cat = AssetCatalog(
        base_name="G", base_archives=[base], mod_order=["broken"],
        loose={"broken": {TEX: TEX}}, bsas={}, loose_winner={TEX: "broken"}, bsa_winner={},
        mod_dir_for=lambda m: mods / m)
    winner = cat.resolve(TEX)
    assert winner.mod == "broken"
    with pytest.raises(OSError):
        cat.read(winner)
    fallback = cat.resolve_readable(TEX)
    assert fallback.mod == BASE
    assert cat.read(fallback) == b"base-tex"
    cat.close()


def test_resolve_readable_is_the_winner_when_it_is_actually_fine(world):
    assert world.resolve_readable(MESH).mod == world.resolve(MESH).mod


def test_resolve_readable_fallback_still_prefers_loose_over_bsa(tmp_path):
    # "broken" is the (unreadable) loose winner. Of the two real fallbacks,
    # "bsa_mod" is higher mod-priority but only has a BSA copy; "loose_mod" is
    # lower-priority but has a genuine loose copy — loose must still win.
    base = _bsa(tmp_path, "base.bsa", {TEX: b"base-tex"})
    mods = tmp_path / "mods"
    _put(mods / "loose_mod", {TEX: b"loose-tex"})
    mods.joinpath("bsa_mod").mkdir(parents=True, exist_ok=True)
    mods.joinpath("bsa_mod", "b.bsa").write_bytes(_bsa(tmp_path, "b.bsa", {TEX: b"bsa-tex"}).read_bytes())
    (mods / "broken").mkdir(parents=True, exist_ok=True)
    cat = AssetCatalog(
        base_name="G", base_archives=[base], mod_order=["loose_mod", "bsa_mod", "broken"],
        loose={"broken": {TEX: TEX}, "loose_mod": {TEX: TEX}},
        bsas={"bsa_mod": [("b.bsa", [TEX])]},
        loose_winner={TEX: "broken"}, bsa_winner={}, mod_dir_for=lambda m: mods / m)
    fallback = cat.resolve_readable(TEX)
    assert fallback.mod == "loose_mod" and fallback.kind == "loose"
    assert cat.read(fallback) == b"loose-tex"
    cat.close()


def test_contested_keys_are_paths_more_than_one_layer_provides(world):
    # MESH: base + modA + modB; TEX: base + modA + modB. ONLY_BASE / NEW_IN_MOD: one layer.
    assert world.contested_keys() == {MESH, TEX}


def test_a_mods_own_loose_and_bsa_copies_are_not_an_override(tmp_path):
    cat = AssetCatalog(
        base_name="G", base_archives=[], mod_order=["m"],
        loose={"m": {MESH: MESH}}, bsas={"m": [("m.bsa", [MESH])]},
        loose_winner={MESH: "m"}, bsa_winner={MESH: "m"},
        mod_dir_for=lambda m: tmp_path / m)
    assert cat.contested_keys() == frozenset()


# -- format scan -------------------------------------------------------------------------
def _nif_blob(bsver):
    from test_nif_reader import _nif, _node
    return _nif([("NiNode", _node(0, []))], bsver=bsver)


@pytest.fixture
def mixed(tmp_path):
    """modA: loose SE + loose LE; modB: BSA with an SE and an LE mesh. Base has an LE
    mesh too, which the scan must ignore (the base game's own files are trusted)."""
    mods = tmp_path / "mods"
    _put(mods / "modA", {"meshes/se.nif": _nif_blob(100), "meshes/le.nif": _nif_blob(83),
                         "meshes/junk.nif": b"not a nif"})
    (mods / "modB").mkdir(parents=True)
    (mods / "modB" / "b.bsa").write_bytes(_bsa(tmp_path, "b.bsa", {
        "meshes/bse.nif": _nif_blob(100), "meshes/ble.nif": _nif_blob(130)}).read_bytes())
    base = _bsa(tmp_path, "Skyrim - Meshes0.bsa", {"meshes/base_le.nif": _nif_blob(83)})
    cat = AssetCatalog(
        base_name="G", base_archives=[base], mod_order=["modA", "modB"],
        loose={"modA": {k: k for k in ("meshes/se.nif", "meshes/le.nif", "meshes/junk.nif")}},
        bsas={"modB": [("b.bsa", ["meshes/bse.nif", "meshes/ble.nif"])]},
        loose_winner={}, bsa_winner={}, mod_dir_for=lambda m: mods / m,
        expected_nif_format=(0x14020007, 100))
    yield cat
    cat.close()


def _entry(cat, mod, path):
    return next(e for e in cat.mod_entries(mod) if e.path == path)


def test_scan_marks_meshes_in_other_formats(mixed):
    assert mixed.scan_formats() == 2
    assert mixed.incompatible_label(_entry(mixed, "modA", "meshes/le.nif")) == "Skyrim LE"
    assert mixed.incompatible_label(_entry(mixed, "modB", "meshes/ble.nif")) == "Fallout 4"
    assert mixed.incompatible_label(_entry(mixed, "modA", "meshes/se.nif")) is None
    assert mixed.incompatible_label(_entry(mixed, "modB", "meshes/bse.nif")) is None
    assert mixed.incompatible_label(_entry(mixed, "modA", "meshes/junk.nif")) is None  # not a NIF: unclassified
    base_le = next(e for e in mixed.base_entries() if e.path == "meshes/base_le.nif")
    assert mixed.incompatible_label(base_le) is None                                   # base is trusted


def test_scan_reports_progress_and_can_be_cancelled(mixed):
    seen = []
    mixed.scan_formats(progress=lambda i, n: seen.append((i, n)))
    assert seen[0][0] == 0 and seen[-1] == (5, 5)
    fresh = mixed
    fresh._bad.clear()
    assert fresh.scan_formats(cancel=lambda: True) == 0            # stops before the first file


def test_scan_without_an_expected_format_does_nothing(tmp_path):
    cat = AssetCatalog(base_name="G", base_archives=[], mod_order=[], loose={}, bsas={},
                       loose_winner={}, bsa_winner={}, mod_dir_for=lambda m: None)
    assert cat.scan_formats() == 0


def test_mark_incompatible_and_read_head(mixed):
    e = _entry(mixed, "modA", "meshes/se.nif")
    assert mixed.incompatible_label(e) is None
    mixed.mark_incompatible(e, "Skyrim LE")
    assert mixed.incompatible_label(e) == "Skyrim LE" and mixed.incompatible_count() == 1
    assert mixed.read_head(e, 8) == mixed.read(e)[:8]
    assert mixed.read_head(_entry(mixed, "modB", "meshes/bse.nif"), 8) == \
        mixed.read(_entry(mixed, "modB", "meshes/bse.nif"))[:8]


def test_siblings_are_the_other_files_in_the_same_folder_and_layer(tmp_path):
    folder = "meshes/armor/blades/"
    base = _bsa(tmp_path, "Skyrim - Meshes0.bsa", {
        folder + "bladesarmor.nif": b"a", folder + "bladesarmor_1.nif": b"b",
        folder + "bladeshelmet.nif": b"c", "meshes/other/x.nif": b"d"})
    mods = tmp_path / "mods"
    _put(mods / "modA", {folder + "bladesarmor_1.nif": b"e", "meshes/other/y.nif": b"f"})
    cat = AssetCatalog(
        base_name="G", base_archives=[base], mod_order=["modA"],
        loose={"modA": {folder + "bladesarmor_1.nif": folder + "bladesarmor_1.nif",
                        "meshes/other/y.nif": "meshes/other/y.nif"}},
        bsas={}, loose_winner={}, bsa_winner={}, mod_dir_for=lambda m: mods / m)
    display = next(e for e in cat.base_entries() if e.path == folder + "bladesarmor.nif")
    assert [e.path for e in cat.siblings(display)] == [folder + "bladesarmor_1.nif",
                                                       folder + "bladeshelmet.nif"]   # same folder, same layer only
    mod_worn = next(e for e in cat.mod_entries("modA") if e.path.endswith("bladesarmor_1.nif"))
    assert cat.siblings(mod_worn) == []                             # modA has nothing else in that folder
    cat.close()


def test_entry_in_layer_finds_that_layers_own_copy_even_if_it_loses(world):
    # MESH: base BSA, modA loose (wins), modB BSA (loses to the loose file).
    b = world.entry_in_layer("modB", MESH)
    assert (b.mod, b.kind, b.archive, b.is_winner) == ("modB", "bsa", "b.bsa", False)
    a = world.entry_in_layer("modA", MESH)
    assert (a.kind, a.is_winner) == ("loose", True)
    base = world.entry_in_layer(BASE, MESH)
    assert (base.mod, base.archive, base.is_winner) == (BASE, "Skyrim - Meshes0.bsa", False)
    assert world.entry_in_layer("modA", NEW_IN_MOD) is None              # that layer doesn't ship it
    assert world.entry_in_layer(BASE, NEW_IN_MOD) is None
    assert world.entry_in_layer("modB", "MESHES\\ARMOR\\IRON\\CUIRASS.NIF").mod == "modB"   # normalised lookup
    assert world.read(b) == b"B-bsa-mesh"


def test_slots_of_prefers_the_authoritative_plugin_map_over_the_mesh_guess(world):
    entry = world.entry_in_layer("modA", MESH)
    assert world.slots_of(entry) is None                             # MESH's bytes aren't a real NIF
    world._authoritative_slots[MESH] = frozenset({32, 34, 38})
    assert world.slots_of(entry) == frozenset({32, 34, 38})           # ground truth wins, no read needed
    assert world.entry_in_layer("modB", NEW_IN_MOD) is not None
    assert world.slots_of(world.entry_in_layer("modB", NEW_IN_MOD)) is None   # untouched path: falls back as before


def test_slots_need_authority_excludes_unauthored_meshes_instead_of_guessing(tmp_path):
    # Fallout 4's own mesh-embedded slots aren't reliable (a real mesh's own
    # segments can claim an unrelated slot alongside meaningless placeholder
    # numbers) — with slots_need_authority set, a mesh with no ARMA coverage
    # must come back None (unknown), never a guess from the file itself.
    base = _bsa(tmp_path, "Fallout4 - Meshes.ba2", {MESH: b"some-bytes"})
    cat = AssetCatalog(
        base_name="Game", base_archives=[base], mod_order=[],
        loose={}, bsas={}, loose_winner={}, bsa_winner={}, mod_dir_for=lambda m: None,
        authoritative_slots={MESH: frozenset({33})}, slots_need_authority=True)
    authored = cat.entry_in_layer(BASE, MESH)
    assert cat.slots_of(authored) == frozenset({33})           # covered: still ground truth
    cat.close()

    cat2 = AssetCatalog(
        base_name="Game", base_archives=[base], mod_order=[],
        loose={}, bsas={}, loose_winner={}, bsa_winner={}, mod_dir_for=lambda m: None,
        authoritative_slots={}, slots_need_authority=True)
    unauthored = cat2.entry_in_layer(BASE, MESH)
    assert cat2.slots_of(unauthored) is None                    # not covered: excluded, no fallback guess
    cat2.close()
