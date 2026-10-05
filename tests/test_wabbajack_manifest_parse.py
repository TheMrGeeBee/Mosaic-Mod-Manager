"""Tests for Utils.wabbajack.wabbajack_manifest's modlist/archive/directive
parsing. All pure -- no I/O, no network, no game access."""
from __future__ import annotations

from Utils.wabbajack.wabbajack_manifest import (
    CreateBSADirective,
    FromArchiveDirective,
    GoogleDriveState,
    HttpState,
    InlineFileDirective,
    LoversLabState,
    MediaFireState,
    MegaState,
    NexusState,
    PatchedFromArchiveDirective,
    RemappedInlineFileDirective,
    TransformedTextureDirective,
    UnknownDirective,
    UnknownState,
    WabbajackCDNState,
    mosaic_game_for,
    parse_archive,
    parse_archive_state,
    parse_directive,
    parse_modlist,
)


# ---------------------------------------------------------------------------
# Archive states
# ---------------------------------------------------------------------------

def test_nexus_state_parses_ids_and_metadata():
    state = parse_archive_state({
        "$type": "NexusDownloader, Wabbajack.Lib",
        "GameName": "SkyrimSpecialEdition", "ModID": 123, "FileID": 456,
        "Name": "Some Mod", "Author": "Someone", "Version": "1.0",
    })
    assert isinstance(state, NexusState)
    assert (state.game_name, state.mod_id, state.file_id) == ("SkyrimSpecialEdition", 123, 456)
    assert state.name == "Some Mod"


def test_state_type_tolerates_plus_state_suffix():
    state = parse_archive_state({"$type": "NexusDownloader+State, Wabbajack.Lib", "ModID": 1})
    assert isinstance(state, NexusState)
    assert state.mod_id == 1


def test_http_state_parses_url_and_headers():
    state = parse_archive_state({
        "$type": "HttpDownloader, Wabbajack.Lib",
        "Url": "https://example.com/file.7z", "Headers": ["Authorization:Bearer x"],
    })
    assert isinstance(state, HttpState)
    assert state.url == "https://example.com/file.7z"
    assert state.headers == ["Authorization:Bearer x"]


def test_wabbajack_cdn_state():
    state = parse_archive_state({
        "$type": "WabbajackCDNDownloader, Wabbajack.Lib", "Url": "https://cdn.wabbajack.org/x"})
    assert isinstance(state, WabbajackCDNState)
    assert state.url == "https://cdn.wabbajack.org/x"


def test_google_drive_state():
    state = parse_archive_state({
        "$type": "GoogleDriveDownloader, Wabbajack.Lib", "Id": "abc123"})
    assert isinstance(state, GoogleDriveState)
    assert state.file_id == "abc123"


def test_mediafire_and_mega_and_loverslab_states():
    assert isinstance(parse_archive_state(
        {"$type": "MediaFireDownloader, Wabbajack.Lib", "Url": "u"}), MediaFireState)
    assert isinstance(parse_archive_state(
        {"$type": "MegaDownloader, Wabbajack.Lib", "Url": "u"}), MegaState)
    assert isinstance(parse_archive_state(
        {"$type": "LoversLabDownloader, Wabbajack.Lib", "Url": "u"}), LoversLabState)


def test_unknown_state_keeps_raw_dict():
    raw = {"$type": "SomeFutureDownloader, Wabbajack.Lib", "Weird": True}
    state = parse_archive_state(raw)
    assert isinstance(state, UnknownState)
    assert state.type_name == "SomeFutureDownloader, Wabbajack.Lib"
    assert state.raw == raw


def test_parse_archive_wraps_state_and_metadata():
    archive = parse_archive({
        "Hash": "abc==", "Meta": "[General]\n", "Name": "file.7z", "Size": 100,
        "State": {"$type": "NexusDownloader, Wabbajack.Lib", "ModID": 1, "FileID": 2},
    })
    assert archive.hash == "abc=="
    assert archive.size == 100
    assert isinstance(archive.state, NexusState)


# ---------------------------------------------------------------------------
# Directives
# ---------------------------------------------------------------------------

def test_from_archive_directive():
    d = parse_directive({
        "$type": "FromArchive, Wabbajack.Lib", "To": "mods/Foo/foo.esp",
        "Hash": "h==", "Size": 10, "ArchiveHashPath": ["archive-hash", "internal/path"],
    })
    assert isinstance(d, FromArchiveDirective)
    assert d.to == "mods/Foo/foo.esp"
    assert d.archive_hash_path == ["archive-hash", "internal/path"]


