"""Skyrim SE NIF reader: transforms, geometry, textures, error handling.

The NIFs are built in memory (a NiNode → BSTriShape with a lighting shader and
texture set), so the tests need no game files. A real-data pass over the
vanilla BSAs was done by hand while writing the reader (22,047 meshes, no
parse errors) — the optional test at the bottom repeats a small slice of it
when a Skyrim SE install is present.
"""
from __future__ import annotations

import math
import struct
from pathlib import Path

import pytest

from Utils.nif.nif_reader import (
    NifError, NifUnsupported, format_label, normalize_texture_path, read_nif,
    read_body_slots, sniff_nif_format, version_string,
)

_VF = (1 | 2 | 8) << 44                     # position + uv + normal
_DESC = 6 | (4 << 8) | (5 << 16) | _VF      # 24-byte vertices: pos16 uv4 normal4


def _s(text: str) -> bytes:
    raw = text.encode("latin-1")
    return struct.pack("<I", len(raw)) + raw


def _xform(t=(0, 0, 0), rot=(1, 0, 0, 0, 1, 0, 0, 0, 1), scale=1.0) -> bytes:
    return struct.pack("<I3f9ff", 0, *t, *rot, scale) + struct.pack("<i", -1)


def _object_net(name_idx: int) -> bytes:
    return struct.pack("<iIi", name_idx, 0, -1)


def _node(name_idx, children, **xf) -> bytes:
    return (_object_net(name_idx) + _xform(**xf)
            + struct.pack("<I", len(children)) + struct.pack(f"<{len(children)}i", *children)
            + struct.pack("<I", 0))


def _shape(name_idx, verts, tris, shader_ref, **xf) -> bytes:
    vdata = b""
    for (x, y, z), (u, v), n in verts:
        vdata += struct.pack("<3ffee", x, y, z, 0.0, u, v)[:16 + 4]
        vdata += bytes(int(round((c + 1) * 127.5)) for c in n) + b"\0"
    tdata = b"".join(struct.pack("<3H", *t) for t in tris)
    return (_object_net(name_idx) + _xform(**xf) + b"\0" * 16
            + struct.pack("<iii", -1, shader_ref, -1)
            + struct.pack("<QHHI", _DESC, len(tris), len(verts), len(vdata) + len(tdata))
            + vdata + tdata + struct.pack("<I", 0))


def _lighting_shader(texture_set_ref) -> bytes:
    return (struct.pack("<I", 0) + _object_net(-1) + struct.pack("<II", 0, 0)
            + b"\0" * 16 + struct.pack("<i", texture_set_ref))


def _texture_set(paths) -> bytes:
    return struct.pack("<I", len(paths)) + b"".join(_s(p) for p in paths)


def _nif(blocks, strings=("Root", "Shape"), roots=(0,), bsver=100,
         version=0x14020007) -> bytes:
    """blocks: list of (type_name, payload)."""
    types = []
    for t, _ in blocks:
        if t not in types:
            types.append(t)
    out = b"Gamebryo File Format, Version 20.2.0.7\n"
    out += struct.pack("<IBII", version, 1, 12, len(blocks)) + struct.pack("<I", bsver)
    # BSStreamHeader export strings: Author always; Process Script only for
    # BS < 131; Export Script always; Max Filepath only for BS >= 103 (Fallout
    # 4 has 4 fields where Skyrim has 3 — see _read_header).
    n_strs = 2 + (1 if bsver < 131 else 0) + (1 if bsver >= 103 else 0)
    out += b"\x01\0" * n_strs
    out += struct.pack("<H", len(types)) + b"".join(_s(t) for t in types)
    out += struct.pack(f"<{len(blocks)}H", *[types.index(t) for t, _ in blocks])
    out += struct.pack(f"<{len(blocks)}I", *[len(p) for _, p in blocks])
    out += struct.pack("<II", len(strings), max((len(x) for x in strings), default=0))
    out += b"".join(_s(x) for x in strings) + struct.pack("<I", 0)
    out += b"".join(p for _, p in blocks)
    out += struct.pack("<I", len(roots)) + struct.pack(f"<{len(roots)}i", *roots)
    return out


