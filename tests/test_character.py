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


# -- equipment groups and assembly ---------------------------------------------------------------
from Utils.nif.character import (  # noqa: E402
    GROUPS, LAYER_ORDER, assemble, guess_gender, paired_gender, slot_group, weight_variant,
)


@pytest.mark.parametrize("path,slots,want", [
    ("meshes/armor/iron/f/cuirass_1.nif", {32, 34, 38}, "body"),
    ("meshes/armor/iron/f/gauntlets_1.nif", {33, 34}, "hands"),
    ("meshes/armor/iron/f/boots_1.nif", {37, 38}, "feet"),
    ("meshes/armor/iron/f/helmet.nif", {131}, "head"),                  # helmet …
    ("meshes/actors/character/character assets/hair/male/hair01.nif", {131, 141}, "hair"),  # … hair, same slot
    ("meshes/armor/x/circlet.nif", {142}, "circlet"),
    ("meshes/armor/x/amulet.nif", {35}, "amulet"),
    ("meshes/armor/x/ring.nif", {36}, "ring"),
    ("meshes/armor/x/sleeves.nif", {34}, "arms"),
    ("meshes/armor/x/greaves.nif", {38}, "legs"),
    ("meshes/armor/x/robe.nif", {32, 33, 34, 37}, "body"),               # body wins over its extras
    ("meshes/clutter/mug.nif", set(), None),
    ("meshes/armor/x/weird.nif", {5}, None),
])
def test_slot_group(path, slots, want):
    assert slot_group(path, frozenset(slots)) == want


def test_groups_and_layers_are_consistent():
    keys = [g for g, _ in GROUPS]
    assert len(keys) == len(set(keys))
    assert set(LAYER_ORDER) == set(keys)


def test_assemble_layers_pieces_over_the_base():
    base = [NifScene([shape("torso", [32, 34, 38], [4, 2, 3])]),
            NifScene([shape("hands", [33], [2])]), NifScene([shape("feet", [37], [2])]),
            NifScene([shape("head", [130], [5])])]
    boots = NifScene([shape("boots", [37, 38], [3])])
    helmet = NifScene([shape("helm", [131], [4])])
    out = assemble(base, {"feet": boots, "head": helmet})
    names = [s.name for s in out.shapes]
    assert names == ["torso", "hands", "head", "boots", "helm"]        # base feet replaced, layers in order
    assert out.shapes[0].part_slots == (32, 34)                        # calves hidden by the boots


def test_a_later_layer_hides_the_hair_it_overlaps():
    hair = NifScene([shape("hair", [131, 141], [3, 2])])
    helmet = NifScene([shape("helm", [131], [4])])
    out = assemble([], {"head": helmet, "hair": hair})                 # dict order must not matter
    names = [s.name for s in out.shapes]
    assert names == ["hair", "helm"]
    assert out.shapes[0].part_slots == (141,)                          # the hair's 131 part is under the helmet


def test_assemble_with_nothing_equipped_is_just_the_base():
    base = [NifScene([shape("torso", [32], [2])])]
    assert [s.name for s in assemble(base, {}).shapes] == ["torso"]


# -- skinning -------------------------------------------------------------------------------------------
import math  # noqa: E402

from Utils.nif.character import bone_transforms, skin_scene, skin_shape  # noqa: E402
from Utils.nif.nif_reader import SkinData  # noqa: E402

IDENT = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
RZ90 = (0.0, -1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)          # +90° about Z, row-major


def skinned(local, weights, bone_names, xf, normals=None, baked=None):
    """A shape with vertices *local* (flat xyz), per-bone (ids, weights) and skin→bone transforms."""
    lp = array("f", local)
    sk = SkinData(tuple(bone_names), tuple(xf),
                  tuple((array("H", ids), array("f", ws)) for ids, ws in weights), lp,
                  array("f", normals) if normals else None)
    return NifShape("s", array("f", baked if baked is not None else local),
                    array("f", normals) if normals else None, None,
                    array("H", [0, 1, 2]), is_skinned=True, skin=sk)


def test_bones_from_nodes():
    nodes = [NifNode("a", -1, (1, 2, 3), RZ90, 2.0), NifNode("", 0, (0, 0, 0))]
    assert bone_transforms(nodes) == {"a": (RZ90, 2.0, (1, 2, 3))}            # unnamed nodes skipped


