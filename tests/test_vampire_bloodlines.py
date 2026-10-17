"""Vampire: The Masquerade - Bloodlines handler: -game folder deploy, vanilla
protection, Unofficial Patch layering and launch command."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from Utils.deploy.deploy import LinkMode

spec = importlib.util.spec_from_file_location(
    "vampire_bloodlines",
    Path(__file__).resolve().parent.parent / "src" / "Games" /
    "Vampire The Masquerade - Bloodlines" / "vampire_bloodlines.py",
)
_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_module)
VampireBloodlines = _module.VampireBloodlines


def _game(tmp_path: Path, profile: str = "default") -> VampireBloodlines:
    root = tmp_path / "Vampire The Masquerade - Bloodlines"
    (root / "Vampire").mkdir(parents=True)
    (root / "Vampire" / "pack000.vpk").write_text("vanilla")
    game = VampireBloodlines()
    game._game_path = root
    game._staging_path = tmp_path / "staging"
    prof = game.get_profile_root() / "profiles" / profile
    prof.mkdir(parents=True)
    (prof / "modlist.txt").write_text("")
    game.set_active_profile_dir(prof)
    return game


def test_identity():
    g = VampireBloodlines()
    assert g.steam_id == "2600"
    assert g.exe_name == "vampire.exe"          # what Steam installs (lowercase)
    assert "Vampire.exe" in g.exe_name_alts
    assert g.game_id == "vampire_bloodlines"


@pytest.mark.parametrize("profile,expected", [
    ("default", "Mosaic_default"),
    ("My Cool Profile", "Mosaic_My_Cool_Profile"),
    ("a/b\\c", "Mosaic_a_b_c"),
    ("", "Mosaic_default"),
])
def test_mod_dir_name_has_no_spaces(profile, expected):
    assert _module._mod_dir_name(profile) == expected


def test_data_path_is_sibling_of_vanilla(tmp_path):
    g = _game(tmp_path, "My Profile")
    data = g.get_mod_data_path()
    assert data == g._game_path / "Mosaic_My_Profile"
    assert data.parent == (g._game_path / "Vampire").parent


def test_launch_command_passes_game_folder_via_steam(tmp_path):
    g = _game(tmp_path, "p1")
    cmd = g.get_launch_command()
    assert cmd[cmd.index("-applaunch") + 1] == "2600"
    assert cmd[cmd.index("-game") + 1] == "Mosaic_p1"


def test_deploy_guard_rejects_vanilla_folder(tmp_path):
    g = _game(tmp_path)
    with pytest.raises(RuntimeError):
        g._check_target(g._game_path / "Vampire")
    with pytest.raises(RuntimeError):
        g._check_target(g._game_path / "Vampire" / "cfg")
    g._check_target(g._game_path / "Mosaic_default")  # fine


def test_deploy_layers_patch_under_mods_and_leaves_vanilla_untouched(tmp_path):
    g = _game(tmp_path)
    root = g._game_path
    # Unofficial Patch base layer
    (root / "Unofficial_Patch" / "python").mkdir(parents=True)
    (root / "Unofficial_Patch" / "python" / "fileutil.py").write_text("patch")
    (root / "Unofficial_Patch" / "python" / "other.py").write_text("patch-other")
    # A staged mod overriding one patch file
    mod_py = g.get_mod_staging_path() / "ModA" / "python"
    mod_py.mkdir(parents=True)
    (mod_py / "fileutil.py").write_text("mod")
    filemap = g.get_effective_filemap_path()
    filemap.parent.mkdir(parents=True, exist_ok=True)
    filemap.write_text("python/fileutil.py\tModA\n")

    before = sorted((root / "Vampire").rglob("*"))
    g.deploy(mode=LinkMode.HARDLINK, profile="default")

    target = root / "Mosaic_default" / "python"
    assert (target / "fileutil.py").read_text() == "mod"
    assert (target / "other.py").read_text() == "patch-other"
    assert sorted((root / "Vampire").rglob("*")) == before
    assert (root / "Vampire" / "pack000.vpk").read_text() == "vanilla"
    # Mosaic never compiles scripts
    assert not list(root.rglob("*.pyc"))


def test_wizard_tools_lists_the_patch_wizard_and_resolves():
    """wizard_tools is a property the Wizard menu iterates; a plain method here
    made the menu fall back to "No wizard tools for this game"."""
    from Utils.plugins.plugin_loader import get_all_wizard_tools
    from wizards_qt import get_spec

    tools = get_all_wizard_tools(VampireBloodlines())
    by_id = {t.id: t for t in tools}
    patch = by_id["install_unofficial_patch"]
    assert patch.dialog_class_path == "wizards.vtmb_patch.VtmbPatchWizard"
    assert get_spec(patch.dialog_class_path) is not None


def test_loader_is_offered_as_a_run_option_without_game_arg(tmp_path):
    g = _game(tmp_path)
    assert g.framework_launch_exes == {"VTMB Loader": "Loader.exe"}
    # Loader.exe is a mod picker; Play's own launch command is the -game one.
    assert "-game" in g.get_launch_command()


def test_loader_appears_in_run_dropdown_when_present(tmp_path, monkeypatch):
    from Utils.exe_launch import exe_launch as el

    g = _game(tmp_path)
    (g._game_path / "vampire.exe").write_text("steam-stub")
    monkeypatch.setattr(el, "game_is_steam_install", lambda game: True)
    assert el.detect_framework_exes(g) == []          # not installed yet
    (g._game_path / "Loader.exe").write_text("loader")
    assert [p.name for p in el.detect_framework_exes(g)] == ["Loader.exe"]


def _pe(laa: bool) -> bytes:
    head = bytearray(256)
    head[0:2] = b"MZ"
    head[0x3C:0x40] = (0x80).to_bytes(4, "little")
    head[0x80:0x84] = b"PE\0\0"
    head[0x84 + 18:0x84 + 20] = (0x0102 | (0x20 if laa else 0)).to_bytes(2, "little")
    return bytes(head)


def _deploy_with_exe(tmp_path, laa):
    g = _game(tmp_path)
    (g._game_path / "vampire.exe").write_bytes(_pe(laa))
    filemap = g.get_effective_filemap_path()
    filemap.parent.mkdir(parents=True, exist_ok=True)
    filemap.write_text("")
    (g._game_path / "Loader.exe").write_text("x")      # keep the Loader warning out
    g.deploy(mode=LinkMode.HARDLINK, profile="default")
    return g.pop_deploy_warnings()


def test_deploy_warns_while_steams_stock_exe_is_unpatched(tmp_path):
    warnings = _deploy_with_exe(tmp_path, laa=False)
    assert any("4 GB" in w for w in warnings)


def test_deploy_is_quiet_once_the_exe_is_patched(tmp_path):
    assert not any("4 GB" in w for w in _deploy_with_exe(tmp_path, laa=True))


def _patch_layer_with_config(g, text="UP default config\n"):
    cfg = g._game_path / "Unofficial_Patch" / "cfg"
    cfg.mkdir(parents=True)
    (cfg / "config.cfg").write_text(text)
    (cfg / "autoexec.cfg").write_text("autoexec\n")
    return cfg / "config.cfg"


def _empty_filemap(g, extra=""):
    fm = g.get_effective_filemap_path()
    fm.parent.mkdir(parents=True, exist_ok=True)
    fm.write_text(extra)


@pytest.mark.parametrize("mode", [LinkMode.SYMLINK, LinkMode.HARDLINK])
def test_config_cfg_is_a_private_copy_not_a_link_into_the_patch(tmp_path, mode):
    g = _game(tmp_path)
    up_cfg = _patch_layer_with_config(g)
    _empty_filemap(g)
    g.deploy(mode=mode, profile="default")

    mine = g._game_path / "Mosaic_default" / "cfg" / "config.cfg"
    assert mine.is_file() and not mine.is_symlink()
    assert not os.path.samefile(mine, up_cfg)
    assert mine.read_text() == "UP default config\n"
    # the game saving its settings must not reach the shared patch layer
    mine.write_text("my keybinds\n")
    assert up_cfg.read_text() == "UP default config\n"
    # everything else from the patch is still just linked
    auto = g._game_path / "Mosaic_default" / "cfg" / "autoexec.cfg"
    assert auto.exists() and os.path.samefile(auto, g._game_path / "Unofficial_Patch" / "cfg" / "autoexec.cfg")


def test_settings_survive_restore_and_the_next_deploy_even_without_a_filemap_rebuild(tmp_path):
    """The real Play flow: Restore rescues the config to Overwrite, then Deploy runs
    with the OLD filemap (no Overwrite entry for it). Settings must still come back."""
    g = _game(tmp_path)
    up_cfg = _patch_layer_with_config(g)
    _empty_filemap(g)
    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    mine = g._game_path / "Mosaic_default" / "cfg" / "config.cfg"
    assert mine.read_text() == "UP default config\n"     # first run: the patch default
    mine.write_text("auto renew on\n")                    # the game rewrites its config

    g.restore()
    saved = g.get_effective_overwrite_path() / "cfg" / "config.cfg"
    assert saved.read_text() == "auto renew on\n"        # rescued, not lost

    g.deploy(mode=LinkMode.SYMLINK, profile="default")   # filemap NOT rebuilt
    assert mine.is_file() and not mine.is_symlink()
    assert mine.read_text() == "auto renew on\n"         # back from the saved copy
    assert up_cfg.read_text() == "UP default config\n"   # base layer never touched

    # and it keeps working for the cycle after that
    mine.write_text("auto renew on, more\n")
    g.restore()
    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    assert mine.read_text() == "auto renew on, more\n"
    assert up_cfg.read_text() == "UP default config\n"


def test_settings_survive_when_the_filemap_does_list_the_overwrite_file(tmp_path):
    g = _game(tmp_path)
    up_cfg = _patch_layer_with_config(g)
    _empty_filemap(g)
    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    mine = g._game_path / "Mosaic_default" / "cfg" / "config.cfg"
    mine.write_text("my keybinds\n")
    g.restore()

    _empty_filemap(g, "cfg/config.cfg\t[Overwrite]\n")   # a rebuilt filemap
    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    assert mine.read_text() == "my keybinds\n"
    assert up_cfg.read_text() == "UP default config\n"


def _write_saves(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "autosave.sav").write_text("save one\n")
    (folder / "sp_tutorial_1.HL1").write_text("level state\n")


def test_saves_survive_restore_and_the_next_deploy_without_a_filemap_rebuild(tmp_path):
    """Restore moves saves to Overwrite; the next Play deploys with the OLD filemap,
    so they must be brought back anyway — and a new autosave must not clobber them."""
    g = _game(tmp_path)
    save_up = g._game_path / "Unofficial_Patch" / "save"
    save_up.mkdir(parents=True)
    (save_up / "Vampire-999.sav").write_text("")        # the patch's empty placeholder
    _patch_layer_with_config(g)
    _empty_filemap(g)
    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    mine = g._game_path / "Mosaic_default" / "save"
    _write_saves(mine)                                  # the game saves

    g.restore()
    ow = g.get_effective_overwrite_path() / "save"
    assert (ow / "autosave.sav").read_text() == "save one\n"      # rescued
    assert not (g._game_path / "Mosaic_default" / "save" / "autosave.sav").exists()

    g.deploy(mode=LinkMode.SYMLINK, profile="default")             # filemap NOT rebuilt
    assert (mine / "autosave.sav").read_text() == "save one\n"    # back
    assert (mine / "sp_tutorial_1.HL1").read_text() == "level state\n"
    assert (mine / "Vampire-999.sav").exists()                    # placeholder still there

    # the game writes a newer autosave through the link; nothing is lost on the next cycle
    (mine / "autosave.sav").write_text("save two\n")
    g.restore()
    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    assert (mine / "autosave.sav").read_text() == "save two\n"
    assert (save_up / "Vampire-999.sav").read_text() == ""        # patch layer untouched


def test_relinking_never_replaces_a_file_the_deploy_already_placed(tmp_path):
    g = _game(tmp_path)
    ow = g.get_effective_overwrite_path() / "save"
    ow.mkdir(parents=True)
    (ow / "autosave.sav").write_text("from overwrite\n")
    mod_dir = g._game_path / "Mosaic_default"
    (mod_dir / "save").mkdir(parents=True)
    (mod_dir / "save" / "autosave.sav").write_text("already placed\n")
    assert g._relink_saved_state(mod_dir, g.get_effective_overwrite_path(), lambda _m: None) == 0
    assert (mod_dir / "save" / "autosave.sav").read_text() == "already placed\n"


def _ui_file(folder: Path, version="12.0"):
    f = folder / "resource" / "gameui_english.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(('"GameUI_Patch" "Unofficial Patch %s"\r\n' % version).encode("utf-16"))


def test_unofficial_patch_counts_as_a_present_framework(tmp_path):
    """So 'requires Unofficial Patch' on a Nexus mod doesn't warn when the wizard
    (or a prepatched GOG install) already provides it."""
    from Utils.wine_proton.framework_detect import STATE_INSTALLED, STATE_MISSING, detect_frameworks

    g = _game(tmp_path)
    fm = tmp_path / "filemap.txt"
    fm.write_text("")
    (st,) = detect_frameworks(g, fm, tmp_path / "modlist.txt")
    assert st.label == "Unofficial Patch" and st.state == STATE_MISSING

    _ui_file(g._game_path / "Unofficial_Patch")
    (st,) = detect_frameworks(g, fm, tmp_path / "modlist.txt")
    assert st.state == STATE_INSTALLED
    # the deploy pipeline matches a requirement by this label inside its name
    requirement = "Vampire The Masquerade - Bloodlines Unofficial Patch"
    assert any(fw.lower() in requirement.lower() for fw in g.frameworks)


def test_framework_follows_a_patch_that_lives_in_the_vanilla_folder(tmp_path):
    from Utils.wine_proton.framework_detect import STATE_INSTALLED, detect_frameworks

    g = _game(tmp_path)
    _ui_file(g._game_path / "Vampire", "11.8")           # prepatched, GOG-style
    assert g.frameworks == {"Unofficial Patch": "Vampire/resource/gameui_english.txt"}
    fm = tmp_path / "filemap.txt"
    fm.write_text("")
    (st,) = detect_frameworks(g, fm, tmp_path / "modlist.txt")
    assert st.state == STATE_INSTALLED


def _terminal(g, name="haven_pc.txt", user="suckhead"):
    d = g._game_path / "Unofficial_Patch" / "vdata" / "hackterminals"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(f'"email_username"\t"{user}"\n')
    return d / name


def test_hacking_terminals_are_private_copies_and_the_players_name_stays_private(tmp_path):
    """The engine rewrites email_username at load; it must not reach the patch layer."""
    g = _game(tmp_path)
    up_term = _terminal(g)
    _patch_layer_with_config(g)
    _empty_filemap(g)
    g.deploy(mode=LinkMode.SYMLINK, profile="default")

    mine = g._game_path / "Mosaic_default" / "vdata" / "hackterminals" / "haven_pc.txt"
    assert mine.is_file() and not mine.is_symlink()
    mine.write_text('"email_username"\t"Fiona"\n')          # the game personalises it
    assert up_term.read_text() == '"email_username"\t"suckhead"\n'

    g.restore()
    assert (g.get_effective_overwrite_path() / "vdata" / "hackterminals" / "haven_pc.txt"
            ).read_text() == '"email_username"\t"Fiona"\n'
    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    assert mine.read_text() == '"email_username"\t"Fiona"\n'      # kept for this profile
    assert up_term.read_text() == '"email_username"\t"suckhead"\n'


def test_unchanged_private_copies_are_not_saved_to_overwrite(tmp_path):
    """Identical copies are dropped on Restore, so a later patch update isn't shadowed."""
    g = _game(tmp_path)
    _terminal(g, "haven_pc.txt")
    _terminal(g, "other_pc.txt")
    _patch_layer_with_config(g)
    _empty_filemap(g)
    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    mine = g._game_path / "Mosaic_default" / "vdata" / "hackterminals"
    (mine / "haven_pc.txt").write_text('"email_username"\t"Fiona"\n')   # only this changes

    g.restore()
    ow = g.get_effective_overwrite_path()
    assert (ow / "vdata" / "hackterminals" / "haven_pc.txt").is_file()
    assert not (ow / "vdata" / "hackterminals" / "other_pc.txt").exists()
    assert not (ow / "cfg" / "config.cfg").exists()             # untouched default config too