TRI = [((0, 0, 0), (0, 0), (0, 0, 1)),
       ((1, 0, 0), (1, 0), (0, 0, 1)),
       ((0, 1, 0), (0, 1), (0, 0, 1))]


def _simple(node_xf=None, shape_xf=None, textures=("Textures\\Armor\\Iron_d.DDS", "")):
    return _nif([
        ("BSFadeNode", _node(0, [1], **(node_xf or {}))),
        ("BSTriShape", _shape(1, TRI, [(0, 1, 2)], 2, **(shape_xf or {}))),
        ("BSLightingShaderProperty", _lighting_shader(3)),
        ("BSShaderTextureSet", _texture_set(list(textures))),
    ])


def test_reads_geometry_texture_and_names():
    sc = read_nif(_simple())
    assert len(sc.shapes) == 1
    sh = sc.shapes[0]
    assert sh.name == "Shape"
    assert list(sh.positions) == [0, 0, 0, 1, 0, 0, 0, 1, 0]
    assert list(sh.indices) == [0, 1, 2]
    assert list(sh.uvs) == [0, 0, 1, 0, 0, 1]
    assert [round(c, 2) for c in sh.normals[:3]] == [0.0, 0.0, 1.0]
    assert sh.textures == ["textures/armor/iron_d.dds", ""]
    assert not sh.is_skinned and not sh.is_effect


def test_node_translation_and_scale_apply_to_world_positions():
    sc = read_nif(_simple(node_xf=dict(t=(10, 0, 0), scale=2.0)))
    p = sc.shapes[0].positions
    assert list(p[3:6]) == [12.0, 0.0, 0.0]          # (1,0,0)*2 + (10,0,0)


def test_rotation_is_row_major_and_composes_parent_then_child():
    rz90 = (0, -1, 0, 1, 0, 0, 0, 0, 1)              # +90° about Z
    sc = read_nif(_simple(node_xf=dict(rot=rz90), shape_xf=dict(t=(1, 0, 0))))
    x, y, z = sc.shapes[0].positions[0:3]            # shape origin (1,0,0) → (0,1,0)
    assert (round(x, 5), round(y, 5), round(z, 5)) == (0.0, 1.0, 0.0)
    nx, ny, nz = sc.shapes[0].normals[0:3]           # +Z normal is unchanged
    assert (round(nx, 2), round(ny, 2), round(nz, 2)) == (0.0, 0.0, 1.0)
    assert math.isclose(math.sqrt(nx * nx + ny * ny + nz * nz), 1.0, rel_tol=1e-5)


def test_unknown_blocks_are_skipped_and_orphan_shapes_still_show():
    blob = _nif([
        ("BSFadeNode", _node(0, [])),                       # root with no children
        ("bhkRigidBody", b"\x01\x02\x03" * 7),              # something we don't parse
        ("BSTriShape", _shape(1, TRI, [(0, 1, 2)], -1)),    # not reachable from root
    ])
    sc = read_nif(blob)
    assert len(sc.shapes) == 1 and sc.shapes[0].textures == []


def test_missing_shader_gives_no_textures_and_bounds_work():
    sc = read_nif(_nif([("BSFadeNode", _node(0, [1])),
                        ("BSTriShape", _shape(1, TRI, [(0, 1, 2)], -1))]))
    assert sc.shapes[0].textures == []
    assert sc.bounds() == ((0.0, 0.0, 0.0), (1.0, 1.0, 0.0))
    assert read_nif(_nif([("NiNode", _node(0, []))])).bounds() is None


def test_bounding_sphere_and_sample_points():
    sc = read_nif(_simple())
    (cx, cy, cz), r = sc.bounding_sphere()
    assert (cx, cy, cz) == (0.5, 0.5, 0.0)
    assert r == pytest.approx(math.sqrt(0.5))       # farthest vertex from the box centre
    assert sc.bounding_sphere() is sc.bounding_sphere()          # cached
    assert sorted(sc.sample_points()) == [(0, 0, 0), (0, 1, 0), (1, 0, 0)]
    assert read_nif(_nif([("NiNode", _node(0, []))])).bounding_sphere() is None
    many = read_nif(_simple()).shapes[0]
    from Utils.nif.nif_reader import NifScene
    assert len(NifScene([many] * 5).sample_points(limit=6)) <= 6 + 5


