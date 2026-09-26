"""Minimal Skyrim SE (NIF 20.2.0.7, BS version 100) reader for previewing meshes.

Not a general NIF library: it reads only what a viewer needs — the node tree
(transforms), BSTriShape / BSDynamicTriShape / BSMeshLODTriShape geometry, and
the texture paths from BSLightingShaderProperty + BSShaderTextureSet or
BSEffectShaderProperty. Every block's byte size is in the file header, so the
hundreds of block types we don't understand (physics, animation, particles…)
are skipped without being parsed.

Skinned meshes are drawn in their bind pose (vertices as stored, no bone
transforms), which is the correct rest pose for armour and bodies.

Pure Python, no numpy: struct decodes half-floats natively.
"""

from __future__ import annotations

import struct
from array import array
from dataclasses import dataclass, field

_HEADER_PREFIX = b"Gamebryo File Format, Version "
SKYRIM_SE_VERSION = 0x14020007
SKYRIM_SE_BSVER = 100

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
                          "BSLODTriShape"})


class NifError(Exception):
    """The file isn't a NIF we can read."""


class NifUnsupported(NifError):
    """A valid NIF, but a game/version this reader doesn't handle."""


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


@dataclass
class NifScene:
    shapes: list[NifShape]

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


def normalize_texture_path(p: str) -> str:
    """Lowercase, forward slashes, rooted at ``textures/`` ("" stays "")."""
    p = p.replace("\\", "/").strip().lower()
    while p.startswith("/"):
        p = p[1:]
    if not p:
        return ""
    return p if p.startswith("textures/") else "textures/" + p


# -- header ---------------------------------------------------------------------
@dataclass
class _Header:
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
            raise NifUnsupported(f"NIF version {version:#x} (only Skyrim SE 20.2.0.7)")
        if endian != 1:
            raise NifUnsupported("big-endian NIF")
        r.u32()                          # user version
        nblocks = r.u32()
        bsver = r.u32()
        if bsver != SKYRIM_SE_BSVER:
            raise NifUnsupported(f"BS version {bsver} (only Skyrim SE = 100)")
        r.short_str(); r.short_str(); r.short_str()   # author, process, export
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
    return _Header(nblocks, types, [i & 0x7FFF for i in idx], sizes, strings, r.p)


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


def _decode_vertices(b: bytes, base: int, nverts: int, vsize: int, desc: int):
    """Decode packed BSVertexData → (positions|None, uvs|None, normals|None), flat.
    Positions are None when the descriptor has no position attribute (dynamic
    shapes keep them in a separate block).

    Skyrim SE positions are always 32-bit floats: the FULLPREC flag is only
    meaningful from BS version 130 (a 32-byte vertex adds up only that way).
    Attribute offsets come from the descriptor's nibbles (in dwords)."""
    def off(attr: int) -> int:
        return ((desc >> (4 * attr + 4)) & 0xF) * 4

    has_pos = bool(desc & _VF_VERTEX)
    has_uv = bool(desc & _VF_UV)
    has_n = bool(desc & _VF_NORMAL)
    uv_off, n_off = off(1), off(3)
    pos = array("f") if has_pos else None
    uvs = array("f") if has_uv else None
    nrm = array("f") if has_n else None
    unpack_pos = struct.Struct("<3f").unpack_from
    unpack_uv = struct.Struct("<2e").unpack_from
    for i in range(nverts):
        o = base + i * vsize
        if has_pos:
            pos.extend(unpack_pos(b, o))
        if has_uv:
            uvs.extend(unpack_uv(b, o + uv_off))
        if has_n:
            nrm.extend((b[o + n_off] / 127.5 - 1.0, b[o + n_off + 1] / 127.5 - 1.0,
                        b[o + n_off + 2] / 127.5 - 1.0))
    return pos, uvs, nrm


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
    pos, uvs, nrm = _decode_vertices(r.b, r.p, nverts, vsize, desc)
    r.p += data_size
    tris = array("H")
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
    return pos, uvs, nrm, tris


