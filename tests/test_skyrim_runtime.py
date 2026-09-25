"""The Skyrim runtime swap must be transactional: either every file ends up
patched and backed up, or the game folder is left exactly as it was.

Real-world motivation: "Gate To Sovngarde" declares Skyrim 1.7.104 (what Steam
serves) but needs SKSE64, which only supports 1.6.1170. The SRS mod that ships the
1.7.104 -> 1.6.1170 patches can't be hosted by Mosaic's deploy (version.dll hijack,
Data_Core snapshot), so Mosaic applies the same hash-verified HDiffPatch patches
itself. That edits files Steam owns, so a failure halfway must never leave a
half-patched game, and the user must be able to revert.

hpatchz isn't required to run these: a stub stands in for it and — like the real
thing — refuses a patch that doesn't match the exact source file. (The real tool
was checked against the real SRS patches by hand: all 9 files, forward and reverse,
hashes matching the manifest.)
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from pe_builder import build_pe
from Utils.modding_tools import skyrim_runtime as sr

SRC = (1, 7, 104, 0)
DST = (1, 6, 1170, 0)

ORIG = {
    "SkyrimSE.exe": build_pe(SRC) + b"ae-exe",
    "SkyrimSELauncher.exe": build_pe((1, 0, 0, 0)) + b"ae-launcher",
    "Data/Skyrim.esm": b"TES4-ae-skyrim",
    "Data/Update.esm": b"TES4-ae-update",
}
NEW = {
    "SkyrimSE.exe": build_pe(DST) + b"se-exe",
    "SkyrimSELauncher.exe": build_pe((1, 0, 0, 1)) + b"se-launcher",
    "Data/Skyrim.esm": b"TES4-se-skyrim",
    "Data/Update.esm": b"TES4-se-update",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_patch(source: bytes, output: bytes) -> bytes:
    """A fake patch bound to exactly one source, producing a fixed output."""
    return b"FAKE-HDIFF\n" + _sha(source).encode() + b"\n" + output


class StubHpatchz:
    """Stands in for ``hpatchz -f OLD PATCH OUT``."""

    def __init__(self, fail_on: str | None = None):
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        old, patch, out = (Path(p) for p in cmd[-3:])
        if self.fail_on and old.name == self.fail_on:
            return subprocess.CompletedProcess(cmd, 1, "", "patch run error")
        _magic, expected, payload = patch.read_bytes().split(b"\n", 2)
        if _sha(old.read_bytes()).encode() != expected:
            return subprocess.CompletedProcess(cmd, 1, "", "oldData checksum mismatch")
        out.write_bytes(payload)
        return subprocess.CompletedProcess(cmd, 0, "", "")


def write_swap(swap: Path, *, mutate=None) -> Path:
    """A RuntimeSwap folder shaped like SRS's: manifest.json + patches/forward/."""
    (swap / "patches" / "forward").mkdir(parents=True)
    entries = []
    for i, rel in enumerate(ORIG):
        name = f"{i:04d}_{rel.replace('/', '_')}_forward.hdiff"
        patch = make_patch(ORIG[rel], NEW[rel])
        (swap / "patches" / "forward" / name).write_bytes(patch)
        entries.append({
            "path": rel,
            "forwardPatch": f"forward/{name}",
            "forwardPatchSha256": _sha(patch),
            "sourcePresent": True, "targetPresent": True,
            "sourceSha256": _sha(ORIG[rel]), "targetSha256": _sha(NEW[rel]),
            "sourceSize": len(ORIG[rel]), "targetSize": len(NEW[rel]),
        })
    manifest = {
        "format": 3, "algorithm": "hdiffpatch-hdiffw26-zstd", "appId": 489830,
        "sourceVersion": "1.7.104", "targetVersion": "1.6.1170", "files": entries,
    }
    if mutate:
        mutate(manifest)
    (swap / "manifest.json").write_text(json.dumps(manifest))
    return swap


@pytest.fixture
def game_root(tmp_path):
    root = tmp_path / "Skyrim Special Edition"
    for rel, data in ORIG.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.chmod(0o755)
    return root


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "config" / "Skyrim Special Edition"


@pytest.fixture
def transition(tmp_path):
    return sr.load_transition(write_swap(tmp_path / "mod" / "RuntimeSwap"))


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _apply(game_root, transition, state_dir, stub=None):
    stub = stub or StubHpatchz()
    sr.apply_transition(game_root, transition, state_dir, hpatchz="hpatchz", run=stub)
    return stub


