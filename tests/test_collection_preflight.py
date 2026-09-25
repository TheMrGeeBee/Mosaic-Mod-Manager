"""Collection preflight: decide, before anything is downloaded or any profile is
created, whether the game is at the runtime the collection needs.

Real-world motivation: "Gate To Sovngarde" is built for Skyrim 1.7.104 (what Steam
serves) but needs SKSE64, which supports only 1.6.1170; it lists the SRS runtime
swap mod (Nexus 189855) to get there. Mosaic applies that mod's patches itself and
must therefore (a) notice the requirement up front, (b) fetch and verify the
archive, and (c) never install the mod, whose version.dll hook can't be hosted.
"""
from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from pe_builder import build_pe
from test_skyrim_runtime import DST, NEW, ORIG, SRC, write_swap  # noqa: F401 — shared fixtures
from Utils.collections import collection_preflight as pf
from Utils.collections import runtime_swap_fix as fix
from Utils.modding_tools import skyrim_runtime as sr

SRS_MOD_ID = 189855
SRS_FILE_ID = 796868


@dataclass
class Mod:
    mod_id: int = 0
    file_id: int = 0
    mod_name: str = ""
    file_name: str = ""
    size_bytes: int = 0
    md5: str = ""
    optional: bool = False


@dataclass
class Game:
    steam_id: str = "489830"


SRS = Mod(SRS_MOD_ID, SRS_FILE_ID, "SRS - Best of All Worlds", "srs.zip", 0, "")
OTHER = Mod(4242, 1, "Some Mod")


@pytest.fixture
def game_root(tmp_path):
    root = tmp_path / "Skyrim Special Edition"
    for rel, data in ORIG.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "config"


@pytest.fixture
def transition(tmp_path):
    return sr.load_transition(write_swap(tmp_path / "mod" / "RuntimeSwap"))


# ---- recognising the swap mod ---------------------------------------------------

def test_find_runtime_swap_mod():
    assert pf.find_runtime_swap_mod([OTHER, SRS]) is SRS
    assert pf.find_runtime_swap_mod([OTHER]) is None
    assert pf.find_runtime_swap_mod([]) is None


def test_without_runtime_swap_mods_drops_only_the_swap_mod():
    kept, dropped = pf.without_runtime_swap_mods([OTHER, SRS, Mod(7, 7)])
    assert [m.mod_id for m in kept] == [4242, 7]
    assert dropped == [SRS]


def test_mod_with_a_garbage_id_is_not_mistaken_for_the_swap_mod():
    assert pf.find_runtime_swap_mod([Mod(mod_id="nope")]) is None


@pytest.mark.parametrize("manifest,expected", [
    ({"info": {"gameVersions": ["1.7.104.0"]}}, ["1.7.104.0"]),
    ({"info": {}}, []),
    ({"info": {"gameVersions": "1.7.104"}}, []),
    ({}, []),
    (None, []),
])
def test_manifest_game_versions_is_tolerant(manifest, expected):
    assert pf.manifest_game_versions(manifest) == expected


def test_is_skyrim_se():
    assert pf.is_skyrim_se(Game("489830")) and not pf.is_skyrim_se(Game("377160"))


# ---- the runtime check ----------------------------------------------------------

def _rt(game_root, state_dir, transition, **kw):
    kw.setdefault("game_running", False)
    return pf.check_skyrim_runtime(game_root, state_dir, transition, **kw)


def test_unverified_runtime_offers_the_fix_and_blocks(game_root, state_dir):
    (c,) = _rt(game_root, state_dir, None, collection_versions=["1.7.104.0"])
    assert not c.ok and c.blocking and c.fix == pf.FIX_RUNTIME_SWAP
    assert "1.7.104.0" in c.detail


def test_game_at_source_needs_the_swap(game_root, state_dir, transition):
    (c,) = _rt(game_root, state_dir, transition)
    assert (c.key, c.ok, c.blocking, c.fix) == ("skyrim-runtime", False, True, pf.FIX_RUNTIME_SWAP)
    assert "1.7.104.0" in c.title and "1.6.1170.0" in c.title


def test_game_at_target_passes(game_root, state_dir, transition):
    (game_root / "SkyrimSE.exe").write_bytes(NEW["SkyrimSE.exe"])
    (c,) = _rt(game_root, state_dir, transition, game_running=True)   # running is irrelevant now
    assert c.ok and "1.6.1170.0" in c.title


def test_running_game_blocks_the_swap_without_a_fix(game_root, state_dir, transition):
    checks = _rt(game_root, state_dir, transition, game_running=True)
    running = [c for c in checks if c.key == "skyrim-running"]
    assert running and running[0].blocking and running[0].fix is None


