"""Morrowind (OpenMW): the game folder is the Morrowind install, not OpenMW's own.

Bug report: on a native Arch install the only folder the configure view could
validate was the one holding ``openmw-launcher`` (/usr/bin), so mods had no
"Data Files" to go into and every deploy failed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from Games.Morrowind import openmw as openmw_mod  # noqa: E402
from Games.Morrowind.openmw import OpenMW  # noqa: E402
from Utils.deploy.deploy import LinkMode  # noqa: E402


def _morrowind(tmp_path: Path, data_name: str = "Data Files") -> Path:
    root = tmp_path / "Morrowind"
    data = root / data_name
    data.mkdir(parents=True)
    (data / "Morrowind.esm").write_text("vanilla")
    (root / "Morrowind.exe").write_text("exe")
    return root


def _program_folder(tmp_path: Path) -> Path:
    """What /usr/bin is: the OpenMW launcher and nothing else."""
    d = tmp_path / "usr_bin"
    d.mkdir()
    (d / "openmw-launcher").write_text("#!/bin/sh")
    return d


def _game(tmp_path: Path, game_folder: Path) -> OpenMW:
    g = OpenMW()
    g._game_path = game_folder
    g._staging_path = tmp_path / "staging"
    g._openmw_cfg_path = tmp_path / "cfg" / "openmw.cfg"
    prof = g.get_profile_root() / "profiles" / "default"
    prof.mkdir(parents=True)
    (prof / "modlist.txt").write_text("")
    g.set_active_profile_dir(prof)
    return g


@pytest.fixture
def cfg_candidates(tmp_path, monkeypatch):
    """Point openmw.cfg detection at a fake home so the real one is never read."""
    cfg = tmp_path / "home_cfg" / "openmw.cfg"
    monkeypatch.setattr(openmw_mod, "_OPENMW_CFG_CANDIDATES", [cfg])
    return cfg


# -- what the configure view validates against ------------------------------------------------------
def test_the_exe_checked_is_morrowinds_not_the_launcher():
    g = OpenMW()
    names = [g.exe_name, *g.exe_name_alts]
    assert "openmw-launcher" not in names
    assert names == ["Morrowind Launcher.exe", "Morrowind.exe"]    # the key existing launch settings use
    assert g.game_folder_is_install_dir is True


def test_the_launch_command_does_not_depend_on_the_game_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(openmw_mod, "_OPENMW_CFG_CANDIDATES", [tmp_path / "none.cfg"])
    monkeypatch.setattr(openmw_mod.shutil, "which",
                        lambda n: "/usr/bin/openmw-launcher" if n == "openmw-launcher" else None)
    assert OpenMW().get_launch_command() == ["/usr/bin/openmw-launcher"]


# -- the reported failure ---------------------------------------------------------------------------
def test_the_program_folder_is_rejected_with_the_fix_spelled_out(tmp_path):
    g = _game(tmp_path, _program_folder(tmp_path))
    errors = g.validate_install()
    assert len(errors) == 1                                # one clear message, not two vague ones
    assert "Morrowind installation" in errors[0] and "Data Files" in errors[0]
    assert "OpenMW program folder" in errors[0]


def test_deploy_into_the_program_folder_raises_the_same_message(tmp_path):
    g = _game(tmp_path, _program_folder(tmp_path))
    with pytest.raises(RuntimeError, match="Morrowind installation"):
        g.deploy(mode=LinkMode.HARDLINK, profile="default")
    assert not (tmp_path / "cfg" / "openmw.cfg").exists()   # no bogus data= line was written


def test_a_real_install_validates(tmp_path):
    assert _game(tmp_path, _morrowind(tmp_path)).validate_install() == []


@pytest.mark.parametrize("name", ["Data Files", "data files", "DATA FILES"])
def test_data_files_is_matched_ignoring_case(tmp_path, name):
    root = _morrowind(tmp_path, name)
    g = _game(tmp_path, root)
    assert g.get_mod_data_path() == root / name
    assert g.validate_install() == []
    assert g.runtime_snapshot_exclude_dirs() == {name}


# -- finding the install from openmw.cfg -------------------------------------------------------------
def test_the_install_is_read_from_openmw_cfg(tmp_path, cfg_candidates):
    root = _morrowind(tmp_path)
    cfg_candidates.parent.mkdir(parents=True)
    cfg_candidates.write_text(f'encoding=win1252\ndata="{root / "Data Files"}"\ncontent=Morrowind.esm\n')
    assert OpenMW().detect_game_path() == root


def test_the_install_is_found_with_a_lowercase_data_dir_and_esm(tmp_path, cfg_candidates):
    root = tmp_path / "MW"
    (root / "data files").mkdir(parents=True)
    (root / "data files" / "morrowind.esm").write_text("x")
    cfg_candidates.parent.mkdir(parents=True)
    cfg_candidates.write_text(f"data={root / 'data files'}\n")
    assert OpenMW().detect_game_path() == root


@pytest.mark.parametrize("body", [
    "",                                            # nothing in it
    "encoding=win1252\n",                          # no data= line
    "data=\"/does/not/exist\"\n",                  # a folder that is gone
])
def test_no_install_is_guessed_without_a_usable_data_line(tmp_path, cfg_candidates, body):
    cfg_candidates.parent.mkdir(parents=True)
    cfg_candidates.write_text(body)
    assert OpenMW().detect_game_path() is None


def test_a_data_dir_without_morrowind_esm_is_not_the_install(tmp_path, cfg_candidates):
    other = tmp_path / "SomeMod" / "Data Files"
    other.mkdir(parents=True)
    cfg_candidates.parent.mkdir(parents=True)
    cfg_candidates.write_text(f'data="{other}"\n')
    assert OpenMW().detect_game_path() is None


def test_no_openmw_cfg_means_nothing_to_detect(cfg_candidates):
    assert not cfg_candidates.exists()
    assert OpenMW().detect_game_path() is None


# -- mods actually get installed ---------------------------------------------------------------------
def test_deploy_puts_mods_in_data_files_and_writes_openmw_cfg_then_restore_undoes_it(tmp_path):
    root = _morrowind(tmp_path)
    g = _game(tmp_path, root)
    mods = g.get_effective_mod_staging_path()
    (mods / "ModA" / "textures").mkdir(parents=True)
    (mods / "ModA" / "foo.esp").write_text("plugin")
    (mods / "ModA" / "textures" / "t.dds").write_text("tex")
    (mods / "ModB").mkdir()
    (mods / "ModB" / "bar.bsa").write_text("archive")
    filemap = g.get_effective_filemap_path()
    filemap.parent.mkdir(parents=True, exist_ok=True)
    filemap.write_text("foo.esp\tModA\ntextures/t.dds\tModA\nbar.bsa\tModB\n")
    (g._active_profile_dir / "plugins.txt").write_text("*foo.esp\n")

    g.deploy(mode=LinkMode.HARDLINK, profile="default")

    data = root / "Data Files"
    assert (data / "foo.esp").read_text() == "plugin"
    assert (data / "textures" / "t.dds").read_text() == "tex"
    assert (data / "Morrowind.esm").read_text() == "vanilla"       # vanilla files are still there
    cfg = (tmp_path / "cfg" / "openmw.cfg").read_text().splitlines()
    assert f'data="{data}"' in cfg
    content = [ln for ln in cfg if ln.startswith("content=")]
    assert content[0] == "content=Morrowind.esm" and content[-1] == "content=foo.esp"
    assert "fallback-archive=bar.bsa" in cfg

    g.restore()

    assert (data / "Morrowind.esm").read_text() == "vanilla"
    assert not (data / "foo.esp").exists() and not (data / "textures").exists()
    after = (tmp_path / "cfg" / "openmw.cfg").read_text().splitlines()
    assert "content=foo.esp" not in after and "fallback-archive=bar.bsa" not in after
    assert f'data="{data}"' in after