def test_skin_places_a_bone_relative_mesh_on_the_bone():
    """A hair-like shape (skin→bone = identity) sits at the bone's world transform."""
    sh = skinned([0, 0, 0, 1, 0, 0, 0, 1, 0], [([0, 1, 2], [1, 1, 1])], ["Head"],
                 [(IDENT, 1.0, (0, 0, 0))])
    out = skin_shape(sh, {"Head": (IDENT, 1.0, (0, 0, 120))})
    assert list(out.positions) == [0, 0, 120, 1, 0, 120, 0, 1, 120]
    rot = skin_shape(sh, {"Head": (RZ90, 1.0, (0, 0, 120))})                  # bone rotated 90°: x → y
    assert [round(v, 5) for v in rot.positions[3:6]] == [0.0, 1.0, 120.0]
    assert sh.positions[2] == 0                                                # original untouched


def test_skin_reproduces_the_authored_placement_for_skeleton_space_meshes():
    """Armour/body: bone_world · skin_to_bone equals where the file already puts the shape."""
    world = (IDENT, 1.0, (0, 0, 68.9))
    s_to_b = (IDENT, 1.0, (0, -1.55, 51.43))                                   # → total (0,-1.55,120.33)
    baked = [0, -1.55, 120.33, 1, -1.55, 120.33, 0, -0.55, 120.33]
    sh = skinned([0, 0, 0, 1, 0, 0, 0, 1, 0], [([0, 1, 2], [1, 1, 1])], ["Pelvis"], [s_to_b], baked=baked)
    out = skin_shape(sh, {"Pelvis": world})
    assert all(math.isclose(a, b, abs_tol=1e-4) for a, b in zip(out.positions, baked))


def test_blended_weights_are_normalised_and_unweighted_vertices_stay():
    sh = skinned([0, 0, 0, 0, 0, 0, 5, 5, 5], [([0], [0.25]), ([0], [0.25])], ["A", "B"],
                 [(IDENT, 1.0, (0, 0, 0))] * 2, baked=[9, 9, 9, 9, 9, 9, 7, 7, 7])
    out = skin_shape(sh, {"A": (IDENT, 1.0, (0, 0, 0)), "B": (IDENT, 1.0, (10, 0, 0))})
    assert list(out.positions[0:3]) == [5.0, 0.0, 0.0]                         # halfway, despite weights summing to 0.5
    assert list(out.positions[3:6]) == [9, 9, 9] and list(out.positions[6:9]) == [7, 7, 7]  # unweighted: as before


def test_normals_rotate_with_the_bone():
    sh = skinned([0, 0, 0, 1, 0, 0, 0, 1, 0], [([0, 1, 2], [1, 1, 1])], ["Head"],
                 [(IDENT, 1.0, (0, 0, 0))], normals=[1, 0, 0] * 3)
    out = skin_shape(sh, {"Head": (RZ90, 1.0, (0, 0, 0))})
    assert [round(v, 5) for v in out.normals[0:3]] == [0.0, 1.0, 0.0]


def test_a_missing_bone_leaves_the_shape_alone():
    sh = skinned([0, 0, 0, 1, 0, 0, 0, 1, 0], [([0], [1]), ([1], [1])], ["Head", "Cape"],
                 [(IDENT, 1.0, (0, 0, 0))] * 2)
    assert skin_shape(sh, {"Head": (IDENT, 1.0, (0, 0, 5))}) is sh            # "Cape" not on this skeleton
    plain = NifShape("p", array("f", [0] * 9), None, None, array("H", [0, 1, 2]))
    assert skin_shape(plain, {"Head": (IDENT, 1.0, (0, 0, 5))}) is plain
    assert skin_scene(NifScene([plain]), {}).shapes[0] is plain


def test_assemble_poses_pieces_on_the_skeleton_when_given_bones():
    hair = NifScene([skinned([0, 0, 0, 1, 0, 0, 0, 1, 0], [([0, 1, 2], [1, 1, 1])], ["Head"],
                             [(IDENT, 1.0, (0, 0, 0))])])
    hair.shapes[0] = replace_slots(hair.shapes[0], [131], [1])
    out = assemble([], {"hair": hair}, {"Head": (IDENT, 1.0, (0, 0, 120))})
    assert out.shapes[0].positions[2] == 120
    assert assemble([], {"hair": hair}).shapes[0].positions[2] == 0            # no bones: unchanged


