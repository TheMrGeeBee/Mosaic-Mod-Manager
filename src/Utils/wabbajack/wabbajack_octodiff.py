"""Applies an Octodiff delta, the patch format Wabbajack uses for
``PatchedFromArchive`` (real modlists ship ``OCTODELTA`` patches, not
bsdiff -- seen in Horizon 1.4.4's Better Console patch, 2026-10-07).

Layout (little-endian, as written by Octodiff's ``BinaryDeltaWriter``)::

    "OCTODELTA"                 9-byte magic
    version                     1 byte (0x01)
    hash algorithm name         .NET string: 7-bit-encoded length + UTF-8 ("SHA1")
    hash length                 int32
    expected output hash        <hash length> bytes, hash of the *patched* file
    ">>>"                       end of metadata
    commands, until EOF:
      0x60 offset:int64 length:int64   copy bytes from the source file
      0x80 length:int64 <bytes>        literal data

The header layout and the 0x80 command were confirmed by decoding a real
patch; the 0x60 copy command follows Octodiff's writer.
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path

MAGIC = b"OCTODELTA"
_END_OF_METADATA = b">>>"
_COPY = 0x60
_DATA = 0x80
_CHUNK = 1 << 20


class OctodiffError(Exception):
    """The patch is malformed, or applying it didn't produce the file it
    describes."""


def is_octodiff(head: bytes) -> bool:
    return head.startswith(MAGIC)


def _read_exact(fh, n: int) -> bytes:
    data = fh.read(n)
    if len(data) != n:
        raise OctodiffError("patch is truncated")
    return data


def _read_dotnet_string(fh) -> str:
    length, shift = 0, 0
    while True:
        byte = _read_exact(fh, 1)[0]
        length |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
        if shift > 28:
            raise OctodiffError("bad string length in patch header")
    return _read_exact(fh, length).decode("utf-8")


def _hasher(name: str):
    try:
        return hashlib.new(name.replace("-", "").lower())
    except ValueError as exc:
        raise OctodiffError(f"unsupported patch hash algorithm {name!r}") from exc


def apply_octodiff(source: "str | Path", patch: "str | Path", dest: "str | Path") -> None:
    """Write ``dest`` = ``source`` patched by the Octodiff delta ``patch``,
    streaming, and check it against the hash recorded in the patch. On any
    failure ``dest`` is removed and :class:`OctodiffError` raised."""
    dest = Path(dest)
    try:
        with open(patch, "rb") as pf, open(source, "rb") as sf, open(dest, "wb") as out:
            if _read_exact(pf, len(MAGIC)) != MAGIC:
                raise OctodiffError("not an Octodiff patch")
            _read_exact(pf, 1)  # format version
            hasher = _hasher(_read_dotnet_string(pf))
            (hash_len,) = struct.unpack("<i", _read_exact(pf, 4))
            if not 0 < hash_len <= 64:
                raise OctodiffError("bad hash length in patch header")
            expected = _read_exact(pf, hash_len)
            if _read_exact(pf, len(_END_OF_METADATA)) != _END_OF_METADATA:
                raise OctodiffError("patch header isn't terminated")

            def emit(data: bytes) -> None:
                out.write(data)
                hasher.update(data)

            while True:
                cmd = pf.read(1)
                if not cmd:
                    break
                if cmd[0] == _COPY:
                    offset, length = struct.unpack("<qq", _read_exact(pf, 16))
                    if offset < 0 or length < 0:
                        raise OctodiffError("negative copy range in patch")
                    sf.seek(offset)
                    while length:
                        chunk = sf.read(min(_CHUNK, length))
                        if not chunk:
                            raise OctodiffError("patch copies past the end of the source file")
                        emit(chunk)
                        length -= len(chunk)
                elif cmd[0] == _DATA:
                    (length,) = struct.unpack("<q", _read_exact(pf, 8))
                    if length < 0:
                        raise OctodiffError("negative data length in patch")
                    while length:
                        chunk = _read_exact(pf, min(_CHUNK, length))
                        emit(chunk)
                        length -= len(chunk)
                else:
                    raise OctodiffError(f"unknown patch command 0x{cmd[0]:02x}")
        if hasher.digest() != expected:
            raise OctodiffError("patched file doesn't match the hash in the patch")
    except (OctodiffError, OSError, struct.error) as exc:
        dest.unlink(missing_ok=True)
        if isinstance(exc, OctodiffError):
            raise
        raise OctodiffError(str(exc)) from exc
