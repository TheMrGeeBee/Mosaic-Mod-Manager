"""
esp_records.py
Generic record/GRUP walker for Bethesda plugin files (Skyrim SE/LE .esp/.esm/.esl).

Only reads what `armor_records.py` (and future callers) need: top-level GRUPs
whose label matches a wanted record type (ARMO, ARMA, TXST, RACE, NPC_, ...),
the records inside them, and each record's subrecords as raw bytes. Does not
walk into worldspace/cell GRUPs (type != 0) or resolve FormIDs against
masters — callers that need cross-plugin identity should do that themselves.

Record header (24 bytes): sig(4s) size(u32) flags(u32) formid(u32)
                           revision(u32) version(u16) unknown(u16)
`size` is the byte length of the subrecord block that follows the header —
it does NOT include the 24-byte header itself.

GRUP header (24 bytes): "GRUP"(4s) group_size(u32) label(4s) group_type(u32)
                         stamp(u16) unknown(u16) version(u16) unknown(u16)
`group_size` DOES include this 24-byte header — the group's subrecord/record
payload is `group_size - 24` bytes.

Subrecord header: sig(4s) size(u16), data `size` bytes. A "XXXX" subrecord
means the *next* subrecord's real size is a u32 carried in XXXX's own 4-byte
payload (the next subrecord's own u16 size field is 0 and must be ignored).

A record whose flags bit 0x00040000 is set is stored compressed: its data
starts with a u32 "decompressed size" followed by a zlib stream for the rest.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

_REC_HDR = struct.Struct("<4sIII4x")     # sig, size, flags, formid (+4 unused)
_GRP_HDR = struct.Struct("<4sI4sI8x")    # "GRUP", group_size, label, group_type (+8 unused)
_SUB_HDR = struct.Struct("<4sH")
_COMPRESSED_FLAG = 0x00040000


class EspError(Exception):
    pass


@dataclass
class Record:
    sig: str
    formid: int
    subs: dict = field(default_factory=dict)   # subrecord sig -> list[bytes]

    def sub(self, sig: str) -> "bytes | None":
        """First occurrence of *sig*, or None."""
        vals = self.subs.get(sig)
        return vals[0] if vals else None

    def all_subs(self, sig: str) -> list:
        return self.subs.get(sig, [])


def _split_subrecords(block: bytes) -> dict:
    """A record's raw subrecord block -> {sig: [payload, ...]}, honouring XXXX
    size overrides. Malformed trailing bytes are ignored (best-effort, matches
    how the game engine itself tolerates trailing junk in some tools' output)."""
    out: dict = {}
    pos = 0
    n = len(block)
    pending_size: "int | None" = None
    while pos + 6 <= n:
        sig, size = _SUB_HDR.unpack_from(block, pos)
        pos += 6
        if pending_size is not None:
            size = pending_size
            pending_size = None
        if pos + size > n:
            break
        sig_s = sig.decode("ascii", errors="replace")
        if sig_s == "XXXX":
            if size == 4:
                pending_size = struct.unpack_from("<I", block, pos)[0]
            pos += size
            continue
        out.setdefault(sig_s, []).append(block[pos:pos + size])
        pos += size
    return out


def _read_record(buf: bytes, pos: int) -> "tuple[Record | None, int]":
    """One record starting at *pos* -> (Record or None on decompress failure, new pos)."""
    sig, size, flags, formid = _REC_HDR.unpack_from(buf, pos)
    body_start = pos + 24
    body = buf[body_start:body_start + size]
    new_pos = body_start + size
    if flags & _COMPRESSED_FLAG:
        if len(body) < 4:
            return None, new_pos
        try:
            body = zlib.decompress(body[4:])
        except zlib.error:
            return None, new_pos
    rec = Record(sig.decode("ascii", errors="replace"), formid, _split_subrecords(body))
    return rec, new_pos


def iter_records(data: bytes, wanted_sigs: "set[str]"):
    """Yield every Record in *data* whose signature is in *wanted_sigs*, from
    the file's top-level GRUPs (group_type 0) only. Nested groups (cells,
    worldspaces, ...) are skipped entirely without being parsed, since none
    of the record types this reader targets live inside them."""
    n = len(data)
    if n < 24 or data[:4] != b"TES4":
        raise EspError("not a TES4 plugin")
    _, tes4_size, _, _ = _REC_HDR.unpack_from(data, 0)
    pos = 24 + tes4_size
    while pos + 24 <= n:
        sig4 = data[pos:pos + 4]
        if sig4 != b"GRUP":
            break                                  # anything else at top level is unexpected; stop cleanly
        _, group_size, label, group_type = _GRP_HDR.unpack_from(data, pos)
        group_end = pos + group_size
        if group_size < 24 or group_end > n:
            break
        label_s = label.decode("ascii", errors="replace")
        if group_type == 0 and label_s in wanted_sigs:
            p = pos + 24
            while p + 24 <= group_end:
                if data[p:p + 4] == b"GRUP":
                    _, sub_gsize, *_ = _GRP_HDR.unpack_from(data, p)
                    p += max(sub_gsize, 24)
                    continue
                rec, p = _read_record(data, p)
                if rec is not None and rec.sig in wanted_sigs:
                    yield rec
        pos = group_end


def read_records(plugin_path: Path, wanted_sigs: "set[str]") -> list:
    """Read *plugin_path* and return every wanted record as a list (empty on
    any I/O or format error rather than raising, matching plugin_parser.py's
    existing best-effort convention)."""
    try:
        data = plugin_path.read_bytes()
    except OSError:
        return []
    try:
        return list(iter_records(data, wanted_sigs))
    except (EspError, struct.error):
        return []
