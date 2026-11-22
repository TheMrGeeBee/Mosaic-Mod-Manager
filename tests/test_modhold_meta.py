"""Modhold meta.ini keys: round trip, coexistence with Nexus keys, install hook."""
from __future__ import annotations

import configparser
from types import SimpleNamespace

from Modhold.modhold_meta import (
    ModholdMeta,
    is_modhold_mod,
    read_modhold_meta,
    write_modhold_meta,
)


def _meta(**kw):
    base = dict(mod_id=12, file_id=1508, version="1.0.7", name="Falkreath Shop",
                url="https://modholdmods.com/g/skyrim-se/mods/12/x",
                uploader="SethSaidThat", sha256="ab" * 32, game="skyrim-se")
    base.update(kw)
    return ModholdMeta(**base)


def test_round_trip(tmp_path):
    p = tmp_path / "meta.ini"
    write_modhold_meta(p, _meta(locked="beta", loadout="3"))
    got = read_modhold_meta(p)
    assert (got.mod_id, got.file_id, got.version) == (12, 1508, "1.0.7")
    assert (got.name, got.uploader, got.game) == ("Falkreath Shop", "SethSaidThat", "skyrim-se")
    assert got.sha256 == "ab" * 32 and got.locked == "beta" and got.loadout == "3"
    assert got.latest_version == "1.0.7" and got.latest_file_id == 1508
    assert got.installed
    assert is_modhold_mod(p)


def test_missing_file_reads_as_defaults(tmp_path):
    got = read_modhold_meta(tmp_path / "nope.ini")
    assert got.mod_id == 0 and not is_modhold_mod(tmp_path / "nope.ini")


def test_existing_nexus_keys_are_preserved(tmp_path):
    p = tmp_path / "meta.ini"
    p.write_text("[General]\nmodid=2014\nfileid=77\nversion=3.1\n"
                 "installationFile=Some Mod-2014-3-1.zip\n", encoding="utf-8")
    write_modhold_meta(p, _meta())
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(p, encoding="utf-8")
    assert cp["General"]["modid"] == "2014"
    assert cp["General"]["installationFile"] == "Some Mod-2014-3-1.zip"
    assert cp["General"]["modholdModId"] == "12"


def test_percent_in_a_name_does_not_break_reading(tmp_path):
    p = tmp_path / "meta.ini"
    write_modhold_meta(p, _meta(name="100% Better Skies"))
    assert read_modhold_meta(p).name == "100% Better Skies"


def test_rewrite_clears_a_stale_ignore_update(tmp_path):
    p = tmp_path / "meta.ini"
    write_modhold_meta(p, _meta(ignore_update=True, ignored_version="1.0.6"))
    assert read_modhold_meta(p).ignore_update
    write_modhold_meta(p, _meta())
    got = read_modhold_meta(p)
    assert not got.ignore_update and got.ignored_version == ""


def test_corrupt_meta_ini_does_not_raise(tmp_path):
    p = tmp_path / "meta.ini"
    p.write_bytes(b"\xff\xfe not an ini \x00")
    write_modhold_meta(p, _meta())
    assert read_modhold_meta(p).mod_id == 12


def test_install_hook_stamps_modhold_keys_and_skips_nexus_lookup(tmp_path, monkeypatch):
    """A prebuilt meta carrying ``modhold_info`` gets stamped by _write_install_meta."""
    from Nexus.nexus_meta import NexusModMeta
    from Utils.mods import mod_install

    # The archive name embeds Nexus-style ids; a Nexus lookup must not be attempted.
    monkeypatch.setattr(mod_install, "_build_nexus_api",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Nexus lookup")))
    archive = tmp_path / "MCM Helper-53000-1-6-2.7z"
    archive.write_bytes(b"x")
    dest = tmp_path / "staging" / "MCM Helper"
    dest.mkdir(parents=True)
    prebuilt = NexusModMeta()
    prebuilt.modhold_info = _meta(mod_id=786, file_id=1182, name="MCM Helper")
    game = SimpleNamespace(nexus_game_domain="skyrimspecialedition", game_id="skyrim_se")
    logs = []
    mod_install._write_install_meta(dest, archive, game, logs.append,
                                    prebuilt_meta=prebuilt)
    got = read_modhold_meta(dest / "meta.ini")
    assert got.mod_id == 786 and got.file_id == 1182
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(dest / "meta.ini", encoding="utf-8")
    assert cp["General"]["installationFile"] == archive.name
    assert not any("skipped" in m for m in logs), logs


def test_install_without_modhold_info_writes_no_modhold_keys(tmp_path):
    from Nexus.nexus_meta import NexusModMeta
    from Utils.mods import mod_install

    archive = tmp_path / "a.zip"
    archive.write_bytes(b"x")
    dest = tmp_path / "m"
    dest.mkdir()
    game = SimpleNamespace(nexus_game_domain="skyrimspecialedition", game_id="skyrim_se")
    mod_install._write_install_meta(dest, archive, game, lambda *_: None,
                                    prebuilt_meta=NexusModMeta())
    assert not is_modhold_mod(dest / "meta.ini")
