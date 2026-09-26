"""Assemble a character preview: a selected mesh (armour, clothes, a body mod)
worn on the game's own base body, with the game's skeleton for the bone overlay.

Which body/skeleton files to use, and which body parts an armour replaces, come
from the same conventions the game uses (vanilla file names; the "slot" of each
skinned partition). Nothing here reads files: callers resolve paths through the
asset catalog, so a body or skeleton replacer mod is picked up automatically.
"""

from __future__ import annotations

from dataclasses import replace

from array import array

from Utils.nif.nif_reader import NifScene, NifShape

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


def detect_weight(path: str) -> int:
    """Body-weight variant of a mesh (``..._0.nif`` slim, ``..._1.nif`` heavy);
    1 when the name doesn't say."""
    stem = path.replace("\\", "/").lower().rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return 0 if stem.endswith("_0") else 1


def auto_gender(path: str, scene: NifScene) -> "str | None":
    """Which base body to wear *scene* on when the choice is left on Auto: only
    wearable gear qualifies (armour/clothes or a body/hands/feet mesh, and
    skinned with body slots, so creatures and props get none); the gender comes
    from the path, defaulting to female when it doesn't say."""
    p = path.replace("\\", "/").lower()
    wearable = p.startswith(("meshes/armor/", "meshes/clothes/")) or "character assets" in p
    if not wearable or not is_skinned(scene) or not covered_slots(scene):
        return None
    return detect_gender(path) or "female"


def body_paths(gender: str, weight: int = 1) -> list[str]:
    """The base game's body, hands and feet meshes for *gender*."""
    return [f"{BODY_DIR}{gender}{part}_{weight}.nif" for part in ("body", "hands", "feet")]


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
