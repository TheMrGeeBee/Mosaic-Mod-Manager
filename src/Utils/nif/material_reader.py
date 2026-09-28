"""Bethesda material files (.bgsm/.bgem) — Fallout 4 shapes can reference one
of these instead of (or as well as) embedding their own texture set directly,
so a mod can offer camo/colour variants without separate meshes: the mesh's
own BSShaderTextureSet is left mostly blank and the real diffuse/normal/
specular paths live in the material file (Materials\\...\\name.bgsm). Verified
byte-for-byte against 72 real .bgsm files and a real .bgem file from a live
mod (CROSS Collection, all version 1) — a real shape's own texture set had
only a normal map (or nothing at all) in it, with the actual colour texture
only reachable through its material.

Pure parser, no I/O — the caller resolves the material path through the
asset catalog the same way it resolves any other file (see
Utils.nif.character/asset_loader), matching nif_reader's own convention of
never reading a second file itself.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from Utils.nif.nif_reader import normalize_texture_path

# magic(4) + version(4) + a fixed block of blend-mode/alpha/flag fields(55)
# that this reader has no need to interpret — verified against real files:
# the first texture string always starts at this exact offset for version 1.
_HEADER_LEN = 63
_MAX_STRING = 1024


@dataclass(frozen=True)
class MaterialTextures:
    diffuse: str = ""
    normal: str = ""
    specular: str = ""


def _read_pstr(data: bytes, cursor: int) -> "tuple[str, int]":
    """A length-prefixed string whose length INCLUDES a trailing NUL byte —
    verified: the length prefix for "CROSS\\coa\\coa\\coa_02_d.dds" (26 chars)
    is 27, and byte 27 of the string data is the NUL."""
    n = struct.unpack_from("<I", data, cursor)[0]
    cursor += 4
    if n < 0 or n > _MAX_STRING or cursor + n > len(data):
        raise struct.error("bad material string length")
    raw = data[cursor:cursor + n]
    return normalize_texture_path(raw.rstrip(b"\x00").decode("utf-8", errors="replace")), cursor + n


def read_material(data: bytes) -> "MaterialTextures | None":
    """The texture paths a .bgsm/.bgem file's shapes should use, or None if
    *data* isn't a recognised material file (wrong magic, an unverified
    version, or too short) — never raises. A BGEM's second texture slot
    ("Greyscale") isn't diffuse/normal/specular in the same sense a BGSM's
    is, so only its diffuse is read; nothing here has needed a BGEM's other
    fields yet."""
    if len(data) < _HEADER_LEN or data[:4] not in (b"BGSM", b"BGEM"):
        return None
    version = struct.unpack_from("<I", data, 4)[0]
    if version not in (1, 2):
        return None                      # only versions 1 (mods) and 2 (the base game
                                          # itself, verified on 2,261 real files) checked so far
    try:
        cursor = _HEADER_LEN
        diffuse, cursor = _read_pstr(data, cursor)
        if data[:4] == b"BGSM":
            normal, cursor = _read_pstr(data, cursor)
            specular, cursor = _read_pstr(data, cursor)
            return MaterialTextures(diffuse=diffuse, normal=normal, specular=specular)
        return MaterialTextures(diffuse=diffuse)
    except (struct.error, IndexError):
        return None
