"""_detect_scents walks each mod folder once per scan, not once per marker."""

import os

from Utils.modding_tools import skygen_core as sg


def _touch(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_marker_files_are_detected_case_insensitively_at_any_depth(tmp_path):
    mod = tmp_path / "mod"
    _touch(mod / "Foo.esp")
    _touch(mod / "deep" / "er" / "FOO_SWAP.ini")
    _touch(mod / "x" / "foo_DISTR.ini")
    _touch(mod / "foo_kid.ini")
    _touch(mod / "a" / "Foo_DSD.json")
    _touch(mod / "b" / "FOO_dsd.yaml")
    scents = sg._detect_scents(mod / "Foo.esp", mod)
    assert {"PRE_PATCHED_BOS", "BOS_FRAMEWORK", "PRE_PATCHED_SPID",
            "PRE_PATCHED_KID", "PRE_PATCHED_DSD"} <= scents


def test_other_plugins_markers_do_not_match(tmp_path):
    mod = tmp_path / "mod"
    _touch(mod / "Foo.esp")
    _touch(mod / "Bar_SWAP.ini")
    _touch(mod / "Bar_DSD.json")
    scents = sg._detect_scents(mod / "Foo.esp", mod)
    assert "PRE_PATCHED_BOS" not in scents
    assert "PRE_PATCHED_DSD" not in scents


def test_uppercase_extension_is_not_matched_like_rglob(tmp_path):
    mod = tmp_path / "mod"
    _touch(mod / "Foo.esp")
    _touch(mod / "Foo_SWAP.INI")
    assert "PRE_PATCHED_BOS" not in sg._detect_scents(mod / "Foo.esp", mod)


def test_data_subfolder_is_searched(tmp_path):
    mod = tmp_path / "mod"
    _touch(mod / "Foo.esp")
    _touch(mod / "Data" / "Foo_kid.ini")
    assert "PRE_PATCHED_KID" in sg._detect_scents(mod / "Foo.esp", mod)


def test_folder_is_walked_once_across_plugins(tmp_path, monkeypatch):
    mod = tmp_path / "mod"
    for name in ("A.esp", "B.esp", "C.esp"):
        _touch(mod / name)
    _touch(mod / "B_kid.ini")
    calls = []
    real = sg._index_scent_files
    monkeypatch.setattr(sg, "_index_scent_files",
                        lambda root: calls.append(root) or real(root))
    index = {}
    results = {n: sg._detect_scents(mod / n, mod, index)
               for n in ("A.esp", "B.esp", "C.esp")}
    assert calls == [mod]
    assert "PRE_PATCHED_KID" in results["B.esp"]
    assert "PRE_PATCHED_KID" not in results["A.esp"]


def test_symlinked_directory_is_not_followed(tmp_path):
    mod = tmp_path / "mod"
    outside = tmp_path / "outside"
    _touch(outside / "Foo_kid.ini")
    _touch(mod / "Foo.esp")
    try:
        os.symlink(outside, mod / "link", target_is_directory=True)
    except OSError:
        return
    assert "PRE_PATCHED_KID" not in sg._detect_scents(mod / "Foo.esp", mod)