def test_a_patch_update_reaches_profiles_that_never_changed_those_files(tmp_path):
    g = _game(tmp_path)
    up_term = _terminal(g)
    _patch_layer_with_config(g)
    _empty_filemap(g)
    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    g.restore()                                                   # nothing was changed
    up_term.write_text('"email_username"\t"new in patch 12.1"\n')
    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    mine = g._game_path / "Mosaic_default" / "vdata" / "hackterminals" / "haven_pc.txt"
    assert mine.read_text() == '"email_username"\t"new in patch 12.1"\n'


def test_settings_persist_when_the_patch_ships_no_config_cfg(tmp_path):
    """A pristine patch has no cfg/config.cfg: the game creates it, Restore saves it,
    and the next deploy must bring it back (no filemap rebuild in between)."""
    g = _game(tmp_path)
    (g._game_path / "Unofficial_Patch" / "cfg").mkdir(parents=True)
    (g._game_path / "Unofficial_Patch" / "cfg" / "default.cfg").write_text("d\n")
    _empty_filemap(g)
    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    mine = g._game_path / "Mosaic_default" / "cfg" / "config.cfg"
    assert not mine.exists()                         # nothing to start from
    mine.write_text("auto renew on\n")                # the game creates its config

    g.restore()
    assert (g.get_effective_overwrite_path() / "cfg" / "config.cfg").read_text() == "auto renew on\n"
    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    assert mine.is_file() and not mine.is_symlink()
    assert mine.read_text() == "auto renew on\n"
    assert not (g._game_path / "Unofficial_Patch" / "cfg" / "config.cfg").exists()