# ---- manifest ------------------------------------------------------------------

def test_load_transition_reads_versions_and_files(transition):
    assert transition.source == SRC
    assert transition.target == DST
    assert transition.app_id == 489830
    assert {f.path for f in transition.files} == set(ORIG)


def test_parse_version_pads_and_rejects_junk():
    assert sr.parse_version("1.6.1170") == (1, 6, 1170, 0)
    with pytest.raises(sr.RuntimeSwapError):
        sr.parse_version("one.two")


def test_find_swap_dir_locates_manifest_under_a_wrapper(tmp_path):
    write_swap(tmp_path / "wrapper" / "RuntimeSwap")
    assert sr.find_swap_dir(tmp_path) == tmp_path / "wrapper" / "RuntimeSwap"
    assert sr.find_swap_dir(tmp_path / "wrapper" / "RuntimeSwap") == \
        tmp_path / "wrapper" / "RuntimeSwap"
    assert sr.find_swap_dir(tmp_path / "nothing") is None


@pytest.mark.parametrize("mutate,fragment", [
    (lambda m: m.update(algorithm="bsdiff"), "can't apply"),
    (lambda m: m.update(files=[]), "lists no files"),
    (lambda m: m["files"][2].update(sourcePresent=False), "adds or removes a file"),
    (lambda m: m["files"][2].update(path="../../etc/passwd"), "unsafe path"),
    (lambda m: m["files"][2].update(path="/etc/passwd"), "unsafe path"),
    (lambda m: m["files"][2].update(forwardPatchSha256="0" * 64), "corrupt"),
    (lambda m: m["files"][2].update(forwardPatch="forward/nope.hdiff"), "missing from the archive"),
    (lambda m: m["files"][0].pop("sourceSha256"), "malformed"),
    (lambda m: m.update(files=[e for e in m["files"] if e["path"] != "SkyrimSE.exe"]),
     "can't be verified"),
])
def test_load_transition_refuses_unsafe_or_damaged_manifests(tmp_path, mutate, fragment):
    swap = write_swap(tmp_path / "RuntimeSwap", mutate=mutate)
    with pytest.raises(sr.RuntimeSwapError, match=fragment):
        sr.load_transition(swap)


# ---- assess --------------------------------------------------------------------

def _assess(game_root, state_dir, transition, **kw):
    kw.setdefault("hpatchz", "hpatchz")
    kw.setdefault("game_running", False)
    return sr.assess(game_root, state_dir, transition, **kw)


def test_assess_source_is_applicable(game_root, state_dir, transition):
    a = _assess(game_root, state_dir, transition)
    assert (a.state, a.version) == ("source", SRC)
    assert a.can_apply and not a.blockers and not a.can_revert


def test_assess_unknown_version_is_unsupported(game_root, state_dir, transition):
    (game_root / "SkyrimSE.exe").write_bytes(build_pe((1, 6, 640, 0)))
    a = _assess(game_root, state_dir, transition)
    assert a.state == "unsupported" and not a.can_apply


@pytest.mark.parametrize("make_marker", [
    lambda root: (root / "Data" / ".mm_deployed").write_bytes(b""),
    lambda root: (root / "Data_Core").mkdir(),
    lambda root: (root / "SkyrimSELauncher.bak").write_bytes(b"x"),
])
def test_assess_blocks_while_deployed(game_root, state_dir, transition, make_marker):
    make_marker(game_root)
    a = _assess(game_root, state_dir, transition)
    assert not a.can_apply
    assert any("restore" in b.lower() for b in a.blockers)


def test_assess_blocks_when_running_or_tool_missing_or_file_missing(game_root, state_dir, transition):
    (game_root / "Data" / "Update.esm").unlink()
    a = _assess(game_root, state_dir, transition, hpatchz=None, game_running=True)
    text = " ".join(a.blockers)
    assert "running" in text and "hpatchz" in text and "Update.esm" in text


def test_assess_after_apply_reports_swapped_and_revertible(game_root, state_dir, transition):
    _apply(game_root, transition, state_dir)
    a = _assess(game_root, state_dir, transition)
    assert (a.state, a.swapped_by_mosaic) == ("target", True)
    assert a.can_revert and not a.can_apply


