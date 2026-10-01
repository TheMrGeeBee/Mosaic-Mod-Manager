"""Utils.modding_tools.tw3_version -- detects whether an installed Witcher 3
is Next-Gen (4.x) or Remastered (5.x), by reading witcher3.exe's own PE
file-version resource. Real-world motivation: a Nexus Collection targets one
branch or the other, and until now Mosaic had no way to know which one is
actually installed.

Remastered moved the exe from bin/x64/ to bin/x64_dx12/ -- confirmed against
a real installed Remastered copy (5.0.15.61352) via direct PE read during
this session's live testing.
"""
from __future__ import annotations

from pe_builder import build_pe

from Utils.modding_tools.tw3_version import (
    classify_tw3_version,
    describe_tw3_version,
    find_witcher3_exe,
    read_tw3_version,
)


def test_finds_remastered_exe_under_x64_dx12(tmp_path):
    exe_dir = tmp_path / "bin" / "x64_dx12"
    exe_dir.mkdir(parents=True)
    (exe_dir / "witcher3.exe").write_bytes(build_pe((5, 0, 15, 61352)))
    assert find_witcher3_exe(tmp_path) == exe_dir / "witcher3.exe"


def test_finds_next_gen_exe_under_x64_when_no_dx12_folder(tmp_path):
    exe_dir = tmp_path / "bin" / "x64"
    exe_dir.mkdir(parents=True)
    (exe_dir / "witcher3.exe").write_bytes(build_pe((4, 4, 0, 0)))
    assert find_witcher3_exe(tmp_path) == exe_dir / "witcher3.exe"


def test_prefers_x64_dx12_over_x64_when_both_exist(tmp_path):
    dx12_dir = tmp_path / "bin" / "x64_dx12"
    dx12_dir.mkdir(parents=True)
    (dx12_dir / "witcher3.exe").write_bytes(build_pe((5, 0, 15, 61352)))
    legacy_dir = tmp_path / "bin" / "x64"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "witcher3.exe").write_bytes(build_pe((4, 4, 0, 0)))
    assert find_witcher3_exe(tmp_path) == dx12_dir / "witcher3.exe"


def test_finds_nothing_when_neither_subfolder_has_the_exe(tmp_path):
    assert find_witcher3_exe(tmp_path) is None


def test_read_tw3_version_end_to_end(tmp_path):
    exe_dir = tmp_path / "bin" / "x64_dx12"
    exe_dir.mkdir(parents=True)
    (exe_dir / "witcher3.exe").write_bytes(build_pe((5, 0, 15, 61352)))
    assert read_tw3_version(tmp_path) == (5, 0, 15, 61352)


def test_read_tw3_version_none_when_exe_missing(tmp_path):
    assert read_tw3_version(tmp_path) is None


def test_classify_major_5_is_remastered():
    assert classify_tw3_version((5, 0, 15, 61352)) == "remastered"


def test_classify_major_6_is_also_remastered():
    # Future-proofing: any major >= 5 counts as the Remastered branch.
    assert classify_tw3_version((6, 0, 0, 0)) == "remastered"


def test_classify_major_4_is_next_gen():
    assert classify_tw3_version((4, 4, 0, 0)) == "next-gen"


def test_classify_major_3_is_unknown():
    # Pre-Next-Gen (e.g. GOTY-era) isn't a branch Mosaic distinguishes.
    assert classify_tw3_version((3, 0, 0, 0)) == "unknown"


def test_classify_none_is_unknown():
    assert classify_tw3_version(None) == "unknown"


def test_describe_remastered(tmp_path):
    exe_dir = tmp_path / "bin" / "x64_dx12"
    exe_dir.mkdir(parents=True)
    (exe_dir / "witcher3.exe").write_bytes(build_pe((5, 0, 15, 61352)))
    assert describe_tw3_version(tmp_path) == "5.0.15.61352 (Remastered)"


def test_describe_next_gen(tmp_path):
    exe_dir = tmp_path / "bin" / "x64"
    exe_dir.mkdir(parents=True)
    (exe_dir / "witcher3.exe").write_bytes(build_pe((4, 4, 0, 0)))
    assert describe_tw3_version(tmp_path) == "4.4.0.0 (Next-Gen)"


def test_describe_unknown_when_exe_missing(tmp_path):
    assert describe_tw3_version(tmp_path) == "Version unknown"