def test_settings_persist_even_without_any_patch_layer(tmp_path):
    g = _game(tmp_path)                              # no Unofficial_Patch/ at all
    _empty_filemap(g)
    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    mine = g._game_path / "Mosaic_default" / "cfg" / "config.cfg"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text("kept\n")
    g.restore()
    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    assert mine.read_text() == "kept\n"


_TOPBAR = "materials/interface/charactermaintenance/cm_topbar.tth"


def _stage_mod_file(g, mod, rel, data: bytes):
    f = g.get_mod_staging_path() / mod / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return f


def _patch_file(g, rel, data: bytes):
    f = g._game_path / "Unofficial_Patch" / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return f


def test_a_mods_file_the_patch_script_rewrites_stays_pristine_in_staging(tmp_path):
    """The HD overhaul replaces cm_topbar; the patch's 'Vampire hunter switch' then
    copies its own variant over it at load. That write must not reach the mod."""
    g = _game(tmp_path)
    up = _patch_file(g, _TOPBAR, b"patch variant")
    hd = _stage_mod_file(g, "HD Mod", _TOPBAR, b"hd version")
    _patch_layer_with_config(g)
    _empty_filemap(g, f"{_TOPBAR}\tHD Mod\n")
    g.deploy(mode=LinkMode.SYMLINK, profile="default")

    mine = g._game_path / "Mosaic_default" / _TOPBAR
    assert mine.is_file() and not mine.is_symlink()
    assert mine.read_bytes() == b"hd version"             # the HD mod still wins visually
    mine.write_bytes(b"patch variant")                    # vamputil.copyfile() at load

    assert hd.read_bytes() == b"hd version"               # mod staging untouched
    assert up.read_bytes() == b"patch variant"            # patch layer untouched