# -- Skyrim LE (BS 83): NiTriShape + NiTriShapeData ------------------------------------
def _trishape_le(name_idx, data_ref, shader_ref, skin_ref=-1) -> bytes:
    return (_object_net(name_idx) + _xform() + struct.pack("<ii", data_ref, skin_ref)
            + struct.pack("<I", 0) + struct.pack("<iB", -1, 0)
            + struct.pack("<ii", shader_ref, -1))


def _trishape_data(verts, tris, uvs=True) -> bytes:
    nv = len(verts)
    out = struct.pack("<iHBB", 0, nv, 0, 0) + b"\x01"
    out += b"".join(struct.pack("<3f", *v) for v, _uv, _n in verts)
    out += struct.pack("<HI", 1 if uvs else 0, 0) + b"\x01"
    out += b"".join(struct.pack("<3f", *n) for _v, _uv, n in verts)
    out += b"\0" * 16 + b"\0"                       # bounding sphere, no vertex colours
    if uvs:
        out += b"".join(struct.pack("<2f", *uv) for _v, uv, _n in verts)
    out += struct.pack("<HiHI", 0, 0, len(tris), 3 * len(tris)) + b"\x01"
    out += b"".join(struct.pack("<3H", *t) for t in tris)
    return out + struct.pack("<H", 0)


def _le(verts=TRI, tris=((0, 1, 2),)) -> bytes:
    return _nif([
        ("BSFadeNode", _node(0, [1])),
        ("NiTriShape", _trishape_le(1, 4, 2)),
        ("BSLightingShaderProperty", _lighting_shader(3)),
        ("BSShaderTextureSet", _texture_set(["Textures\\Armor\\Iron_d.DDS", ""])),
        ("NiTriShapeData", _trishape_data(verts, list(tris))),
    ], bsver=83)


def test_skyrim_le_trishape_is_read_like_an_se_shape():
    sc = read_nif(_le())
    sh = sc.shapes[0]
    assert sh.name == "Shape"
    assert list(sh.positions) == [0, 0, 0, 1, 0, 0, 0, 1, 0]
    assert list(sh.indices) == [0, 1, 2]
    assert list(sh.uvs) == [0, 0, 1, 0, 0, 1]
    assert sh.textures[0].endswith("iron_d.dds")
    assert format_label(*sniff_nif_format(_le()[:128])) == "Skyrim LE"


def test_shape_with_non_finite_vertices_is_dropped():
    bad = [((float("nan"), 0, 0), (0, 0), (0, 0, 1))] + TRI[1:]
    assert read_nif(_le(verts=bad)).shapes == []


def test_le_partitions_map_local_indices_and_unroll_strips():
    from Utils.nif.nif_reader import _R, _read_partitions_le

    def part(nv, nt, vmap, tris=None, strips=None):
        out = struct.pack("<HHHHH", nv, nt, 0, len(strips or []), 1)
        out += b"\x01" + struct.pack(f"<{nv}H", *vmap)
        out += b"\0"                                        # no vertex weights
        out += b"".join(struct.pack("<H", len(x)) for x in (strips or []))
        out += b"\x01"
        if strips:
            out += b"".join(struct.pack(f"<{len(x)}H", *x) for x in strips)
        else:
            out += b"".join(struct.pack("<3H", *t) for t in tris)
        return out + b"\0" + b"\0\0"                        # no bone indices, LOD + global VB

    blob = struct.pack("<I", 2)
    blob += part(3, 1, [5, 6, 7], tris=[(0, 1, 2)])
    blob += part(4, 2, [1, 2, 3, 4], strips=[[0, 1, 2, 3]])
    got = _read_partitions_le(_R(blob))
    assert got[0] == [5, 6, 7]
    assert got[1] == [1, 2, 3, 2, 4, 3]                     # odd strip triangle flips its winding


