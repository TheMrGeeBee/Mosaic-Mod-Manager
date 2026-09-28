"""Assemble a character preview: a selected mesh (armour, clothes, a body mod)
worn on the game's own base body, with the game's skeleton for the bone overlay.

Which body/skeleton files to use, and which body parts an armour replaces, come
from the same conventions the game uses (vanilla file names; the "slot" of each
skinned partition). Nothing here reads files: callers resolve paths through the
asset catalog, so a body or skeleton replacer mod is picked up automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field, replace
from typing import Iterable

from array import array

from Utils.nif.nif_reader import NifNode, NifScene, NifShape, compose_transform


@dataclass(frozen=True)
class GameProfile:
    """Everything about how a game lays out its character assets and slots
    that the functions below need — so the same assembly/skinning/hiding
    logic works for a second game without duplicating it. Defaults (the bare
    module-level constants, and every function's default parameter) are
    Skyrim's; pass a different profile — currently only FALLOUT4_PROFILE — to
    use another game's conventions instead."""
    body_dir: str
    skeletons: dict                    # {"male"/"female": path}
    groups: list                       # [(key, label), ...], display order
    layer_order: list                  # group keys, base-to-outermost draw order
    slot_groups: dict                  # {group_key: frozenset of slot numbers}
    base_parts: tuple                  # body-file components ("body", "hands", …)
    has_weight_suffix: bool = True      # base body files come in _0/_1 pairs
    fold_duplicate_slots: bool = False  # Skyrim's "+100" duplicate slot range
    hair_needs_folder_tiebreak: bool = False   # Skyrim: helmet and hair share slot 131
    # Whether one equipped piece can hide the covered partitions of an EARLIER
    # equipped piece (not the base body — that's always hidden, unconditionally,
    # by every piece). True for Skyrim, where a mesh's own embedded partition
    # numbers ARE the real BOD2 equip-slot numbers (30 head/31 hair/… — verified,
    # this is why a helmet correctly hides hair: its own partitions really do
    # include slot 131). False for Fallout 4: its meshes embed a completely
    # different, per-mesh "Biped Object" numbering for internal dismemberment,
    # NOT the ARMA/BOD2 equip-slot numbers — two unrelated pieces can share a
    # small Biped Object id by coincidence (verified: a real left-leg piece and
    # a real torso piece both used internal id 3) and wrongly wipe each other
    # out under the old single mechanism. Real ARMA-based per-slot equip
    # exclusivity would be the fully correct fix, but the base-body hide
    # already works via this same embedded-number comparison and must stay
    # untouched, so for now this profile flag simply stops FO4 pieces from
    # hiding each other, matching how the tab's UI already treats each group
    # as its own independent slot the user picks directly.
    pieces_hide_each_other: bool = True
    head_fmt: str = "{gender}head.nif"
    eyes_fmt: "str | None" = "eyes{gender}.nif"   # None: eyes are part of the head mesh itself
    # Equip groups the base body/hands file(s) alone can satisfy in the picker
    # (a "body" file's own partitions also cover arms/legs, even though the
    # file itself isn't named after them).
    base_part_groups: frozenset = dc_field(default_factory=lambda: frozenset(
        {"body", "hands", "feet", "arms", "legs"}))
    # Which group wins when a mesh's slots match more than one group's keys —
    # checked in this order, first match wins; groups not listed keep falling
    # back to slot_groups' own dict order. None (Skyrim's default) means
    # "just use slot_groups' dict order", unchanged from before this field
    # existed. Fallout 4 needs an explicit order: a real "torso" armor piece
    # legitimately reserves slot 30 (head) too, purely to hide clipping with
    # a separate helmet — but slot_groups happens to list "head" before
    # "torso", so the old dict-order matching put it in the Head picker
    # instead, a real bug (verified: a real chest/back rig equipped into
    # "Head"). Reservation-only slots (head/eyes/pipboy/backpack) are listed
    # last so whatever piece a mesh actually covers always wins over a slot
    # it's merely reserving.
    group_priority: "tuple | None" = None
    # {group_key: bone name} — a group whose real vanilla mesh can ship with
    # NO skin data at all (verified: every shape in FO4's own pipboy.nif has
    # is_skinned=False) and instead relies on a runtime engine convention
    # (attach the whole mesh rigidly to this one bone) that no static NIF
    # field records — skin_shape() has nothing to pose such a shape with, so
    # it was left at its raw file-local coordinates (near the world origin,
    # i.e. visibly at the character's feet instead of the wrist). Verified
    # against a real skinned alternate of the same item (pipboylowvault81.nif,
    # which DOES carry real skin weights to "PipboyBone" and renders
    # correctly): rigidly attaching the unskinned vanilla mesh to that same
    # bone lands it in the same wrist-height region.
    rigid_attach: dict = dc_field(default_factory=dict)


BODY_DIR = "meshes/actors/character/character assets/"
SKELETONS = {
    "male": BODY_DIR + "skeleton.nif",
    "female": "meshes/actors/character/character assets female/skeleton_female.nif",
}



# -- equipment slots ----------------------------------------------------------------------------
# What a character can wear, in the order the slot list shows them. Each piece
# belongs to exactly one group; equipping into a group replaces what was there.
GROUPS = [("head", "Head"), ("hair", "Hair"), ("circlet", "Circlet"), ("ears", "Ears"),
          ("amulet", "Amulet"), ("body", "Body"), ("arms", "Forearms"), ("hands", "Hands"),
          ("ring", "Ring"), ("legs", "Calves"), ("feet", "Feet")]
GROUP_LABELS = dict(GROUPS)

# Layering when pieces overlap: later entries are drawn "on top" and hide the
# covered partitions of everything before them (base body first, hair lowest).
LAYER_ORDER = ["hair", "ears", "body", "arms", "legs", "hands", "feet", "amulet", "ring",
               "head", "circlet"]

SKYRIM_PROFILE = GameProfile(
    body_dir=BODY_DIR, skeletons=SKELETONS, groups=GROUPS, layer_order=LAYER_ORDER,
    slot_groups={"body": frozenset({32}), "hands": frozenset({33}), "feet": frozenset({37}),
                "head": frozenset({30, 31}), "circlet": frozenset({42}), "amulet": frozenset({35}),
                "ring": frozenset({36}), "ears": frozenset({43}), "arms": frozenset({34}),
                "legs": frozenset({38})},
    base_parts=("body", "hands", "feet"), fold_duplicate_slots=True, hair_needs_folder_tiebreak=True)

# Fallout 4: verified against real Fallout4.esm ARMA records (BOD2 masks), not
# guessed — see the NIF/character viewer memory note for the worked examples
# (Glasses always 47, Pip-Boy always 60, Body always 33, gloves 34+35). FO4
# has no Skyrim-style "+100" duplicate slot range and no helmet/hair slot
# collision (30 vs 31 are already distinct), so neither of those Skyrim
# quirks apply. No separate feet mesh (boots replace part of the body/legs
# coverage instead) and no per-NPC body-weight morph — base body/hands ship
# as a single file, not a _0/_1 pair (confirmed on a real CBBE-replaced body).
#
# Torso(41)/L-Arm(42)/R-Arm(43)/L-Leg(44)/R-Leg(45) are a second, separate
# limb-detail layer with NO Skyrim equivalent — real user report + real data
# confirmed these must each get their own row, not be folded into
# Body/Arms/Legs: FO4 armor is commonly modular per limb (a left-arm piece
# from one mod worn alongside an unrelated right-arm piece), so merging
# left+right into one slot meant equipping one silently unequipped the
# other. Left/right sides aren't split for hands (34+35 have only ever been
# seen together on one glove mesh, never as separate L/R pieces) or for the
# main Body(33) slot itself (a full one-piece outfit, not limb-modular).
FALLOUT4_BODY_DIR = "meshes/actors/character/characterassets/"
FALLOUT4_SKELETONS = {"male": FALLOUT4_BODY_DIR + "skeleton.nif",
                      "female": FALLOUT4_BODY_DIR + "skeleton.nif"}   # one skeleton, both genders
FALLOUT4_GROUPS = [("head", "Head"), ("hair", "Hair"), ("body", "Body"), ("torso", "Torso (armor)"),
                   ("l_arm", "Left Arm"), ("r_arm", "Right Arm"), ("hands", "Hands"),
                   ("l_leg", "Left Leg"), ("r_leg", "Right Leg"), ("eyes", "Eyes/Glasses"),
                   ("pipboy", "Pip-Boy"), ("backpack", "Backpack")]
FALLOUT4_LAYER_ORDER = ["hair", "body", "torso", "l_arm", "r_arm", "l_leg", "r_leg", "hands",
                        "eyes", "pipboy", "backpack", "head"]
FALLOUT4_PROFILE = GameProfile(
    body_dir=FALLOUT4_BODY_DIR, skeletons=FALLOUT4_SKELETONS, groups=FALLOUT4_GROUPS,
    layer_order=FALLOUT4_LAYER_ORDER,
    # No slot-number entry for "hair" at all — see slot_group()'s docstring:
    # real Fallout 4 hairstyles carry no slot data (hair is a race-menu
    # HeadPart mesh there, never armor-equipped), while slot 31 alone is
    # legitimately used by plenty of non-hair items (helmets, a DLC space-
    # suit liner, even a creature mesh) purely to hide the hair underneath —
    # confirmed on real ARMA data. Only the hair-folder check (below)
    # identifies "hair" for this game.
    slot_groups={"head": frozenset({30}), "body": frozenset({33}),
                "torso": frozenset({41}), "l_arm": frozenset({42}), "r_arm": frozenset({43}),
                "hands": frozenset({34, 35}), "l_leg": frozenset({44}), "r_leg": frozenset({45}),
                "eyes": frozenset({47}), "pipboy": frozenset({60}), "backpack": frozenset({61})},
    base_parts=("body", "hands"), has_weight_suffix=False,
    head_fmt="base{gender}head.nif", eyes_fmt=None,
    base_part_groups=frozenset({"body", "hands", "torso", "l_arm", "r_arm", "l_leg", "r_leg"}),
    hair_needs_folder_tiebreak=True,
    # A mesh's own embedded segment numbers are an internal per-mesh "Biped
    # Object" numbering, NOT the ARMA/BOD2 equip-slot numbers (verified: a
    # real left-leg piece and a real torso piece both happened to embed
    # internal id 3) — comparing them across two DIFFERENT equipped pieces
    # produced false collisions (equipping a left leg silently made an
    # already-equipped torso vanish). See pieces_hide_each_other's own
    # docstring for why the base-body hide (which uses this same comparison
    # and does work) is left untouched.
    pieces_hide_each_other=False,
    group_priority=("body", "torso", "l_arm", "r_arm", "hands", "l_leg", "r_leg",
                    "head", "eyes", "pipboy", "backpack"),
    rigid_attach={"pipboy": "PipboyBone"})


def profile_for_game(game_id: "str | None") -> GameProfile:
    """The GameProfile matching *game_id* — Fallout4's if it says so, Skyrim's
    (the historical default) for anything else, including None."""
    return FALLOUT4_PROFILE if game_id == "Fallout4" else SKYRIM_PROFILE


def detect_gender(path: str) -> "str | None":
    """'female' / 'male' from a mesh path (``.../f/cuirass_1.nif``,
    ``femalebody_0.nif``, Fallout 4's ``f_torso_heavy.nif``…), or None when the
    path doesn't say.

    The ``f_``/``m_`` filename-prefix check exists for Fallout 4, whose whole
    modular-armor scene names files this way (``f_torso_heavy.nif``,
    ``m_leg_lite_pad_l.nif``) rather than Skyrim's folder/suffix conventions —
    verified as a real gap: a real profile had female-prefixed pieces (a
    female left-arm/left-leg pair) passing the "only meshes for a male body"
    filter and showing up equippable on a male character, because neither
    existing check (word in name, exact folder segment, folder starting with
    "female"/"male") ever looks at a filename's own leading prefix. Also seen,
    harmlessly, on a handful of real Skyrim mods that happen to use the same
    prefix convention (a Telvanni-style gauntlets/boots set) — correctly
    gendered there too, not just for Fallout 4."""
    p = path.replace("\\", "/").lower()
    name = p.rsplit("/", 1)[-1]
    parts = p.split("/")[:-1]
    if "female" in name or "f" in parts or any(x.startswith("female") for x in parts):
        return "female"
    if "male" in name or "m" in parts or any(x.startswith("male") for x in parts):
        return "male"
    if name.startswith("f_"):
        return "female"
    if name.startswith("m_"):
        return "male"
    return None


def _stem(path: str) -> str:
    """Lowercase file name without extension or ``_0``/``_1`` weight suffix."""
    name = path.replace("\\", "/").lower().rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return name[:-2] if name.endswith(("_0", "_1")) else name


def _sibling_stems(path: str, sibling_paths: "Iterable[str]") -> set:
    folder = path.replace("\\", "/").lower().rpartition("/")[0]
    return {_stem(p) for p in sibling_paths
            if p.replace("\\", "/").lower().rpartition("/")[0] == folder}


def paired_gender(path: str, sibling_paths: "Iterable[str]") -> "str | None":
    """Gender from the naming pairs Bethesda's own armour uses, read off the
    folder's other files:

    * ``bladesarmor_1`` (male) / ``bladesarmorf_1`` (female) — one side plain;
    * ``bootsm_1`` / ``bootsf_1`` — both sides lettered.

    A trailing letter alone proves nothing (``wolf``, ``helm``): it only counts
    when the counterpart exists beside it, and a plain name is male only when an
    ``f`` counterpart exists. None otherwise."""
    names = _sibling_stems(path, sibling_paths)
    stem = _stem(path)
    # prefix + gender letter + optional number: bootsf, body1f_1, circletf10
    m = re.fullmatch(r"(.*?)([fm])(\d*)", stem)
    if m and m.group(1):
        prefix, letter, digits = m.groups()
        other = "m" if letter == "f" else "f"
        if prefix + digits in names or prefix + other + digits in names:
            return "female" if letter == "f" else "male"
    plain = re.fullmatch(r"(.*?)(\d*)", stem)
    if plain and plain.group(1) + "f" + plain.group(2) in names:
        return "male"
    return None


_RACE_WORDS = ("argonian", "khajiit", "khaajit", "khajit")      # Bethesda spells Khajiit three ways


def is_race_variant(path: str, sibling_paths: "Iterable[str]") -> bool:
    """A beast-race version of a piece of gear: the race spelled out in the name
    (``circletargonianf1``), or Bethesda's race letter after the gender letter
    (``hatfk`` Khajiit / ``hatma`` Argonian) with the plain ``hatf``/``hatm`` beside
    it. The viewer's characters are human, so these never fit."""
    name = path.replace("\\", "/").lower().rsplit("/", 1)[-1]
    if any(w in name for w in _RACE_WORDS):
        return True
    m = re.fullmatch(r"(.+[fm])([ka])", _stem(path))
    return bool(m) and m.group(1) in _sibling_stems(path, sibling_paths)


def guess_gender(path: str, sibling_paths: "Iterable[str]" = ()) -> "str | None":
    """detect_gender() first (folder/word markers), then the f-suffix pair."""
    return detect_gender(path) or paired_gender(path, sibling_paths)


def detect_weight(path: str) -> int:
    """Body-weight variant of a mesh (``..._0.nif`` slim, ``..._1.nif`` heavy);
    1 when the name doesn't say."""
    stem = path.replace("\\", "/").lower().rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return 0 if stem.endswith("_0") else 1


def auto_gender(path: str, scene: NifScene, sibling_paths: "Iterable[str]" = (),
                profile: GameProfile = SKYRIM_PROFILE) -> "str | None":
    """Which base body to wear *scene* on when the choice is left on Auto: only
    wearable gear qualifies (armour/clothes or a body/hands/feet mesh, and
    skinned with body slots, so creatures and props get none); the gender comes
    from the path or its f-suffix pair (see guess_gender), defaulting to female when
    neither says."""
    p = path.replace("\\", "/").lower()
    body_dir = profile.body_dir.rstrip("/").lower()
    wearable = p.startswith(("meshes/armor/", "meshes/clothes/")) or body_dir in p
    if not wearable or not is_skinned(scene) or not covered_slots(scene):
        return None
    return guess_gender(path, sibling_paths) or "female"


def weight_variant(path: str, weight: int) -> "str | None":
    """*path* with its body-weight suffix swapped to *weight* (``cuirass_1.nif`` →
    ``cuirass_0.nif``), or None when the name carries no ``_0``/``_1`` suffix.
    Skyrim ships most worn meshes in a slim (_0) and a heavy (_1) version that the
    game blends by the actor's weight; the two must match the body underneath."""
    base, dot, ext = path.rpartition(".")
    if not dot or not (base.endswith("_0") or base.endswith("_1")):
        return None
    return f"{base[:-1]}{1 if weight else 0}.{ext}"


def body_paths(gender: str, weight: int = 1, head: bool = False,
               profile: GameProfile = SKYRIM_PROFILE) -> list[str]:
    """The base game's body/hands(/feet) meshes for *gender* (plus the default
    head, and eyes when the game keeps them separate, with head=True). Games
    without a body-weight morph (profile.has_weight_suffix False) have no
    _0/_1 suffix at all — *weight* is then ignored."""
    suffix = f"_{weight}" if profile.has_weight_suffix else ""
    paths = [f"{profile.body_dir}{gender}{part}{suffix}.nif" for part in profile.base_parts]
    if head:
        paths.append(f"{profile.body_dir}{profile.head_fmt.format(gender=gender)}")
        if profile.eyes_fmt:
            paths.append(f"{profile.body_dir}{profile.eyes_fmt.format(gender=gender)}")
    return paths


def is_skinned(scene: NifScene) -> bool:
    return any(sh.is_skinned for sh in scene.shapes)


def covered_slots(scene: NifScene) -> frozenset:
    """Body slots (32 body, 33 hands, 37 feet…) the scene's skinned shapes cover."""
    out: set = set()
    for sh in scene.shapes:
        if sh.is_skinned:
            out |= sh.slots
    return frozenset(out)


def hide_covered(shape: NifShape, covered: frozenset) -> "NifShape | None":
    """*shape* without the partitions whose slot is in *covered* — the way the
    game hides the base body under an armour — or None if nothing is left.
    A shape that can't be split is dropped whole only if all its slots are covered."""
    if not covered or not shape.slots & covered:
        return shape
    if not shape.part_tris:
        return None if shape.slots <= covered else shape
    kept_idx = array("H")
    kept_slots: list[int] = []
    kept_tris: list[int] = []
    pos = 0
    for slot, n in zip(shape.part_slots, shape.part_tris):
        chunk = shape.indices[pos * 3:(pos + n) * 3]
        pos += n
        if slot not in covered:
            kept_idx.extend(chunk)
            kept_slots.append(slot)
            kept_tris.append(n)
    if not kept_idx:
        return None
    return replace(shape, indices=kept_idx, slots=frozenset(kept_slots),
                   part_slots=tuple(kept_slots), part_tris=tuple(kept_tris))


def compose(selected: NifScene, bodies: list[NifScene]) -> NifScene:
    """*selected* plus the base *bodies*, minus the body parts *selected* covers."""
    covered = covered_slots(selected)
    shapes = list(selected.shapes)
    for b in bodies:
        for sh in b.shapes:
            kept = hide_covered(sh, covered)
            if kept is not None:
                shapes.append(kept)
    return NifScene(shapes)


def slot_group(path: str, slots: "frozenset | None", profile: GameProfile = SKYRIM_PROFILE) -> "str | None":
    """Which equipment group a mesh belongs to, from its body slots and path;
    None when it has no body slots (props, weapons, shields are not wearable yet)
    and isn't a hair-folder mesh either.

    Slot numbers are the game's BSDismemberBodyPartType (Skyrim: 30 head,
    31 hair, 32 body, 33 hands, 34 forearms, 35 amulet, 36 ring, 37 feet,
    38 calves, 42 circlet, 43 ears; +100 for the 'Skyrim' duplicates — 130
    head, 131 hair…; a helmet and a hairstyle both use 131, so the folder
    breaks that tie. Fallout 4 uses a different, non-overlapping set — see
    FALLOUT4_PROFILE.

    The hair-folder check runs before the "no slots" bail (and before the
    slot-number check) for two real, verified-on-real-data reasons: (1) real
    Fallout 4 hairstyles carry no slot data at all — hair is a HeadPart
    (race-menu) mesh there, never armor-equipped, so nothing ever authors an
    ARMA slot for one; and (2) real Fallout 4 items OUTSIDE the hair folder
    (helmets, a DLC space-suit liner, even a creature mesh) legitimately
    reserve slot 31 purely to hide the hair underneath — so trusting slot 31
    alone put those in the Hair picker instead of real hairstyles. Fallout 4
    therefore has no slot-number entry for "hair" at all in its own profile;
    only the folder identifies it."""
    p = path.replace("\\", "/").lower()
    if profile.hair_needs_folder_tiebreak and ("character assets/hair/" in p or "/hair/" in p):
        return "hair"
    if not slots:
        return None
    s = {x % 100 if profile.fold_duplicate_slots and x >= 100 else x for x in slots}
    order = profile.group_priority or profile.slot_groups.keys()
    for group in order:
        keys = profile.slot_groups.get(group)
        if keys and s & keys:
            return group
    return None


def assemble(base: list[NifScene], pieces: "dict[str, NifScene]",
             bones: "dict | None" = None, profile: GameProfile = SKYRIM_PROFILE) -> NifScene:
    """The base parts (body, hands, feet, head) plus the equipped *pieces*
    (group → scene), posed on *bones* when given (see skin_shape). Layers are
    applied in profile.layer_order: each piece always removes the partitions
    it covers from the base body (so a boot hides the base calves), and —
    only when profile.pieces_hide_each_other — also from every piece drawn
    before it (so a Skyrim helmet correctly hides the hair it overlaps).
    Fallout 4 turns that second part off (see the flag's own docstring)."""
    if bones:                                    # pose everything on the skeleton first
        base = [skin_scene(b, bones) for b in base]
        pieces = {g: skin_scene(sc, bones) for g, sc in pieces.items()}
        for g in list(pieces):                   # a group's own unskinned prop convention, if any
            bone_name = profile.rigid_attach.get(g)
            if bone_name and bone_name in bones:
                sc_ = pieces[g]
                pieces[g] = NifScene(
                    [rigid_attach_shape(sh, bones[bone_name]) for sh in sc_.shapes], sc_.nodes)
    base_shapes = [sh for b in base for sh in b.shapes]
    piece_shapes: list[NifShape] = []
    layer_order = profile.layer_order
    ordered = sorted(pieces.items(),
                     key=lambda kv: layer_order.index(kv[0]) if kv[0] in layer_order else -1)
    for _group, scene in ordered:
        covered = covered_slots(scene)
        base_shapes = [k for k in (hide_covered(sh, covered) for sh in base_shapes) if k is not None]
        if profile.pieces_hide_each_other:
            piece_shapes = [k for k in (hide_covered(sh, covered) for sh in piece_shapes) if k is not None]
        piece_shapes.extend(scene.shapes)
    return NifScene(base_shapes + piece_shapes)


# -- the picker: which meshes can go in a slot ------------------------------------------------------------
def _is_base_part(name: str, profile: GameProfile) -> bool:
    """Matches e.g. femalebody_1.nif (Skyrim) or femalebody.nif (Fallout 4, no
    weight suffix at all) against *profile*'s own base_parts."""
    parts_alt = "|".join(re.escape(p) for p in profile.base_parts)
    suffix = r"(_[01])?" if profile.has_weight_suffix else ""
    return re.fullmatch(rf"(male|female)({parts_alt}){suffix}\.nif", name) is not None


# Top-level mesh folders that hold scenery, not gear — skipped only to save the
# picker from opening files that can never be worn; the slots decide everything else.
_SCENERY_ROOTS = frozenset({
    "architecture", "landscape", "lod", "terrain", "plants", "dungeons", "effects", "clutter",
    "furniture", "weapons", "sky", "traps", "magic", "interface", "water", "markers", "cameras",
    "shadertest", "fx", "grass", "trees", "rocks", "ships", "ui", "textures", "sound"})


def is_wearable_path(path: str, group: "str | None" = None,
                     profile: GameProfile = SKYRIM_PROFILE) -> bool:
    """Whether *path* could be worn in *group* (any group when None), judged by
    folder and name alone — the cheap first cut before the mesh is opened; the
    mesh's own body slots make the real decision (fits_slot).

    Everything outside an ``actors`` folder qualifies except scenery folders —
    which covers armour and clothes wherever they live (``meshes/armor``, the DLC
    and Creation Club folders, a mod's own folder). Under ``meshes/actors`` only two
    things do: hairstyles (for "hair") and the base body/hands(/feet) files
    (femalebody_1.nif…), which a body mod replaces (for the body-part slots).
    Heads, eyes, race and creature parts, first-person arms, child gear and
    beast-race variants never do."""
    p = path.replace("\\", "/").lower()
    name = p.rsplit("/", 1)[-1]
    if not p.startswith("meshes/") or not p.endswith(".nif"):
        return False
    if "1stperson" in name or "/1stperson" in p or name.endswith("_1st.nif"):
        return False
    if any(w in name for w in _RACE_WORDS) or "/child/" in p or "/children/" in p or name.startswith("child"):
        return False                                   # beast races and child gear: not for the adult human character
    if p.startswith(profile.body_dir + "hair/"):
        return group in (None, "hair")
    if p.startswith(profile.body_dir) and "/" not in p[len(profile.body_dir):]:
        return _is_base_part(name, profile) and group in (None, *profile.base_part_groups)
    if "/actors/" in p:                                # creature/race parts, also inside DLC/CC/mod folders
        return False
    return p.split("/")[1] not in _SCENERY_ROOTS


def fits_slot(path: str, slots: "frozenset | None", group: "str | None",
             profile: GameProfile = SKYRIM_PROFILE) -> bool:
    """Whether the mesh at *path* with body *slots* belongs in *group*: it must be
    a wearable path and its slots must place it in that group (any group when None,
    as long as it has slots at all) — except a hair-folder mesh, which needs no
    slots at all (see slot_group's docstring: real Fallout 4 hairstyles carry
    none, the folder alone already proves what it is)."""
    if not is_wearable_path(path, group, profile):
        return False
    p = path.replace("\\", "/").lower()
    if profile.hair_needs_folder_tiebreak and ("character assets/hair/" in p or "/hair/" in p):
        return group in (None, "hair")
    if not slots:
        return False
    return group is None or slot_group(path, slots, profile) == group


def gender_fits(path: str, gender: "str | None", sibling_paths: "Iterable[str]" = ()) -> bool:
    """False only when the mesh is known to be made for the *other* gender."""
    g = guess_gender(path, sibling_paths)
    return gender is None or g is None or g == gender


# -- body weight ------------------------------------------------------------------------------------------
def _lerp(a: array, b: array, t: float) -> array:
    return array("f", [x + (y - x) * t for x, y in zip(a, b)])


def _lerp_unit(a: array, b: array, t: float) -> array:
    """Blend two arrays of xyz normals and re-normalise each."""
    out = _lerp(a, b, t)
    for i in range(0, len(out) - 2, 3):
        n = (out[i] ** 2 + out[i + 1] ** 2 + out[i + 2] ** 2) ** 0.5 or 1.0
        out[i], out[i + 1], out[i + 2] = out[i] / n, out[i + 1] / n, out[i + 2] / n
    return out


def blend_shape(slim: NifShape, heavy: NifShape, t: float) -> NifShape:
    """The shape at body weight *t* (0 = the ``_0`` mesh, 1 = the ``_1`` mesh).

    The game morphs between the two files vertex by vertex, so the two must have
    the same vertices in the same order; when they don't, the heavy one is used
    unchanged. Everything but geometry (triangles, UVs, textures, slots, bones and
    weights) comes from the heavy mesh. For a skinned shape it is the shape's own
    vertices that are blended — the skinning to the skeleton happens afterwards."""
    if t <= 0.0:
        return slim
    if t >= 1.0 or len(slim.positions) != len(heavy.positions):
        return heavy
    normals = heavy.normals
    if slim.normals is not None and heavy.normals is not None \
            and len(slim.normals) == len(heavy.normals):
        normals = _lerp_unit(slim.normals, heavy.normals, t)
    skin = heavy.skin
    if slim.skin is not None and heavy.skin is not None \
            and len(slim.skin.local_positions) == len(heavy.skin.local_positions):
        ln = None
        if slim.skin.local_normals is not None and heavy.skin.local_normals is not None \
                and len(slim.skin.local_normals) == len(heavy.skin.local_normals):
            ln = _lerp_unit(slim.skin.local_normals, heavy.skin.local_normals, t)
        skin = replace(heavy.skin,
                       local_positions=_lerp(slim.skin.local_positions, heavy.skin.local_positions, t),
                       local_normals=ln)
    return replace(heavy, positions=_lerp(slim.positions, heavy.positions, t),
                   normals=normals, skin=skin)


def blend_scene(slim: NifScene, heavy: NifScene, t: float) -> NifScene:
    """Blend two weight variants of one mesh file, pairing their shapes by name
    (and vertex count). Shapes only the heavy file has are kept as they are."""
    if t <= 0.0:
        return slim
    if t >= 1.0:
        return heavy
    by_name = {(sh.name, len(sh.positions)): sh for sh in slim.shapes}
    return NifScene([blend_shape(by_name[k], sh, t) if (k := (sh.name, len(sh.positions))) in by_name else sh
                     for sh in heavy.shapes], heavy.nodes)


# -- skinning ---------------------------------------------------------------------------------------
def bone_transforms(nodes: "list[NifNode]") -> dict:
    """{bone name: (rotation, scale, translation)} — the skeleton's rest pose."""
    return {n.name: (n.rotation, n.scale, n.position) for n in nodes if n.name}


def skin_shape(shape: NifShape, bones: dict) -> NifShape:
    """Pose *shape* on the skeleton described by *bones* (bone_transforms()).

    A skinned vertex sits at  Σ weight · (bone_world · skin_to_bone · vertex),
    with the vertex in the shape's own space and the weights and skin→bone
    transforms from the file. Armour and bodies are authored so this equals
    where the file already places them; hair, eyes and other head-attached
    meshes are stored relative to a bone and only land in the right place this
    way. Returned unchanged when the shape has no skin data at all. A bone
    the skeleton doesn't have simply contributes nothing (as if its weight
    were zero) rather than voiding the whole shape — verified against a real
    file (a raider body with a dangling-coat cloth-physics rig): 30 of its
    87 bones are pure runtime cloth-sim bones with no entry in any static
    skeleton.nif, ever (there's nothing to load — they don't exist outside
    a physics engine), so requiring every bone used left the ~60 real body
    bones unposed too, rendering the whole body as one giant unposed mesh
    sitting at its raw file-space coordinates. A vertex weighted entirely by
    missing bones still keeps its old spot (wsum stays 0, already handled
    below) — only the physics-only cloth tips are affected, not the body."""
    sk = shape.skin
    if sk is None:
        return shape
    lp, ln = sk.local_positions, sk.local_normals
    nv = len(lp) // 3
    acc = [0.0] * (nv * 3)
    nacc = [0.0] * (nv * 3) if ln is not None else None
    wsum = [0.0] * nv
    for name, s_xf, (ids, ws) in zip(sk.bone_names, sk.bone_xf, sk.weights):
        if name not in bones:
            continue
        r, sc, t = compose_transform(bones[name], s_xf)
        for vid, w in zip(ids, ws):
            if vid >= nv or w <= 0.0:
                continue
            k = 3 * vid
            x, y, z = lp[k], lp[k + 1], lp[k + 2]
            acc[k] += w * (t[0] + sc * (r[0] * x + r[1] * y + r[2] * z))
            acc[k + 1] += w * (t[1] + sc * (r[3] * x + r[4] * y + r[5] * z))
            acc[k + 2] += w * (t[2] + sc * (r[6] * x + r[7] * y + r[8] * z))
            if nacc is not None:
                a, b, c = ln[k], ln[k + 1], ln[k + 2]
                nacc[k] += w * (r[0] * a + r[1] * b + r[2] * c)
                nacc[k + 1] += w * (r[3] * a + r[4] * b + r[5] * c)
                nacc[k + 2] += w * (r[6] * a + r[7] * b + r[8] * c)
            wsum[vid] += w
    pos = array("f", shape.positions)                # unweighted vertices keep their old spot
    nrm = array("f", shape.normals) if shape.normals is not None and nacc is not None else shape.normals
    for v in range(min(nv, len(pos) // 3)):
        if wsum[v] > 1e-6:
            k = 3 * v
            inv = 1.0 / wsum[v]
            pos[k], pos[k + 1], pos[k + 2] = acc[k] * inv, acc[k + 1] * inv, acc[k + 2] * inv
            if nrm is not None and nacc is not None:
                nx, ny, nz = nacc[k], nacc[k + 1], nacc[k + 2]
                ln_ = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
                nrm[k], nrm[k + 1], nrm[k + 2] = nx / ln_, ny / ln_, nz / ln_
    return replace(shape, positions=pos, normals=nrm)


def skin_scene(scene: NifScene, bones: dict) -> NifScene:
    return NifScene([skin_shape(sh, bones) for sh in scene.shapes])


def rigid_attach_shape(shape: NifShape, bone_xf) -> NifShape:
    """*shape* rigidly transformed as if it were a permanently-fused child of
    the bone whose (rotation, scale, translation) is *bone_xf* — for a prop
    mesh with no skin data at all, so skin_shape() has nothing to pose it
    with (see GameProfile.rigid_attach's own docstring for why this is
    needed and how it was verified). A no-op for an already-skinned shape."""
    if shape.is_skinned:
        return shape
    r, sc, t = bone_xf
    pos = array("f", shape.positions)
    for i in range(0, len(pos), 3):
        x, y, z = pos[i], pos[i + 1], pos[i + 2]
        pos[i] = t[0] + sc * (r[0] * x + r[1] * y + r[2] * z)
        pos[i + 1] = t[1] + sc * (r[3] * x + r[4] * y + r[5] * z)
        pos[i + 2] = t[2] + sc * (r[6] * x + r[7] * y + r[8] * z)
    nrm = shape.normals
    if nrm is not None:
        nrm = array("f", nrm)
        for i in range(0, len(nrm), 3):
            a, b, c = nrm[i], nrm[i + 1], nrm[i + 2]
            nx = r[0] * a + r[1] * b + r[2] * c
            ny = r[3] * a + r[4] * b + r[5] * c
            nz = r[6] * a + r[7] * b + r[8] * c
            ln = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
            nrm[i], nrm[i + 1], nrm[i + 2] = nx / ln, ny / ln, nz / ln
    return replace(shape, positions=pos, normals=nrm)
