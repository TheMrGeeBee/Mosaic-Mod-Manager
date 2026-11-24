"""Making, finding and deleting a profile's own game copy ("Stock Game")."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from Utils import game_copy as gc
from Utils.profile.profile_state import merge_profile_settings


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def steam(tmp_path):
    root = tmp_path / "lib" / "steamapps" / "common" / "Skyrim Special Edition"
    (root / "Data").mkdir(parents=True)
    (root / "SkyrimSE.exe").write_bytes(b"exe 1.7.104")
    (root / "Data" / "Skyrim.esm").write_bytes(b"esm")
    (root / "Data" / "ccNew.esl").write_bytes(b"only in 1.7.104")
    (root / "Data" / "Skyrim - Textures0.bsa").write_bytes(b"x" * 4096)
    return root


@pytest.fixture
def store(tmp_path):
    """A build store holding 1.6.1170: its own exe, and no ccNew.esl."""
    state = tmp_path / "state"
    slot = state / "version_store" / "1.6.1170"
    slot.mkdir(parents=True)
    (slot / "SkyrimSE.exe").write_bytes(b"exe 1.6.1170")
    index = {"version": 1, "base": "Steam build 1", "current": "Steam build 1",
             "managed": ["SkyrimSE.exe", "Data/ccNew.esl"],
             "slots": {"1.6.1170": {"dir": str(slot), "at": "2026-11-22T19:15:34+00:00",
                                    "files": {"SkyrimSE.exe": _sha(slot / "SkyrimSE.exe"),
                                              "Data/ccNew.esl": None}}}}
    (state / "version_store.json").write_text(json.dumps(index), encoding="utf-8")
    return state


@pytest.fixture
def fake_reflink(monkeypatch):
    """tmpfs has no reflinks: stand in for the FICLONE ioctl with a byte copy."""
    def ioctl(dst_fd, req, src_fd):
        assert req == gc._FICLONE
        os.lseek(src_fd, 0, 0)
        while chunk := os.read(src_fd, 1 << 16):
            os.write(dst_fd, chunk)
    monkeypatch.setattr(gc.fcntl, "ioctl", ioctl)


def _make(steam, dest, store, build, **kw):
    return gc.create_copy(game_name="Skyrim Special Edition", steam_root=steam, dest=dest,
                          build=build, current_build="Steam build 1", state_dir=store,
                          exe_file="SkyrimSE.exe", **kw)


def test_copy_at_the_installed_build(steam, store, tmp_path, fake_reflink):
    dest = tmp_path / "Modlists" / "GTS" / "Stock Game"
    info = _make(steam, dest, store, "Steam build 1")
    assert (dest / "SkyrimSE.exe").read_bytes() == b"exe 1.7.104"
    assert (dest / "Data" / "ccNew.esl").is_file()
    assert info.method == "reflink" and info.build == "Steam build 1"
    assert gc.read_marker(dest).source == str(steam)
    assert not dest.with_name("Stock Game" + gc._PARTIAL_SUFFIX).exists()


def test_copy_at_a_stored_build(steam, store, tmp_path, fake_reflink):
    dest = tmp_path / "Modlists" / "GTS" / "Stock Game"
    _make(steam, dest, store, "1.6.1170")
    assert (dest / "SkyrimSE.exe").read_bytes() == b"exe 1.6.1170"
    assert not (dest / "Data" / "ccNew.esl").exists()        # that build has none
    assert (dest / "Data" / "Skyrim.esm").read_bytes() == b"esm"   # unmanaged: kept
    # The Steam install is never written.
    assert (steam / "SkyrimSE.exe").read_bytes() == b"exe 1.7.104"
    assert (steam / "Data" / "ccNew.esl").is_file()


def test_a_damaged_stored_file_leaves_nothing_behind(steam, store, tmp_path, fake_reflink):
    (store / "version_store" / "1.6.1170" / "SkyrimSE.exe").write_bytes(b"tampered")
    dest = tmp_path / "Modlists" / "GTS" / "Stock Game"
    with pytest.raises(gc.GameCopyError, match="damaged"):
        _make(steam, dest, store, "1.6.1170")
    assert not dest.exists() and not list(dest.parent.glob("*" + gc._PARTIAL_SUFFIX))


def test_unknown_build_is_refused(steam, store, tmp_path, fake_reflink):
    with pytest.raises(gc.GameCopyError, match="doesn't keep"):
        _make(steam, tmp_path / "M" / "Stock Game", store, "1.5.97")


def test_no_reflink_asks_before_a_real_copy(steam, store, tmp_path):
    dest = tmp_path / "Modlists" / "GTS" / "Stock Game"
    with pytest.raises(gc.ReflinkUnavailable) as err:
        _make(steam, dest, store, "Steam build 1")
    assert err.value.size == gc.tree_bytes(steam) and not dest.exists()
    info = _make(steam, dest, store, "Steam build 1", allow_full_copy=True)
    assert info.method == "copy" and (dest / "Data" / "Skyrim.esm").read_bytes() == b"esm"


def test_refuses_while_the_steam_install_is_deployed(steam, store, tmp_path, fake_reflink):
    (steam / "Data" / ".mm_deployed").write_text("")
    with pytest.raises(gc.GameCopyError, match="Restore the game first"):
        _make(steam, tmp_path / "M" / "Stock Game", store, "Steam build 1")
    (steam / "Data" / ".mm_deployed").unlink()
    (steam / "Data" / "Mod.esp").symlink_to(tmp_path / "staging" / "Mod.esp")
    with pytest.raises(gc.GameCopyError, match="linked"):
        _make(steam, tmp_path / "M" / "Stock Game", store, "Steam build 1")


@pytest.mark.parametrize("where", ["library", "inside", "nonempty"])
def test_bad_destinations(steam, store, tmp_path, fake_reflink, where):
    dest = {"library": steam.parent / "Copy",
            "inside": steam / "Copy",
            "nonempty": tmp_path / "M" / "Stock Game"}[where]
    if where == "nonempty":
        dest.mkdir(parents=True)
        (dest / "something").write_text("x")
    with pytest.raises(gc.GameCopyError):
        _make(steam, dest, store, "Steam build 1")


def test_profiles_using_and_delete(steam, store, tmp_path, fake_reflink):
    dest = tmp_path / "Modlists" / "GTS" / "Stock Game"
    _make(steam, dest, store, "1.6.1170")
    profiles = tmp_path / "profiles"
    for name in ("GTS", "GTS_alt", "Other"):
        (profiles / name).mkdir(parents=True)
    merge_profile_settings(profiles / "GTS", {"game_path": str(dest)})
    merge_profile_settings(profiles / "GTS_alt", {"game_path": str(dest) + "/"})
    assert gc.profiles_using(profiles, dest) == ["GTS", "GTS_alt"]
    with pytest.raises(gc.GameCopyError, match="Still used by: GTS, GTS_alt"):
        gc.delete_copy(dest, profiles)
    for name in ("GTS", "GTS_alt"):
        merge_profile_settings(profiles / name, {"game_path": None})
    (dest / "Data" / ".mm_deployed").write_text("")
    with pytest.raises(gc.GameCopyError, match="Restore first"):
        gc.delete_copy(dest, profiles)
    (dest / "Data" / ".mm_deployed").unlink()
    gc.delete_copy(dest, profiles)
    assert not dest.exists()


def test_never_deletes_a_folder_mosaic_did_not_make(steam, tmp_path):
    with pytest.raises(gc.GameCopyError, match="isn't a game copy"):
        gc.delete_copy(steam, tmp_path / "profiles")
    assert steam.is_dir()


def test_default_destination_is_next_to_the_modlist(tmp_path):
    assert gc.default_destination(tmp_path, "Gate_To_Sovngarde") == (
        tmp_path / "Gate_To_Sovngarde" / "Stock Game")
    assert gc.default_destination(tmp_path, 'a/b:c').name == "Stock Game"
