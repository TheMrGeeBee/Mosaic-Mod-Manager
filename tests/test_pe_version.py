"""read_file_version() must read a Windows exe's four-part file version.

Real-world motivation: the Fallout 4 downgrade wizard has to know whether the
installed Fallout4.exe is 1.11.240.0 (Anniversary Edition, patchable) or
1.10.163.0 (Old-Gen, already downgraded) before touching anything, and to
verify the patched result. The repo had no PE reader, so a minimal one walks
just far enough to reach the VS_FIXEDFILEINFO in the RT_VERSION resource.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from pe_builder import build_pe
from Utils.wizard_support.pe_version import format_version, read_file_version


@pytest.mark.parametrize("pe32_plus", [True, False])
@pytest.mark.parametrize("version", [(1, 11, 240, 0), (1, 10, 163, 0), (0, 7, 9, 65535)])
def test_reads_version_from_pe32_and_pe32_plus(tmp_path, version, pe32_plus):
    exe = tmp_path / "a.exe"
    exe.write_bytes(build_pe(version, pe32_plus=pe32_plus))
    assert read_file_version(exe) == version


def test_format_version():
    assert format_version((1, 11, 240, 0)) == "1.11.240.0"


def test_pe_without_a_resource_directory_is_none(tmp_path):
    exe = tmp_path / "a.exe"
    exe.write_bytes(build_pe((1, 2, 3, 4), with_resources=False))
    assert read_file_version(exe) is None


def test_not_a_pe_is_none(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello " * 50)
    assert read_file_version(f) is None


def test_empty_and_truncated_and_missing_are_none(tmp_path):
    empty = tmp_path / "empty.exe"
    empty.write_bytes(b"")
    truncated = tmp_path / "trunc.exe"
    truncated.write_bytes(build_pe((1, 2, 3, 4))[:0x120])
    assert read_file_version(empty) is None
    assert read_file_version(truncated) is None
    assert read_file_version(tmp_path / "missing.exe") is None


def test_corrupt_signature_is_none(tmp_path):
    raw = bytearray(build_pe((1, 2, 3, 4)))
    i = raw.index(struct.pack("<I", 0xFEEF04BD))
    raw[i] ^= 0xFF
    exe = tmp_path / "a.exe"
    exe.write_bytes(bytes(raw))
    assert read_file_version(exe) is None


_REAL_FO4 = Path("/home/mrgeebee/games/steamapps/common/Fallout 4/Fallout4.exe")


@pytest.mark.skipif(not _REAL_FO4.is_file(), reason="Fallout 4 not installed here")
def test_real_fallout4_exe_reads_as_a_1_x_version():
    v = read_file_version(_REAL_FO4)
    assert v is not None and v[:2] in {(1, 10), (1, 11)}
