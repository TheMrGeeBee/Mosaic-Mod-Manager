"""
openmw_settings.py
Read and edit a few OpenMW engine settings in ``settings.cfg``.

settings.cfg sits next to openmw.cfg and only holds settings the user changed. The
OpenMW launcher writes to it as well, so edits here are line-based: an existing key is
changed in place, a missing key is added to its section, and every other line
(comments, unknown keys, other sections) is left exactly as it was.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

SETTINGS_FILE = "settings.cfg"
BACKUP_SUFFIX = ".mosaic-bak"


@dataclass(frozen=True)
class SettingDef:
    section: str
    key: str
    label: str
    kind: str            # "bool" | "int" | "float" | "choice"
    default: str         # OpenMW's own default, shown when the key is not in the file
    choices: tuple = ()  # (value, label) pairs for kind == "choice"
    minimum: float = 0
    maximum: float = 0


# Keys and defaults checked against the defaults shipped with OpenMW 0.51 (/etc/openmw/defaults.bin).
KNOWN_SETTINGS: tuple[SettingDef, ...] = (
    SettingDef("Groundcover", "enabled", "Enable groundcover (needed for grass mods)", "bool", "false"),
    SettingDef("Groundcover", "stomp mode", "Grass stomping", "choice", "2",
               choices=(("0", "Off"), ("1", "Ground only"), ("2", "Ground and legs")), ),
    SettingDef("Groundcover", "stomp intensity", "Stomp intensity", "choice", "1",
               choices=(("0", "Low"), ("1", "Medium"), ("2", "High"))),
    SettingDef("Groundcover", "density", "Grass density", "float", "1.0", minimum=0.0, maximum=1.0),
    SettingDef("Terrain", "distant terrain", "Distant terrain", "bool", "false"),
    SettingDef("Post Processing", "enabled", "Post processing (shaders)", "bool", "false"),
    SettingDef("Shadows", "enable shadows", "Shadows", "bool", "false"),
    SettingDef("Camera", "field of view", "Field of view", "float", "60.0", minimum=30.0, maximum=140.0),
)


def settings_cfg_path(openmw_cfg: Path) -> Path:
    """settings.cfg lives in the same directory as the openmw.cfg being managed."""
    return Path(openmw_cfg).parent / SETTINGS_FILE


def _split_section(line: str) -> "str | None":
    s = line.strip()
    if s.startswith("[") and "]" in s:
        return s[1:s.index("]")].strip()
    return None


def _split_kv(line: str) -> "tuple[str, str] | None":
    s = line.strip()
    if not s or s.startswith("#") or s.startswith("[") or "=" not in s:
        return None
    k, v = s.split("=", 1)
    return k.strip(), v.strip()


def read_settings(path: Path) -> "dict[tuple[str, str], str]":
    """``{(section, key): value}`` for every key in the file (keys lowercased)."""
    out: dict[tuple[str, str], str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    section = ""
    for line in text.splitlines():
        sec = _split_section(line)
        if sec is not None:
            section = sec
            continue
        kv = _split_kv(line)
        if kv:
            out[(section.lower(), kv[0].lower())] = kv[1]
    return out


def get_value(values: "dict[tuple[str, str], str]", d: SettingDef) -> str:
    return values.get((d.section.lower(), d.key.lower()), d.default)


def set_values(path: Path, changes: "dict[tuple[str, str], str]") -> "list[str]":
    """Apply ``{(section, key): value}`` to the file; returns the lines that changed.

    A backup (``settings.cfg.mosaic-bak``) is made once, before the first change.
    Line endings of the existing file (LF or CRLF) are kept.
    """
    path = Path(path)
    raw = path.read_bytes() if path.is_file() else b""
    eol = "\r\n" if b"\r\n" in raw else "\n"
    lines = raw.decode("utf-8", errors="replace").splitlines()

    pending = {(s.lower(), k.lower()): (s, k, str(v)) for (s, k), v in changes.items()}
    changed: list[str] = []

    section = ""
    last_in_section: dict[str, int] = {}   # lowercase section -> index after its last key/line
    for i, line in enumerate(lines):
        sec = _split_section(line)
        if sec is not None:
            section = sec
            last_in_section[section.lower()] = i + 1
            continue
        kv = _split_kv(line)
        if kv is None:
            continue
        last_in_section[section.lower()] = i + 1
        ident = (section.lower(), kv[0].lower())
        if ident in pending:
            s, k, v = pending.pop(ident)
            if kv[1] != v:
                indent = line[: len(line) - len(line.lstrip())]
                lines[i] = f"{indent}{kv[0]} = {v}"
                changed.append(f"[{s}] {k} = {v}")
            # Same value: nothing to write, but the key counts as handled.

    # Missing keys: add to the end of an existing section, else a new section at the end.
    by_section: dict[str, list[tuple[str, str, str]]] = {}
    for (sec_l, _), (s, k, v) in pending.items():
        by_section.setdefault(sec_l, []).append((s, k, v))
    inserts: list[tuple[int, list[str]]] = []
    appended: list[str] = []
    for sec_l, items in by_section.items():
        new_lines = [f"{k} = {v}" for _, k, v in items]
        changed.extend(f"[{s}] {k} = {v}" for s, k, v in items)
        if sec_l in last_in_section:
            inserts.append((last_in_section[sec_l], new_lines))
        else:
            if appended or lines:
                appended.append("")
            appended.append(f"[{items[0][0]}]")
            appended.extend(new_lines)
    for idx, new_lines in sorted(inserts, key=lambda t: -t[0]):
        lines[idx:idx] = new_lines
    lines.extend(appended)

    if not changed:
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if path.is_file() and not backup.exists():
        shutil.copy2(path, backup)
    from Utils.atomic_write import write_atomic_text
    write_atomic_text(path, eol.join(lines) + eol, encoding="utf-8")
    return changed