def test_hardlink_deploys_are_isolated_too(tmp_path):
    g = _game(tmp_path)
    hd = _stage_mod_file(g, "HD Mod", _TOPBAR, b"hd version")
    _patch_layer_with_config(g)
    _empty_filemap(g, f"{_TOPBAR}\tHD Mod\n")
    g.deploy(mode=LinkMode.HARDLINK, profile="default")
    mine = g._game_path / "Mosaic_default" / _TOPBAR
    assert not os.path.samefile(mine, hd)                 # not the same inode any more
    mine.write_bytes(b"changed by the game")
    assert hd.read_bytes() == b"hd version"


def test_unchanged_private_copies_are_dropped_and_a_changed_one_is_saved(tmp_path):
    g = _game(tmp_path)
    _patch_file(g, "vdata/system/stats.txt", b"patch stats")
    _stage_mod_file(g, "HD Mod", _TOPBAR, b"hd version")
    _patch_layer_with_config(g)
    _empty_filemap(g, f"{_TOPBAR}\tHD Mod\n")
    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    root = g._game_path / "Mosaic_default"
    (root / "vdata/system/stats.txt").write_bytes(b"hunter stats")   # the script changes this one only

    g.restore()
    ow = g.get_effective_overwrite_path()
    assert (ow / "vdata/system/stats.txt").read_bytes() == b"hunter stats"   # saved
    assert not (ow / _TOPBAR).exists()                                       # unchanged: dropped, not saved
    assert not (ow / "cfg/config.cfg").exists()

    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    assert (root / "vdata/system/stats.txt").read_bytes() == b"hunter stats"  # character state kept
    assert (root / _TOPBAR).read_bytes() == b"hd version"


