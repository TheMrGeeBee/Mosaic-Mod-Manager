"""Assemble a character preview: a selected mesh (armour, clothes, a body mod)
worn on the game's own base body, with the game's skeleton for the bone overlay.

Which body/skeleton files to use, and which body parts an armour replaces, come
from the same conventions the game uses (vanilla file names; the "slot" of each
skinned partition). Nothing here reads files: callers resolve paths through the
asset catalog, so a body or skeleton replacer mod is picked up automatically.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from array import array

from Utils.nif.nif_reader import NifNode, NifScene, NifShape, compose_transform

BODY_DIR = "meshes/actors/character/character assets/"
SKELETONS = {
    "male": BODY_DIR + "skeleton.nif",
    "female": "meshes/actors/character/character assets female/skeleton_female.nif",
}


def detect_gender(path: str) -> "str | None":
    """'female' / 'male' from a mesh path (``.../f/cuirass_1.nif``,
    ``femalebody_0.nif``…), or None when the path doesn't say."""
    p = path.replace("\\", "/").lower()
    name = p.rsplit("/", 1)[-1]
    parts = p.split("/")[:-1]
    if "female" in name or "f" in parts or any(x.startswith("female") for x in parts):
        return "female"
    if "male" in name or "m" in parts or any(x.startswith("male") for x in parts):
        return "male"
    return None


def _stem(path: str) -> str:
    """Lowercase file name without extension or ``_0``/``_1`` weight suffix."""
    name = path.replace("\\", "/").lower().rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return name[:-2] if name.endswith(("_0", "_1")) else name


def paired_gender(path: str, sibling_paths: "Iterable[str]") -> "str | None":
    """Gender from the ``f``-suffix naming pair seen in Bethesda's own armour
    (``bladesarmor_1.nif`` male, ``bladesarmorf_1.nif`` female, side by side in
    one folder). A trailing "f" alone proves nothing (``wolf``), so it only counts
    when a same-named file *without* it sits in the same folder — and a name with
    no "f" is male only when a counterpart *with* one exists. None otherwise."""
    folder = path.replace("\\", "/").lower().rpartition("/")[0]
    names = {_stem(p) for p in sibling_paths
             if p.replace("\\", "/").lower().rpartition("/")[0] == folder}
    stem = _stem(path)
    if stem.endswith("f") and stem[:-1] in names:
        return "female"
    if stem + "f" in names:
        return "male"
    return None


def guess_gender(path: str, sibling_paths: "Iterable[str]" = ()) -> "str | None":
    """detect_gender() first (folder/word markers), then the f-suffix pair."""
    return detect_gender(path) or paired_gender(path, sibling_paths)


def detect_weight(path: str) -> int:
    """Body-weight variant of a mesh (``..._0.nif`` slim, ``..._1.nif`` heavy);
    1 when the name doesn't say."""
    stem = path.replace("\\", "/").lower().rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return 0 if stem.endswith("_0") else 1


def auto_gender(path: str, scene: NifScene, sibling_paths: "Iterable[str]" = ()) -> "str | None":
    """Which base body to wear *scene* on when the choice is left on Auto: only
    wearable gear qualifies (armour/clothes or a body/hands/feet mesh, and
    skinned with body slots, so creatures and props get none); the gender comes
    from the path or its f-suffix pair (see guess_gender), defaulting to female when
    neither says."""
    p = path.replace("\\", "/").lower()
    wearable = p.startswith(("meshes/armor/", "meshes/clothes/")) or "character assets" in p
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


def body_paths(gender: str, weight: int = 1, head: bool = False) -> list[str]:
    """The base game's body, hands and feet meshes for *gender* (plus the default
    head and eyes with head=True — they have no weight variants)."""
    paths = [f"{BODY_DIR}{gender}{part}_{weight}.nif" for part in ("body", "hands", "feet")]
    if head:
        paths += [f"{BODY_DIR}{gender}head.nif", f"{BODY_DIR}eyes{gender}.nif"]
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


def slot_group(path: str, slots: frozenset) -> "str | None":
    """Which equipment group a mesh belongs to, from its body slots and path;
    None when it has no body slots (props, weapons, shields are not wearable yet).

    Slot numbers are the game's BSDismemberBodyPartType: 30 head, 31 hair,
    32 body, 33 hands, 34 forearms, 35 amulet, 36 ring, 37 feet, 38 calves,
    42 circlet, 43 ears; +100 for the 'Skyrim' duplicates (130 head, 131 hair…).
    A helmet and a hairstyle both use 131, so the folder breaks that tie."""
    p = path.replace("\\", "/").lower()
    if not slots:
        return None
    if "character assets/hair/" in p or "/hair/" in p:
        return "hair"
    s = {x % 100 if x >= 100 else x for x in slots}
    for group, keys in (("body", {32}), ("hands", {33}), ("feet", {37}),
                        ("head", {30, 31}), ("circlet", {42}), ("amulet", {35}),
                        ("ring", {36}), ("ears", {43}), ("arms", {34}), ("legs", {38})):
        if s & keys:
            return group
    return None


def assemble(base: list[NifScene], pieces: "dict[str, NifScene]",
             bones: "dict | None" = None) -> NifScene:
    """The base parts (body, hands, feet, head) plus the equipped *pieces*
    (group → scene), posed on *bones* when given (see skin_shape). Layers are
    applied in LAYER_ORDER: each piece removes the
    partitions it covers from everything drawn before it — so boots hide the
    calves, a helmet hides the hair it overlaps — and adds its own shapes."""
    if bones:                                    # pose everything on the skeleton first
        base = [skin_scene(b, bones) for b in base]
        pieces = {g: skin_scene(sc, bones) for g, sc in pieces.items()}
    shapes = [sh for b in base for sh in b.shapes]
    ordered = sorted(pieces.items(),
                     key=lambda kv: LAYER_ORDER.index(kv[0]) if kv[0] in LAYER_ORDER else -1)
    for _group, scene in ordered:
        covered = covered_slots(scene)
        shapes = [k for k in (hide_covered(sh, covered) for sh in shapes) if k is not None]
        shapes.extend(scene.shapes)
    return NifScene(shapes)


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
    way. Returned unchanged when the shape has no skin data or the skeleton
    lacks any of its bones (a partial pose would distort it)."""
    sk = shape.skin
    if sk is None or not all(name in bones for name in sk.bone_names):
        return shape
    lp, ln = sk.local_positions, sk.local_normals
    nv = len(lp) // 3
    acc = [0.0] * (nv * 3)
    nacc = [0.0] * (nv * 3) if ln is not None else None
    wsum = [0.0] * nv
    for name, s_xf, (ids, ws) in zip(sk.bone_names, sk.bone_xf, sk.weights):
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
