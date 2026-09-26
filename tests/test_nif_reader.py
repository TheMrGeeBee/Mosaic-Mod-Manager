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
    sniff_nif_format, version_string,
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
    out += b"\x01\0" * 3
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


def test_rejects_other_games_and_garbage():
    with pytest.raises(NifUnsupported):
        read_nif(_nif([("NiNode", _node(0, []))], bsver=83))       # Skyrim LE
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
        read_nif(_nif([("NiNode", _node(0, []))], bsver=83))
    assert (e.value.version, e.value.bsver) == (0x14020007, 83)
    assert format_label(e.value.version, e.value.bsver) == "Skyrim LE"


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
