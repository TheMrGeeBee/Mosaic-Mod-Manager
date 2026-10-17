"""The DDS header Wabbajack puts in front of a texture it reads out of a BA2.

A DX10 BA2 stores only the texture's pixel data and a few fields (size, mip
count, DXGI format, cubemap flag); whoever extracts it writes a DDS header.
Wabbajack hashes -- and builds its binary patches against -- the file *with
its own header*, so a patch whose source is a BA2 texture only applies if
Mosaic writes the very same bytes. Mosaic's general BA2 extractor
(``Utils.archives.ba2_extract._make_dds_header``) always writes a DX10
extension header with the first chunk's size as the linear size; Wabbajack
writes a legacy header (``DXT1``/``DXT5``/``BC5U``/...) for every format
that has one, the top mip's size as the linear size, and depth 1. Real case:
Wasteland Genesis AE, 230 textures failed to patch (all multi-chunk BC5 and
BC3 sources) until this matched.

This follows Wabbajack's ``DX10Entry.WriteHeader`` →
``DirectXTexUtility.GenerateDDSHeader(metadata, FORCEDX10EXTMISC2)``
(wabbajack-tools/wabbajack, GPL-3.0, files
``Wabbajack.Compression.BSA/BA2Archive/DX10Entry.cs`` and
``Wabbajack.Compression.BSA/DirectXTexUtil.cs``), including its quirks: a
cubemap with a legacy header gets no cubemap caps, and ``FORCEDX10EXTMISC2``
does not force the DX10 header for formats that have a legacy one.
"""
from __future__ import annotations

import struct

_FOURCC = 0x4
_RGB = 0x40
_RGBA = 0x41
_LUMINANCE = 0x20000
_LUMINANCE_A = 0x20001
_ALPHA = 0x2
_BUMPDUDV = 0x80000


def _cc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "little")


# DXGI format -> (flags, fourCC, rgb bit count, r, g, b, a masks), from
# DirectXTexUtil.GetPixelFormat. Formats not listed get the DX10 header.
_LEGACY: "dict[int, tuple[int, int, int, int, int, int, int]]" = {
    28: (_RGBA, 0, 32, 0x000000ff, 0x0000ff00, 0x00ff0000, 0xff000000),   # R8G8B8A8_UNORM
    35: (_RGB, 0, 32, 0x0000ffff, 0xffff0000, 0, 0),                      # R16G16_UNORM
    49: (_LUMINANCE_A, 0, 16, 0x00ff, 0, 0, 0xff00),                      # R8G8_UNORM
    56: (_LUMINANCE, 0, 16, 0xffff, 0, 0, 0),                             # R16_UNORM
    61: (_LUMINANCE, 0, 8, 0xff, 0, 0, 0),                                # R8_UNORM
    65: (_ALPHA, 0, 8, 0, 0, 0, 0xff),                                    # A8_UNORM
    68: (_FOURCC, _cc("RGBG"), 0, 0, 0, 0, 0),                            # R8G8_B8G8_UNORM
    69: (_FOURCC, _cc("GRGB"), 0, 0, 0, 0, 0),                            # G8R8_G8B8_UNORM
    71: (_FOURCC, _cc("DXT1"), 0, 0, 0, 0, 0),                            # BC1_UNORM
    74: (_FOURCC, _cc("DXT3"), 0, 0, 0, 0, 0),                            # BC2_UNORM
    77: (_FOURCC, _cc("DXT5"), 0, 0, 0, 0, 0),                            # BC3_UNORM
    80: (_FOURCC, _cc("BC4U"), 0, 0, 0, 0, 0),                            # BC4_UNORM
    81: (_FOURCC, _cc("BC4S"), 0, 0, 0, 0, 0),                            # BC4_SNORM
    83: (_FOURCC, _cc("BC5U"), 0, 0, 0, 0, 0),                            # BC5_UNORM
    84: (_FOURCC, _cc("BC5S"), 0, 0, 0, 0, 0),                            # BC5_SNORM
    85: (_RGB, 0, 16, 0xf800, 0x07e0, 0x001f, 0),                         # B5G6R5_UNORM
    86: (_RGBA, 0, 16, 0x7c00, 0x03e0, 0x001f, 0x8000),                   # B5G5R5A1_UNORM
    51: (_BUMPDUDV, 0, 16, 0x00ff, 0xff00, 0, 0),                         # R8G8_SNORM
    31: (_BUMPDUDV, 0, 32, 0x000000ff, 0x0000ff00, 0x00ff0000, 0xff000000),  # R8G8B8A8_SNORM
    37: (_BUMPDUDV, 0, 32, 0x0000ffff, 0xffff0000, 0, 0),                 # R16G16_SNORM
    87: (_RGBA, 0, 32, 0x00ff0000, 0x0000ff00, 0x000000ff, 0xff000000),   # B8G8R8A8_UNORM
    88: (_RGB, 0, 32, 0x00ff0000, 0x0000ff00, 0x000000ff, 0),             # B8G8R8X8_UNORM
    115: (_RGBA, 0, 16, 0x0f00, 0x00f0, 0x000f, 0xf000),                  # B4G4R4A4_UNORM
    107: (_FOURCC, _cc("YUY2"), 0, 0, 0, 0, 0),                           # YUY2
    # Legacy D3DX formats: the D3DFMT number as the fourCC.
    2: (_FOURCC, 116, 0, 0, 0, 0, 0),    # R32G32B32A32_FLOAT
    10: (_FOURCC, 113, 0, 0, 0, 0, 0),   # R16G16B16A16_FLOAT
    11: (_FOURCC, 36, 0, 0, 0, 0, 0),    # R16G16B16A16_UNORM
    13: (_FOURCC, 110, 0, 0, 0, 0, 0),   # R16G16B16A16_SNORM
    16: (_FOURCC, 115, 0, 0, 0, 0, 0),   # R32G32_FLOAT
    34: (_FOURCC, 112, 0, 0, 0, 0, 0),   # R16G16_FLOAT
    41: (_FOURCC, 114, 0, 0, 0, 0, 0),   # R32_FLOAT
    54: (_FOURCC, 111, 0, 0, 0, 0, 0),   # R16_FLOAT
}