def test_a_mod_update_reaches_profiles_that_never_changed_the_file(tmp_path):
    g = _game(tmp_path)
    mod = _stage_mod_file(g, "HD Mod", _TOPBAR, b"hd v1")
    _patch_layer_with_config(g)
    _empty_filemap(g, f"{_TOPBAR}\tHD Mod\n")
    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    g.restore()                                            # nothing was changed
    mod.write_bytes(b"hd v2")                              # the mod is updated
    g.deploy(mode=LinkMode.SYMLINK, profile="default")
    assert (g._game_path / "Mosaic_default" / _TOPBAR).read_bytes() == b"hd v2"


def test_the_list_covers_every_file_the_patch_script_writes():
    from importlib import import_module  # noqa: F401  (module already loaded above)
    for rel in ("scripts/kb_act.lst", "vdata/system/stats.txt", "vdata/system/strings.txt",
                "vdata/system/credits.txt", "vdata/signs/death.txt",
                "sound/interface/infobar/need_more_blood.wav", _TOPBAR,
                "materials/hud/new_ui/bloodbar/bloodbar.vmt",
                "materials/hud/disciplines/bloodheal_base.ttz", "cfg/config.cfg"):
        assert rel in _module._MUTABLE_PATCH_FILES


_COLLECTION_UP = "VTMBup115rc9.1.exe"     # the real off-site entry in "Enhanced Textures and SFX"


