"""Minimal Skyrim reader (NIF 20.2.0.7: SE = BS version 100, LE = 83) for previewing meshes.

Not a general NIF library: it reads only what a viewer needs — the node tree
(transforms), geometry (SE: BSTriShape / BSDynamicTriShape / BSMeshLODTriShape;
LE: NiTriShape + NiTriShapeData), and the texture paths from
BSLightingShaderProperty + BSShaderTextureSet or BSEffectShaderProperty. Every block's byte size is in the file header, so the
hundreds of block types we don't understand (physics, animation, particles…)
are skipped without being parsed.

Skinned meshes are drawn in their bind pose (vertices as stored, no bone
transforms), which is the correct rest pose for armour and bodies.

Pure Python, no numpy: struct decodes half-floats natively.
"""

from __future__ import annotations

import math
import struct
from array import array
from dataclasses import dataclass, field

_HEADER_PREFIX = b"Gamebryo File Format, Version "
SKYRIM_SE_VERSION = 0x14020007
SKYRIM_SE_BSVER = 100
SKYRIM_LE_BSVER = 83
FALLOUT4_BSVER = 130
_SUPPORTED_BSVERS = (SKYRIM_LE_BSVER, SKYRIM_SE_BSVER, FALLOUT4_BSVER)

# BSVertexDesc attribute flags (bits 44+ of the 64-bit descriptor).
_VF_VERTEX = 1 << 44
_VF_UV = 1 << 45
_VF_UV2 = 1 << 46
_VF_NORMAL = 1 << 47
_VF_TANGENT = 1 << 48
_VF_COLOR = 1 << 49
_VF_SKINNED = 1 << 50
_VF_EYEDATA = 1 << 52
_VF_FULLPREC = 1 << 54

_NODE_TYPES = frozenset({
    "NiNode", "BSFadeNode", "BSMultiBoundNode", "BSOrderedNode", "BSValueNode",
    "BSTreeNode", "NiBillboardNode", "NiSwitchNode", "NiLODNode",
    "BSLeafAnimNode", "BSBlastNode", "BSDamageStage", "BSDebrisNode",
    "BSMasterParticleSystem", "NiBSAnimationNode",
})
_SHAPE_TYPES = frozenset({"BSTriShape", "BSDynamicTriShape", "BSMeshLODTriShape",
                          "BSLODTriShape", "NiTriShape", "BSSubIndexTriShape"})


class NifError(Exception):
    """The file isn't a NIF we can read."""


class NifUnsupported(NifError):
    """A valid NIF, but a game/version this reader doesn't handle."""

    def __init__(self, message: str, version: int = 0, bsver: int = 0):
        super().__init__(message)
        self.version = version
        self.bsver = bsver


SKYRIM_SE_FORMAT = (SKYRIM_SE_VERSION, SKYRIM_SE_BSVER)
SKYRIM_LE_FORMAT = (SKYRIM_SE_VERSION, SKYRIM_LE_BSVER)
FALLOUT4_FORMAT = (SKYRIM_SE_VERSION, FALLOUT4_BSVER)

_BS_GAMES = {100: "Skyrim SE", 83: "Skyrim LE", 34: "Fallout 3 / New Vegas",
             130: "Fallout 4", 155: "Fallout 76", 172: "Starfield", 11: "Oblivion"}


def version_string(version: int) -> str:
    return ".".join(str((version >> s) & 0xFF) for s in (24, 16, 8, 0))


def format_label(version: int, bsver: int) -> str:
    """Human name for a NIF's (version, BS version): "Skyrim LE", "Fallout 4"…"""
    if version >= 0x14000005 and bsver in _BS_GAMES:
        return _BS_GAMES[bsver]
    return f"NIF {version_string(version)}" + (f", BS {bsver}" if bsver else "")


def sniff_nif_format(head: bytes) -> "tuple[int, int] | None":
    """(version, BS version) from a file's first ~100 bytes, or None if it isn't
    a NIF. Cheap — for classifying files without parsing them."""
    if not head.startswith((b"Gamebryo File Format, Version ",
                            b"NetImmerse File Format, Version ")):
        return None
    nl = head.find(b"\n")
    if nl < 0:
        return None
    try:
        version, = struct.unpack_from("<I", head, nl + 1)
        if version < 0x14000003:                  # pre-20.0.0.3 layout has no BS header
            return (version, 0)
        _endian, user, _nblocks, user2 = struct.unpack_from("<BIII", head, nl + 5)
    except struct.error:
        return None
    return (version, user2 if user >= 10 else user)




@dataclass
class SkinData:
    """What a skinned shape needs to be re-posed on a skeleton: the shape's own
    vertices (local space), and per bone its name, its skin→bone transform
    (rotation row-major 3x3, scale, translation) and the vertices it weights."""
    bone_names: tuple
    bone_xf: tuple                     # per bone: (r9, scale, translation)
    weights: tuple                     # per bone: (array('H') vertex ids, array('f') weights)
    local_positions: array
    local_normals: "array | None"


@dataclass
class NifShape:
    name: str
    positions: array          # world-space xyz, flat
    normals: "array | None"   # world-space unit normals, flat, or None
    uvs: "array | None"       # uv, flat
    indices: array            # triangle list, unsigned short
    textures: list[str] = field(default_factory=list)   # slot order, normalised
    is_skinned: bool = False
    is_effect: bool = False   # BSEffectShaderProperty (glow, fx) — not a solid surface
    slots: frozenset = frozenset()   # body slots (32 body, 33 hands, 37 feet…) it covers
    # Per-partition split of `indices` (skinned shapes only): partition i covers
    # part_slots[i] and owns the next part_tris[i] triangles, in order.
    part_slots: tuple = ()
    part_tris: tuple = ()
    skin: "SkinData | None" = None
    # The external .bgsm/.bgem file (BSLightingShaderProperty's own Name
    # field) this shape's shader references, normalised like a texture path
    # but rooted where it actually lives — "materials/..." — or "" when the
    # shape has no material reference (most Skyrim content, and any FO4 shape
    # that embeds its own full texture set directly). A real Fallout 4 shape
    # can leave its embedded BSShaderTextureSet's diffuse slot blank and rely
    # entirely on this file for it (verified: real CROSS Collection armor
    # pieces) — see Utils.nif.material_reader, which resolves it through the
    # asset catalog since this module never reads a second file itself.
    material_name: str = ""


