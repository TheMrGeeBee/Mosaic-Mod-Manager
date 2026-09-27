"""Random access to single files inside a Bethesda BSA (v104 / v105) or BA2
(Fallout 4 / FO4 VR / Fallout 76).

``extract_bsa``/``extract_ba2`` unpack a whole archive; the asset viewers only
ever want one mesh or texture at a time, out of archives holding tens of
thousands of files. ``BsaFile`` reads the table of contents once (dispatching
on the archive's magic bytes) and then decompresses individual files on
demand, whichever format the archive turns out to be.

BSA v104 (Oblivion, Skyrim LE, Fallout 3/NV) stores files zlib-compressed;
v105 (Skyrim SE) uses LZ4 frames. BA2 (Fallout 4+) has two record shapes —
GNRL (general files, zlib) and DX10 (textures, one zlib chunk per mip,
reassembled into a standalone .dds by _read_dx10). Paths are lowercase with
forward slashes in both formats.
"""

from __future__ import annotations

import struct
import threading
import zlib
from pathlib import Path

import lz4.frame

from Utils.archives.ba2_extract import Ba2ExtractError, _make_dds_header

_AF_HAS_DIR_NAMES = 0x1
_AF_HAS_FILE_NAMES = 0x2
_AF_COMPRESSED_DEF = 0x4
_AF_EMBED_FILE_NAMES = 0x100
_FILE_COMPRESS_INVERT = 0x40000000
_FILE_SIZE_MASK = 0x3FFFFFFF


class BsaReadError(Exception):
    """The archive is unreadable or the requested file isn't in it."""