def test_collection_off_site_patch_not_needed_when_same_or_newer_is_installed(tmp_path):
    g = _game(tmp_path)
    assert not g.offsite_mod_satisfied(_COLLECTION_UP)        # nothing installed yet
    _ui_file(g._game_path / "Unofficial_Patch", "12.0")
    assert g.offsite_mod_satisfied(_COLLECTION_UP)            # 12.0 >= 11.5
    assert g.offsite_mod_satisfied("VTMBup120.6.exe")         # equal
    assert not g.offsite_mod_satisfied("VTMBup121.exe")       # a newer one IS needed
    assert not g.offsite_mod_satisfied("Some Other Tool.exe") # never swallows unrelated mods


def test_base_games_never_skip_off_site_mods(tmp_path):
    from Games.base_game import BaseGame
    assert BaseGame.offsite_mod_satisfied(object(), _COLLECTION_UP) is False


def test_collection_panel_drops_the_satisfied_off_site_patch_only(tmp_path):
    from gui_qt.collections.collection_detail_view import _split_offsite_with_modio

    manifest = {"mods": [
        {"name": _COLLECTION_UP, "source": {
            "type": "browse", "url": "https://www.moddb.com/mods/vtmb-unofficial-patch"}},
        {"name": "Some Manual Tool", "source": {
            "type": "browse", "url": "https://example.com/tool"}},
    ]}
    g = _game(tmp_path)
    manual, auto = _split_offsite_with_modio(manifest, "vampirebloodlines", g)
    assert [n for n, _u in manual] == [_COLLECTION_UP, "Some Manual Tool"]   # nothing installed

    _ui_file(g._game_path / "Unofficial_Patch", "12.0")
    manual, auto = _split_offsite_with_modio(manifest, "vampirebloodlines", g)
    assert [n for n, _u in manual] == ["Some Manual Tool"]                  # the patch is gone
    # and without a game, behaviour is unchanged
    manual, _auto = _split_offsite_with_modio(manifest, "vampirebloodlines")
    assert len(manual) == 2