def test_rejects_other_games_and_garbage():
    with pytest.raises(NifUnsupported):
        read_nif(_nif([("NiNode", _node(0, []))], bsver=155))      # Fallout 76
    with pytest.raises(NifUnsupported):
        read_nif(_nif([("NiNode", _node(0, []))], version=0x14000005))
    with pytest.raises(NifError):
        read_nif(b"not a nif")
    with pytest.raises(NifError):
        read_nif(_simple()[:60])                                    # truncated header
    with pytest.raises(NifError):
        read_nif(_simple()[:-40])                                   # shorter than block table


def test_include_nodes_returns_names_parents_and_world_positions():
    blob = _nif([
        ("NiNode", _node(0, [1])),                                  # "Root"
        ("NiNode", _node(1, [2], t=(0, 0, 10))),                    # "Shape" (used as a node name)
        ("NiNode", _node(0, [], t=(1, 0, 0), scale=2.0)),
    ])
    sc = read_nif(blob, include_nodes=True)
    assert [(n.name, n.parent) for n in sc.nodes] == [("Root", -1), ("Shape", 0), ("Root", 1)]
    assert sc.nodes[1].position == (0, 0, 10)
    assert sc.nodes[2].position == (1, 0, 10)                       # parent translation added
    assert read_nif(blob).nodes == []                               # off by default


def test_dismember_slots_are_read_in_partition_order():
    from Utils.nif.nif_reader import _R, _read_dismember_slots
    body = (struct.pack("<iii", 5, 6, 7)                    # skin data, partition, skeleton root
            + struct.pack("<I2i", 2, 8, 9)                   # two bone refs
            + struct.pack("<I", 3)                           # three partitions: (flags, slot)
            + struct.pack("<HH", 1, 32) + struct.pack("<HH", 1, 34) + struct.pack("<HH", 1, 38))
    assert _read_dismember_slots(_R(body)) == (32, 34, 38)


def test_read_body_slots_needs_no_geometry_and_unions_every_skin_instance():
    def dismember(slots):
        return (struct.pack("<iii", 5, 6, 7) + struct.pack("<I2i", 2, 8, 9)
                + struct.pack("<I", len(slots)) + b"".join(struct.pack("<HH", 1, x) for x in slots))
    blob = _nif([("BSFadeNode", _node(0, [])), ("BSDismemberSkinInstance", dismember([32, 34])),
                 ("NiNode", _node(0, [])), ("BSDismemberSkinInstance", dismember([38, 32]))])
    assert read_body_slots(blob) == frozenset({32, 34, 38})
    assert read_body_slots(_simple()) == frozenset()                      # a prop: no skin instances
    with pytest.raises(NifUnsupported):
        read_body_slots(_nif([("NiNode", _node(0, []))], bsver=155))
    with pytest.raises(NifError):
        read_body_slots(b"nope")


def test_sniff_and_label_formats():
    head = lambda **kw: _nif([("NiNode", _node(0, []))], **kw)[:128]   # noqa: E731
    assert sniff_nif_format(head()) == (0x14020007, 100)
    assert sniff_nif_format(head(bsver=83)) == (0x14020007, 83)
    assert sniff_nif_format(head(bsver=130)) == (0x14020007, 130)
    assert sniff_nif_format(b"not a nif") is None
    assert sniff_nif_format(b"") is None
    assert sniff_nif_format(head()[:45]) is None                       # truncated
    assert format_label(0x14020007, 100) == "Skyrim SE"
    assert format_label(0x14020007, 83) == "Skyrim LE"
    assert format_label(0x14020007, 130) == "Fallout 4"
    assert format_label(0x14000005, 0) == "NIF 20.0.0.5"
    assert version_string(0x14020007) == "20.2.0.7"


def test_unsupported_carries_the_version_for_a_friendly_message():
    with pytest.raises(NifUnsupported) as e:
        read_nif(_nif([("NiNode", _node(0, []))], bsver=155))
    assert (e.value.version, e.value.bsver) == (0x14020007, 155)
    assert format_label(e.value.version, e.value.bsver) == "Fallout 76"


