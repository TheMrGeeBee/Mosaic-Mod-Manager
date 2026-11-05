"""Tests for Utils.wabbajack.wabbajack_preflight. Pure -- modlists are built
from plain dicts, and free disk space is injected."""
from __future__ import annotations

from Utils.collections.collection_preflight import blocking_failures, warnings
from Utils.wabbajack.wabbajack_manifest import parse_modlist
from Utils.wabbajack.wabbajack_preflight import (
    FIX_LOVERSLAB_LOGIN,
    FIX_MANUAL_DOWNLOADS,
    check_directives,
    check_game,
    check_inno_tool,
    check_sources,
    run_preflight,
)

GIB = 1 << 30


def _archive(type_name, size=1):
    return {"Hash": "h", "Name": "a.7z", "Size": size, "State": {"$type": f"{type_name}, Wabbajack.Lib"}}


def _modlist(game="SkyrimSpecialEdition", archives=(), directives=()):
    return parse_modlist({"GameType": game, "Archives": list(archives),
                          "Directives": list(directives)})


def test_game_supported_and_active():
    [check] = check_game(_modlist(), "Skyrim Special Edition")
    assert check.ok


def test_game_unsupported_blocks():
    [check] = check_game(_modlist(game="BaldursGate3"), "Baldur's Gate 3")
    assert not check.ok and check.blocking
    assert "BaldursGate3" in check.detail


def test_cyberpunk_supported_and_active():
    [check] = check_game(_modlist(game="Cyberpunk2077"), "Cyberpunk 2077")
    assert check.ok


def test_game_mismatch_with_active_game_blocks():
    [check] = check_game(_modlist(game="Fallout4"), "Skyrim Special Edition")
    assert not check.ok and check.blocking
    assert "Fallout 4" in check.detail


def test_directives_all_supported():
    ml = _modlist(directives=[{"$type": "FromArchive, Wabbajack.Lib", "To": "a"},
                              {"$type": "RemappedInlineFile, Wabbajack.Lib", "To": "b"}])
    [check] = check_directives(ml)
    assert check.ok


def _create_bsa(state):
    return {"$type": "CreateBSA, Wabbajack.Lib", "To": "mods/A/a.bsa", "TempID": "t", "State": state}


def test_directives_unsupported_are_counted_and_block():
    ml = _modlist(directives=[
        _create_bsa({"$type": "BSAState, Compression.BSA", "Version": 105}),  # buildable
        _create_bsa({"$type": "TES3State, Compression.BSA"}),
        _create_bsa({"$type": "TES3State, Compression.BSA"}),
        {"$type": "TransformedTexture, Wabbajack.Lib", "To": "t.dds"},
        {"$type": "SomethingNew, Wabbajack.Lib", "To": "x"},
    ])
    [check] = check_directives(ml)
    assert not check.ok and check.blocking
    assert "2 unbuildable archives (Morrowind-format BSA)" in check.detail
    assert "1 converted texture;" in check.detail
    assert "1 unrecognised step (SomethingNew)" in check.detail


def test_buildable_archives_alone_pass():
    ml = _modlist(directives=[
        _create_bsa({"$type": "BSAState, Compression.BSA", "Version": 104}),
        _create_bsa({"$type": "BA2State, Compression.BSA", "Version": 8, "Type": "DX10"}),
    ])
    [check] = check_directives(ml)
    assert check.ok


def test_sources_all_automatic():
    ml = _modlist(archives=[_archive("HttpDownloader"), _archive("MegaDownloader")])
    [check] = check_sources(ml, nexus_premium=False, loverslab_logged_in=False)
    assert check.ok


def test_sources_unknown_blocks_others_warn():
    ml = _modlist(archives=[
        _archive("FutureDownloader"),
        _archive("ManualDownloader"),
        _archive("NexusDownloader"),
        _archive("LoversLabDownloader"),
    ])
    checks = check_sources(ml, nexus_premium=False, loverslab_logged_in=False)
    assert [c.key for c in blocking_failures(checks)] == ["sources"]
    assert {c.key for c in warnings(checks)} == {"manual", "nexus-premium", "loverslab"}
    [ll] = [c for c in checks if c.key == "loverslab"]
    assert ll.fix == FIX_LOVERSLAB_LOGIN


def test_sources_premium_and_login_silence_warnings():
    ml = _modlist(archives=[_archive("NexusDownloader"), _archive("LoversLabDownloader")])
    [check] = check_sources(ml, nexus_premium=True, loverslab_logged_in=True)
    assert check.ok


