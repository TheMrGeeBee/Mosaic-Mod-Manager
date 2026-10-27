"""Starfield has no Starfield.ccc; Creations downloaded from the in-game menu are
listed only in the prefix's ContentCatalog.txt and must count as game-owned."""
from __future__ import annotations

import json
from pathlib import Path

from Games.Bethesda.starfield import Starfield
from Utils.exe_launch.game_helpers import _read_ccc_manifest


def _game(prefix: Path) -> Starfield:
    g = Starfield.__new__(Starfield)
    g._prefix_path = prefix
    g.get_game_path = lambda: None
    return g


def _write_catalog(prefix: Path, entries: dict) -> None:
    d = prefix / Starfield._APPDATA_SUBPATH
    d.mkdir(parents=True)
    (d / "ContentCatalog.txt").write_text(json.dumps(entries), encoding="utf-8")


def test_catalog_plugin_is_vanilla_only_when_present_in_data(tmp_path):
    _write_catalog(tmp_path, {"TM_a": {"Files": ["sfbgs019.esm", "sfbgs021.esm"]}})
    result = _read_ccc_manifest(_game(tmp_path), "Starfield.ccc", {"sfbgs019.esm"})
    assert result == {"sfbgs019.esm": "sfbgs019.esm"}


def test_missing_or_broken_catalog_is_harmless(tmp_path):
    assert _game(tmp_path).catalog_vanilla_plugins() == []
    _write_catalog(tmp_path, {})
    (tmp_path / Starfield._APPDATA_SUBPATH / "ContentCatalog.txt").write_text("{nope")
    assert _game(tmp_path).catalog_vanilla_plugins() == []


def test_catalog_files_are_whitelisted_from_the_restore_sweep(tmp_path):
    _write_catalog(tmp_path, {
        "TM_a": {"Files": ["sfbgs019 - main.ba2", "sfbgs019.esm"]},
        "TM_b": {"Files": ["meaningfulbackgroundsplus.esm"]},
    })
    match = _game(tmp_path).restore_whitelist_matcher(rel_prefix="data/")
    # In-game Creations downloads stay in Data/ — plugins and archives alike,
    # Bethesda's and third-party ones alike.
    assert match("sfbgs019.esm")
    assert match("sfbgs019 - main.ba2")
    assert match("meaningfulbackgroundsplus.esm")
    # A genuine runtime file is still swept to overwrite/.
    assert not match("some mod.esp")
    # The game-root Creations/ staging folder is protected by the root sweep.
    root_match = _game(tmp_path).restore_whitelist_matcher()
    assert root_match("creations/anything.ba2")
    assert root_match("data/sfbgs019.esm")


def test_creations_folder_is_whitelisted_without_a_catalog(tmp_path):
    match = _game(tmp_path).restore_whitelist_matcher()
    assert match("creations/x.ba2")
    assert not match("data/sfbgs019.esm")


def test_restore_keeps_a_creation_downloaded_in_game(tmp_path):
    from Utils.deploy.deploy import restore_data_core

    _write_catalog(tmp_path, {"TM_a": {"Files": ["sfbgs019.esm"]}})
    game_dir = tmp_path / "game"
    data = game_dir / "Data"
    core = game_dir / "Data_Core"
    data.mkdir(parents=True)
    core.mkdir()
    (core / "Starfield.esm").write_text("vanilla")
    # Downloaded from the in-game Creations menu while deployed: a plain file
    # in the live Data/, in no filemap and not in the Data_Core snapshot.
    (data / "SFBGS019.esm").write_text("creation")
    (data / "runtime.log").write_text("junk")
    overwrite = tmp_path / "profile" / "overwrite"
    overwrite.mkdir(parents=True)

    restore_data_core(
        data, overwrite_dir=overwrite,
        restore_whitelist=_game(tmp_path).restore_whitelist_matcher(
            rel_prefix="data/"),
    )

    assert (data / "SFBGS019.esm").read_text() == "creation"
    assert (data / "Starfield.esm").is_file()
    assert not (overwrite / "SFBGS019.esm").exists()
    assert (overwrite / "runtime.log").is_file()


def test_every_creation_in_the_catalog_counts_as_game_owned(tmp_path):
    """Bethesda's free CC (sfbgs*) is not the whole catalog: its paid Creations
    (sfta*, kgcdoom) and third-party ones come down the same in-game menu and
    are equally not Mosaic's."""
    _write_catalog(tmp_path, {
        "TM_a": {"Files": ["sfbgs019 - main.ba2", "sfbgs019.esm"]},
        "TM_b": {"Files": ["sfta01.esm", "sfta01 - main.ba2"]},
        "TM_c": {"Files": ["kgcdoom.esm"]},
        "TM_d": {"Files": ["meaningfulbackgroundsplus.esm"]},
    })
    g = _game(tmp_path)
    g.get_effective_filemap_path = lambda: tmp_path / "nope" / "filemap.txt"
    assert g.catalog_vanilla_plugins() == [
        "sfbgs019.esm", "sfta01.esm", "kgcdoom.esm",
        "meaningfulbackgroundsplus.esm",
    ]


def test_a_creation_a_mod_also_provides_stays_a_mod(tmp_path):
    """Same content installed from Nexus through Mosaic: that copy IS staged by
    a mod, so it keeps its mod row. An overwrite/ entry is not a mod — a build
    that swept a Creation in there must not turn it into one."""
    _write_catalog(tmp_path, {
        "TM_a": {"Files": ["meaningfulbackgrounds.esm"]},
        "TM_b": {"Files": ["sfbgs019.esm"]},
        "TM_c": {"Files": ["sfbgs021.esm"]},
    })
    filemap = tmp_path / "filemap.txt"
    filemap.write_text(
        "meaningfulbackgrounds.esm\tMeaningful Backgrounds\n"
        "sfbgs021.esm\tOverwrite\n"
        "Textures/sfbgs019.esm\tSome Mod\n",   # not top-level — not a plugin slot
        encoding="utf-8")
    g = _game(tmp_path)
    g.get_effective_filemap_path = lambda: filemap
    assert g.catalog_vanilla_plugins() == ["sfbgs019.esm", "sfbgs021.esm"]


def test_creations_are_named_as_needing_a_plugins_txt_entry(tmp_path):
    _write_catalog(tmp_path, {"TM_a": {"Files": ["SFBGS019.esm", "sfta01.esm"]}})
    data = tmp_path / "Data"
    data.mkdir()
    (data / "SFBGS019.esm").touch()
    (data / "sfta01.esm").touch()
    g = _game(tmp_path)
    g.get_effective_filemap_path = lambda: tmp_path / "nope" / "filemap.txt"
    g.get_vanilla_plugins_path = lambda: data
    assert g.plugins_txt_listed_vanilla() == {"sfbgs019.esm", "sfta01.esm"}


def test_a_creation_with_no_file_is_not_listed(tmp_path):
    """The catalog still lists a Creation whose files are gone. Writing an
    entry for it only feeds the panel's phantom prune."""
    _write_catalog(tmp_path, {"TM_a": {"Files": ["sfbgs019.esm", "sfbgs021.esm"]}})
    data = tmp_path / "Data"
    data.mkdir()
    (data / "sfbgs019.esm").touch()
    g = _game(tmp_path)
    g.get_effective_filemap_path = lambda: tmp_path / "nope" / "filemap.txt"
    g.get_vanilla_plugins_path = lambda: data
    assert g.plugins_txt_listed_vanilla() == {"sfbgs019.esm"}
