"""detect_bain() must not treat an archive with loose executables at its root as
a BAIN complex package.

Real-world motivation: the Fallout 4 Script Extender 0.6.23 archive is
``f4se_0_06_23/{Data/, src/, f4se_loader.exe, f4se_1_10_163.dll,
f4se_steam_loader.dll, ...}``. ``Data/`` holds ``Scripts`` and ``src/`` holds
``f4se`` (source tree — but ``f4se`` is also a recognised data-dir name), so
both looked like BAIN sub-packages and the archive was classified "complex".
A BAIN install only ever installs the chosen sub-packages and silently drops
loose root files, so the collection install (and any manual one) staged just
the 58 Papyrus scripts — no loader, no DLL — and deploy logged
``f4se_loader.exe not found — skipping launcher swap``: F4SE never started.

A dry run over 1,159 real archives showed only this one has loose .exe/.dll
files at a BAIN archive's root, and that a broader "top-level Data/ means
simple" rule would have wrongly merged genuine alternates (Red Shift PA's
``DATA`` + ``Optional esl version``, Photo Mode's ``Data`` + ``Photos``).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from Utils.installers.bain_installer import bain_unwrap_single_folder, detect_bain


def _make(root: Path, *paths: str) -> Path:
    for p in paths:
        f = root / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    return root


# The real f4se_0_06_23 archive, reduced to the entries that matter.
_F4SE_TREE = (
    "f4se_0_06_23/Data/Scripts/Actor.pex",
    "f4se_0_06_23/Data/Scripts/F4SE.pex",
    "f4se_0_06_23/src/f4se/f4se/main.cpp",
    "f4se_0_06_23/src/common/common/common.vcxproj",
    "f4se_0_06_23/f4se_loader.exe",
    "f4se_0_06_23/f4se_1_10_163.dll",
    "f4se_0_06_23/f4se_steam_loader.dll",
    "f4se_0_06_23/f4se_readme.txt",
    "f4se_0_06_23/f4se_whatsnew.txt",
    "f4se_0_06_23/CustomControlMap.txt",
)


def _detect(tmp_path: Path):
    """What the installer does: unwrap a single wrapper folder, then detect."""
    return detect_bain(bain_unwrap_single_folder(str(tmp_path)))


def test_f4se_archive_with_its_loader_is_a_simple_package(tmp_path):
    _make(tmp_path, *_F4SE_TREE)
    assert _detect(tmp_path) is None


def test_the_same_layout_without_executables_is_unchanged(tmp_path):
    """Pins how narrow the rule is: docs alone at the root don't change anything."""
    _make(tmp_path, *(p for p in _F4SE_TREE if not p.endswith((".exe", ".dll"))))
    subpkgs = _detect(tmp_path)
    assert subpkgs is not None and [p.name for p in subpkgs] == ["Data", "src"]


@pytest.mark.parametrize("loose", ["LOADER.EXE", "Payload.DLL", "dxgi.dll", "tool.exe"])
def test_any_loose_exe_or_dll_at_the_root_means_simple(tmp_path, loose):
    _make(tmp_path, "00 Core/Meshes/a.nif", "01 Optional/Textures/b.dds", loose)
    assert detect_bain(str(tmp_path)) is None


def test_executables_inside_a_sub_package_do_not_count(tmp_path):
    _make(tmp_path, "00 Core/Meshes/a.nif", "00 Core/helper.exe",
          "01 Optional/Textures/b.dds")
    subpkgs = detect_bain(str(tmp_path))
    assert subpkgs is not None and len(subpkgs) == 2


def test_genuine_bain_with_docs_and_images_is_still_detected(tmp_path):
    _make(tmp_path, "00 Core/Meshes/a.nif", "01 Optional/Textures/b.dds",
          "Readme.txt", "header.jpg", "info.xml")
    subpkgs = detect_bain(str(tmp_path))
    assert subpkgs is not None
    assert [p.name for p in subpkgs] == ["00 Core", "01 Optional"]


@pytest.mark.parametrize("tree, names", [
    # Red Shift PA: the main package plus an alternate variant, with screenshots.
    (("DATA/Meshes/a.nif", "Optional esl version/Meshes/b.nif",
      "20200713115632_1.jpg"), ["DATA", "Optional esl version"]),
    # Photo Mode: main package plus optional photos.
    (("Data/Meshes/a.nif", "Photos/Textures/b.dds"), ["Data", "Photos"]),
])
def test_alternate_variants_beside_a_data_folder_stay_bain(tmp_path, tree, names):
    """Guards against the rejected "a top-level Data/ means simple" rule, which
    would have installed both alternates together."""
    _make(tmp_path, *tree)
    subpkgs = detect_bain(str(tmp_path))
    assert subpkgs is not None and sorted(p.name for p in subpkgs) == sorted(names)


def test_a_simple_package_with_only_a_root_dll_is_still_simple(tmp_path):
    _make(tmp_path, "dxgi.dll", "reshade-shaders/Shaders/a.fx")
    assert detect_bain(str(tmp_path)) is None