def test_run_preflight_includes_disk_space_with_modlist_wording(tmp_path):
    ml = _modlist(
        archives=[_archive("HttpDownloader", size=2 * GIB)],
        directives=[{"$type": "FromArchive, Wabbajack.Lib", "To": "a", "Size": 10 * GIB}])
    checks = run_preflight(
        ml, active_game_name="Skyrim Special Edition", staging_root=tmp_path,
        cache_dir=tmp_path, nexus_premium=True, loverslab_logged_in=True,
        free_fn=lambda _p: 5 * GIB)
    [disk] = [c for c in checks if c.key == "disk-space"]
    assert not disk.ok and disk.blocking
    assert disk.detail.startswith("The modlist needs")
    assert [c.key for c in checks] == ["game", "directives", "sources", "disk-space"]


# --- Inno Setup installers --------------------------------------------------

def _inno_modlist(*, used=True, name="setup.exe"):
    """A modlist whose files come from inside an installer archive."""
    archive = {"Hash": "exe1", "Name": name, "Size": 1,
               "State": {"$type": "HttpDownloader, Wabbajack.Lib"}}
    path = ["exe1", r"app\\data\\thing.txt"] if used else ["exe1"]
    directive = {"$type": "FromArchive, Wabbajack.Lib", "To": "mods/M/thing.txt",
                 "Hash": "h", "Size": 1, "ArchiveHashPath": path}
    return _modlist(archives=[archive], directives=[directive])


def test_inno_installer_without_innoextract_blocks():
    [check] = check_inno_tool(_inno_modlist(), which=lambda _n: None)
    assert not check.ok and check.blocking
    assert "innoextract" in check.detail
    assert "setup.exe" in check.detail


def test_inno_installer_with_innoextract_passes():
    [check] = check_inno_tool(_inno_modlist(), which=lambda _n: "/usr/bin/innoextract")
    assert check.ok


def test_exe_archive_not_used_as_a_container_is_not_flagged():
    """An .exe that is only downloaded (never opened to pull files out of)
    needs no extractor -- real case: a modlist shipping an installer for the
    user to run by hand."""
    assert check_inno_tool(_inno_modlist(used=False), which=lambda _n: None) == []


def test_modlist_without_exe_archives_is_not_flagged():
    assert check_inno_tool(_modlist(archives=[_archive("HttpDownloader")]),
                           which=lambda _n: None) == []


# ---------------------------------------------------------------------------
# the manual-download check: it tracks what the preflight queue pane has found
# ---------------------------------------------------------------------------


def _manual_check(n_manual, resolved):
    ml = _modlist(archives=[_archive("ManualDownloader") for _ in range(n_manual)])
    checks = check_sources(ml, nexus_premium=True, loverslab_logged_in=True,
                           manual_resolved=resolved)
    [check] = [c for c in checks if c.key == "manual"]
    return check


def test_manual_unresolved_warns_and_offers_the_queue():
    check = _manual_check(77, 0)
    assert not check.ok and not check.blocking
    assert check.fix == FIX_MANUAL_DOWNLOADS
    assert "0 of 77 archives downloaded" in check.detail


def test_manual_counts_progress():
    assert "12 of 77 archives downloaded" in _manual_check(77, 12).detail


def test_manual_all_downloaded_passes():
    check = _manual_check(3, 3)
    assert check.ok and check.fix is None


def test_manual_defaults_to_none_resolved():
    """An caller that doesn't know about the pane still gets the warning."""
    ml = _modlist(archives=[_archive("ManualDownloader")])
    [check] = [c for c in check_sources(ml, nexus_premium=True,
                                        loverslab_logged_in=True)
               if c.key == "manual"]
    assert not check.ok


def test_manual_resolved_is_clamped():
    """A stale count from a re-run must not read as 'more than everything'."""
    assert _manual_check(2, 99).ok
    assert "0 of 2 archives" in _manual_check(2, -5).detail


def test_run_preflight_threads_manual_resolved():
    ml = _modlist(archives=[_archive("ManualDownloader")])
    checks = run_preflight(ml, active_game_name="Skyrim Special Edition",
                           staging_root=None, cache_dir=None, nexus_premium=True,
                           loverslab_logged_in=True, manual_resolved=1)
    assert [c for c in checks if c.key == "manual"][0].ok