def _read_shape(r: _R, ptype: str):
    """Parse a BSTriShape-family block into a dict of raw geometry."""
    name = _object_net(r, r.strings)
    local = _av_object(r)
    r.skip(16)                           # bounding sphere
    skin = r.i32()
    shader = r.i32()
    r.i32()                              # alpha property
    desc = r.u64()
    ntris = r.u16()
    nverts = r.u16()
    data_size = r.u32()
    vsize = (desc & 0xF) * 4
    pos = uvs = nrm = None
    idx = array("H")
    if data_size and nverts and vsize:
        if data_size != vsize * nverts + ntris * 6:
            raise NifError(f"BSTriShape size mismatch in {name!r}")
        pos, uvs, nrm = _decode_vertices(r.b, r.p, nverts, vsize, desc)
        r.p += vsize * nverts
        idx.frombytes(r.b[r.p:r.p + ntris * 6])
        r.p += ntris * 6
    # Trailer: particle-data size (always present in BS 100), then the
    # subclass extras. We only understand the no-particle-data case.
    dyn = None
    lod0 = 0
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
            "uvs": uvs, "indices": idx, "dyn": dyn}


def _read_texture_set(r: _R) -> list[str]:
    n = r.u32()
    return [normalize_texture_path(r.sized_str()) for _ in range(n)]


def _read_lighting_shader(r: _R) -> int:
    """→ block index of the BSShaderTextureSet (or -1)."""
    r.u32()                              # shader type
    _object_net(r, r.strings)
    r.skip(8)                            # shader flags 1 + 2
    r.skip(16)                           # uv offset + uv scale
    return r.i32()


def _read_effect_shader(r: _R) -> str:
    _object_net(r, r.strings)
    r.skip(8)                            # shader flags 1 + 2
    r.skip(16)                           # uv offset + uv scale
    return normalize_texture_path(r.sized_str())


# -- public API ---------------------------------------------------------------------
def read_nif(data: bytes) -> NifScene:
    """Parse *data* (a whole .nif file) into world-space shapes.

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
    for i, t in enumerate(tname):
        if t in _NODE_TYPES:
            try:
                r = reader(i)
                _object_net(r, hdr.strings)
                locals_[i] = _av_object(r)
                n = r.u32()
                children[i] = [r.i32() for _ in range(n)]
            except (struct.error, IndexError):
                children[i] = []

    shapes: list[NifShape] = []
    seen_shapes: set[int] = set()

    def textures_for(shader_ref: int) -> "tuple[list[str], bool]":
        if not 0 <= shader_ref < hdr.nblocks:
            return [], False
        st = tname[shader_ref]
        try:
            if st == "BSLightingShaderProperty":
                ts = _read_lighting_shader(reader(shader_ref))
                if 0 <= ts < hdr.nblocks and tname[ts] == "BSShaderTextureSet":
                    return _read_texture_set(reader(ts)), False
            elif st == "BSEffectShaderProperty":
                src = _read_effect_shader(reader(shader_ref))
                return ([src] if src else []), True
        except (struct.error, IndexError, UnicodeDecodeError):
            pass
        return [], False

    def emit(i: int, world):
        if i in seen_shapes:
            return
        seen_shapes.add(i)
        try:
            g = _read_shape(reader(i), tname[i])
        except (struct.error, IndexError, NifError):
            return
        if (not g["positions"] or not len(g["indices"])) and g["skin"] >= 0:
            try:
                sr = reader(g["skin"])
                sr.i32()                                     # skin data
                part = sr.i32()                              # skin partition
                if 0 <= part < hdr.nblocks and tname[part] == "NiSkinPartition":
                    got = _read_skin_partition(reader(part))
                    if got is not None:
                        ppos, g["uvs"], g["normals"], g["indices"] = got
                        g["positions"] = ppos if ppos is not None else g["dyn"]
                        if g["positions"] is not None and g["uvs"] is not None \
                                and len(g["uvs"]) // 2 != len(g["positions"]) // 3:
                            g["positions"] = None      # vertex counts disagree
            except (struct.error, IndexError):
                pass
        pos = g["positions"]
        if pos is None or not len(pos) or not len(g["indices"]):
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
        tex, is_fx = textures_for(g["shader"])
        shapes.append(NifShape(
            name=g["name"], positions=wp, normals=wn, uvs=g["uvs"],
            indices=g["indices"], textures=tex,
            is_skinned=g["skin"] >= 0, is_effect=is_fx))

    def walk(i: int, world, depth=0):
        if not 0 <= i < hdr.nblocks or depth > 64:
            return
        t = tname[i]
        if t in _NODE_TYPES:
            w = _compose(world, locals_.get(i, _IDENT))
            for c in children.get(i, []):
                walk(c, w, depth + 1)
        elif t in _SHAPE_TYPES:
            emit(i, world)

    for root in roots:
        walk(root, _IDENT)
    # Shapes not reachable from a root (rare): still show them, untransformed.
    for i, t in enumerate(tname):
        if t in _SHAPE_TYPES and i not in seen_shapes:
            emit(i, _IDENT)
    return NifScene(shapes)
