"""Collection preflight for The Witcher 3: warn (never block) when the
installed game is on the wrong branch for a collection.

Real-world motivation: CD Projekt Red ships Next-Gen (4.x) and Remastered
(5.x) as separate, user-selectable Steam/GOG downloads with no binary-diff
relationship between them -- unlike Skyrim's runtime-swap, Mosaic can't
switch branches for the user, so this can only ever warn and point them at
Steam's Betas tab / GOG Galaxy's version selector, never block the install.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from pe_builder import build_pe
from Utils.collections import collection_preflight as pf


@dataclass
class Game:
    steam_id: str = "292030"


def _exe(root, version, *, remastered=True):
    subdir = "bin/x64_dx12" if remastered else "bin/x64"
    path = root / subdir / "witcher3.exe"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_pe(version))
    return path


# ---- recognising the game --------------------------------------------------

def test_is_witcher3():
    assert pf.is_witcher3(Game("292030")) and not pf.is_witcher3(Game("377160"))


# ---- check_tw3_version ------------------------------------------------------

def test_matching_remastered_passes(tmp_path):
    _exe(tmp_path, (5, 0, 15, 61352))
    checks = pf.check_tw3_version(tmp_path, collection_versions=["5.0"])
    assert [c.key for c in checks] == ["tw3-version"]
    assert checks[0].ok


def test_matching_next_gen_passes(tmp_path):
    _exe(tmp_path, (4, 4, 0, 0), remastered=False)
    checks = pf.check_tw3_version(tmp_path, collection_versions=["4.04"])
    assert checks[0].ok


def test_remastered_install_against_next_gen_collection_warns_not_blocks(tmp_path):
    _exe(tmp_path, (5, 0, 15, 61352))
    checks = pf.check_tw3_version(tmp_path, collection_versions=["4.04"])
    (c,) = checks
    assert not c.ok
    assert not c.blocking
    assert c.fix is None
    assert "Remastered" in c.title and "Next-Gen" in c.title


def test_next_gen_install_against_remastered_collection_warns(tmp_path):
    _exe(tmp_path, (4, 4, 0, 0), remastered=False)
    checks = pf.check_tw3_version(tmp_path, collection_versions=["5.0.1"])
    (c,) = checks
    assert not c.ok and not c.blocking


def test_no_declared_version_passes_silently_informative(tmp_path):
    _exe(tmp_path, (5, 0, 15, 61352))
    checks = pf.check_tw3_version(tmp_path, collection_versions=[])
    assert checks[0].ok


def test_unreadable_installed_version_is_silent(tmp_path):
    # No exe at all under either known subfolder -> can't say anything useful.
    assert pf.check_tw3_version(tmp_path, collection_versions=["5.0"]) == []


def test_garbage_declared_version_does_not_crash_and_does_not_block(tmp_path):
    _exe(tmp_path, (5, 0, 15, 61352))
    checks = pf.check_tw3_version(tmp_path, collection_versions=["not-a-version"])
    assert checks[0].ok  # nothing usable was declared, so it can't disagree


# ---- run_preflight wiring ---------------------------------------------------

def _run(game, game_root, versions):
    return pf.run_preflight(
        game=game, mods=[], manifest={"info": {"gameVersions": versions}},
        game_root=game_root, state_dir=game_root / "state", transition=None,
        game_running=False)


def test_run_preflight_is_silent_for_other_games(tmp_path):
    _exe(tmp_path, (5, 0, 15, 61352))
    assert _run(Game("377160"), tmp_path, ["4.04"]) == []


def test_run_preflight_includes_the_tw3_version_check(tmp_path):
    _exe(tmp_path, (5, 0, 15, 61352))
    checks = _run(Game(), tmp_path, ["4.04"])
    assert [c.key for c in checks] == ["tw3-version"]
    assert not checks[0].ok and not checks[0].blocking
