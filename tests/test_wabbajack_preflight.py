"""Tests for Utils.wabbajack.wabbajack_preflight. Pure -- modlists are built
from plain dicts, and free disk space is injected."""
from __future__ import annotations

from Utils.collections.collection_preflight import blocking_failures, warnings
from Utils.wabbajack.wabbajack_manifest import parse_modlist
from Utils.wabbajack.wabbajack_preflight import (
    FIX_LOVERSLAB_LOGIN,
    check_directives,
    check_game,
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
    [check] = check_game(_modlist(game="Cyberpunk2077"), "Cyberpunk 2077")
    assert not check.ok and check.blocking
    assert "Cyberpunk2077" in check.detail


def test_game_mismatch_with_active_game_blocks():
    [check] = check_game(_modlist(game="Fallout4"), "Skyrim Special Edition")
    assert not check.ok and check.blocking
    assert "Fallout 4" in check.detail


def test_directives_all_supported():
    ml = _modlist(directives=[{"$type": "FromArchive, Wabbajack.Lib", "To": "a"},
                              {"$type": "RemappedInlineFile, Wabbajack.Lib", "To": "b"}])
    [check] = check_directives(ml)
    assert check.ok


def test_directives_unsupported_are_counted_and_block():
    ml = _modlist(directives=[
        {"$type": "CreateBSA, Wabbajack.Lib", "To": "a.bsa"},
        {"$type": "CreateBSA, Wabbajack.Lib", "To": "b.bsa"},
        {"$type": "TransformedTexture, Wabbajack.Lib", "To": "t.dds"},
        {"$type": "SomethingNew, Wabbajack.Lib", "To": "x"},
    ])
    [check] = check_directives(ml)
    assert not check.ok and check.blocking
    assert "2 rebuilt BSA/BA2 archives" in check.detail
    assert "1 converted texture;" in check.detail
    assert "1 unrecognised step (SomethingNew)" in check.detail


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