@dataclass
class NifNode:
    """A NiNode of the file's tree (bones, in a skeleton NIF)."""
    name: str
    parent: int                                  # index into NifScene.nodes, -1 for a root
    position: tuple[float, float, float]         # world space
    rotation: tuple = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)   # world, row-major 3x3
    scale: float = 1.0                                                  # world


@dataclass
class NifScene:
    shapes: list[NifShape]
    nodes: list[NifNode] = field(default_factory=list)   # only with read_nif(include_nodes=True)
    _sphere: object = field(default=None, repr=False, compare=False)

    def sample_points(self, limit: int = 6000) -> list[tuple[float, float, float]]:
        """Up to *limit* vertices spread evenly over all shapes (for view fitting)."""
        total = sum(len(sh.positions) // 3 for sh in self.shapes)
        step = max(1, total // limit)
        pts = []
        for sh in self.shapes:
            p = sh.positions
            for i in range(0, len(p) // 3, step):
                pts.append((p[3 * i], p[3 * i + 1], p[3 * i + 2]))
        return pts

    def bounding_sphere(self) -> "tuple[tuple[float, float, float], float] | None":
        """(centre, radius): centred on the bounding box, radius = the farthest
        vertex from it — tighter than half the box diagonal for round objects.
        Cached; None for a scene with no vertices."""
        if self._sphere is None:
            b = self.bounds()
            if b is None:
                return None
            lo, hi = b
            c = tuple((lo[i] + hi[i]) / 2 for i in range(3))
            r2 = 0.0
            for sh in self.shapes:
                p = sh.positions
                for i in range(0, len(p), 3):
                    d = (p[i] - c[0]) ** 2 + (p[i + 1] - c[1]) ** 2 + (p[i + 2] - c[2]) ** 2
                    if d > r2:
                        r2 = d
            self._sphere = (c, max(r2 ** 0.5, 1e-6))
        return self._sphere

    def bounds(self) -> "tuple[tuple[float, float, float], tuple[float, float, float]] | None":
        lo = [float("inf")] * 3
        hi = [float("-inf")] * 3
        any_v = False
        for sh in self.shapes:
            p = sh.positions
            for i in range(0, len(p), 3):
                any_v = True
                for a in range(3):
                    v = p[i + a]
                    if v < lo[a]:
                        lo[a] = v
                    if v > hi[a]:
                        hi[a] = v
        return (tuple(lo), tuple(hi)) if any_v else None


class _R:
    """Cursor over a bytes blob."""
    __slots__ = ("b", "p", "strings")

    def __init__(self, b: bytes, p: int = 0, strings: "list[str] | None" = None):
        self.b = b
        self.p = p
        self.strings = strings if strings is not None else []

    def u8(self):
        v = self.b[self.p]; self.p += 1; return v

    def u16(self):
        v, = struct.unpack_from("<H", self.b, self.p); self.p += 2; return v

    def u32(self):
        v, = struct.unpack_from("<I", self.b, self.p); self.p += 4; return v

    def i32(self):
        v, = struct.unpack_from("<i", self.b, self.p); self.p += 4; return v

    def u64(self):
        v, = struct.unpack_from("<Q", self.b, self.p); self.p += 8; return v

    def f32(self):
        v, = struct.unpack_from("<f", self.b, self.p); self.p += 4; return v

    def floats(self, n):
        v = struct.unpack_from(f"<{n}f", self.b, self.p); self.p += 4 * n; return v

    def skip(self, n):
        self.p += n

    def sized_str(self) -> str:
        n = self.u32()
        s = self.b[self.p:self.p + n].decode("latin-1")
        self.p += n
        return s

    def short_str(self) -> str:
        n = self.u8()
        s = self.b[self.p:self.p + n].rstrip(b"\0").decode("latin-1")
        self.p += n
        return s


# -- transforms: (R rows (9 floats), scale, translation) ---------------------
_IDENT = ((1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0), 1.0, (0.0, 0.0, 0.0))


def _compose(parent, local):
    """world = parent ∘ local (apply local first, then parent)."""
    pr, ps, pt = parent
    lr, ls, lt = local
    r = (
        pr[0] * lr[0] + pr[1] * lr[3] + pr[2] * lr[6],
        pr[0] * lr[1] + pr[1] * lr[4] + pr[2] * lr[7],
        pr[0] * lr[2] + pr[1] * lr[5] + pr[2] * lr[8],
        pr[3] * lr[0] + pr[4] * lr[3] + pr[5] * lr[6],
        pr[3] * lr[1] + pr[4] * lr[4] + pr[5] * lr[7],
        pr[3] * lr[2] + pr[4] * lr[5] + pr[5] * lr[8],
        pr[6] * lr[0] + pr[7] * lr[3] + pr[8] * lr[6],
        pr[6] * lr[1] + pr[7] * lr[4] + pr[8] * lr[7],
        pr[6] * lr[2] + pr[7] * lr[5] + pr[8] * lr[8],
    )
    x, y, z = (ps * lt[0], ps * lt[1], ps * lt[2])
    t = (pt[0] + pr[0] * x + pr[1] * y + pr[2] * z,
         pt[1] + pr[3] * x + pr[4] * y + pr[5] * z,
         pt[2] + pr[6] * x + pr[7] * y + pr[8] * z)
    return (r, ps * ls, t)


# Public alias: world = parent ∘ local for (rotation 3x3 row-major, scale, translation).
compose_transform = _compose


def _normalize_rooted_path(p: str, root: str) -> str:
    p = p.replace("\\", "/").strip().lower()
    while p.startswith("/"):
        p = p[1:]
    if not p:
        return ""
    return p if p.startswith(root) else root + p


def normalize_texture_path(p: str) -> str:
    """Lowercase, forward slashes, rooted at ``textures/`` ("" stays "")."""
    return _normalize_rooted_path(p, "textures/")


def normalize_material_path(p: str) -> str:
    """Lowercase, forward slashes, rooted at ``materials/`` ("" stays "") —
    a BSLightingShaderProperty's own Name field, when it references an
    external .bgsm/.bgem file (see Utils.nif.material_reader)."""
    return _normalize_rooted_path(p, "materials/")


# -- header ---------------------------------------------------------------------
@dataclass
class _Header:
    bsver: int
    nblocks: int
    types: list[str]
    block_type: list[int]
    sizes: list[int]
    strings: list[str]
    body_start: int


def _read_header(b: bytes) -> _Header:
    if not b.startswith(_HEADER_PREFIX):
        raise NifError("not a NIF (missing Gamebryo header)")
    try:
        nl = b.index(b"\n")
        r = _R(b, nl + 1)
        version = r.u32()
        endian = r.u8()
        if version != SKYRIM_SE_VERSION:
            raise NifUnsupported(
                f"NIF version {version_string(version)} (only Skyrim {version_string(SKYRIM_SE_VERSION)})",
                version)
        if endian != 1:
            raise NifUnsupported("big-endian NIF", version)
        r.u32()                          # user version
        nblocks = r.u32()
        bsver = r.u32()
        if bsver not in _SUPPORTED_BSVERS:
            raise NifUnsupported(
                f"BS version {bsver} (only Skyrim LE = 83, SE = 100, Fallout 4 = 130)", version, bsver)
        # BSStreamHeader (niftools/nifxml): Author always present; "Unknown Int"
        # only for BS > 130 (Fallout 76 etc.); "Process Script" only for BS < 131
        # (present for both Skyrim and FO4); "Export Script" always present;
        # "Max Filepath" only from BS >= 103 — the field Skyrim's header lacks
        # and Fallout 4's has, verified against a real FO4 mesh (a naive port of
        # the Skyrim-only 3-string layout misaligns everything after it).
        r.short_str()                                   # author
        if bsver > 130:
            r.u32()                                      # unknown int (Fallout 76+)
        if bsver < 131:
            r.short_str()                                 # process script
        r.short_str()                                    # export script
        if bsver >= 103:
            r.short_str()                                 # max filepath (Fallout 4+)
        ntypes = r.u16()
        types = [r.sized_str() for _ in range(ntypes)]
        idx = struct.unpack_from(f"<{nblocks}H", b, r.p); r.p += 2 * nblocks
        sizes = list(struct.unpack_from(f"<{nblocks}I", b, r.p)); r.p += 4 * nblocks
        nstrings = r.u32()
        r.u32()                          # max string length
        strings = [r.sized_str() for _ in range(nstrings)]
        ngroups = r.u32()
        r.skip(4 * ngroups)
    except (struct.error, IndexError, ValueError) as exc:
        raise NifError(f"truncated or corrupt NIF header: {exc}") from exc
    return _Header(bsver, nblocks, types, [i & 0x7FFF for i in idx], sizes, strings, r.p)


# -- block parsers ----------------------------------------------------------------
def _object_net(r: _R, strings: list[str]) -> str:
    name_i = r.i32()
    r.skip(4 * r.u32())                  # extra data refs
    r.i32()                              # controller
    return strings[name_i] if 0 <= name_i < len(strings) else ""


def _av_object(r: _R):
    r.u32()                              # flags
    t = r.floats(3)
    rot = r.floats(9)
    scale = r.f32()
    r.i32()                              # collision object
    return (rot, scale, t)


def _decode_vertices(b: bytes, base: int, nverts: int, vsize: int, desc: int, bsver: int = 0):
    """Decode packed BSVertexData → (positions|None, uvs|None, normals|None,
    bone_indices|None, bone_weights|None), flat. Positions are None when the
    descriptor has no position attribute (dynamic shapes keep them in a
    separate block); bone_indices/weights are None unless *bsver* is Fallout 4
    and the Skinned attribute bit is set.

    Skyrim SE positions are always 32-bit floats: the FULLPREC flag is only
    meaningful from BS version 130. Fallout 4 actually uses it — a body mesh
    verified while adding FO4 support stores half-float positions (FULLPREC
    unset) — so bsver >= 130 is the one case that must check the bit instead
    of assuming full precision.

    Fallout 4 also has no separate skin-weight block (Skyrim's NiSkinPartition):
    weights and bone indices for up to 4 bones are embedded per vertex, right
    after tangent data — verified byte-for-byte against a real FO4 mesh (the
    block's declared Data Size matched exactly with this offset scheme, and
    the weights extracted summed to 1.0 across many real vertices).
    Attribute offsets come from the descriptor's nibbles (in dwords)."""
    def off(attr: int) -> int:
        return ((desc >> (4 * attr + 4)) & 0xF) * 4

    has_pos = bool(desc & _VF_VERTEX)
    has_uv = bool(desc & _VF_UV)
    has_n = bool(desc & _VF_NORMAL)
    has_skin = bsver >= FALLOUT4_BSVER and bool(desc & _VF_SKINNED)
    full_pos = bsver < FALLOUT4_BSVER or bool(desc & _VF_FULLPREC)
    uv_off, n_off, skin_off = off(1), off(3), off(6)
    pos = array("f") if has_pos else None
    uvs = array("f") if has_uv else None
    nrm = array("f") if has_n else None
    bone_idx = array("B") if has_skin else None
    bone_w = array("f") if has_skin else None
    unpack_pos = (struct.Struct("<3f") if full_pos else struct.Struct("<3e")).unpack_from
    unpack_uv = struct.Struct("<2e").unpack_from
    unpack_bw = struct.Struct("<4e").unpack_from
    for i in range(nverts):
        o = base + i * vsize
        if has_pos:
            pos.extend(unpack_pos(b, o))
        if has_uv:
            uvs.extend(unpack_uv(b, o + uv_off))
        if has_n:
            nrm.extend((b[o + n_off] / 127.5 - 1.0, b[o + n_off + 1] / 127.5 - 1.0,
                        b[o + n_off + 2] / 127.5 - 1.0))
        if has_skin:
            bone_w.extend(unpack_bw(b, o + skin_off))
            bone_idx.extend(b[o + skin_off + 8:o + skin_off + 12])
    return pos, uvs, nrm, bone_idx, bone_w


def _read_skin_partition(r: _R):
    """NiSkinPartition (BS 100) → (positions, uvs, normals, indices) or None.

    Skinned SSE shapes keep no geometry of their own: the shared vertex array
    and the true (global-index) triangles of every dismember partition live
    here."""
    nparts = r.u32()
    data_size = r.u32()
    vsize = r.u32()
    desc = r.u64()
    if data_size == 0 or vsize == 0:
        return None
    nverts = data_size // vsize
    pos, uvs, nrm, _bi, _bw = _decode_vertices(r.b, r.p, nverts, vsize, desc)
    r.p += data_size
    tris = array("H")
    counts: list[int] = []
    for _ in range(nparts):
        nv, nt, nb, nstrips, nw = r.u16(), r.u16(), r.u16(), r.u16(), r.u16()
        r.skip(2 * nb)                               # bones
        if r.u8():
            r.skip(2 * nv)                           # vertex map
        if r.u8():
            r.skip(4 * nv * nw)                      # vertex weights
        lens = [r.u16() for _ in range(nstrips)]
        if r.u8():                                   # has faces
            r.skip(2 * sum(lens) if nstrips else 6 * nt)
        if r.u8():
            r.skip(nv * nw)                          # bone indices
        r.skip(2)                                    # LOD level + global VB
        r.skip(8)                                    # per-partition vertex desc
        chunk = array("H")
        chunk.frombytes(r.b[r.p:r.p + nt * 6])
        r.p += nt * 6
        tris.extend(chunk)
        counts.append(nt)
    return pos, uvs, nrm, tris, tuple(counts)


def _read_shape(r: _R, ptype: str, bsver: int = 0, block_end: "int | None" = None):
    """Parse a BSTriShape-family block into a dict of raw geometry.

    Fallout 4's Num Triangles field is a uint32 (Skyrim's is a uint16) and its
    BSSubIndexTriShape has no particle-data trailer — instead, once the
    vertex/triangle arrays end, its own dismemberment segment data follows
    (see _read_fo4_segments).

    *block_end* (the block's own byte-exact end, from the header's own size
    table — always trustworthy, that's the whole point of the table) bounds
    every count read from inside the block itself. A real malformed file
    (found via live testing on a large real mod collection: a 2-byte pad
    before the file's own root-list footer threw every offset after it off
    by 2) can otherwise misread ntris/nverts as huge-but-technically-in-range
    garbage that a plain equality check against Fallout 4's own leniently-
    trusted computed size doesn't catch — the vertex-decode loop then runs
    for a very long time (not infinite, but indistinguishable from a hang on
    a large mesh) before finally erroring out once it truly overflows the
    buffer. Checked unconditionally (not just for the FO4 leniency path)
    since any bsver's stored fields could in principle be corrupt the same
    way."""
    name = _object_net(r, r.strings)
    local = _av_object(r)
    r.skip(16)                           # bounding sphere
    skin = r.i32()
    shader = r.i32()
    r.i32()                              # alpha property
    desc = r.u64()
    ntris = r.u32() if bsver >= FALLOUT4_BSVER else r.u16()
    nverts = r.u16()
    data_size = r.u32()
    vsize = (desc & 0xF) * 4
    pos = uvs = nrm = bone_idx = bone_w = None
    idx = array("H")
    if data_size and nverts and vsize:
        expected = vsize * nverts + ntris * 6
        if block_end is not None and r.p + expected > block_end:
            raise NifError(f"BSTriShape geometry overruns its own block in {name!r}")
        # Fallout 4's own "Data Size" is a documented calc'd field (nifxml),
        # and real-world exporters (Outfit Studio/CBBE, verified on an actual
        # 1st-person body mesh) write a stale value that doesn't match it —
        # the game itself clearly ignores it too, since the mesh loads fine
        # in-game. Skyrim's exporters have never been seen to disagree with
        # the formula across 22k+ real meshes, so keep the strict check there
        # (a real mismatch signals real corruption worth dropping the shape
        # over), but for FO4 trust the computed size over the stored one.
        if data_size != expected:
            if bsver < FALLOUT4_BSVER:
                raise NifError(f"BSTriShape size mismatch in {name!r}")
        pos, uvs, nrm, bone_idx, bone_w = _decode_vertices(r.b, r.p, nverts, vsize, desc, bsver)
        r.p += vsize * nverts
        idx.frombytes(r.b[r.p:r.p + ntris * 6])
        r.p += ntris * 6
    dyn = None
    lod0 = 0
    fo4_segments: list = []
    if bsver >= FALLOUT4_BSVER:
        if ptype == "BSSubIndexTriShape" and data_size:
            try:
                fo4_segments = _read_fo4_segments(r)
            except (struct.error, IndexError, NifError):
                pass
    else:
        # Trailer: particle-data size (always present in BS 100), then the
        # subclass extras. We only understand the no-particle-data case.
        try:
            if r.u32() == 0:
                if ptype == "BSDynamicTriShape":
                    dsize = r.u32()
                    if dsize >= 16 * nverts:
                        v4 = struct.unpack_from(f"<{4 * nverts}f", r.b, r.p)
                        dyn = array("f")
                        for k in range(0, len(v4), 4):
                            dyn.extend(v4[k:k + 3])
                elif ptype in ("BSMeshLODTriShape", "BSLODTriShape"):
                    lod0 = r.u32()           # draw LOD0 only (the array holds all LODs)
        except struct.error:
            pass
    if lod0 and 0 < lod0 <= len(idx) // 3:
        idx = idx[:lod0 * 3]
    return {"name": name, "local": local, "skin": skin, "shader": shader,
            "positions": pos if pos is not None else dyn, "normals": nrm,
            "uvs": uvs, "indices": idx, "dyn": dyn,
            "bone_idx": bone_idx, "bone_w": bone_w, "fo4_segments": fo4_segments}


def _read_fo4_segments(r: "_R") -> list:
    """BSSubIndexTriShape's dismemberment tail (BSGeometrySegmentData +
    optional BSGeometrySegmentSharedData) -> [(start_tri, num_tris, slot), ...]
    for every LEAF segment/sub-segment (one that actually owns triangles).

    Verified byte-for-byte against real FO4 meshes (final read position landed
    exactly on the block's own declared end on both a plain armour piece and a
    25-total-segment body mesh). Segments form a tree (each top-level segment
    may have sub-segments); a body mesh's real triangles live in the
    sub-segments, with the top-level entry a zero-triangle container. When
    present, the shared per-segment table's ``User Index`` gives each entry's
    ``Biped Object`` (Bethesda's own doc: "like the body part types in
    Skyrim") in flat declaration order — one entry per top-level segment (its
    own self-reference, recognisable by ``Bone ID`` 0xffffffff) then one per
    its sub-segments, in order. Semantics not yet cross-checked against a real
    FO4 ARMA record, so treat the resulting numbers as approximate, exactly
    like the Skyrim LE/SE partition-derived slots. Falls back to the top-level
    segment's own declared index when no shared table is present."""
    r.u32()                                             # num primitives (== the shape's own triangle count)
    num_segments = r.u32()
    total_segments = r.u32()
    # A real mesh has at most a few dozen segments/sub-segments; a corrupt or
    # misaligned read (see _read_shape's block_end check for the geometry
    # equivalent of this) can otherwise turn into a very long, technically-
    # bounded-but-effectively-hanging loop before it finally errors out.
    if num_segments > 10_000 or total_segments > 10_000 or total_segments < num_segments:
        raise NifError("Fallout 4 segment counts look corrupt")
    # (start_tri, num_tris, own top-level segment index) for every leaf, plus
    # the flat declaration-order slot (including zero-triangle self entries)
    # so the shared table below can be zipped back onto the right leaf.
    entries: list = []               # (flat_index, start_tri, num_tris, seg_i)
    flat_i = 0
    for seg_i in range(num_segments):
        start_idx, n_prim, _parent, n_sub = r.u32(), r.u32(), r.u32(), r.u32()
        entries.append((flat_i, start_idx // 3, n_prim, seg_i))
        flat_i += 1
        for _ in range(n_sub):
            s_start, s_nprim, _s_parent, _unused = r.u32(), r.u32(), r.u32(), r.u32()
            entries.append((flat_i, s_start // 3, s_nprim, seg_i))
            flat_i += 1
    if num_segments < total_segments:
        shared_nseg, shared_total = r.u32(), r.u32()
        r.skip(4 * shared_nseg)                          # per-top-level-segment start offsets (unused)
        user_by_flat = []
        for _ in range(shared_total):
            user_index, _bone_id, ncut = r.u32(), r.u32(), r.u32()
            r.skip(4 * ncut)
            user_by_flat.append(user_index)
        ssf_len = r.u16()
        r.skip(ssf_len)                                    # SSF file path (unused)
        return [(start_tri, n_prim, user_by_flat[fi] if fi < len(user_by_flat) else seg_i)
                for fi, start_tri, n_prim, seg_i in entries if n_prim]
    return [(start_tri, n_prim, seg_i) for _fi, start_tri, n_prim, seg_i in entries if n_prim]

def _read_dismember_slots(r: _R) -> tuple:
    """BSDismemberSkinInstance → its partitions' body slots, in order
    (BSDismemberBodyPartType: 32 body, 33 hands, 37 feet…). The game hides the
    base body parts whose slot an equipped armour covers."""
    r.i32(); r.i32(); r.i32()                    # skin data, skin partition, skeleton root
    r.skip(4 * r.u32())                          # bone refs
    n = r.u32()
    slots = []
    for _ in range(n):
        r.u16()                                  # part flags
        slots.append(r.u16())
    return tuple(slots)


def _read_skin(inst: _R, data_reader, names: dict) -> "tuple | None":
    """NiSkinInstance + NiSkinData → (bone_names, bone_xf, weights), or None if
    the data is missing or has no per-bone vertex weights."""
    data_ref = inst.i32()
    inst.i32(); inst.i32()                        # skin partition, skeleton root
    n = inst.u32()
    bone_refs = [inst.i32() for _ in range(n)]
    d = data_reader(data_ref)
    if d is None:
        return None
    d.floats(13)                                   # overall skin transform (unused: see SkinData)
    nb = d.u32()
    has_weights = d.u8()
    if not has_weights or nb != n:
        return None
    xf, weights = [], []
    for _ in range(nb):
        v = d.floats(13)
        xf.append((v[:9], v[12], v[9:12]))
        d.floats(4)                                # bounding sphere
        nv = d.u16()
        ids, ws = array("H"), array("f")
        for k in range(nv):
            ids.append(d.u16())
            ws.append(d.f32())
        weights.append((ids, ws))
    return tuple(names.get(b, "") for b in bone_refs), tuple(xf), tuple(weights)


def _read_fo4_skin(inst: _R, data_reader, names: dict,
                   bone_idx: "array | None", bone_w: "array | None") -> "tuple | None":
    """BSSkin::Instance + BSSkin::BoneData → (bone_names, bone_xf, weights),
    matching _read_skin's return shape so skin_shape()/skin_scene() in
    character.py need no changes to pose either game's meshes.

    Unlike Skyrim, Fallout 4 has no per-bone vertex-weight list in the skin
    data at all — weights and bone indices for up to 4 bones are embedded
    directly in the shape's own vertex data (see _decode_vertices) and must be
    inverted here into the per-bone (vertex ids, weights) lists the rest of
    the skinning code expects. BSSkinBoneTrans orders its fields bounding
    sphere first then rotation/translation/scale — the opposite of Skyrim's
    NiSkinData — verified against a real FO4 mesh."""
    inst.i32()                                     # skeleton root (Ptr, unused: names come from bone refs)
    data_ref = inst.i32()
    n = inst.u32()
    bone_refs = [inst.i32() for _ in range(n)]
    if not bone_idx or not bone_w or len(bone_idx) != 4 * (len(bone_w) // 4):
        return None
    d = data_reader(data_ref)
    if d is None:
        return None
    nb = d.u32()
    if nb != n:
        return None
    xf = []
    for _ in range(nb):
        d.floats(4)                                # bounding sphere (first, unlike NiSkinData)
        r9 = d.floats(9)
        t = d.floats(3)
        sc = d.f32()
        xf.append((r9, sc, t))
    per_bone_ids = [array("H") for _ in range(nb)]
    per_bone_ws = [array("f") for _ in range(nb)]
    nverts = len(bone_w) // 4
    for vid in range(nverts):
        base = vid * 4
        for s in range(4):
            bi, w = bone_idx[base + s], bone_w[base + s]
            if w > 0.0 and bi < nb:
                per_bone_ids[bi].append(vid)
                per_bone_ws[bi].append(w)
    weights = tuple(zip(per_bone_ids, per_bone_ws))
    return tuple(names.get(b, "") for b in bone_refs), tuple(xf), weights


def _read_fo4_shape_slots(r: "_R") -> "frozenset":
    """A BSSubIndexTriShape's segment slot numbers, skipping past the vertex
    and triangle arrays without decoding them — the read_body_slots() fast
    path for Fallout 4 (see _read_fo4_segments for what the numbers mean and
    _read_shape for the fields this mirrors)."""
    _object_net(r, r.strings)
    _av_object(r)
    r.skip(16)                           # bounding sphere
    r.i32(); r.i32(); r.i32()            # skin, shader, alpha refs
    desc = r.u64()
    ntris = r.u32()
    nverts = r.u16()
    data_size = r.u32()
    if not data_size:
        return frozenset()
    r.skip(data_size)
    return frozenset(slot for _s, _n, slot in _read_fo4_segments(r))


def read_body_slots(data: bytes) -> frozenset:
    """The body slots (32 body, 33 hands, 37 feet…) a mesh covers, without parsing
    any geometry: just the header and the small BSDismemberSkinInstance blocks
    (Skyrim) or BSSubIndexTriShape's own segment tail (Fallout 4, skipped over
    rather than decoded). Empty for meshes with no such blocks (props, weapons,
    unskinned models). Raises NifError/NifUnsupported like read_nif for files
    it can't read. Fast enough to run over thousands of files (a picker's
    candidate list)."""
    hdr = _read_header(data)
    slots: set = set()
    pos = hdr.body_start
    for size, t in zip(hdr.sizes, hdr.block_type):
        if t < len(hdr.types) and hdr.types[t] == "BSDismemberSkinInstance":
            try:
                slots.update(_read_dismember_slots(_R(data, pos, hdr.strings)))
            except (struct.error, IndexError):
                pass
        elif hdr.bsver >= FALLOUT4_BSVER and t < len(hdr.types) and hdr.types[t] == "BSSubIndexTriShape":
            try:
                slots.update(_read_fo4_shape_slots(_R(data, pos, hdr.strings)))
            except (struct.error, IndexError, NifError):
                pass
        pos += size
    return frozenset(slots)


def _read_trishape_data(r: _R):
    """NiTriShapeData (Skyrim LE) → (positions, normals|None, uvs|None, triangle
    indices). Each block is checked against its size in the header, so a layout
    slip shows up as an exception, not a garbled mesh."""
    r.i32()                                        # group id
    nv = r.u16()
    r.u8(); r.u8()                                 # keep / compress flags
    pos = array("f")
    if r.u8():
        pos = array("f", struct.unpack_from(f"<{3 * nv}f", r.b, r.p))
        r.skip(12 * nv)
    flags = r.u16()                                # BS vector flags: bit 0 = a UV set, 0x1000 = tangents
    r.u32()                                        # material CRC
    nrm = None
    has_n = r.u8()
    if has_n:
        nrm = array("f", struct.unpack_from(f"<{3 * nv}f", r.b, r.p))
        r.skip(12 * nv)
        if flags & 0x1000:
            r.skip(24 * nv)                        # tangents + bitangents
    r.skip(16)                                     # bounding sphere
    if r.u8():
        r.skip(16 * nv)                            # vertex colours
    uvs = None
    if flags & 1:
        uvs = array("f", struct.unpack_from(f"<{2 * nv}f", r.b, r.p))
        r.skip(8 * nv)
    r.u16()                                        # consistency flags
    r.i32()                                        # additional data
    ntri = r.u16()
    r.u32()                                        # number of triangle points
    idx = array("H")
    if r.u8():
        idx = array("H", struct.unpack_from(f"<{3 * ntri}H", r.b, r.p))
        r.skip(6 * ntri)
    return pos, nrm, uvs, idx


def _read_trishape_le(r: _R, reader) -> dict:
    """NiTriShape (Skyrim LE) → the same dict _read_shape returns for SE."""
    name = _object_net(r, r.strings)
    local = _av_object(r)
    data_ref, skin = r.i32(), r.i32()
    n = r.u32()                                    # materials: names, extra data, active, dirty flag
    r.skip(8 * n)
    r.i32(); r.u8()
    shader, _alpha = r.i32(), r.i32()
    pos, nrm, uvs, idx = array("f"), None, None, array("H")
    d = reader(data_ref) if data_ref >= 0 else None
    if d is not None:
        pos, nrm, uvs, idx = _read_trishape_data(d)
    return {"name": name, "local": local, "skin": skin, "shader": shader,
            "positions": pos, "normals": nrm, "uvs": uvs, "indices": idx, "dyn": None, "le": True}


def _read_partitions_le(r: _R) -> "list[list[int]] | None":
    """NiSkinPartition (Skyrim LE) → each partition's triangles as flat lists of
    *global* vertex indices (partition-local indices mapped through its vertex map;
    strips unrolled). The partitions together cover the shape's triangles once, so
    they give the same per-partition split the SE files carry."""
    out: list[list[int]] = []
    for _ in range(r.u32()):
        nv, nt, nb, ns, nw = r.u16(), r.u16(), r.u16(), r.u16(), r.u16()
        r.skip(2 * nb)                             # bones
        vmap = None
        if r.u8():
            vmap = struct.unpack_from(f"<{nv}H", r.b, r.p)
            r.skip(2 * nv)
        if r.u8():
            r.skip(4 * nv * nw)                    # vertex weights
        lens = [r.u16() for _ in range(ns)]
        local: list[int] = []
        if r.u8():
            if ns:
                for ln in lens:
                    strip = struct.unpack_from(f"<{ln}H", r.b, r.p)
                    r.skip(2 * ln)
                    for i in range(len(strip) - 2):
                        a, b, c = strip[i], strip[i + 1], strip[i + 2]
                        if a != b and b != c and a != c:
                            local += (a, c, b) if i % 2 else (a, b, c)
            else:
                local = list(struct.unpack_from(f"<{3 * nt}H", r.b, r.p))
                r.skip(6 * nt)
        if r.u8():
            r.skip(nv * nw)                        # bone indices
        r.skip(2)                                  # LOD level + global VB
        out.append([vmap[i] for i in local] if vmap is not None else local)
    return out


def _read_texture_set(r: _R) -> list[str]:
    n = r.u32()
    return [normalize_texture_path(r.sized_str()) for _ in range(n)]


def _read_lighting_shader(r: _R) -> "tuple[int, str]":
    """→ (block index of the BSShaderTextureSet (or -1), the property's own
    Name field normalised as a material path — non-empty only when this
    shape's real texture set lives in an external .bgsm/.bgem file)."""
    r.u32()                              # shader type
    name = normalize_material_path(_object_net(r, r.strings))
    r.skip(8)                            # shader flags 1 + 2
    r.skip(16)                           # uv offset + uv scale
    return r.i32(), name


def _read_effect_shader(r: _R) -> str:
    _object_net(r, r.strings)
    r.skip(8)                            # shader flags 1 + 2
    r.skip(16)                           # uv offset + uv scale
    return normalize_texture_path(r.sized_str())


# -- public API ---------------------------------------------------------------------
def read_nif(data: bytes, include_nodes: bool = False) -> NifScene:
    """Parse *data* (a whole .nif file) into world-space shapes.

    *include_nodes* also returns the node tree (names + world positions) —
    wanted for skeletons, skipped otherwise to keep scenes small.

    Raises NifUnsupported for other games/versions, NifError for corrupt files.
    Blocks we don't understand are skipped; a shape that fails to parse is
    dropped rather than failing the whole file."""
    hdr = _read_header(data)
    if hdr.nblocks == 0:
        return NifScene([])
    starts = []
    p = hdr.body_start
    for s in hdr.sizes:
        starts.append(p)
        p += s
    if p > len(data):
        raise NifError("NIF is shorter than its block table says")
    footer = _R(data, p)
    try:
        nroots = footer.u32()
        roots = [footer.i32() for _ in range(nroots)]
    except struct.error:
        roots = [0]

    tname = [hdr.types[t] if t < len(hdr.types) else "" for t in hdr.block_type]

    def reader(i: int) -> _R:
        return _R(data, starts[i], hdr.strings)

    # Cheap pass: node children + texture-set/shader lookups.
    children: dict[int, list[int]] = {}
    locals_: dict[int, tuple] = {}
    names: dict[int, str] = {}
    for i, t in enumerate(tname):
        if t in _NODE_TYPES:
            try:
                r = reader(i)
                names[i] = _object_net(r, hdr.strings)
                locals_[i] = _av_object(r)
                n = r.u32()
                children[i] = [r.i32() for _ in range(n)]
            except (struct.error, IndexError):
                children[i] = []

    shapes: list[NifShape] = []
    seen_shapes: set[int] = set()

    def textures_for(shader_ref: int) -> "tuple[list[str], bool, str]":
        if not 0 <= shader_ref < hdr.nblocks:
            return [], False, ""
        st = tname[shader_ref]
        try:
            if st == "BSLightingShaderProperty":
                ts, material_name = _read_lighting_shader(reader(shader_ref))
                if 0 <= ts < hdr.nblocks and tname[ts] == "BSShaderTextureSet":
                    return _read_texture_set(reader(ts)), False, material_name
                return [], False, material_name
            elif st == "BSEffectShaderProperty":
                src = _read_effect_shader(reader(shader_ref))
                return ([src] if src else []), True, ""
        except (struct.error, IndexError, UnicodeDecodeError):
            pass
        return [], False, ""

    def emit(i: int, world):
        if i in seen_shapes:
            return
        seen_shapes.add(i)
        try:
            if tname[i] == "NiTriShape":
                g = _read_trishape_le(
                    reader(i), lambda ref: reader(ref) if 0 <= ref < hdr.nblocks
                    and tname[ref] == "NiTriShapeData" else None)
            else:
                g = _read_shape(reader(i), tname[i], hdr.bsver, starts[i] + hdr.sizes[i])
        except (struct.error, IndexError, NifError):
            return
        part_tris: tuple = ()
        if (not g["positions"] or not len(g["indices"])) and g["skin"] >= 0:
            try:
                sr = reader(g["skin"])
                sr.i32()                                     # skin data
                part = sr.i32()                              # skin partition
                if 0 <= part < hdr.nblocks and tname[part] == "NiSkinPartition":
                    got = _read_skin_partition(reader(part))
                    if got is not None:
                        ppos, g["uvs"], g["normals"], g["indices"], part_tris = got
                        g["positions"] = ppos if ppos is not None else g["dyn"]
                        if g["positions"] is not None and g["uvs"] is not None \
                                and len(g["uvs"]) // 2 != len(g["positions"]) // 3:
                            g["positions"] = None      # vertex counts disagree
            except (struct.error, IndexError):
                pass
        pos = g["positions"]
        if pos is None or not len(pos) or not len(g["indices"]):
            return
        if not all(math.isfinite(v) for v in pos):
            return                               # NaN/inf vertices would poison framing and bounds
        if max(g["indices"]) >= len(pos) // 3:
            # A real file's own authored data can be internally inconsistent
            # (found via live testing: a third-party-converted hair mesh
            # whose triangle indices reference vertices past its own decoded
            # count — the vertex/triangle counts each parse correctly and
            # self-consistently, this isn't a reader misalignment, the source
            # content itself is just broken). Every downstream consumer
            # (the GL viewport, hide_covered, skin_shape) assumes indices fit
            # inside positions, so this must be caught here rather than
            # crash or corrupt rendering further down the line.
            return
        r9, sc, t = _compose(world, g["local"])
        wp = array("f", bytes(4 * len(pos)))
        for k in range(0, len(pos), 3):
            x, y, z = pos[k], pos[k + 1], pos[k + 2]
            wp[k] = t[0] + sc * (r9[0] * x + r9[1] * y + r9[2] * z)
            wp[k + 1] = t[1] + sc * (r9[3] * x + r9[4] * y + r9[5] * z)
            wp[k + 2] = t[2] + sc * (r9[6] * x + r9[7] * y + r9[8] * z)
        wn = None
        if g["normals"] is not None:
            nrm = g["normals"]
            wn = array("f", bytes(4 * len(nrm)))
            for k in range(0, len(nrm), 3):
                x, y, z = nrm[k], nrm[k + 1], nrm[k + 2]
                nx = r9[0] * x + r9[1] * y + r9[2] * z
                ny = r9[3] * x + r9[4] * y + r9[5] * z
                nz = r9[6] * x + r9[7] * y + r9[8] * z
                ln = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
                wn[k], wn[k + 1], wn[k + 2] = nx / ln, ny / ln, nz / ln
        tex, is_fx, material_name = textures_for(g["shader"])
        part_slots: tuple = ()
        if g["skin"] >= 0 and tname[g["skin"]] == "BSDismemberSkinInstance":
            try:
                part_slots = _read_dismember_slots(reader(g["skin"]))
            except (struct.error, IndexError):
                pass
        elif g.get("fo4_segments"):
            # Only the *set* of slot numbers is used, not per-triangle hiding:
            # a real body mesh's own top-level segment and its sub-segments
            # were found to report overlapping/non-tiling triangle ranges
            # (e.g. a torso segment's own range plus 4 "sub" ranges that
            # extend past its end) — real, verified FO4 data, not a parsing
            # bug. Splitting hide_covered() by these ranges could hide the
            # wrong triangles, so part_tris is left unset here and the
            # mismatch check below falls back to treating the shape as one
            # unsplit piece, same as any other shape whose slots can't be
            # trusted for partition-level hiding.
            part_slots = tuple(sorted({slot for _s, _n, slot in g["fo4_segments"]}))
        if g.get("le") and g["skin"] >= 0 and part_slots:
            # LE keeps all triangles in the shape's own data and the per-slot split in
            # the skin partitions: re-order the triangles partition by partition so
            # part_slots[i] owns the next part_tris[i], as in SE files.
            try:
                sr = reader(g["skin"])
                sr.i32()
                pref = sr.i32()
                if 0 <= pref < hdr.nblocks and tname[pref] == "NiSkinPartition":
                    parts = _read_partitions_le(reader(pref))
                    total = sum(len(t) for t in parts)
                    if parts and total == len(g["indices"]):
                        g["indices"] = array("H", [v for t in parts for v in t])
                        part_tris = tuple(len(t) // 3 for t in parts)
            except (struct.error, IndexError):
                pass
        if len(part_slots) != len(part_tris) or sum(part_tris) * 3 != len(g["indices"]):
            part_tris = ()                       # can't split reliably: treat as one piece
        skin_data = None
        if g["skin"] >= 0 and tname[g["skin"]] in ("BSDismemberSkinInstance", "NiSkinInstance"):
            try:
                got = _read_skin(
                    reader(g["skin"]),
                    lambda ref: reader(ref) if 0 <= ref < hdr.nblocks and tname[ref] == "NiSkinData" else None,
                    names)
                if got is not None:
                    skin_data = SkinData(got[0], got[1], got[2], g["positions"], g["normals"])
            except (struct.error, IndexError):
                pass
        elif g["skin"] >= 0 and tname[g["skin"]] == "BSSkin::Instance":
            try:
                got = _read_fo4_skin(
                    reader(g["skin"]),
                    lambda ref: reader(ref) if 0 <= ref < hdr.nblocks and tname[ref] == "BSSkin::BoneData" else None,
                    names, g.get("bone_idx"), g.get("bone_w"))
                if got is not None:
                    skin_data = SkinData(got[0], got[1], got[2], g["positions"], g["normals"])
            except (struct.error, IndexError):
                pass
        shapes.append(NifShape(
            name=g["name"], positions=wp, normals=wn, uvs=g["uvs"],
            indices=g["indices"], textures=tex,
            is_skinned=g["skin"] >= 0, is_effect=is_fx, slots=frozenset(part_slots),
            part_slots=part_slots if part_tris else (), part_tris=part_tris, skin=skin_data,
            material_name=material_name))

    nodes: list[NifNode] = []
    seen_nodes: set[int] = set()

    def walk(i: int, world, depth=0, parent=-1):
        if not 0 <= i < hdr.nblocks or depth > 64:
            return
        t = tname[i]
        if t in _NODE_TYPES:
            # A node is walked at most once, full stop — not just depth-limited.
            # A real malformed file (found via live testing: a large real mod
            # collection) had a child list that revisits an ancestor, and the
            # depth cap alone doesn't stop that: at branching factor 2 it's
            # 2**64 node visits before the cap finally bites, which in
            # practice is indistinguishable from a permanent hang. This
            # matches how emit()/seen_shapes already treats shapes — visit
            # once, not once per reachable path.
            if i in seen_nodes:
                return
            seen_nodes.add(i)
            w = _compose(world, locals_.get(i, _IDENT))
            me = parent
            if include_nodes:
                me = len(nodes)
                nodes.append(NifNode(names.get(i, ""), parent, w[2], w[0], w[1]))
            for c in children.get(i, []):
                walk(c, w, depth + 1, me)
        elif t in _SHAPE_TYPES:
            emit(i, world)

    for root in roots:
        walk(root, _IDENT)
    # Shapes not reachable from a root (rare): still show them, untransformed.
    for i, t in enumerate(tname):
        if t in _SHAPE_TYPES and i not in seen_shapes:
            emit(i, _IDENT)
    return NifScene(shapes, nodes)