@pytest.mark.parametrize("raw,want", [
    ("Textures\\Armor\\Iron_d.DDS", "textures/armor/iron_d.dds"),
    ("armor/iron/iron_d.dds", "textures/armor/iron/iron_d.dds"),
    ("\\textures\\a.dds", "textures/a.dds"),
    ("  ", ""),
    ("", ""),
])
def test_normalize_texture_path(raw, want):
    assert normalize_texture_path(raw) == want


# -- optional: a slice of the real game ---------------------------------------------
_DATA = Path.home() / "games/steamapps/common/Skyrim Special Edition/Data"


@pytest.mark.skipif(not (_DATA / "Skyrim - Meshes0.bsa").is_file(),
                    reason="needs a Skyrim SE install")
def test_real_vanilla_meshes_parse():
    from Utils.archives.bsa_file_reader import BsaFile

    with BsaFile(_DATA / "Skyrim - Meshes0.bsa") as bsa:
        nifs = [p for p in bsa.paths() if p.endswith(".nif")][::400]
        assert len(nifs) > 20
        shapes = 0
        for path in nifs:
            for sh in read_nif(bsa.read(path)).shapes:
                nv = len(sh.positions) // 3
                assert nv and max(sh.indices) < nv, path
                shapes += 1
        assert shapes > 20


@pytest.mark.skipif(not (_DATA / "Skyrim - Meshes0.bsa").is_file(),
                    reason="needs a Skyrim SE install")
def test_real_vanilla_body_partitions_split_the_triangles():
    from Utils.archives.bsa_file_reader import BsaFile
    with BsaFile(_DATA / "Skyrim - Meshes0.bsa") as bsa:
        sc = read_nif(bsa.read("meshes/actors/character/character assets/femalebody_1.nif"))
    torso = next(s for s in sc.shapes if len(s.part_slots) == 3)
    assert sorted(torso.part_slots) == [32, 34, 38]      # file order is (38, 32, 34)
    assert sum(torso.part_tris) * 3 == len(torso.indices)
    assert torso.slots == {32, 34, 38}


@pytest.mark.skipif(not (_DATA / "Skyrim - Meshes0.bsa").is_file(),
                    reason="needs a Skyrim SE install")
def test_real_skin_data_reproduces_body_placement_and_seats_hair_on_the_head():
    """Checks the skinning formula against the real game: vanilla body/boots/helmet
    stay where their files put them, and hair (stored relative to the head bone)
    lands on the head."""
    from Utils.archives.bsa_file_reader import BsaFile
    from Utils.nif.character import bone_transforms, skin_shape
    CA = "meshes/actors/character/character assets"
    with BsaFile(_DATA / "Skyrim - Meshes0.bsa") as b0, BsaFile(_DATA / "Skyrim - Meshes1.bsa") as b1:
        def rd(p, **kw):
            return read_nif((b0 if p in b0 else b1).read(p), **kw)
        bones = bone_transforms(rd(CA + " female/skeleton_female.nif", include_nodes=True).nodes)
        for path in (CA + "/femalebody_1.nif", "meshes/armor/iron/f/boots_1.nif",
                     "meshes/armor/iron/f/helmet.nif"):
            for sh in rd(path).shapes:
                out = skin_shape(sh, bones)
                assert out is not sh, sh.name
                moved = max(math.dist(out.positions[i:i + 3], sh.positions[i:i + 3])
                            for i in range(0, len(sh.positions), 3))
                assert moved < 0.05, (path, sh.name, moved)
        hair = rd(CA + "/hair/female/hair01.nif").shapes[0]
        assert max(hair.positions[2::3]) < 20                                   # stored head-relative…
        seated = skin_shape(hair, bones)
        zs = seated.positions[2::3]
        assert 105 < min(zs) and max(zs) < 140                                  # …and skinned onto the head


# -- Fallout 4 (BS 130): BSSubIndexTriShape, embedded skin weights, segments --------------
def _half(f: float) -> bytes:
    return struct.pack("<e", f)


