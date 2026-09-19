"""The Fallout 4 downgrade must be transactional: either all three root files
end up patched and backed up, or the game folder is left exactly as it was.

Real-world motivation: the "A StoryWealth" collection is built for Old-Gen
(1.10.163) but Steam serves the Anniversary Edition (1.11.240). The collection's
own FO4Down.exe can't run under Proton (terminal UI, .NET ICU crash, Steam
login), so Mosaic applies the community AE→OG xdelta patch itself. That edits
files Steam owns, so a failure halfway must never leave a half-downgraded game,
and the user must be able to revert.

xdelta3 isn't required to run these: a stub stands in for it and — like the
real thing — refuses a patch that doesn't match the exact source file.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from pe_builder import build_pe
from Utils.modding_tools import fo4_downgrade as fo4

ORIG = {
    "Fallout4.exe": build_pe((1, 11, 240, 0)) + b"ae-exe",
    "Fallout4Launcher.exe": build_pe((1, 3, 23, 0)) + b"ae-launcher",
    "steam_api64.dll": build_pe((7, 40, 51, 27)) + b"ae-steam",
}
NEW = {
    "Fallout4.exe": build_pe((1, 10, 163, 0)) + b"og-exe",
    "Fallout4Launcher.exe": build_pe((1, 3, 23, 1)) + b"og-launcher",
    "steam_api64.dll": build_pe((7, 40, 51, 27)) + b"og-steam",
}


def make_patch(source: bytes, output: bytes) -> bytes:
    """A fake patch bound to exactly one source, producing a fixed output."""
    return b"FAKE-XDELTA\n" + hashlib.sha256(source).hexdigest().encode() + b"\n" + output


class StubXdelta:
    """Stands in for ``xdelta3 -d -f -s SOURCE PATCH OUTPUT``."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        source, patch, out = (Path(p) for p in cmd[-3:])
        _magic, expected, payload = patch.read_bytes().split(b"\n", 2)
        if hashlib.sha256(source.read_bytes()).hexdigest().encode() != expected:
            return subprocess.CompletedProcess(
                cmd, 1, "", "xdelta3: target window checksum mismatch: XD3_INVALID_INPUT")
        out.write_bytes(payload)
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def game_root(tmp_path):
    root = tmp_path / "Fallout 4"
    root.mkdir()
    for name, data in ORIG.items():
        (root / name).write_bytes(data)
        (root / name).chmod(0o755)
    return root


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "config" / "Fallout 4"


@pytest.fixture
def patches(tmp_path):
    d = tmp_path / "patches" / "AE to LastGen"
    d.mkdir(parents=True)
    for name in ORIG:
        (d / f"{name}.xdelta").write_bytes(make_patch(ORIG[name], NEW[name]))
    return fo4.find_patches(tmp_path / "patches")


def _tree(root: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(root.iterdir())}


def _apply(game_root, patches, state_dir, stub=None):
    stub = stub or StubXdelta()
    fo4.apply_downgrade(game_root, patches, state_dir, xdelta3="xdelta3", run=stub)
    return stub


# ---- find_patches ------------------------------------------------------------

def test_find_patches_matches_each_target_and_keeps_launcher_separate(tmp_path):
    d = tmp_path / "nested" / "deeper"
    d.mkdir(parents=True)
    for name in ("Fallout4.exe.xdelta", "Fallout4Launcher.exe.xdelta",
                 "steam_api64.dll.xdelta", "readme.txt"):
        (d / name).write_bytes(b"x")
    found = fo4.find_patches(tmp_path)
    assert {t: p.name for t, p in found.items()} == {
        "Fallout4.exe": "Fallout4.exe.xdelta",
        "Fallout4Launcher.exe": "Fallout4Launcher.exe.xdelta",
        "steam_api64.dll": "steam_api64.dll.xdelta",
    }


def test_find_patches_tolerates_other_naming_styles(tmp_path):
    for name in ("FALLOUT4 (1.11.240 to 1.10.163).vcdiff",
                 "fallout4launcher_ae_to_lastgen.xdelta",
                 "Steam_API64.xdelta"):
        (tmp_path / name).write_bytes(b"x")
    found = fo4.find_patches(tmp_path)
    assert found["Fallout4.exe"].suffix == ".vcdiff"
    assert found["Fallout4Launcher.exe"].name.startswith("fallout4launcher")
    assert found["steam_api64.dll"].name == "Steam_API64.xdelta"


