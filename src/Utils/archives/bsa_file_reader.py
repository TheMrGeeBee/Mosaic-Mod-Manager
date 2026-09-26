"""Random access to single files inside a Bethesda BSA (v104 / v105).

``extract_bsa`` unpacks a whole archive; the asset viewers only ever want one
mesh or texture at a time, out of archives holding tens of thousands of files.
``BsaFile`` reads the table of contents once and then decompresses individual
files on demand.

v104 (Oblivion, Skyrim LE, Fallout 3/NV) stores files zlib-compressed; v105
(Skyrim SE) uses LZ4 frames. Paths are lowercase with forward slashes.
"""

from __future__ import annotations

import struct
import threading
import zlib
from pathlib import Path

import lz4.frame

_AF_HAS_DIR_NAMES = 0x1
_AF_HAS_FILE_NAMES = 0x2
_AF_COMPRESSED_DEF = 0x4
_AF_EMBED_FILE_NAMES = 0x100
_FILE_COMPRESS_INVERT = 0x40000000
_FILE_SIZE_MASK = 0x3FFFFFFF


class BsaReadError(Exception):
    """The archive is unreadable or the requested file isn't in it."""


class BsaFile:
    """An open BSA with its file table loaded.

        with BsaFile(path) as bsa:
            for p in bsa.paths(): ...
            data = bsa.read("meshes/armor/iron/m/cuirass_0.nif")
    """

    def __init__(self, path: "Path | str"):
        self.path = Path(path)
        self._f = None
        self._files: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()      # one shared file handle → serialise reads
        self.version = 0
        self._flags = 0
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
        if f.read(4) != b"BSA\x00":
            raise BsaReadError(f"{self.path.name} is not a BSA archive")
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

    # -- access -------------------------------------------------------------------
    def paths(self) -> list[str]:
        return list(self._files)

    def __contains__(self, path: str) -> bool:
        return self._norm(path) in self._files

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
