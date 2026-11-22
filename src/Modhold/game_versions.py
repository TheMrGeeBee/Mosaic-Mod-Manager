"""
game_versions.py
Compare the game version a Modhold loadout was built for with the one installed.

A loadout carries ``game_version`` (what it targets, e.g. "1.6.1170") and
``works_on`` (other versions known to work). Mods for one build of a game often
fail on another (Skyrim SKSE plugins above all), so the install preflight warns
when they differ.

Only a *clear* difference warns. When either side is missing or is not a plain
dotted number the answer is ``unknown`` and nothing is said. The installed version is
only known for games with a Game Version table (see :func:`installed_game_version`),
so a game whose exe reports a build number that mods never use gets no false alarm.
Qt-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

_PART = re.compile(r"\d+$")

OK = "ok"                  # the installed version is the one the loadout targets
WORKS_ON = "works_on"      # not the target, but listed as known to work
MISMATCH = "mismatch"      # a different version: warn
UNKNOWN = "unknown"        # can't tell: say nothing


def normalize(version) -> Optional[tuple[int, ...]]:
    """``"v1.7.104.0"`` -> ``(1, 7, 104, 0)``; None when it isn't a dotted number."""
    if not isinstance(version, str):
        return None
    text = version.strip().lower().lstrip("v")
    parts = text.split(".")
    if len(parts) < 2 or not all(_PART.match(p) for p in parts):
        return None
    return tuple(int(p) for p in parts)


def same_version(a, b) -> bool:
    """Equal as versions: ``1.7.104`` and ``1.7.104.0`` are the same build."""
    na, nb = normalize(a), normalize(b)
    if na is None or nb is None:
        return False
    width = max(len(na), len(nb))
    return na + (0,) * (width - len(na)) == nb + (0,) * (width - len(nb))


@dataclass(frozen=True)
class VersionCheck:
    status: str
    installed: str = ""
    target: str = ""
    works_on: tuple[str, ...] = field(default_factory=tuple)

    @property
    def mismatch(self) -> bool:
        return self.status == MISMATCH


def check(installed: str, game_version: str, works_on=()) -> VersionCheck:
    """How the installed game version relates to what the loadout targets."""
    works = tuple(works_on or ())
    base = dict(installed=installed or "", target=game_version or "", works_on=works)
    if normalize(installed) is None or normalize(game_version) is None:
        return VersionCheck(UNKNOWN, **base)
    if same_version(installed, game_version):
        return VersionCheck(OK, **base)
    if any(same_version(installed, w) for w in works):
        return VersionCheck(WORKS_ON, **base)
    return VersionCheck(MISMATCH, **base)


def installed_game_version(game) -> str:
    """The installed game's version as people name it, or "" when that isn't known.

    Only a game with a Game Version table (Utils/steam/versions: Skyrim SE, Fallout 4)
    has trustworthy version names; for any other game the exe reports a developer
    build number (Cyberpunk 3.0.88.46235 where mods say 2.3), and comparing that with
    what a loadout author wrote would raise false warnings. Those games read as
    unknown, so nothing is said and the loadout's own version is used where one is
    needed. Reads the same way as the Game Version wizard, including a Skyrim runtime
    swap (an older exe on the newer build's data). Never raises.
    """
    try:
        root = game.get_game_path()
        app_id = str(getattr(game, "steam_id", "") or "")
        if not root or not app_id:
            return ""
        from Utils.config_paths import get_game_config_dir
        from Utils.steam import depot_versions as dv
        state_dir = get_game_config_dir(game.name)
        table = dv.load_table(app_id, state_dir / dv.USER_VERSIONS_FILENAME)
        if not table.exe_file:
            return ""
        return dv.installed_version(root, state_dir, table.exe_file)
    except Exception:
        return ""