def replace_slots(sh, slots, tris):
    from dataclasses import replace
    return replace(sh, slots=frozenset(slots), part_slots=tuple(slots), part_tris=tuple(tris))


def test_head_paths_include_the_eyes():
    p = body_paths("female", 1, head=True)
    assert p[-2:] == ["meshes/actors/character/character assets/femalehead.nif",
                      "meshes/actors/character/character assets/eyesfemale.nif"]
    assert len(body_paths("male", 1)) == 3


@pytest.mark.parametrize("path,weight,want", [
    ("meshes/armor/iron/f/cuirass_1.nif", 0, "meshes/armor/iron/f/cuirass_0.nif"),
    ("meshes/armor/iron/f/cuirass_0.nif", 1, "meshes/armor/iron/f/cuirass_1.nif"),
    ("meshes/armor/iron/f/cuirass_1.nif", 1, "meshes/armor/iron/f/cuirass_1.nif"),   # already matching
    ("Meshes\\Armor\\Blades\\BladesArmorF_1.NIF", 0, "Meshes\\Armor\\Blades\\BladesArmorF_0.NIF"),
    ("meshes/armor/blades/bladeshelmet.nif", 0, None),                                # no weight suffix
    ("meshes/armor/x/greaves_2.nif", 1, None),                                        # not a weight suffix
    ("noextension_1", 0, None),
])
def test_weight_variant(path, weight, want):
    assert weight_variant(path, weight) == want


# -- gender from the f-suffix pair (Blades armour: bladesboots_1 male, bladesbootsf_1 female) -------------------
BLADES = ["meshes/armor/blades/" + n for n in (
    "bladesboots_0.nif", "bladesboots_1.nif", "bladesbootsf_0.nif", "bladesbootsf_1.nif",
    "bladeshelmet.nif", "bladeshelmetf.nif", "bladesarmor.nif", "bladesarmor_1.nif")]


@pytest.mark.parametrize("path,want", [
    ("meshes/armor/blades/bladesbootsf_0.nif", "female"),
    ("meshes/armor/blades/bladesbootsf_1.nif", "female"),
    ("meshes/armor/blades/bladesboots_1.nif", "male"),                # has an f-counterpart → the male one
    ("meshes/armor/blades/bladeshelmetf.nif", "female"),               # no weight suffix at all
    ("meshes/armor/blades/bladeshelmet.nif", "male"),
    ("meshes/armor/blades/bladesarmor_1.nif", None),                   # bladesarmorf isn't in this list
    ("MESHES\\ARMOR\\BLADES\\BLADESBOOTSF_1.NIF", "female"),        # case / slashes
])
def test_paired_gender(path, want):
    assert paired_gender(path, BLADES) == want


def test_a_trailing_f_alone_proves_nothing():
    assert paired_gender("meshes/armor/x/wolf_1.nif", ["meshes/armor/x/wolf_1.nif", "meshes/armor/x/scarf_1.nif"]) is None
    assert paired_gender("meshes/armor/x/scarf_1.nif", ["meshes/armor/x/scarf_1.nif"]) is None
    # A counterpart in ANOTHER folder doesn't count.
    assert paired_gender("meshes/armor/a/bootsf_1.nif", ["meshes/armor/b/boots_1.nif"]) is None


def test_guess_gender_prefers_explicit_markers_then_the_pair():
    assert guess_gender("meshes/armor/iron/m/boots_1.nif", ["meshes/armor/iron/m/bootsf_1.nif"]) == "male"   # folder wins
    assert guess_gender("meshes/armor/blades/bladesbootsf_1.nif", BLADES) == "female"
    assert guess_gender("meshes/armor/blades/bladesbootsf_1.nif") is None                                   # no siblings given


def test_auto_gender_uses_the_pair():
    armor = NifScene([shape("b", [37, 38], [3])])
    assert auto_gender("meshes/armor/blades/bladesboots_1.nif", armor, BLADES) == "male"
    assert auto_gender("meshes/armor/blades/bladesbootsf_1.nif", armor, BLADES) == "female"
    assert auto_gender("meshes/armor/blades/bladesboots_1.nif", armor) == "female"     # nothing known → default