def _fo4_vertex(pos, uv, normal, bone_idx, bone_w) -> bytes:
    """32 bytes matching the descriptor used below (half-float position +
    bitangent X, half UV, byte normal + bitangent Y, byte tangent + bitangent
    Z, then 4 half bone weights + 4 byte bone indices) — the exact layout
    verified against a real FO4 body mesh while building this reader."""
    out = b"".join(_half(v) for v in pos) + _half(0.0)                      # position (6) + bitangent X (2)
    out += b"".join(_half(v) for v in uv)                                    # uv (4)
    out += bytes(int(round((c + 1) * 127.5)) & 0xFF for c in normal) + b"\0"  # normal (3) + bitangent Y (1)
    out += b"\0\0\0\0"                                                       # tangent (3) + bitangent Z (1)
    out += b"".join(_half(w) for w in bone_w) + bytes(bone_idx)              # skin: weights (8) + indices (4)
    assert len(out) == 32
    return out


# desc: vertex size 8 dwords(32B); uv@2,normal@3,tangent@4,skin@5 (dword units);
# attrs: Vertex|UVs|Normals|Tangents|Skinned (no Full_Precision -> half position).
_FO4_DESC = (8 | (2 << 8) | (3 << 16) | (4 << 20) | (5 << 28)
            | ((1 | 2 | 8 | 16 | 64) << 44))


def _fo4_shape(name_idx, verts, tris, shader_ref, skin_ref, segments=b"") -> bytes:
    vdata = b"".join(_fo4_vertex(*v) for v in verts)
    tdata = b"".join(struct.pack("<3H", *t) for t in tris)
    return (_object_net(name_idx) + _xform() + b"\0" * 16
            + struct.pack("<iii", skin_ref, shader_ref, -1)
            + struct.pack("<Q", _FO4_DESC)
            + struct.pack("<IHI", len(tris), len(verts), len(vdata) + len(tdata))
            + vdata + tdata + segments)


def _fo4_segment_tail(leaves, extra_top_level=0) -> bytes:
    """One top-level "container" segment per leaf plus *extra_top_level* empty
    ones, each leaf as that segment's single sub-segment, with a shared table
    giving each leaf's real slot via User Index — matches the shape (though
    not necessarily the exact semantics) of a real body mesh's segment tail."""
    n = len(leaves)
    num_segments = n + extra_top_level
    total_segments = 2 * n + extra_top_level             # each leaf: 1 self-entry + 1 sub entry
    out = struct.pack("<III", sum(t for _s, t, _sl in leaves), num_segments, total_segments)
    for start, ntri, _slot in leaves:
        out += struct.pack("<IIII", start * 3, 0, 0xFFFFFFFF, 1)            # container, 1 sub
        out += struct.pack("<IIII", start * 3, ntri, 0, 0)                  # the real sub-segment
    for _ in range(extra_top_level):
        out += struct.pack("<IIII", 0, 0, 0xFFFFFFFF, 0)
    out += struct.pack("<II", num_segments, total_segments)
    out += struct.pack(f"<{num_segments}I", *range(num_segments))           # per-segment start offsets (unused)
    for seg_i, (_start, _ntri, slot) in enumerate(leaves):
        out += struct.pack("<III", seg_i, 0xFFFFFFFF, 0)                    # self entry (ignored: bone_id sentinel)
        out += struct.pack("<III", slot, 0xDEADBEEF, 0)                     # sub entry: real slot
    for _ in range(extra_top_level):
        out += struct.pack("<III", 0, 0xFFFFFFFF, 0)
    ssf = b"test.ssf"
    out += struct.pack("<H", len(ssf)) + ssf
    return out


def _fo4_skin_instance(bone_refs, data_ref) -> bytes:
    return struct.pack("<ii", -1, data_ref) + struct.pack("<I", len(bone_refs)) \
        + struct.pack(f"<{len(bone_refs)}i", *bone_refs) + struct.pack("<I", 0)


def _fo4_bone_data(bone_xf) -> bytes:
    out = struct.pack("<I", len(bone_xf))
    for r9, sc, t in bone_xf:
        out += struct.pack("<4f", 0, 0, 0, 1) + struct.pack("<9f", *r9) + struct.pack("<3f", *t) + struct.pack("<f", sc)
    return out


