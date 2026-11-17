"""Utils.archive_tools: the shared 7z lookup and wrapper-folder stripping."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from Utils import archive_tools
from Utils.archive_tools import find_7z, strip_single_top_dir


def _which_only(monkeypatch, available: dict):
    """Make shutil.which see exactly the binaries in *available* (name -> path)."""
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: available.get(name))


# -- find_7z -----------------------------------------------------------------

def test_find_7z_prefers_appimage_build_then_standalone_then_p7zip(monkeypatch):
    everything = {"7zzs": "/opt/7zzs", "7zz": "/app/bin/7zz", "7z": "/usr/bin/7z", "7za": "/usr/bin/7za"}

    _which_only(monkeypatch, dict(everything))
    assert find_7z() == "/opt/7zzs"
    del everything["7zzs"]
    _which_only(monkeypatch, dict(everything))
    assert find_7z() == "/app/bin/7zz"
    del everything["7zz"]
    _which_only(monkeypatch, dict(everything))
    assert find_7z() == "/usr/bin/7z"
    del everything["7z"]
    _which_only(monkeypatch, dict(everything))
    assert find_7z() == "/usr/bin/7za"


def test_find_7z_returns_none_when_nothing_is_installed(monkeypatch):
    _which_only(monkeypatch, {})
    assert find_7z() is None


def test_find_7z_reads_path_on_every_call(monkeypatch):
    """Launchers extend PATH at startup; a cached answer would go stale."""
    _which_only(monkeypatch, {})
    assert find_7z() is None
    _which_only(monkeypatch, {"7z": "/usr/bin/7z"})
    assert find_7z() == "/usr/bin/7z"


def test_find_7z_does_not_look_for_other_names(monkeypatch):
    asked = []
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: asked.append(name))
    find_7z()
    assert asked == ["7zzs", "7zz", "7z", "7za"]
    assert archive_tools._SEVEN_ZIP_NAMES == tuple(asked)


# -- strip_single_top_dir ----------------------------------------------------

def test_single_wrapper_folder_is_stripped(tmp_path):
    (tmp_path / "f4se_0_07_07" / "Data").mkdir(parents=True)

    assert strip_single_top_dir(tmp_path) == tmp_path / "f4se_0_07_07"


def test_several_entries_are_left_alone(tmp_path):
    (tmp_path / "Data").mkdir()
    (tmp_path / "readme.txt").write_text("x")

    assert strip_single_top_dir(tmp_path) == tmp_path


def test_two_folders_are_left_alone(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    assert strip_single_top_dir(tmp_path) == tmp_path


def test_single_file_is_not_a_wrapper(tmp_path):
    (tmp_path / "only.txt").write_text("x")

    assert strip_single_top_dir(tmp_path) == tmp_path


def test_macos_metadata_folder_is_ignored(tmp_path):
    (tmp_path / "__MACOSX").mkdir()
    (tmp_path / "Mod").mkdir()

    assert strip_single_top_dir(tmp_path) == tmp_path / "Mod"


def test_only_macos_metadata_means_nothing_to_strip(tmp_path):
    (tmp_path / "__MACOSX").mkdir()

    assert strip_single_top_dir(tmp_path) == tmp_path


def test_empty_directory(tmp_path):
    assert strip_single_top_dir(tmp_path) == tmp_path


def test_only_the_outer_wrapper_is_stripped(tmp_path):
    (tmp_path / "outer" / "inner").mkdir(parents=True)

    assert strip_single_top_dir(tmp_path) == tmp_path / "outer"
    assert isinstance(strip_single_top_dir(tmp_path), Path)


@pytest.mark.parametrize("n_wrappers", [1, 2])
def test_result_is_a_directory(tmp_path, n_wrappers):
    p = tmp_path
    for i in range(n_wrappers):
        p = p / f"w{i}"
    p.mkdir(parents=True)

    assert strip_single_top_dir(tmp_path).is_dir()