def test_reshade_wizard_hints_match_the_32_bit_directx9_engine():
    g = VampireBloodlines()
    assert g.reshade_dll == "d3d9.dll"      # shaderapidx9.dll => DirectX 9
    assert g.reshade_arch == 32             # vampire.exe is machine 0x14c (x86)


def test_vtm_loader_plugins_and_loose_res_files_are_routed_and_restored(tmp_path):
    """Container Stack Fix ships Bin/loader/*.vtm (read from the game root, not the
    -game folder); Plain Dialogue ships a loose trackerscheme.res (belongs in resource/)."""
    g = _game(tmp_path)
    root = g._game_path
    _patch_file(g, "resource/trackerscheme.res", b"patch")
    _patch_file(g, "resource/other.res", b"patch-other")
    _stage_mod_file(g, "StackFix", "Bin/loader/container-stack-fix.vtm", b"vtm")
    _stage_mod_file(g, "Dialogue", "trackerscheme.res", b"mod")
    _stage_mod_file(g, "Dialogue", "resource/nested.res", b"nested")
    _empty_filemap(g, "Bin/loader/container-stack-fix.vtm\tStackFix\n"
                      "trackerscheme.res\tDialogue\n"
                      "resource/nested.res\tDialogue\n")

    g.deploy(mode=LinkMode.HARDLINK, profile="default")

    assert (root / "Bin" / "loader" / "container-stack-fix.vtm").read_bytes() == b"vtm"
    res = root / "Mosaic_default" / "resource"
    assert (res / "trackerscheme.res").read_bytes() == b"mod"     # mod wins over the patch
    assert (res / "other.res").read_bytes() == b"patch-other"
    assert (res / "nested.res").read_bytes() == b"nested"          # already in resource/
    assert not (root / "Mosaic_default" / "trackerscheme.res").exists()
    assert not (root / "Mosaic_default" / "Bin").exists()

    g.restore()
    assert not (root / "Bin" / "loader" / "container-stack-fix.vtm").exists()


def test_vtm_mods_are_flagged_as_root_installs_but_res_mods_are_not():
    from Utils.deploy.deploy_custom_rules import mods_matching_root_rules

    flagged = mods_matching_root_rules(
        {"StackFix": ["Bin/loader/container-stack-fix.vtm", "README.txt"],
         "Dialogue": ["trackerscheme.res"],
         "Textures": ["materials/a.vmt"]},
        VampireBloodlines().custom_routing_rules)
    assert flagged == {"StackFix"}


def test_option_folder_packs_get_the_sub_package_picker():
    assert VampireBloodlines().supports_bain
