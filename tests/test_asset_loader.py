"""AssetLoader's material resolution: a shape whose own embedded texture set
leaves its diffuse blank (or wrong) because it relies on an external
.bgsm/.bgem gets that diffuse filled in from the real material file, resolved
through the same catalog as everything else."""
from __future__ import annotations

from Utils.nif.asset_catalog import AssetCatalog
from gui_qt.nif_viewer.asset_loader import AssetLoader
from test_material_reader import _bgsm
from test_nif_reader import TRI, _lighting_shader, _nif, _node, _shape, _texture_set

MESH = "meshes/cross/coa/chest.nif"
MAT = "materials/cross/coa/coa_02.bgsm"


def _mesh_with_material(name_idx=2, texset=("",)):
    return _nif([
        ("BSFadeNode", _node(0, [1])),
        ("BSTriShape", _shape(1, TRI, [(0, 1, 2)], 2)),
        ("BSLightingShaderProperty", _lighting_shader(3, name_idx=name_idx)),
        ("BSShaderTextureSet", _texture_set(list(texset))),
    ], strings=("Root", "Shape", MAT.replace("materials/", "Materials\\").replace("/", "\\")))


def _catalog(tmp_path, files: dict) -> AssetCatalog:
    mod = tmp_path / "modX"
    for rel, data in files.items():
        p = mod / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return AssetCatalog(
        base_name="Game", base_archives=[], mod_order=["modX"],
        loose={"modX": {k: k for k in files}}, bsas={}, loose_winner={}, bsa_winner={},
        mod_dir_for=lambda m: tmp_path / m)


def test_a_shapes_diffuse_comes_from_its_material_when_it_has_one(tmp_path):
    cat = _catalog(tmp_path, {MESH: _mesh_with_material(), MAT: _bgsm()})
    loader = AssetLoader()
    sc = loader.nif_entry(cat, cat.resolve(MESH))
    assert sc.shapes[0].material_name == MAT
    assert sc.shapes[0].textures[0] == "textures/cross/coa/coa_02_d.dds"
    cat.close()


def test_material_wins_even_over_a_non_empty_embedded_slot(tmp_path):
    # Verified on real files: the embedded slot can hold a stray leftover
    # path (e.g. a normal map) that the engine never actually reads once a
    # material is referenced — the material must still win.
    cat = _catalog(tmp_path, {MESH: _mesh_with_material(texset=("textures/leftover_n.tga",)), MAT: _bgsm()})
    loader = AssetLoader()
    sc = loader.nif_entry(cat, cat.resolve(MESH))
    assert sc.shapes[0].textures[0] == "textures/cross/coa/coa_02_d.dds"
    cat.close()


def test_no_material_reference_leaves_the_embedded_texture_alone(tmp_path):
    mesh = _nif([
        ("BSFadeNode", _node(0, [1])),
        ("BSTriShape", _shape(1, TRI, [(0, 1, 2)], 2)),
        ("BSLightingShaderProperty", _lighting_shader(3, name_idx=-1)),
        ("BSShaderTextureSet", _texture_set(["textures/plain_d.dds"])),
    ])
    cat = _catalog(tmp_path, {MESH: mesh})
    loader = AssetLoader()
    sc = loader.nif_entry(cat, cat.resolve(MESH))
    assert sc.shapes[0].material_name == ""
    assert sc.shapes[0].textures[0] == "textures/plain_d.dds"
    cat.close()


def test_a_missing_material_file_does_not_crash_or_change_the_shape(tmp_path):
    cat = _catalog(tmp_path, {MESH: _mesh_with_material(texset=("textures/leftover_n.tga",))})  # no MAT file
    loader = AssetLoader()
    sc = loader.nif_entry(cat, cat.resolve(MESH))
    assert sc.shapes[0].textures[0] == "textures/leftover_n.tga"   # unchanged: nothing to fall back to
    cat.close()


def test_the_same_material_is_only_read_once_across_shapes(tmp_path, monkeypatch):
    from Utils.nif.nif_reader import read_nif

    cat = _catalog(tmp_path, {MESH: _mesh_with_material(), MAT: _bgsm()})
    loader = AssetLoader()
    mesh_bytes = cat.read(cat.resolve(MESH))                # read once, outside the tracked window

    reads = []
    real_read = cat.read
    monkeypatch.setattr(cat, "read", lambda e: (reads.append(e.path), real_read(e))[1])

    sc = loader._apply_materials(cat, read_nif(mesh_bytes))     # first: a real cache miss
    after_first = reads.count(MAT)
    assert after_first > 0
    sc2 = loader._apply_materials(cat, read_nif(mesh_bytes))    # second: same material, must be cached
    assert sc.shapes[0].textures[0] == sc2.shapes[0].textures[0] == "textures/cross/coa/coa_02_d.dds"
    assert reads.count(MAT) == after_first     # no additional reads: served from _mat_cache
    cat.close()