def test_unknown_version_blocks_with_no_fix_and_points_at_steam_verify(game_root, state_dir, transition):
    (game_root / "SkyrimSE.exe").write_bytes(build_pe((1, 6, 640, 0)))
    (c,) = _rt(game_root, state_dir, transition)
    assert not c.ok and c.blocking and c.fix is None
    assert "1.6.640.0" in c.title and "Verify integrity" in c.detail


def test_unreadable_exe_is_reported_not_crashed(game_root, state_dir, transition):
    (game_root / "SkyrimSE.exe").write_bytes(b"not a pe")
    (c,) = _rt(game_root, state_dir, transition)
    assert not c.ok and "unreadable" in c.title


def test_missing_hpatchz_does_not_block_the_check(game_root, state_dir, transition, monkeypatch):
    monkeypatch.setattr(sr, "find_hpatchz", lambda: None)
    (c,) = _rt(game_root, state_dir, transition)
    assert c.fix == pf.FIX_RUNTIME_SWAP           # the fix fetches it on demand


# ---- disk space -----------------------------------------------------------------

GIB = 1 << 30


def test_disk_space_blocks_when_the_mods_cannot_fit(tmp_path):
    (c,) = pf.check_disk_space(tmp_path, tmp_path, 100 * GIB, 40 * GIB, free_fn=lambda _p: 50 * GIB)
    assert not c.ok and c.blocking


def test_disk_space_only_warns_when_archives_on_top_are_tight(tmp_path):
    (c,) = pf.check_disk_space(tmp_path, tmp_path, 100 * GIB, 40 * GIB, free_fn=lambda _p: 120 * GIB)
    assert not c.ok and not c.blocking


def test_disk_space_ok(tmp_path):
    (c,) = pf.check_disk_space(tmp_path, tmp_path, 100 * GIB, 40 * GIB, free_fn=lambda _p: 500 * GIB)
    assert c.ok


def test_disk_space_silent_when_the_size_is_unknown(tmp_path):
    assert pf.check_disk_space(tmp_path, tmp_path, 0, 0, free_fn=lambda _p: 1) == []
    assert pf.check_disk_space(None, None, 100 * GIB, 0) == []


def test_disk_space_works_for_a_staging_dir_that_does_not_exist_yet(tmp_path):
    (c,) = pf.check_disk_space(tmp_path / "not" / "yet", None, 1, 0)
    assert c.ok


# ---- run_preflight --------------------------------------------------------------

def _run(game, mods, game_root, state_dir, transition=None, **kw):
    return pf.run_preflight(game=game, mods=mods, manifest={"info": {"gameVersions": ["1.7.104.0"]}},
                            game_root=game_root, state_dir=state_dir, transition=transition,
                            game_running=False, **kw)


def test_run_preflight_is_silent_for_other_games(game_root, state_dir):
    assert _run(Game("377160"), [OTHER, SRS], game_root, state_dir) == []


def test_run_preflight_is_silent_for_a_skyrim_collection_without_a_swap_mod(game_root, state_dir):
    assert _run(Game(), [OTHER], game_root, state_dir) == []


def test_run_preflight_checks_the_runtime_for_a_skyrim_collection_with_the_swap_mod(game_root, state_dir):
    checks = _run(Game(), [OTHER, SRS], game_root, state_dir)
    assert [c.key for c in checks] == ["skyrim-runtime"]
    assert pf.fixable(checks) and pf.blocking_failures(checks) and not pf.warnings(checks)


def test_run_preflight_sums_archive_sizes_for_the_disk_check(game_root, state_dir, tmp_path):
    mods = [Mod(1, 1, size_bytes=30 * GIB), Mod(2, 2, size_bytes=20 * GIB)]
    checks = _run(Game("377160"), mods, game_root, state_dir, staging_root=tmp_path,
                  cache_dir=tmp_path, install_size=100 * GIB, free_fn=lambda _p: 120 * GIB)
    (c,) = checks
    assert c.key == "disk-space" and not c.ok and not c.blocking      # 100 + 50 > 120


# ---- finding and preparing the archive ------------------------------------------

def _make_swap_zip(path: Path, tmp_path: Path) -> Path:
    src = write_swap(tmp_path / "zipsrc" / "RuntimeSwap")
    with zipfile.ZipFile(path, "w") as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src.parent))
    return path


def _cached(tmp_path, name="SRS-189855-1-1-0.zip", *, fileid=SRS_FILE_ID):
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    archive = _make_swap_zip(cache / name, tmp_path)
    if fileid is not None:
        (cache / (name + ".fileid")).write_text(str(fileid))
    return cache, archive


def test_find_swap_archive_matches_by_fileid_sidecar(tmp_path):
    cache, archive = _cached(tmp_path)
    assert pf.find_swap_archive(SRS, [tmp_path / "nowhere", cache]) == archive


def test_find_swap_archive_ignores_a_different_file_id(tmp_path):
    cache, _ = _cached(tmp_path, fileid=111)
    assert pf.find_swap_archive(SRS, [cache]) is None


