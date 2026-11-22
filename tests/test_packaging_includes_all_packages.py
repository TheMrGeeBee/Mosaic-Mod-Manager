"""Every top-level Python package under src/ must be copied into the AppImage and the Flatpak.

Both builds copy an explicit list of folders, so a new package that is not added
to the list works from source and then fails only in the packaged app with
``ModuleNotFoundError`` (the Modhold package did exactly that in v1.15.0).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
NOT_SHIPPED = {"appimage"}      # build tooling, not part of the installed tree


def _packages() -> list[str]:
    return sorted(
        d.name for d in SRC.iterdir()
        if d.is_dir() and d.name not in NOT_SHIPPED
        and not d.name.startswith(("__", "."))      # caches, a local .venv
        and any(d.rglob("*.py")))


def _appimage_dirs() -> set[str]:
    text = (SRC / "appimage" / "PKGBUILD").read_text(encoding="utf-8")
    m = re.search(r"for _dir in ([^;]+); do", text)
    assert m, "the folder loop in src/appimage/PKGBUILD changed shape; update this test"
    return set(m.group(1).split())


def _flatpak_dirs() -> set[str]:
    text = (ROOT / "flatpak" / "io.github.TheMrGeeBee.MosaicModManager.yml").read_text(
        encoding="utf-8")
    return set(re.findall(r"\bsrc/([A-Za-z_][\w]*)(?=[\s/]|$)", text))


def test_the_package_scan_finds_the_known_packages():
    assert {"Utils", "Games", "gui_qt", "Modhold"} <= set(_packages())


@pytest.mark.parametrize("package", _packages())
def test_the_appimage_build_copies_the_package(package):
    assert package in _appimage_dirs(), f"src/{package} is missing from src/appimage/PKGBUILD"


@pytest.mark.parametrize("package", _packages())
def test_the_flatpak_build_copies_the_package(package):
    assert package in _flatpak_dirs(), (
        f"src/{package} is missing from the cp line in the Flatpak manifest")
