"""Turn a skeleton NIF's node tree into bone segments for drawing.

A skeleton NIF mixes real bones (``NPC Spine [Spn0]``, ``NPC L Hand [LHnd]``,
the skirt/cloak physics bones) with helper nodes the game attaches things to
(weapon sockets, camera, animation objects). Only bones are drawn; each bone
joins its nearest bone ancestor so dropping a helper never breaks the chain.
"""

from __future__ import annotations

from array import array

from Utils.nif.nif_reader import NifNode

# Node names (case-insensitive prefixes) that are attachment points, not bones.
_HELPER_PREFIXES = ("weapon", "camera", "animobject", "characterbumper", "quiver",
                    "shield", "backweapon", "npc head magicnode", "npc l magicnode",
                    "npc r magicnode", "npceyebone")


def is_bone(node: NifNode) -> bool:
    name = node.name.strip().lower()
    return bool(name) and not name.endswith(".nif") and not name.startswith(_HELPER_PREFIXES)


def bone_segments(nodes: list[NifNode]) -> "tuple[array, array]":
    """(lines, joints): *lines* is a flat xyz array holding two points per bone
    (child, its nearest bone ancestor); *joints* one point per bone."""
    keep = [is_bone(n) for n in nodes]
    lines, joints = array("f"), array("f")
    for i, n in enumerate(nodes):
        if not keep[i]:
            continue
        joints.extend(n.position)
        p = n.parent
        while p >= 0 and not keep[p]:
            p = nodes[p].parent
        if p >= 0:
            lines.extend(nodes[p].position)
            lines.extend(n.position)
    return lines, joints
