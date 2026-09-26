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
