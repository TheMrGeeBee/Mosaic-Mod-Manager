"""Generic ESP/ESM record reader: GRUP/record/subrecord parsing, XXXX size
overrides, compressed records. Byte layout confirmed against a real Skyrim.esm
(TES4 header -> GRUP("GMST") -> GMST record) while writing this."""
from __future__ import annotations

import struct
import zlib

import pytest

from Utils.plugins.esp_records import EspError, iter_records, read_records

_COMPRESSED = 0x00040000


def _sub(sig: str, data: bytes) -> bytes:
    return struct.pack("<4sH", sig.encode(), len(data)) + data


def _rec(sig: str, formid: int, subs: bytes, flags: int = 0) -> bytes:
    body = subs
    if flags & _COMPRESSED:
        body = struct.pack("<I", len(subs)) + zlib.compress(subs)
    return struct.pack("<4sIII8x", sig.encode(), len(body), flags, formid) + body


def _grup(label: str, records: bytes, group_type: int = 0) -> bytes:
    header_size = 24
    return (struct.pack("<4sI4sI8x", b"GRUP", header_size + len(records),
                        label.encode(), group_type) + records)


def _plugin(groups: bytes) -> bytes:
    tes4 = _rec("TES4", 0, b"")
    return tes4 + groups


def test_reads_records_from_a_wanted_top_level_group():
    rec1 = _rec("ARMA", 0x800, _sub("EDID", b"Foo\0"))
    rec2 = _rec("ARMA", 0x801, _sub("EDID", b"Bar\0"))
    data = _plugin(_grup("ARMA", rec1 + rec2))
    got = list(iter_records(data, {"ARMA"}))
    assert [(r.formid, r.sub("EDID")) for r in got] == [(0x800, b"Foo\0"), (0x801, b"Bar\0")]


def test_ignores_unwanted_groups_and_record_types():
    wanted = _rec("ARMA", 1, _sub("EDID", b"A\0"))
    data = _plugin(_grup("ARMA", wanted) + _grup("TXST", _rec("TXST", 2, b"")))
    got = list(iter_records(data, {"ARMA"}))
    assert len(got) == 1 and got[0].formid == 1


def test_skips_nested_non_top_groups_without_parsing_them():
    # A group nested inside a wanted top-level group (group_type != 0, e.g. a
    # persistent-cell-shaped GRUP) full of junk that would not parse as a
    # record — must be skipped wholesale by its own size, not walked into.
    junk = b"\xff" * 40
    nested = _grup("JUNK", junk, group_type=6)
    wanted = _rec("ARMA", 5, _sub("EDID", b"X\0"))
    data = _plugin(_grup("ARMA", nested + wanted))
    got = list(iter_records(data, {"ARMA"}))
    assert [r.formid for r in got] == [5]


def test_xxxx_overrides_the_next_subrecords_size():
    big = b"x" * 400                                    # bigger than a u16 can't express anyway,
    payload = _sub("XXXX", struct.pack("<I", len(big))) + struct.pack("<4sH", b"DATA", 0) + big
    data = _plugin(_grup("ARMA", _rec("ARMA", 1, payload)))
    got = list(iter_records(data, {"ARMA"}))
    assert got[0].sub("DATA") == big


def test_compressed_record_is_transparently_decompressed():
    subs = _sub("EDID", b"Compressed\0")
    data = _plugin(_grup("ARMA", _rec("ARMA", 9, subs, flags=_COMPRESSED)))
    got = list(iter_records(data, {"ARMA"}))
    assert got[0].sub("EDID") == b"Compressed\0"


def test_read_records_is_best_effort_on_bad_input(tmp_path):
    assert read_records(tmp_path / "missing.esp", {"ARMA"}) == []
    bad = tmp_path / "bad.esp"
    bad.write_bytes(b"not a plugin at all")
    assert read_records(bad, {"ARMA"}) == []
    with pytest.raises(EspError):
        list(iter_records(b"nope", {"ARMA"}))