class BsaFile:
    """An open BSA or BA2 with its file table loaded.

        with BsaFile(path) as bsa:
            for p in bsa.paths(): ...
            data = bsa.read("meshes/armor/iron/m/cuirass_0.nif")
    """

    def __init__(self, path: "Path | str"):
        self.path = Path(path)
        self._f = None
        self._files: dict[str, tuple[int, int]] = {}   # BSA only
        self._records: dict[str, dict] = {}             # BA2 only
        self._lock = threading.Lock()      # one shared file handle → serialise reads
        self.version = 0
        self._flags = 0
        self._kind = ""                     # "bsa" or "ba2", set by _load_toc
        self._ba2_type = ""                 # "GNRL" or "DX10", BA2 only
        try:
            self._f = self.path.open("rb")
            self._load_toc()
        except (OSError, struct.error) as exc:
            self.close()
            raise BsaReadError(f"cannot read {self.path.name}: {exc}") from exc
        except BsaReadError:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._f is not None:
            self._f.close()
            self._f = None

    # -- table of contents ------------------------------------------------------
    def _load_toc(self):
        f = self._f
        magic = f.read(4)
        if magic == b"BSA\x00":
            self._kind = "bsa"
            self._load_toc_bsa()
        elif magic == b"BTDX":
            self._kind = "ba2"
            self._load_toc_ba2()
        else:
            raise BsaReadError(f"{self.path.name} is not a BSA or BA2 archive")

    def _load_toc_bsa(self):
        f = self._f
        (self.version, folder_off, self._flags, folder_count, file_count,
         _folder_names_len, file_names_len, _file_flags) = struct.unpack("<8I", f.read(32))
        if self.version not in (104, 105):
            raise BsaReadError(f"unsupported BSA version {self.version}")
        if not (self._flags & _AF_HAS_DIR_NAMES and self._flags & _AF_HAS_FILE_NAMES):
            raise BsaReadError("archive has no folder/file names")
        f.seek(folder_off)
        rec = 24 if self.version == 105 else 16
        raw = f.read(rec * folder_count)
        counts = [struct.unpack_from("<QI", raw, i * rec)[1] for i in range(folder_count)]
        folders: list[str] = []
        specs: list[tuple[int, int]] = []
        for c in counts:
            n = f.read(1)[0]
            folders.append(f.read(n).rstrip(b"\0").decode("latin-1")
                           .replace("\\", "/").lower())
            for _ in range(c):
                _h, size, off = struct.unpack("<QII", f.read(16))
                specs.append((size, off))
        names = f.read(file_names_len).decode("latin-1").lower().split("\0")
        if len(names) < file_count:
            raise BsaReadError("file-name block is shorter than the file count")
        i = 0
        for folder, c in zip(folders, counts):
            for _ in range(c):
                self._files[f"{folder}/{names[i]}" if folder else names[i]] = specs[i]
                i += 1

    def _load_toc_ba2(self):
        """Same record layout as ba2_extract._extract, minus the writing —
        see that module for the authoritative field-by-field reference
        (verified against real vanilla FO4 archives)."""
        f = self._f
        rest = f.read(20)
        if len(rest) < 20:
            raise BsaReadError(f"{self.path.name}: truncated BA2 header")
        self.version, type_tag, file_count, name_table_offset = struct.unpack("<I4sIQ", rest)
        if type_tag not in (b"GNRL", b"DX10"):
            raise BsaReadError(f"{self.path.name}: unsupported BA2 type {type_tag!r}")
        self._ba2_type = type_tag.decode("ascii")

        records: list[dict] = []
        if self._ba2_type == "GNRL":
            for _ in range(file_count):
                buf = f.read(36)
                if len(buf) < 36:
                    raise BsaReadError(f"{self.path.name}: truncated GNRL record")
                (_name_hash, _ext, _dir_hash, _flags, data_offset,
                 packed_size, unpacked_size, _end_marker) = struct.unpack("<I4sIIQIII", buf)
                records.append({"data_offset": data_offset, "packed_size": packed_size,
                                "unpacked_size": unpacked_size})
        else:  # DX10
            for _ in range(file_count):
                hdr = f.read(24)
                if len(hdr) < 24:
                    raise BsaReadError(f"{self.path.name}: truncated DX10 record header")
                (_name_hash, _ext, _dir_hash, _unk1, num_chunks, _chunk_size,
                 height, width, num_mips, dxgi_format,
                 _unk16) = struct.unpack("<I4sIBBHHHBBH", hdr)
                chunks = []
                for _c in range(num_chunks):
                    cb = f.read(24)
                    if len(cb) < 24:
                        raise BsaReadError(f"{self.path.name}: truncated DX10 chunk header")
                    (data_offset, packed_size, unpacked_size,
                     _start_mip, _end_mip, _end_marker) = struct.unpack("<QIIHHI", cb)
                    chunks.append({"data_offset": data_offset, "packed_size": packed_size,
                                   "unpacked_size": unpacked_size})
                records.append({"height": height, "width": width, "num_mips": num_mips,
                                "dxgi_format": dxgi_format, "chunks": chunks})

        f.seek(name_table_offset)
        names: list[str] = []
        for _ in range(file_count):
            ln_raw = f.read(2)
            if len(ln_raw) < 2:
                raise BsaReadError(f"{self.path.name}: truncated BA2 name table")
            ln = struct.unpack("<H", ln_raw)[0]
            nb = f.read(ln)
            if len(nb) < ln:
                raise BsaReadError(f"{self.path.name}: truncated BA2 name entry")
            names.append(nb.decode("latin-1").replace("\\", "/").lower())
        if len(names) != len(records):
            raise BsaReadError(f"{self.path.name}: name/record count mismatch")
        self._records = dict(zip(names, records))

    # -- access -------------------------------------------------------------------
    def paths(self) -> list[str]:
        return list(self._files) if self._kind == "bsa" else list(self._records)

    def __contains__(self, path: str) -> bool:
        key = self._norm(path)
        return key in (self._files if self._kind == "bsa" else self._records)

    @staticmethod
    def _norm(path: str) -> str:
        return path.replace("\\", "/").lstrip("/").lower()

    def read(self, path: str) -> bytes:
        """The decompressed bytes of *path*. Raises BsaReadError if absent/corrupt."""
        return self._read(path, None)

    def read_head(self, path: str, n: int = 128) -> bytes:
        """The first *n* decompressed bytes of *path* — for sniffing a file's
        type without decompressing all of it."""
        return self._read(path, n)

    def _read(self, path: str, limit: "int | None") -> bytes:
        if self._kind == "ba2":
            return self._read_ba2(path, limit)
        return self._read_bsa(path, limit)

    def _read_bsa(self, path: str, limit: "int | None") -> bytes:
        key = self._norm(path)
        spec = self._files.get(key)
        if spec is None:
            raise BsaReadError(f"{path} is not in {self.path.name}")
        size_field, offset = spec
        size = size_field & _FILE_SIZE_MASK
        compressed = bool(self._flags & _AF_COMPRESSED_DEF) ^ bool(
            size_field & _FILE_COMPRESS_INVERT)
        try:
            with self._lock:
                self._f.seek(offset)
                block = self._f.read(size)
            if len(block) < size:
                raise BsaReadError(f"short read for {path}")
            if self._flags & _AF_EMBED_FILE_NAMES and block:
                block = block[1 + block[0]:]
            if not compressed:
                return block if limit is None else block[:limit]
            body = block[4:]                          # 4-byte original-size prefix
            if self.version == 105:
                if limit is None:
                    return lz4.frame.decompress(body)
                return lz4.frame.LZ4FrameDecompressor().decompress(body, max_length=limit)
            if limit is None:
                return zlib.decompress(body)
            return zlib.decompressobj().decompress(body, limit)
        except (OSError, zlib.error, RuntimeError, ValueError) as exc:
            raise BsaReadError(f"cannot decompress {path}: {exc}") from exc

    def _read_ba2(self, path: str, limit: "int | None") -> bytes:
        key = self._norm(path)
        rec = self._records.get(key)
        if rec is None:
            raise BsaReadError(f"{path} is not in {self.path.name}")
        try:
            with self._lock:
                if self._ba2_type == "GNRL":
                    data = self._read_ba2_gnrl(rec, limit)
                else:
                    data = self._read_ba2_dx10(rec, limit)
        except (OSError, zlib.error, ValueError, Ba2ExtractError) as exc:
            raise BsaReadError(f"cannot decompress {path}: {exc}") from exc
        return data

    def _read_ba2_gnrl(self, rec: dict, limit: "int | None") -> bytes:
        f = self._f
        f.seek(rec["data_offset"])
        if rec["packed_size"] == 0:
            n = rec["unpacked_size"] if limit is None else min(limit, rec["unpacked_size"])
            return f.read(n)
        body = f.read(rec["packed_size"])
        if limit is None:
            return zlib.decompress(body)
        return zlib.decompressobj().decompress(body, limit)

    def _read_ba2_dx10(self, rec: dict, limit: "int | None") -> bytes:
        """DDS reassembly always needs every mip chunk (the header alone can't
        be sliced out) — *limit* only trims the final buffer, same as
        read_head() does for any other whole-file BSA read."""
        f = self._f
        payload_parts: list[bytes] = []
        first_chunk_unpacked = 0
        for i, chunk in enumerate(rec["chunks"]):
            f.seek(chunk["data_offset"])
            if chunk["packed_size"] == 0:
                data = f.read(chunk["unpacked_size"])
            else:
                data = zlib.decompress(f.read(chunk["packed_size"]))
            if i == 0:
                first_chunk_unpacked = len(data)
            payload_parts.append(data)
        header = _make_dds_header(
            height=rec["height"], width=rec["width"],
            mip_count=max(rec["num_mips"], 1), dxgi_format=rec["dxgi_format"],
            pitch_or_linear_size=first_chunk_unpacked)
        whole = header + b"".join(payload_parts)
        return whole if limit is None else whole[:limit]