def test_assess_record_goes_stale_when_steam_restores_the_newer_exe(game_root, state_dir, transition):
    _apply(game_root, transition, state_dir)
    (game_root / "SkyrimSE.exe").write_bytes(ORIG["SkyrimSE.exe"])   # Steam re-downloaded it
    a = _assess(game_root, state_dir, transition)
    assert a.state == "source" and not a.swapped_by_mosaic and a.can_apply


# ---- apply ---------------------------------------------------------------------

def test_apply_patches_every_file_and_records_state(game_root, state_dir, transition):
    stub = _apply(game_root, transition, state_dir)
    assert _tree(game_root) == {rel: NEW[rel] for rel in ORIG}
    assert sr.read_runtime_version(game_root) == DST
    assert len(stub.calls) == len(ORIG)
    assert all(call[:2] == ["hpatchz", "-f"] for call in stub.calls)
    state = sr.load_state(state_dir)
    assert state["applied"] and state["from"] == "1.7.104.0" and state["to"] == "1.6.1170.0"
    assert set(state["files"]) == set(ORIG)
    for rel in ORIG:
        assert state["files"][rel] == {"original_sha256": _sha(ORIG[rel]),
                                       "patched_sha256": _sha(NEW[rel])}
        assert (sr.backup_dir(state_dir) / rel).read_bytes() == ORIG[rel]
    # No temp debris left in the game folder.
    assert not [p for p in game_root.rglob(".*mosaic-*")]


def test_apply_keeps_file_modes(game_root, state_dir, transition):
    _apply(game_root, transition, state_dir)
    assert (game_root / "SkyrimSE.exe").stat().st_mode & 0o777 == 0o755


def test_apply_without_hpatchz_says_how_to_install_it(game_root, state_dir, transition):
    with pytest.raises(sr.RuntimeSwapError, match="hpatchz was not found"):
        sr.apply_transition(game_root, transition, state_dir, hpatchz=None)


@pytest.mark.parametrize("make_marker", [
    lambda root: (root / "Data" / ".mm_deployed").write_bytes(b""),
    lambda root: (root / "Data_Core").mkdir(),
    lambda root: (root / "SkyrimSELauncher.bak").write_bytes(b"x"),
])
def test_apply_refuses_while_deployed_and_touches_nothing(game_root, state_dir, transition, make_marker):
    make_marker(game_root)
    before = _tree(game_root)
    stub = StubHpatchz()
    with pytest.raises(sr.RuntimeSwapError, match="Restore the game first"):
        sr.apply_transition(game_root, transition, state_dir, hpatchz="hpatchz", run=stub)
    assert _tree(game_root) == before and not stub.calls
    assert not sr.backup_dir(state_dir).exists()


def test_apply_refuses_the_wrong_version(game_root, state_dir, transition):
    (game_root / "SkyrimSE.exe").write_bytes(build_pe((1, 6, 640, 0)))
    before = _tree(game_root)
    with pytest.raises(sr.RuntimeSwapError, match="only applies to 1.7.104.0"):
        _apply(game_root, transition, state_dir)
    assert _tree(game_root) == before


def test_apply_refuses_when_already_at_target(game_root, state_dir, transition):
    _apply(game_root, transition, state_dir)
    with pytest.raises(sr.RuntimeSwapError, match="already 1.6.1170.0"):
        _apply(game_root, transition, state_dir)


def test_apply_refuses_a_modified_source_file_before_touching_anything(game_root, state_dir, transition):
    (game_root / "Data" / "Skyrim.esm").write_bytes(b"TES4-modified-by-a-tool")
    before = _tree(game_root)
    stub = StubHpatchz()
    with pytest.raises(sr.RuntimeSwapError, match=r"not the original.*Skyrim\.esm"):
        sr.apply_transition(game_root, transition, state_dir, hpatchz="hpatchz", run=stub)
    assert _tree(game_root) == before and not stub.calls
    assert not sr.backup_dir(state_dir).exists()


def test_apply_refuses_when_disk_is_too_small(game_root, state_dir, transition, monkeypatch):
    monkeypatch.setattr(sr, "_free_bytes", lambda _p: 10)
    before = _tree(game_root)
    with pytest.raises(sr.RuntimeSwapError, match="free disk space"):
        _apply(game_root, transition, state_dir)
    assert _tree(game_root) == before


def test_a_failing_patch_midway_leaves_the_game_untouched(game_root, state_dir, transition):
    before = _tree(game_root)
    with pytest.raises(sr.RuntimeSwapError, match=r"hpatchz could not patch"):
        _apply(game_root, transition, state_dir, StubHpatchz(fail_on="Update.esm"))
    assert _tree(game_root) == before
    assert not sr.backup_dir(state_dir).exists()
    assert sr.load_state(state_dir) is None