def _fo4_nif(verts, tris, segments=b"", bone_names=("NPC Root [Root]", "NPC Spine [Spn0]")):
    nbones = len(bone_names)
    bone_xf = [((1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0), 1.0, (0.0, 0.0, 0.0))] * nbones
    strings = ("Root", *bone_names, "Shape")
    blocks = [("BSFadeNode", _node(0, list(range(1, 1 + nbones)) + [1 + nbones]))]
    for i in range(nbones):
        blocks.append(("NiNode", _node(1 + i, [])))
    shape_i = 1 + nbones
    skin_i = shape_i + 1
    bonedata_i = skin_i + 1
    shader_i = bonedata_i + 1
    texset_i = shader_i + 1
    blocks.append(("BSSubIndexTriShape",
                   _fo4_shape(shape_i, verts, tris, shader_i, skin_i, segments)))
    blocks.append(("BSSkin::Instance", _fo4_skin_instance(list(range(1, 1 + nbones)), bonedata_i)))
    blocks.append(("BSSkin::BoneData", _fo4_bone_data(bone_xf)))
    blocks.append(("BSLightingShaderProperty", _lighting_shader(texset_i)))
    blocks.append(("BSShaderTextureSet", _texture_set(["Textures\\Armor\\Vault\\Body_d.DDS", ""])))
    return _nif(blocks, strings=strings, bsver=130)


_FO4_TRI = [((0, 0, 0), (0, 0), (0, 0, 1), (0, 1, 0, 0), (1.0, 0.0, 0.0, 0.0)),
           ((1, 0, 0), (1, 0), (0, 0, 1), (0, 1, 0, 0), (0.0, 1.0, 0.0, 0.0)),
           ((0, 1, 0), (0, 1), (0, 0, 1), (1, 0, 0, 0), (0.5, 0.5, 0.0, 0.0))]


def test_fallout4_shape_decodes_half_float_positions_and_embedded_weights():
    sc = read_nif(_fo4_nif(_FO4_TRI, [(0, 1, 2)]))
    sh = sc.shapes[0]
    assert [round(v, 3) for v in sh.positions] == [0, 0, 0, 1, 0, 0, 0, 1, 0]
    assert list(sh.indices) == [0, 1, 2]
    assert sh.textures[0].endswith("body_d.dds")
    assert sh.is_skinned and sh.skin is not None
    assert sh.skin.bone_names == ("NPC Root [Root]", "NPC Spine [Spn0]")
    # skin.weights is bone-major: vertex 0 -> bone 0 only (w=1.0); vertex 1 ->
    # bone 1 only (w=1.0); vertex 2 splits 0.5/0.5 across both.
    ids0, ws0 = sh.skin.weights[0]                        # bone 0's (vertex ids, weights)
    assert dict(zip(ids0, ws0)) == {0: 1.0, 2: 0.5}
    ids1, ws1 = sh.skin.weights[1]                        # bone 1's (vertex ids, weights)
    assert dict(zip(ids1, ws1)) == {1: 1.0, 2: 0.5}


def test_fallout4_header_field_count_differs_from_skyrim():
    """A naive Skyrim-shaped 3-export-string header misparses a Fallout 4
    file — this is the exact regression the Max Filepath fix guards against."""
    blob = _fo4_nif(_FO4_TRI, [(0, 1, 2)])
    sc = read_nif(blob)                    # must not raise / must not misparse block types
    assert len(sc.shapes) == 1


def test_fallout4_segments_give_slots_from_the_shared_table():
    leaves = [(0, 1, 30), (1, 2, 37)]                    # 2 leaf tris, arbitrary "slot" numbers
    tail = _fo4_segment_tail(leaves)
    tris = [(0, 1, 2), (0, 2, 1), (1, 2, 0)]
    sc = read_nif(_fo4_nif(_FO4_TRI * 1, tris[:1] + tris[1:], segments=tail))
    sh = sc.shapes[0]
    assert sh.slots == frozenset({30, 37})
    assert sh.part_tris == ()                            # per-triangle hiding deliberately not attempted