def test_find_patches_reports_what_is_missing_and_what_was_found(tmp_path):
    (tmp_path / "Fallout4.exe.xdelta").write_bytes(b"x")
    with pytest.raises(fo4.DowngradeError) as exc:
        fo4.find_patches(tmp_path)
    msg = str(exc.value)
    assert "Fallout4Launcher.exe" in msg and "steam_api64.dll" in msg
    assert "Fallout4.exe.xdelta" in msg


def test_find_patches_refuses_ambiguous_matches(tmp_path):
    for name in ("Fallout4.exe.xdelta", "Fallout4 alt.xdelta",
                 "Fallout4Launcher.exe.xdelta", "steam_api64.dll.xdelta"):
        (tmp_path / name).write_bytes(b"x")
    with pytest.raises(fo4.DowngradeError, match="more than one"):
        fo4.find_patches(tmp_path)


# ---- apply -------------------------------------------------------------------

def test_apply_patches_all_three_backs_up_and_records_state(game_root, patches, state_dir):
    stub = _apply(game_root, patches, state_dir)

    assert _tree(game_root) == NEW                                # only the 3 files, patched
    assert fo4.read_game_version(game_root) == fo4.TO_VERSION
    backup = fo4.backup_dir(state_dir)
    assert {p.name: p.read_bytes() for p in backup.iterdir()} == ORIG
    state = fo4.load_state(state_dir)
    assert state["downgraded"] is True
    assert state["from"] == "1.11.240.0" and state["to"] == "1.10.163.0"
    # xdelta3 is driven as: -d (decode) -f (overwrite) -s SOURCE PATCH OUTPUT
    assert all(c[:4] == ["xdelta3", "-d", "-f", "-s"] for c in stub.calls)
    assert len(stub.calls) == 3


def test_apply_preserves_file_modes(game_root, patches, state_dir):
    _apply(game_root, patches, state_dir)
    assert (game_root / "Fallout4.exe").stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("current, why", [
    ((1, 10, 163, 0), "already"),
    ((1, 10, 984, 0), "1.10.984"),
])
def test_apply_only_accepts_the_anniversary_build(game_root, patches, state_dir, current, why):
    (game_root / "Fallout4.exe").write_bytes(build_pe(current) + b"x")
    before = _tree(game_root)
    with pytest.raises(fo4.DowngradeError, match=why):
        _apply(game_root, patches, state_dir)
    assert _tree(game_root) == before


def test_apply_refuses_while_mods_are_deployed(game_root, patches, state_dir):
    (game_root / "Fallout4Launcher.bak").write_bytes(b"real launcher")
    before = _tree(game_root)
    with pytest.raises(fo4.DowngradeError, match="[Rr]estore"):
        _apply(game_root, patches, state_dir)
    assert _tree(game_root) == before


def test_apply_needs_xdelta3(game_root, patches, state_dir):
    with pytest.raises(fo4.DowngradeError, match="xdelta3"):
        fo4.apply_downgrade(game_root, patches, state_dir, xdelta3=None, run=StubXdelta())
    assert _tree(game_root) == ORIG


def test_apply_needs_all_three_game_files(game_root, patches, state_dir):
    (game_root / "steam_api64.dll").unlink()
    with pytest.raises(fo4.DowngradeError, match="steam_api64.dll"):
        _apply(game_root, patches, state_dir)


def test_wrong_source_for_a_later_file_leaves_the_game_untouched(game_root, tmp_path, state_dir):
    d = tmp_path / "p"
    d.mkdir()
    (d / "Fallout4.exe.xdelta").write_bytes(make_patch(ORIG["Fallout4.exe"], NEW["Fallout4.exe"]))
    (d / "Fallout4Launcher.exe.xdelta").write_bytes(
        make_patch(ORIG["Fallout4Launcher.exe"], NEW["Fallout4Launcher.exe"]))
    # patch built for a different steam_api64.dll than the one on disk
    (d / "steam_api64.dll.xdelta").write_bytes(make_patch(b"some other dll", NEW["steam_api64.dll"]))

    with pytest.raises(fo4.DowngradeError, match=r"steam_api64\.dll.*checksum"):
        _apply(game_root, fo4.find_patches(d), state_dir)

    assert _tree(game_root) == ORIG                    # nothing replaced, no temp files
    assert fo4.load_state(state_dir) is None
    assert not fo4.backup_dir(state_dir).exists()


