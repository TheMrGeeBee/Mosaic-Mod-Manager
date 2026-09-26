"""Character assembly: body/skeleton choice, slot-based hiding, bone selection."""
from __future__ import annotations

from array import array

import pytest

from Utils.nif.character import (
    SKELETONS, auto_gender, body_paths, compose, covered_slots, detect_gender,
    detect_weight, hide_covered, is_skinned,
)
from Utils.nif.nif_reader import NifNode, NifScene, NifShape
from Utils.nif.skeleton import bone_segments, is_bone


def shape(name, part_slots=(), part_tris=(), skinned=True, slots=None):
    """A skinned shape whose partitions own the given triangle counts, in order."""
    n = sum(part_tris) or 1
    idx = array("H", [i % 3 for i in range(n * 3)])
    return NifShape(name=name, positions=array("f", [0, 0, 0, 1, 0, 0, 0, 1, 0]),
                    normals=None, uvs=None, indices=idx, is_skinned=skinned,
                    slots=frozenset(part_slots if slots is None else slots),
                    part_slots=tuple(part_slots), part_tris=tuple(part_tris))


# -- names ----------------------------------------------------------------------------------
@pytest.mark.parametrize("path,want", [
    ("meshes/armor/iron/f/cuirass_1.nif", "female"),
    ("meshes/armor/iron/m/cuirass_1.nif", "male"),
    ("meshes/actors/character/character assets/femalebody_1.nif", "female"),
    ("meshes/actors/character/character assets/malebody_1.nif", "male"),
    ("Meshes\\Armor\\Hide\\F\\Boots_0.nif", "female"),
    ("meshes/armor/ebony/femalecuirass.nif", "female"),       # "female" is checked before "male"
    ("meshes/armor/thing/cuirass.nif", None),
    ("meshes/clutter/mug.nif", None),
])
def test_detect_gender(path, want):
    assert detect_gender(path) == want


def test_weight_and_paths():
    assert detect_weight("a/cuirass_0.nif") == 0
    assert detect_weight("a/cuirass_1.nif") == 1
    assert detect_weight("a/cuirass.nif") == 1
    assert body_paths("female", 0) == [
        "meshes/actors/character/character assets/femalebody_0.nif",
        "meshes/actors/character/character assets/femalehands_0.nif",
        "meshes/actors/character/character assets/femalefeet_0.nif"]
    assert set(SKELETONS) == {"male", "female"}


def test_auto_gender_only_for_wearable_skinned_gear():
    armor = NifScene([shape("c", [32], [4])])
    assert auto_gender("meshes/armor/iron/m/cuirass_1.nif", armor) == "male"
    assert auto_gender("meshes/armor/iron/cuirass_1.nif", armor) == "female"      # path silent
    assert auto_gender("meshes/clothes/x/f/dress.nif", armor) == "female"
    assert auto_gender("meshes/actors/dragon/dragon.nif", armor) is None          # not gear
    assert auto_gender("meshes/armor/iron/f/x.nif", NifScene([shape("s", skinned=False)])) is None
    assert auto_gender("meshes/armor/iron/f/x.nif", NifScene([shape("s", [], [], True, slots=[])])) is None


# -- slot hiding -------------------------------------------------------------------------------
def test_covered_slots_ignores_unskinned_shapes():
    sc = NifScene([shape("a", [32, 38], [2, 1]), shape("b", [33], [1], skinned=False, slots=[33])])
    assert covered_slots(sc) == {32, 38}
    assert is_skinned(sc)


def test_hide_covered_drops_only_the_covered_partitions():
    body = shape("body", [32, 34, 38], [4, 2, 3])          # torso / forearms / calves
    boots_cover = frozenset({37, 38})
    kept = hide_covered(body, boots_cover)
    assert kept.part_slots == (32, 34) and kept.part_tris == (4, 2)
    assert len(kept.indices) == 6 * 3                       # calves' 3 triangles gone
    assert list(kept.indices) == list(body.indices[:6 * 3])  # the torso + forearm triangles, in order
    assert body.part_tris == (4, 2, 3)                      # original untouched


def test_hide_covered_middle_partition_keeps_order():
    body = shape("body", [32, 34, 38], [1, 2, 1])
    kept = hide_covered(body, frozenset({34}))
    assert kept.part_slots == (32, 38)
    assert list(kept.indices) == list(body.indices[:3]) + list(body.indices[9:])


def test_hide_covered_edge_cases():
    body = shape("body", [32], [2])
    assert hide_covered(body, frozenset()) is body           # nothing covered
    assert hide_covered(body, frozenset({33})) is body       # no overlap
    assert hide_covered(body, frozenset({32})) is None       # everything covered → dropped
    unsplit = NifShape("u", array("f", [0] * 9), None, None, array("H", [0, 1, 2]),
                       is_skinned=True, slots=frozenset({32, 34}))
    assert hide_covered(unsplit, frozenset({32})) is unsplit         # can't split: keep
    assert hide_covered(unsplit, frozenset({32, 34, 38})) is None    # wholly covered: drop


def test_compose_puts_the_selection_first_and_hides_covered_body_parts():
    boots = NifScene([shape("boots", [37, 38], [3])])
    bodies = [NifScene([shape("torso", [32, 34, 38], [4, 2, 3])]),
              NifScene([shape("hands", [33], [2])]),
              NifScene([shape("feet", [37], [2])])]
    out = compose(boots, bodies)
    assert [s.name for s in out.shapes] == ["boots", "torso", "hands"]    # feet replaced
    torso = out.shapes[1]
    assert torso.part_slots == (32, 34)                                   # calves replaced
    cuirass = NifScene([shape("cuirass", [32, 38], [5]), shape("embedded_body", [32, 34, 38], [4])])
    out = compose(cuirass, bodies)
    assert [s.name for s in out.shapes] == ["cuirass", "embedded_body", "hands", "feet"]


def test_selecting_a_body_mod_replaces_the_vanilla_body_but_keeps_hands_and_feet():
    custom_body = NifScene([shape("cbbe_body", [32, 34, 38], [8])])
    bodies = [NifScene([shape("torso", [32, 34, 38], [4])]),
              NifScene([shape("hands", [33], [2])]), NifScene([shape("feet", [37], [2])])]
    assert [s.name for s in compose(custom_body, bodies).shapes] == ["cbbe_body", "hands", "feet"]


# -- skeleton ---------------------------------------------------------------------------------------
def nodes():
    return [NifNode("skeleton_female.nif", -1, (0, 0, 0)),
            NifNode("NPC Root [Root]", 0, (0, 0, 0)),
            NifNode("NPC COM [COM ]", 1, (0, 0, 69)),
            NifNode("WeaponSword", 2, (5, 0, 60)),                  # helper: not a bone
            NifNode("NPC Spine [Spn0]", 3, (0, -5, 73)),            # parent is a helper → skips to COM
            NifNode("Camera3rd [Cam3]", 1, (0, 0, 121)),
            NifNode("", 1, (9, 9, 9))]


def test_is_bone():
    n = nodes()
    assert [is_bone(x) for x in n] == [False, True, True, False, True, False, False]


def test_bone_segments_connect_to_the_nearest_bone_ancestor():
    lines, joints = bone_segments(nodes())
    assert len(joints) // 3 == 3                                   # Root, COM, Spine
    segs = [tuple(lines[i:i + 6]) for i in range(0, len(lines), 6)]
    assert segs == [(0, 0, 0, 0, 0, 69),                            # Root → COM
                    (0, 0, 69, 0, -5, 73)]                          # COM → Spine (the sword skipped)