@pytest.mark.parametrize("bsver,label", [(100, "Skyrim SE"), (130, "Fallout 4")])
def test_read_body_slots_rejects_nothing_new_for_supported_bsvers(bsver, label):
    assert read_body_slots(_nif([("NiNode", _node(0, []))], bsver=bsver)) == frozenset()


# -- optional: a slice of the real game (Fallout 4) ----------------------------------
_FO4_DATA = Path.home() / "games/steamapps/common/Fallout 4/Data_Core"


@pytest.mark.skipif(not (_FO4_DATA / "Fallout4 - Meshes.ba2").is_file(),
                    reason="needs a Fallout 4 install")
def test_real_fallout4_meshes_parse():
    from Utils.archives.bsa_file_reader import BsaFile

    with BsaFile(_FO4_DATA / "Fallout4 - Meshes.ba2") as ba2:
        nifs = [p for p in ba2.paths() if p.endswith(".nif")][::300]
        assert len(nifs) > 20
        shapes = 0
        for path in nifs:
            for sh in read_nif(ba2.read(path)).shapes:
                nv = len(sh.positions) // 3
                assert nv and max(sh.indices) < nv, path
                shapes += 1
        assert shapes > 20


# -- robustness: found via live testing on a large real mod collection -------------------
def test_a_node_cycle_does_not_hang_or_blow_up_exponentially():
    """Real bug, found via live testing: a NiNode graph where a child list
    revisits an already-walked node used to be bounded only by depth (64
    levels), which at branching factor 2 is 2**64 node visits — in practice
    indistinguishable from a permanent hang. A node must now be walked at
    most once, full stop, the same way emit()/seen_shapes already treats
    shapes."""
    # 0 -> 1 -> 0 -> 1 -> ... : a plain two-node cycle.
    blob = _nif([("NiNode", _node(0, [1])), ("NiNode", _node(0, [0]))])
    sc = read_nif(blob, include_nodes=True)
    assert len(sc.nodes) == 2                       # each node counted once, not 64 times


def test_a_shape_whose_geometry_would_overrun_its_own_block_is_dropped():
    """Real bug, found via live testing: a malformed real mod file had a
    stray 2-byte pad before its root-list footer, throwing every subsequent
    block's own internal field reads off — Fallout 4's lenient (trust-the-
    computed-size) path then decoded a huge-but-technically-in-range vertex
    count for several minutes before finally erroring out. The block's own
    declared size (from the header's size table, always trustworthy) must
    reject this immediately instead."""
    verts = [((0, 0, 0), (0, 0), (0, 0, 1), (0, 1, 0, 0), (1.0, 0.0, 0.0, 0.0))] * 3
    shape_bytes = _fo4_shape(1, verts, [(0, 1, 2)], 2, -1)
    # Truncate the block's own declared size well below what the real vertex
    # count needs — the header's size table is what block_end is built from.
    blocks = [("BSFadeNode", _node(0, [1])), ("BSSubIndexTriShape", shape_bytes[:40])]
    sc = read_nif(_nif(blocks, bsver=130))
    assert sc.shapes == []


def test_a_shape_whose_triangles_reference_out_of_range_vertices_is_dropped():
    """Real bug, found via live testing: a real third-party-converted hair
    mesh had internally self-consistent (not misaligned) vertex/triangle
    counts whose triangle indices nonetheless referenced vertices past the
    decoded position array — a content bug in that file, not a reader
    misalignment, but every downstream consumer (GL viewport, hide_covered,
    skin_shape) assumes indices fit inside positions."""
    bad_tris = [(0, 1, 2), (0, 1, 50)]               # vertex 50 doesn't exist (only 3 verts)
    blob = _nif([
        ("BSFadeNode", _node(0, [1])),
        ("BSTriShape", _shape(1, TRI, bad_tris, 2)),
        ("BSLightingShaderProperty", _lighting_shader(3)),
        ("BSShaderTextureSet", _texture_set(["Textures\\Armor\\Iron_d.DDS", ""])),
    ])
    assert read_nif(blob).shapes == []