def test_a_patch_that_yields_the_wrong_hash_is_rejected(game_root, state_dir, tmp_path):
    def wrong_target(m):
        m["files"][3]["targetSha256"] = _sha(b"something else")
    tr = sr.load_transition(write_swap(tmp_path / "bad" / "RuntimeSwap", mutate=wrong_target))
    before = _tree(game_root)
    with pytest.raises(sr.RuntimeSwapError, match="expected checksum"):
        _apply(game_root, tr, state_dir)
    assert _tree(game_root) == before and not sr.backup_dir(state_dir).exists()


def test_a_swap_failure_rolls_back_the_files_already_replaced(game_root, state_dir, transition, monkeypatch):
    before = _tree(game_root)
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        # Only the forward swap (temp -> game file) counts; rollbacks/state writes pass.
        if ".mosaic-runtime" in str(src):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("disk went away")
        return real_replace(src, dst)

    monkeypatch.setattr(sr.os, "replace", flaky)
    with pytest.raises(sr.RuntimeSwapError, match="rolled back"):
        _apply(game_root, transition, state_dir)
    assert _tree(game_root) == before
    assert not sr.backup_dir(state_dir).exists()


def test_when_rollback_itself_fails_the_backup_is_kept(game_root, state_dir, transition, monkeypatch):
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        if ".mosaic-runtime" in str(src):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("disk went away")
        if ".mosaic-restore" in str(src):
            raise OSError("still gone")
        return real_replace(src, dst)

    monkeypatch.setattr(sr.os, "replace", flaky)
    with pytest.raises(sr.RuntimeSwapError, match="Could not roll back"):
        _apply(game_root, transition, state_dir)
    # The originals must still be recoverable from the backup.
    for rel in ORIG:
        assert (sr.backup_dir(state_dir) / rel).read_bytes() == ORIG[rel]


# ---- revert --------------------------------------------------------------------

def test_revert_restores_the_originals_exactly(game_root, state_dir, transition):
    _apply(game_root, transition, state_dir)
    sr.revert_transition(game_root, state_dir)
    assert _tree(game_root) == {rel: ORIG[rel] for rel in ORIG}
    assert sr.read_runtime_version(game_root) == SRC
    assert sr.load_state(state_dir) is None
    assert not sr.backup_dir(state_dir).exists()
    assert not [p for p in game_root.rglob(".*mosaic-*")]


def test_revert_without_a_record_says_so(game_root, state_dir):
    with pytest.raises(sr.RuntimeSwapError, match="Nothing to revert"):
        sr.revert_transition(game_root, state_dir)


def test_revert_refuses_a_tampered_backup(game_root, state_dir, transition):
    _apply(game_root, transition, state_dir)
    (sr.backup_dir(state_dir) / "Data" / "Skyrim.esm").write_bytes(b"tampered")
    with pytest.raises(sr.RuntimeSwapError, match="Verify integrity"):
        sr.revert_transition(game_root, state_dir)
    assert _tree(game_root) == {rel: NEW[rel] for rel in ORIG}       # nothing was restored


def test_revert_refuses_a_missing_backup(game_root, state_dir, transition):
    _apply(game_root, transition, state_dir)
    (sr.backup_dir(state_dir) / "SkyrimSE.exe").unlink()
    with pytest.raises(sr.RuntimeSwapError, match="missing or has been modified"):
        sr.revert_transition(game_root, state_dir)


def test_revert_refuses_while_deployed(game_root, state_dir, transition):
    _apply(game_root, transition, state_dir)
    (game_root / "Data" / ".mm_deployed").write_bytes(b"")
    with pytest.raises(sr.RuntimeSwapError, match="Restore the game first"):
        sr.revert_transition(game_root, state_dir)


# ---- hpatchz discovery / managed download --------------------------------------

def _fake_hpatchz_zip(member: str = "linux64/hpatchz") -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member, b"#!/bin/sh\necho fake hpatchz\n")
    return buf.getvalue()