def test_find_swap_archive_rejects_a_truncated_download(tmp_path):
    cache, archive = _cached(tmp_path)
    mod = Mod(SRS_MOD_ID, SRS_FILE_ID, "SRS", size_bytes=archive.stat().st_size * 4)
    assert pf.find_swap_archive(mod, [cache]) is None


class Downloader:
    def __init__(self, result=None, dropped: Path | None = None):
        self.calls = []
        self.result = result
        self.dropped = dropped

    def download_file(self, domain, mod_id, file_id, dest_dir=None, **kw):
        self.calls.append((domain, mod_id, file_id, dest_dir, kw))
        return self.result


@dataclass
class Result:
    success: bool
    file_path: Path | None = None
    error: str = ""


def _prep(mod, tmp_path, cache_dirs, downloader, **kw):
    return fix.prepare_swap(mod, domain="skyrimspecialedition", search_dirs=cache_dirs,
                            dest_dir=tmp_path / "dest", downloader=downloader, **kw)


def test_prepare_swap_uses_the_cached_archive_and_loads_the_transition(tmp_path):
    cache, archive = _cached(tmp_path)
    dl = Downloader()
    prepared = _prep(SRS, tmp_path, [cache], dl)
    try:
        assert prepared.archive == archive and not dl.calls
        assert prepared.transition.source == SRC and prepared.transition.target == DST
        assert all(f.forward_patch.is_file() for f in prepared.transition.files)
    finally:
        prepared.cleanup()
    assert not prepared.workdir.exists()


def test_prepare_swap_downloads_when_missing_and_verifies_md5(tmp_path):
    made = _make_swap_zip(tmp_path / "fetched.zip", tmp_path)
    mod = Mod(SRS_MOD_ID, SRS_FILE_ID, "SRS", "fetched.zip", made.stat().st_size,
              hashlib.md5(made.read_bytes()).hexdigest())
    dl = Downloader(Result(True, made))
    prepared = _prep(mod, tmp_path, [tmp_path / "empty"], dl)
    try:
        domain, mod_id, file_id, dest, kw = dl.calls[0]
        assert (domain, mod_id, file_id, dest) == ("skyrimspecialedition", SRS_MOD_ID, SRS_FILE_ID,
                                                   tmp_path / "dest")
        assert kw["expected_size_bytes"] == made.stat().st_size
        assert len(prepared.transition.files) == len(ORIG)
    finally:
        prepared.cleanup()


def test_prepare_swap_refuses_an_archive_with_the_wrong_md5(tmp_path):
    made = _make_swap_zip(tmp_path / "fetched.zip", tmp_path)
    mod = Mod(SRS_MOD_ID, SRS_FILE_ID, "SRS", md5="0" * 32)
    with pytest.raises(sr.RuntimeSwapError, match="checksum mismatch"):
        _prep(mod, tmp_path, [], Downloader(Result(True, made)))


def test_prepare_swap_without_a_downloader_says_how_to_get_the_file(tmp_path):
    with pytest.raises(sr.RuntimeSwapError, match=r"Premium.*MANUAL download.*nexusmods"):
        _prep(SRS, tmp_path, [tmp_path / "empty"], None, nexus_url="https://www.nexusmods.com/x")


def test_prepare_swap_reports_a_failed_download(tmp_path):
    with pytest.raises(sr.RuntimeSwapError, match="rate limited"):
        _prep(SRS, tmp_path, [], Downloader(Result(False, None, "rate limited")))


def test_prepare_swap_rejects_an_archive_that_is_not_a_swap_mod(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    archive = cache / "other.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("readme.txt", "hi")
    (cache / "other.zip.fileid").write_text(str(SRS_FILE_ID))
    with pytest.raises(sr.RuntimeSwapError, match="isn't a runtime swap mod"):
        _prep(SRS, tmp_path, [cache], Downloader())


def test_prepare_swap_cleans_up_its_temp_dir_when_the_manifest_is_bad(tmp_path):
    def bad(m):
        m["algorithm"] = "bsdiff"

    src = write_swap(tmp_path / "badsrc" / "RuntimeSwap", mutate=bad)
    cache = tmp_path / "cache"
    cache.mkdir()
    with zipfile.ZipFile(cache / "bad.zip", "w") as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src.parent))
    (cache / "bad.zip.fileid").write_text(str(SRS_FILE_ID))
    import glob
    import tempfile
    before = set(glob.glob(f"{tempfile.gettempdir()}/mosaic-runtime-swap-*"))
    with pytest.raises(sr.RuntimeSwapError, match="can't apply"):
        _prep(SRS, tmp_path, [cache], Downloader())
    assert set(glob.glob(f"{tempfile.gettempdir()}/mosaic-runtime-swap-*")) == before