_BLOCK8 = set(range(70, 73)) | set(range(79, 82))                     # BC1, BC4
_BLOCK16 = (set(range(73, 79)) | set(range(82, 85))                   # BC2, BC3, BC5
            | set(range(94, 97)) | set(range(97, 100)))               # BC6H, BC7


def _bits_per_pixel(fmt: int) -> int:
    if 1 <= fmt <= 4:
        return 128
    if 5 <= fmt <= 8:
        return 96
    if 9 <= fmt <= 22:
        return 64
    if 48 <= fmt <= 59 or fmt in (85, 86, 115):
        return 16
    if 60 <= fmt <= 65:
        return 8
    if fmt == 66:
        return 1
    return 32   # 23-47, 67, 87-93: the 32-bit formats


def _pitch(fmt: int, width: int, height: int) -> "tuple[int, int]":
    """(row pitch, slice pitch) as DirectXTexUtil.ComputePitch with no flags."""
    if fmt in _BLOCK8 or fmt in _BLOCK16:
        row = max(1, (width + 3) // 4) * (8 if fmt in _BLOCK8 else 16)
        return row, row * max(1, (height + 3) // 4)
    if fmt in (68, 69, 107):
        row = ((width + 1) >> 1) * 4
        return row, row * height
    row = (width * _bits_per_pixel(fmt) + 7) // 8
    return row, row * height


def wabbajack_dds_header(*, height: int, width: int, mip_count: int,
                         dxgi_format: int, is_cubemap: bool = False) -> bytes:
    """``DDS `` magic + DDS_HEADER (+ DDS_HEADER_DXT10 when the format has no
    legacy pixel format), byte for byte as Wabbajack writes them."""
    flags = 0x1007                                  # CAPS|HEIGHT|WIDTH|PIXELFORMAT
    caps = 0x1000                                   # TEXTURE
    if mip_count > 0:
        flags |= 0x20000                            # MIPMAPCOUNT
        if mip_count > 1:
            caps |= 0x400008                        # COMPLEX|MIPMAP
    row, slice_ = _pitch(dxgi_format, width, height)
    compressed = dxgi_format in _BLOCK8 or dxgi_format in _BLOCK16
    flags |= 0x80000 if compressed else 0x8         # LINEARSIZE / PITCH
    legacy = _LEGACY.get(dxgi_format)
    pf = legacy if legacy is not None else (_FOURCC, _cc("DX10"), 0, 0, 0, 0, 0)
    header = struct.pack("<4s7I11I8I5I", b"DDS ",
                         124, flags, height, width, slice_ if compressed else row,
                         1, mip_count, *([0] * 11),
                         32, *pf,
                         caps, 0, 0, 0, 0)
    if legacy is not None:
        return header
    return header + struct.pack("<5I", dxgi_format, 3, 4 if is_cubemap else 0, 1, 0)