def test_a_patch_that_does_not_yield_1_10_163_is_rejected(game_root, tmp_path, state_dir):
    d = tmp_path / "p"
    d.mkdir()
    bad = dict(NEW, **{"Fallout4.exe": build_pe((1, 11, 999, 0)) + b"?"})
    for name in ORIG:
        (d / f"{name}.xdelta").write_bytes(make_patch(ORIG[name], bad[name]))
    with pytest.raises(fo4.DowngradeError, match="1.11.999.0"):
        _apply(game_root, fo4.find_patches(d), state_dir)
    assert _tree(game_root) == ORIG


def test_failure_while_swapping_files_in_rolls_everything_back(
        game_root, patches, state_dir, monkeypatch):
    real_replace = os.replace
    hit = {"n": 0}

    def flaky(src, dst):
        if Path(dst).name == "steam_api64.dll" and hit["n"] == 0:
            hit["n"] += 1
            raise OSError("disk on fire")
        return real_replace(src, dst)

    monkeypatch.setattr(fo4.os, "replace", flaky)
    with pytest.raises(fo4.DowngradeError, match="rolled back"):
        _apply(game_root, patches, state_dir)

    assert hit["n"] == 1
    assert _tree(game_root) == ORIG                    # the two already-swapped files restored
    assert fo4.load_state(state_dir) is None


# ---- revert ------------------------------------------------------------------

def test_revert_restores_originals_and_clears_state(game_root, patches, state_dir):
    _apply(game_root, patches, state_dir)
    fo4.revert_downgrade(game_root, state_dir)
    assert _tree(game_root) == ORIG
    assert fo4.read_game_version(game_root) == fo4.FROM_VERSION
    assert fo4.load_state(state_dir) is None
    assert not fo4.backup_dir(state_dir).exists()
    assert (game_root / "Fallout4.exe").stat().st_mode & 0o777 == 0o755


def test_can_downgrade_again_after_a_revert(game_root, patches, state_dir):
    _apply(game_root, patches, state_dir)
    fo4.revert_downgrade(game_root, state_dir)
    _apply(game_root, patches, state_dir)
    assert _tree(game_root) == NEW


def test_revert_refuses_a_tampered_backup(game_root, patches, state_dir):
    _apply(game_root, patches, state_dir)
    (fo4.backup_dir(state_dir) / "Fallout4.exe").write_bytes(b"corrupt")
    with pytest.raises(fo4.DowngradeError, match="backup"):
        fo4.revert_downgrade(game_root, state_dir)
    assert _tree(game_root) == NEW                     # game left as downgraded


def test_revert_with_nothing_to_revert(game_root, state_dir):
    with pytest.raises(fo4.DowngradeError, match="[Nn]othing"):
        fo4.revert_downgrade(game_root, state_dir)


def test_revert_refuses_while_mods_are_deployed(game_root, patches, state_dir):
    _apply(game_root, patches, state_dir)
    (game_root / "Fallout4Launcher.bak").write_bytes(b"x")
    with pytest.raises(fo4.DowngradeError, match="[Rr]estore"):
        fo4.revert_downgrade(game_root, state_dir)


# ---- small helpers -----------------------------------------------------------

def test_find_xdelta3_uses_path(monkeypatch):
    monkeypatch.setattr(fo4.shutil, "which", lambda n: "/usr/bin/xdelta3" if n == "xdelta3" else None)
    assert fo4.find_xdelta3() == "/usr/bin/xdelta3"
    monkeypatch.setattr(fo4.shutil, "which", lambda n: None)
    assert fo4.find_xdelta3() is None


def test_launcher_swapped_detects_the_bak(game_root):
    assert fo4.launcher_swapped(game_root) is False
    (game_root / "Fallout4Launcher.bak").write_bytes(b"x")
    assert fo4.launcher_swapped(game_root) is True