def test_patched_from_archive_directive_keeps_patch_id():
    d = parse_directive({
        "$type": "PatchedFromArchive, Wabbajack.Lib", "To": "mods/Foo/foo.esp",
        "Hash": "h==", "Size": 10, "ArchiveHashPath": ["archive-hash", "p"],
        "PatchID": "patch-id-123",
    })
    assert isinstance(d, PatchedFromArchiveDirective)
    assert d.patch_id == "patch-id-123"


def test_inline_file_and_remapped_inline_file_directives():
    inline = parse_directive({
        "$type": "InlineFile, Wabbajack.Lib", "To": "config.ini",
        "Hash": "h==", "Size": 5, "SourceDataID": "data-1",
    })
    assert isinstance(inline, InlineFileDirective)
    assert inline.source_data_id == "data-1"

    remapped = parse_directive({
        "$type": "RemappedInlineFile, Wabbajack.Lib", "To": "profiles/Default/plugins.txt",
        "Hash": "h==", "Size": 5, "SourceDataID": "data-2",
    })
    assert isinstance(remapped, RemappedInlineFileDirective)
    assert remapped.source_data_id == "data-2"


def test_create_bsa_and_create_ba2_directives():
    bsa = parse_directive({
        "$type": "CreateBSA, Wabbajack.Lib", "To": "mods/Foo/Foo.bsa",
        "Hash": "h==", "Size": 100, "TempID": "temp-1",
    })
    assert isinstance(bsa, CreateBSADirective)
    assert bsa.temp_id == "temp-1"

    ba2 = parse_directive({"$type": "CreateBA2, Wabbajack.Lib", "To": "x.ba2", "TempID": "t"})
    assert isinstance(ba2, CreateBSADirective)


def test_transformed_texture_directive():
    d = parse_directive({
        "$type": "TransformedTexture, Wabbajack.Lib", "To": "tex.dds", "Hash": "h==", "Size": 1})
    assert isinstance(d, TransformedTextureDirective)


def test_unknown_directive_keeps_raw_and_destination():
    raw = {"$type": "SomeFutureDirective, Wabbajack.Lib", "To": "weird/path", "Extra": 1}
    d = parse_directive(raw)
    assert isinstance(d, UnknownDirective)
    assert d.to == "weird/path"
    assert d.raw == raw


# ---------------------------------------------------------------------------
# Game mapping
# ---------------------------------------------------------------------------

def test_mosaic_game_for_known_and_unknown():
    assert mosaic_game_for("SkyrimSpecialEdition") == "Skyrim Special Edition"
    assert mosaic_game_for("CyberpunkTheGame") is None
    assert mosaic_game_for("") is None
    assert mosaic_game_for(None) is None


# ---------------------------------------------------------------------------
# Top-level ModList
# ---------------------------------------------------------------------------

def test_parse_modlist_empty_dict_does_not_raise():
    ml = parse_modlist({})
    assert ml.archives == []
    assert ml.directives == []
    assert ml.total_archive_size == 0
    assert ml.total_install_size == 0


def test_parse_modlist_full_shape_and_size_totals():
    ml = parse_modlist({
        "Name": "Test List", "Author": "Someone", "GameType": "SkyrimSpecialEdition",
        "Version": "1.0", "WabbajackVersion": "3.0.0",
        "Archives": [
            {"Hash": "a==", "Name": "a.7z", "Size": 100,
             "State": {"$type": "NexusDownloader, Wabbajack.Lib", "ModID": 1, "FileID": 2}},
            {"Hash": "b==", "Name": "b.7z", "Size": 200,
             "State": {"$type": "UnsupportedSource, X"}},
        ],
        "Directives": [
            {"$type": "FromArchive, Wabbajack.Lib", "To": "mods/A/a.esp", "Size": 10,
             "ArchiveHashPath": ["a==", "a.esp"]},
            {"$type": "SomeFutureDirective, X", "To": "mods/A/weird", "Size": 5},
        ],
    })
    assert ml.name == "Test List"
    assert ml.game_type == "SkyrimSpecialEdition"
    assert ml.total_archive_size == 300
    # UnknownDirective carries no `size` (its shape isn't understood), so the
    # unsupported directive's declared Size is deliberately not counted.
    assert ml.total_install_size == 10
    assert len(ml.unsupported_archives) == 1
    assert len(ml.unsupported_directives) == 1
