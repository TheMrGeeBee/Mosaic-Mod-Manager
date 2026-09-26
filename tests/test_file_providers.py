"""find_providers / resolve_on_disk: which mods ship a path, and where."""
from __future__ import annotations

from pathlib import Path

from Utils.mods.file_providers import find_providers, resolve_on_disk


def _touch(base: Path, rel: str) -> Path:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def _index(**mods):
    return {name: (files, {}) for name, files in mods.items()}


def test_lists_only_mods_shipping_the_path(tmp_path):
    for m in ("A", "B", "C"):
        _touch(tmp_path / m, "Textures/x.dds")
    idx = _index(A={"textures/x.dds": "Textures/x.dds"},
                 B={"textures/x.dds": "Textures/x.dds"},
                 C={"textures/other.dds": "Textures/other.dds"})
    got = find_providers(idx, "textures/x.dds", {"textures/x.dds": "B"},
                         ["A", "B", "C"], lambda m: tmp_path / m)
    assert [p.mod_name for p in got] == ["B", "A"]      # winner first, then order
    assert [p.is_winner for p in got] == [True, False]
    assert all(p.disk_path and p.disk_path.is_file() for p in got)


def test_order_follows_mod_order_when_no_winner_known(tmp_path):
    idx = _index(A={"a.dds": "a.dds"}, B={"a.dds": "a.dds"})
    got = find_providers(idx, "a.dds", {}, ["B", "A"], lambda m: None)
    assert [p.mod_name for p in got] == ["B", "A"]
    assert not any(p.is_winner for p in got)
    assert all(p.disk_path is None for p in got)


def test_disabled_mods_are_left_out_by_mod_order(tmp_path):
    idx = _index(A={"a.dds": "a.dds"}, B={"a.dds": "a.dds"})
    got = find_providers(idx, "a.dds", {"a.dds": "A"}, ["A"], lambda m: None)
    assert [p.mod_name for p in got] == ["A"]


def test_key_is_case_and_slash_insensitive(tmp_path):
    idx = _index(A={"textures/x.dds": "Textures/x.dds"})
    got = find_providers(idx, "Textures\\X.DDS", {}, ["A"], lambda m: None)
    assert len(got) == 1


def test_root_files_ignored():
    idx = {"A": ({}, {"d3d11.dll": "d3d11.dll"})}
    assert find_providers(idx, "d3d11.dll", {}, ["A"], lambda m: None) == []


def test_empty_or_missing_index():
    assert find_providers(None, "a", {}, ["A"], lambda m: None) == []
    assert find_providers({}, "a", {}, ["A"], lambda m: None) == []


def test_resolve_case_insensitive_on_case_sensitive_fs(tmp_path):
    real = _touch(tmp_path, "TEXTURES/Armor/Iron.DDS")
    assert resolve_on_disk(tmp_path, "textures/armor/iron.dds") == real


def test_resolve_readds_strip_prefix(tmp_path):
    real = _touch(tmp_path, "Wrapper/Textures/x.dds")
    assert resolve_on_disk(tmp_path, "Textures/x.dds") is None
    assert resolve_on_disk(tmp_path, "Textures/x.dds", ["wrapper"]) == real


def test_resolve_missing_and_directory(tmp_path):
    _touch(tmp_path, "d/x.dds")
    assert resolve_on_disk(tmp_path, "d/nope.dds") is None
    assert resolve_on_disk(tmp_path, "d") is None       # a folder isn't a file
    assert resolve_on_disk(None, "d/x.dds") is None
    assert resolve_on_disk(tmp_path, "") is None
