"""Read the headline facts of a DDS texture from its header alone.

Pillow decodes the pixels but doesn't expose the mip count or the block-
compression format name, which are exactly what a modder comparing two texture
mods wants to see (BC7 vs BC1, full mip chain or none). The header is a fixed
128 bytes, plus a 20-byte DX10 extension when the FourCC is "DX10".
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

_MAGIC = b"DDS "
_HEADER_LEN = 128
_DX10_LEN = 20

_DDSD_MIPMAPCOUNT = 0x20000
_DDPF_ALPHAPIXELS = 0x1
_DDPF_ALPHA = 0x2
_DDPF_FOURCC = 0x4
_DDPF_RGB = 0x40
_DDPF_LUMINANCE = 0x20000
_DDSCAPS2_CUBEMAP = 0x200
_DDSCAPS2_VOLUME = 0x200000

_FOURCC_NAMES = {
    b"DXT1": "BC1 (DXT1)",
    b"DXT2": "BC2 (DXT2)",
    b"DXT3": "BC2 (DXT3)",
    b"DXT4": "BC3 (DXT4)",
    b"DXT5": "BC3 (DXT5)",
    b"ATI1": "BC4",
    b"BC4U": "BC4",
    b"BC4S": "BC4 (signed)",
    b"ATI2": "BC5",
    b"BC5U": "BC5",
    b"BC5S": "BC5 (signed)",
}

# DXGI_FORMAT values that turn up in game textures.
_DXGI_NAMES = {
    2: "RGBA32 float",
    10: "RGBA16 float",
    24: "RGB10A2",
    28: "RGBA8",
    29: "RGBA8 (sRGB)",
    56: "R16",
    61: "R8",
    65: "A8",
    71: "BC1 (DXT1)",
    72: "BC1 (DXT1, sRGB)",
    74: "BC2 (DXT3)",
    75: "BC2 (DXT3, sRGB)",
    77: "BC3 (DXT5)",
    78: "BC3 (DXT5, sRGB)",
    80: "BC4",
    81: "BC4 (signed)",
    83: "BC5",
    84: "BC5 (signed)",
    87: "BGRA8",
    88: "BGRX8",
    91: "BGRA8 (sRGB)",
    93: "BGRX8 (sRGB)",
    95: "BC6H (unsigned)",
    96: "BC6H (signed)",
    98: "BC7",
    99: "BC7 (sRGB)",
}


@dataclass(frozen=True)
class DdsInfo:
    width: int
    height: int
    mip_count: int
    format: str
    size_bytes: int
    is_cubemap: bool = False
    is_volume: bool = False

    def summary(self) -> str:
        """One line for a status strip, e.g. "2048×2048 · BC7 · 12 mips · 5.3 MB"."""
        kind = " cubemap" if self.is_cubemap else (" volume" if self.is_volume else "")
        mips = f"{self.mip_count} mip{'s' if self.mip_count != 1 else ''}"
        return (f"{self.width}×{self.height}{kind} · {self.format} · {mips} · "
                f"{format_size(self.size_bytes)}")


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _uncompressed_name(flags: int, bits: int, a_mask: int) -> str:
    if flags & _DDPF_LUMINANCE:
        return f"L{bits}"
    if flags & _DDPF_ALPHA and not flags & _DDPF_RGB:
        return f"A{bits}"
    if flags & _DDPF_RGB:
        has_alpha = bool(flags & _DDPF_ALPHAPIXELS and a_mask)
        return f"{bits}-bit RGB{'A' if has_alpha else ''}"
    return f"{bits}-bit"


def read_dds_info(path: "Path | str") -> "DdsInfo | None":
    """Parse *path*'s DDS header. Returns None for a missing, truncated or
    non-DDS file — never raises."""
    try:
        p = Path(path)
        size = p.stat().st_size
        with p.open("rb") as f:
            head = f.read(_HEADER_LEN + _DX10_LEN)
    except OSError:
        return None
    if len(head) < _HEADER_LEN or head[:4] != _MAGIC:
        return None
    try:
        (hdr_size, flags, height, width, _pitch, _depth,
         mips) = struct.unpack_from("<7I", head, 4)
        if hdr_size != 124 or width <= 0 or height <= 0:
            return None
        pf_flags, fourcc = struct.unpack_from("<I4s", head, 80)
        bits, _r, _g, _b, a_mask = struct.unpack_from("<5I", head, 88)
        (caps2,) = struct.unpack_from("<I", head, 112)
    except struct.error:
        return None

    if pf_flags & _DDPF_FOURCC:
        if fourcc == b"DX10":
            if len(head) < _HEADER_LEN + _DX10_LEN:
                return None
            (dxgi,) = struct.unpack_from("<I", head, _HEADER_LEN)
            name = _DXGI_NAMES.get(dxgi, f"DXGI {dxgi}")
        else:
            name = _FOURCC_NAMES.get(
                fourcc, "FourCC " + fourcc.decode("latin-1", "replace"))
    else:
        name = _uncompressed_name(pf_flags, bits, a_mask)

    mip_count = mips if (flags & _DDSD_MIPMAPCOUNT and mips > 0) else 1
    return DdsInfo(
        width=width, height=height, mip_count=mip_count, format=name,
        size_bytes=size,
        is_cubemap=bool(caps2 & _DDSCAPS2_CUBEMAP),
        is_volume=bool(caps2 & _DDSCAPS2_VOLUME),
    )
