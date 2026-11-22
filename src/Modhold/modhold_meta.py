"""
modhold_meta.py
Record where an installed mod came from on Modhold, in the mod's ``meta.ini``.

Same idea as mod.io's meta (Games/Baldur's Gate 3/modio_meta.py): extra keys in
the shared ``[General]`` section, prefixed ``modhold`` so they never collide
with the Nexus keys, written without disturbing anything already in the file.
The update checker (phase 3) reads these back.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from Utils.app_log import app_log

_SECTION = "General"

_K_MOD_ID = "modholdModId"
_K_FILE_ID = "modholdFileId"
_K_VERSION = "modholdVersion"
_K_NAME = "modholdName"
_K_URL = "modholdUrl"
_K_UPLOADER = "modholdUploader"
_K_SHA256 = "modholdSha256"
_K_GAME = "modholdGame"
_K_LOCKED = "modholdLocked"
_K_INSTALLED = "modholdInstalled"
_K_LATEST_FILE_ID = "modholdLatestFileId"
_K_LATEST_VERSION = "modholdLatestVersion"
_K_IGNORE_UPDATE = "modholdIgnoreUpdate"
_K_IGNORED_VERSION = "modholdIgnoredVersion"
_K_FROM_LOADOUT = "modholdLoadout"
_K_FILE_LABEL = "modholdFileLabel"
_K_FILE_KIND = "modholdFileKind"
_K_LATEST_SHA256 = "modholdLatestSha256"


@dataclass
class ModholdMeta:
    mod_id: int = 0
    file_id: int = 0
    version: str = ""
    name: str = ""
    url: str = ""
    uploader: str = ""
    sha256: str = ""
    game: str = ""             # Modhold game slug, e.g. "skyrim-se"
    locked: str = ""           # "" or "beta" (a gated release)
    installed: str = ""
    latest_file_id: int = 0
    latest_version: str = ""
    ignore_update: bool = False
    ignored_version: str = ""
    loadout: str = ""          # loadout id this mod was installed from, if any
    file_label: str = ""       # the installed file's label, to find its successor
    file_kind: str = ""        # "main" or "optional"
    latest_sha256: str = ""    # checksum of the newest counterpart file


def _int(raw: str) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def _load(path: Path) -> configparser.ConfigParser:
    # interpolation=None so a literal '%' in a name/version cannot crash a read.
    cp = configparser.ConfigParser(interpolation=None)
    if path.is_file():
        try:
            cp.read(str(path), encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError):
            cp = configparser.ConfigParser(interpolation=None)
    if not cp.has_section(_SECTION):
        cp.add_section(_SECTION)
    return cp


def write_modhold_meta(meta_ini_path: Path, meta: ModholdMeta) -> None:
    """Write the Modhold keys into meta.ini, preserving everything else."""
    cp = _load(meta_ini_path)
    s = _SECTION
    cp.set(s, _K_MOD_ID, str(meta.mod_id))
    cp.set(s, _K_FILE_ID, str(meta.file_id))
    cp.set(s, _K_VERSION, meta.version)
    cp.set(s, _K_LATEST_FILE_ID, str(meta.latest_file_id or meta.file_id))
    cp.set(s, _K_LATEST_VERSION, meta.latest_version or meta.version)
    for key, value in ((_K_NAME, meta.name), (_K_URL, meta.url),
                       (_K_UPLOADER, meta.uploader), (_K_SHA256, meta.sha256),
                       (_K_GAME, meta.game), (_K_FROM_LOADOUT, meta.loadout),
                       (_K_FILE_LABEL, meta.file_label), (_K_FILE_KIND, meta.file_kind),
                       (_K_LATEST_SHA256, meta.latest_sha256)):
        if value:
            cp.set(s, key, value)
    cp.set(s, _K_LOCKED, meta.locked)
    cp.set(s, _K_INSTALLED,
           meta.installed or datetime.now().isoformat(timespec="seconds"))
    # Always written, so a fresh install clears a stale Ignore-Update choice.
    cp.set(s, _K_IGNORE_UPDATE, "1" if meta.ignore_update else "0")
    cp.set(s, _K_IGNORED_VERSION, meta.ignored_version)
    meta_ini_path.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_ini_path, "w", encoding="utf-8") as f:
        cp.write(f)
    app_log(f"Modhold: wrote meta.ini for mod {meta.mod_id} (file {meta.file_id})")


def _from_parser(cp: configparser.ConfigParser) -> ModholdMeta:
    meta = ModholdMeta()
    g = lambda k: cp.get(_SECTION, k, fallback="")  # noqa: E731
    meta.mod_id = _int(g(_K_MOD_ID))
    meta.file_id = _int(g(_K_FILE_ID))
    meta.version = g(_K_VERSION)
    meta.name = g(_K_NAME)
    meta.url = g(_K_URL)
    meta.uploader = g(_K_UPLOADER)
    meta.sha256 = g(_K_SHA256)
    meta.game = g(_K_GAME)
    meta.locked = g(_K_LOCKED)
    meta.installed = g(_K_INSTALLED)
    meta.latest_file_id = _int(g(_K_LATEST_FILE_ID))
    meta.latest_version = g(_K_LATEST_VERSION)
    meta.ignore_update = g(_K_IGNORE_UPDATE) == "1"
    meta.ignored_version = g(_K_IGNORED_VERSION)
    meta.loadout = g(_K_FROM_LOADOUT)
    meta.file_label = g(_K_FILE_LABEL)
    meta.file_kind = g(_K_FILE_KIND)
    meta.latest_sha256 = g(_K_LATEST_SHA256)
    return meta


def read_modhold_meta(meta_ini_path: Path) -> ModholdMeta:
    """Read the Modhold keys from meta.ini; missing keys take their defaults."""
    if not meta_ini_path.is_file():
        return ModholdMeta()
    return _from_parser(_load(meta_ini_path))


def read_modhold_meta_fast(meta_ini_path: Path) -> "ModholdMeta | None":
    """ModholdMeta for a mod folder, or None when it is not a Modhold mod.

    One text read and a substring test first, so the many non-Modhold mods in a
    modlist refresh cost no ini parse."""
    try:
        text = meta_ini_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if _K_MOD_ID.lower() not in text.lower():
        return None
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read_string(text)
    except configparser.Error:
        return None
    if not cp.has_section(_SECTION):
        return None
    meta = _from_parser(cp)
    return meta if meta.mod_id > 0 else None


def update_pending(meta: ModholdMeta) -> bool:
    """True when the newest counterpart file is newer than the installed one and the
    user has not ignored that version. Same id with another checksum is a file the
    author replaced in place; a lower latest id (a rollback) never counts."""
    if meta.mod_id <= 0 or meta.file_id <= 0 or meta.latest_file_id <= 0:
        return False
    if meta.ignore_update and meta.ignored_version == meta.latest_version:
        return False
    if meta.latest_file_id > meta.file_id:
        return True
    return bool(meta.latest_file_id == meta.file_id and meta.sha256
                and meta.latest_sha256
                and meta.latest_sha256.lower() != meta.sha256.lower())


def is_modhold_mod(meta_ini_path: Path) -> bool:
    """True when this mod's meta.ini carries a Modhold mod id."""
    return read_modhold_meta(meta_ini_path).mod_id > 0
