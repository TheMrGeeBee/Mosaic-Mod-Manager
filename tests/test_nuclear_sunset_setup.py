"""Nuclear Sunset (Fallout NV + TTW), 2026-11-25.

* Its "[NoDelete] Stock New Vegas" holds only what the list adds to the game
  (xNVSE, 4GB patchers, DXVK) -- no FalloutNV.exe, the user copies the game in
  -- so it wasn't recognised as the list's game folder and NVSE was never
  installed. DXVK's d3d9.dll must stay out under Proton.
* Mosaic's 4GB patch makes FalloutNV.exe load NVSE itself.
* TTW can't ship in a Wabbajack list; it leaves an empty mod in its load-order
  place for the TTW installer's output."""
from __future__ import annotations

from Games.Bethesda.fallout_nv import Fallout_NV
from Utils.modding_tools import ttw_tools as tt
from Utils.mods.modlist import read_modlist, write_modlist
from Utils.wabbajack import wabbajack_install as wi
from Utils.wabbajack.wabbajack_manifest import FromArchiveDirective, ModList
from Utils.wabbajack.wabbajack_stock_game import partial_stock_dirs

_STOCK = "[NoDelete] Stock New Vegas"


def _list(*tos):
    return ModList(directives=[FromArchiveDirective(to=t.replace("/", "\\"),
                                                    archive_hash_path=["h", "x"])
                               for t in tos])


def _ns():
    return _list(f"{_STOCK}/nvse_loader.exe", f"{_STOCK}/nvse_1_4.dll",
                 f"{_STOCK}/Data/NVSE/nvse_config.ini", f"{_STOCK}/d3d9.dll",
                 f"{_STOCK}/dxvk.conf", "mods/ModA/a.esp")


def test_a_game_folder_without_the_game_is_found_by_its_script_extender():
    assert partial_stock_dirs(_ns(), Fallout_NV()) == {_STOCK: "Stock New Vegas"}


def test_a_full_game_copy_is_not_a_partial_one():
    ml = _list(f"{_STOCK}/FalloutNV.exe", f"{_STOCK}/nvse_loader.exe",
               f"{_STOCK}/Data/FalloutNV.esm")
    assert partial_stock_dirs(ml, Fallout_NV()) == {}


def test_a_game_without_a_script_extender_has_none():
    class NoSE:
        pass
    assert partial_stock_dirs(_ns(), NoSE()) == {}


def test_its_files_become_a_root_mod_without_dxvk():
    plan = wi.classify_directives(_ns(), root_dirs=partial_stock_dirs(_ns(), Fallout_NV()))
    assert plan.extra_root_mods == ["Stock New Vegas"]
    assert sorted(d.to for d in plan.mod_directives if d.to.startswith("Stock")) == [
        "Stock New Vegas/Data/NVSE/nvse_config.ini", "Stock New Vegas/nvse_1_4.dll",
        "Stock New Vegas/nvse_loader.exe"]
    assert sorted(plan.unplaced) == [f"{_STOCK}/d3d9.dll", f"{_STOCK}/dxvk.conf"]
    assert wi.limit_to_profile_mods(plan, {"ModA"}) == []
    assert any(d.to.startswith("Stock New Vegas/") for d in plan.mod_directives)


def test_a_4gb_patched_new_vegas_keeps_its_launcher(tmp_path):
    root = tmp_path / "Fallout New Vegas"
    root.mkdir()
    (root / "FalloutNVLauncher.exe").write_bytes(b"launcher")
    (root / "nvse_loader.exe").write_bytes(b"nvse")
    (root / "FalloutNV.exe").write_bytes(b"patched")
    (root / "FalloutNV_backup.exe").write_bytes(b"original")
    g = Fallout_NV()
    g._game_path = root
    g._script_extender_swap = True
    logs = []
    g.swap_launcher(logs.append)
    assert (root / "FalloutNVLauncher.exe").read_bytes() == b"launcher"
    assert any("4GB patch" in m for m in logs)


def test_an_unpatched_new_vegas_still_gets_the_loader(tmp_path):
    root = tmp_path / "Fallout New Vegas"
    root.mkdir()
    (root / "FalloutNVLauncher.exe").write_bytes(b"launcher")
    (root / "nvse_loader.exe").write_bytes(b"nvse")
    g = Fallout_NV()
    g._game_path = root
    g._script_extender_swap = True
    g.swap_launcher(lambda m: None)
    assert (root / "FalloutNVLauncher.exe").read_bytes() == b"nvse"


_SLOT = "[NoDelete] [INF] [DB] - Tale of Two Wastelands (TTW)"


def _staging(tmp_path):
    staging = tmp_path / "mods"
    (staging / _SLOT).mkdir(parents=True)
    (staging / _SLOT / "meta.ini").write_text("[General]\n[Wabbajack]\nmodlist = Nuclear Sunset\n")
    patch = staging / "TTW Reputations"
    patch.mkdir()
    (patch / "TTW Reputations.esp").write_bytes(b"x")
    return staging


def test_ttw_goes_into_the_lists_own_empty_slot(tmp_path):
    assert tt.list_slot_name(_staging(tmp_path)) == _SLOT


def test_no_slot_without_a_list(tmp_path):
    staging = tmp_path / "mods"
    (staging / "TTW Reputations").mkdir(parents=True)
    (staging / "TTW Reputations" / "a.esp").write_bytes(b"x")
    (staging / tt.OUTPUT_NAME).mkdir()
    assert tt.list_slot_name(staging) is None


def test_a_filled_slot_is_still_found(tmp_path):
    staging = _staging(tmp_path)
    (staging / _SLOT / "TaleOfTwoWastelands.esm").write_bytes(b"x")
    assert tt.list_slot_name(staging) == _SLOT


def test_registering_keeps_the_slots_place_and_meta(tmp_path, monkeypatch):
    staging = _staging(tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    from Utils.mods.modlist import ModEntry
    write_modlist(profile / "modlist.txt", [
        ModEntry(name="TTW Reputations", enabled=True, locked=False),
        ModEntry(name=_SLOT, enabled=False, locked=False),
        ModEntry(name="ModA", enabled=True, locked=False)])
    indexed = []
    monkeypatch.setattr("Utils.mods.install_as_mod.index_installed_mod",
                        lambda game, name, log_fn: indexed.append(name))

    class G:
        _active_profile_dir = profile
    tt.register_output(G(), staging / _SLOT, log_fn=lambda m: None)
    entries = read_modlist(profile / "modlist.txt")
    assert [e.name for e in entries] == ["TTW Reputations", _SLOT, "ModA"]
    assert next(e for e in entries if e.name == _SLOT).enabled
    assert "Nuclear Sunset" in (staging / _SLOT / "meta.ini").read_text()
    assert indexed == [_SLOT]


def test_only_the_installed_folders_dxvk_is_reported():
    ml = _list(*(d.to.replace("\\", "/") for d in _ns().directives),
               "xDXVK/DXVK 2.6.1/d3d9.dll")
    plan = wi.classify_directives(ml, root_dirs=partial_stock_dirs(ml, Fallout_NV()))
    assert plan.proton_skipped == [f"{_STOCK}/d3d9.dll", f"{_STOCK}/dxvk.conf"]
    assert "xDXVK/DXVK 2.6.1/d3d9.dll" in plan.unplaced