@pytest.fixture
def no_system_hpatchz(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(sr.shutil, "which", lambda _name: None)
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")


def test_ensure_hpatchz_prefers_the_one_on_path(monkeypatch):
    monkeypatch.setattr(sr.shutil, "which", lambda _name: "/usr/bin/hpatchz")

    def boom(_url):
        raise AssertionError("must not download when hpatchz is on PATH")

    assert sr.ensure_hpatchz(fetch=boom) == "/usr/bin/hpatchz"


def test_ensure_hpatchz_downloads_verifies_and_installs_once(no_system_hpatchz, monkeypatch):
    blob = _fake_hpatchz_zip()
    monkeypatch.setattr(sr, "HPATCHZ_ZIP_SHA256", _sha(blob))
    urls: list[str] = []

    def fetch(url):
        urls.append(url)
        return blob

    path = Path(sr.ensure_hpatchz(fetch=fetch))
    assert path == sr.managed_hpatchz_path() and path.is_file()
    assert os.access(path, os.X_OK) and path.read_bytes().startswith(b"#!/bin/sh")
    assert urls == [sr.HPATCHZ_ZIP_URL]
    assert not list(path.parent.glob("*.part"))
    # Second call finds the managed copy and does not download again.
    assert sr.ensure_hpatchz(fetch=fetch) == str(path)
    assert len(urls) == 1
    assert sr.find_hpatchz() == str(path)


def test_ensure_hpatchz_refuses_a_download_with_the_wrong_checksum(no_system_hpatchz):
    with pytest.raises(sr.RuntimeSwapError, match="expected checksum"):
        sr.ensure_hpatchz(fetch=lambda _u: _fake_hpatchz_zip())
    assert not sr.managed_hpatchz_path().exists()
    assert sr.find_hpatchz() is None


def test_ensure_hpatchz_reports_network_failure_with_an_install_hint(no_system_hpatchz):
    def offline(_url):
        raise OSError("network unreachable")

    with pytest.raises(sr.RuntimeSwapError, match=r"network unreachable.*hdiffpatch-bin"):
        sr.ensure_hpatchz(fetch=offline)


def test_ensure_hpatchz_rejects_a_zip_without_the_binary(no_system_hpatchz, monkeypatch):
    blob = _fake_hpatchz_zip(member="other/thing")
    monkeypatch.setattr(sr, "HPATCHZ_ZIP_SHA256", _sha(blob))
    with pytest.raises(sr.RuntimeSwapError, match="unreadable"):
        sr.ensure_hpatchz(fetch=lambda _u: blob)
    assert not sr.managed_hpatchz_path().exists()


def test_ensure_hpatchz_does_not_download_on_unsupported_platforms(no_system_hpatchz, monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "aarch64")

    def boom(_url):
        raise AssertionError("must not download an x86-64 binary on aarch64")

    with pytest.raises(sr.RuntimeSwapError, match="hpatchz was not found"):
        sr.ensure_hpatchz(fetch=boom)


# ---- describe (the wizard's view of the install) ---------------------------------

def test_describe_a_fresh_install_has_nothing_to_revert(game_root, state_dir):
    info = sr.describe(game_root, state_dir, game_running=False)
    assert info.version == SRC and not info.swapped and not info.can_revert
    assert info.recorded_from is None and info.revert_blockers == []


def test_describe_after_a_swap_offers_the_revert(game_root, state_dir, transition):
    _apply(game_root, transition, state_dir)
    info = sr.describe(game_root, state_dir, game_running=False)
    assert info.swapped and info.can_revert
    assert (info.version, info.recorded_from, info.recorded_to) == (DST, SRC, DST)


def test_describe_treats_a_stale_record_as_nothing_to_revert(game_root, state_dir, transition):
    _apply(game_root, transition, state_dir)
    (game_root / "SkyrimSE.exe").write_bytes(build_pe((1, 7, 200, 0)))       # Steam moved on
    info = sr.describe(game_root, state_dir, game_running=False)
    assert not info.swapped and not info.can_revert


def test_describe_reports_revert_blockers(game_root, state_dir, transition):
    _apply(game_root, transition, state_dir)
    (game_root / "Data" / ".mm_deployed").write_bytes(b"")
    info = sr.describe(game_root, state_dir, game_running=True)
    assert info.swapped and not info.can_revert
    text = " ".join(info.revert_blockers)
    assert "running" in text and "restore" in text.lower()


def test_describe_survives_a_damaged_record(game_root, state_dir):
    state_dir.mkdir(parents=True)
    (state_dir / sr.STATE_FILENAME).write_text('{"applied": true, "from": "x.y", "to": "1.6.1170"}')
    info = sr.describe(game_root, state_dir, game_running=False)
    assert not info.swapped
