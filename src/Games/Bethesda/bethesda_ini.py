"""
bethesda_ini.py
Shared INI read/write helpers for the Bethesda game family.

Regex-based line editing (not configparser): Bethesda INIs contain values
configparser cannot round-trip, and edits must preserve the file verbatim.
"""

import re
from pathlib import Path

from Utils.atomic_write import write_atomic_text


def _read_ini_key(ini_path: Path, section: str, key: str) -> "str | None":
    """Return the current value for [section] key, or None if not present."""
    try:
        text = ini_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        text = ini_path.read_text(encoding="utf-8", errors="replace")

    section_re = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=(?P<value>.*)$")

    in_section = False
    for line in text.splitlines():
        m = section_re.match(line)
        if m:
            in_section = m.group("name").strip() == section
            continue
        if in_section:
            km = key_re.match(line)
            if km:
                return km.group("value").rstrip("\r")
    return None


def _set_ini_key(ini_path: Path, section: str, key: str, value: "str | None") -> None:
    """Set or remove a single INI key without disturbing the rest of the file.

    Bethesda game INIs sometimes contain multi-line values (e.g. Fallout.ini's
    [GeneralWarnings] section) that configparser refuses to parse. This helper
    does a line-based edit so the rest of the file is preserved byte-for-byte.
    value=None removes the key; empty [section] blocks are pruned on removal.
    """
    try:
        text = ini_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except UnicodeDecodeError:
        text = ini_path.read_text(encoding="utf-8", errors="replace")

    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline) if text else []

    section_header = f"[{section}]"
    section_re = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")

    section_start = -1
    section_end = len(lines)
    for i, line in enumerate(lines):
        m = section_re.match(line)
        if not m:
            continue
        if section_start == -1 and m.group("name").strip() == section:
            section_start = i
        elif section_start != -1:
            section_end = i
            break

    if section_start == -1:
        if value is None:
            return
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(section_header)
        lines.append(f"{key}={value}")
        lines.append("")
    else:
        key_line = -1
        for i in range(section_start + 1, section_end):
            if key_re.match(lines[i]):
                key_line = i
                break

        if value is None:
            if key_line != -1:
                del lines[key_line]
                section_end -= 1
            has_content = any(
                ln.strip() and not ln.strip().startswith((";", "#"))
                for ln in lines[section_start + 1:section_end]
            )
            if not has_content:
                trailing = section_end
                while trailing < len(lines) and lines[trailing] == "":
                    trailing += 1
                del lines[section_start:trailing]
        else:
            new_line = f"{key}={value}"
            if key_line != -1:
                lines[key_line] = new_line
            else:
                lines.insert(section_end, new_line)

    out = newline.join(lines)
    if text.endswith(newline) and not out.endswith(newline):
        out += newline
    # If the INI is a symlink (profile-specific INI files routed into My Games),
    # write *through* the link to its real target so the edit persists back to the
    # profile's "ini files" folder and the symlink itself survives. An atomic
    # write-temp→rename would clobber the link, turning it into a regular file.
    if ini_path.is_symlink():
        real = ini_path.resolve()
        write_atomic_text(real, out)
    else:
        write_atomic_text(ini_path, out)


# A profile's "ini files" folder can hold INIs an installer put there on
# purpose (a Wabbajack modlist's own). The first deploy that links them must
# not replace them with the live copy from My Games, as it does for an INI the
# user may have changed in the game; this marker lists them until that link.
INSTALLED_INIS_MARKER = ".installed_inis"


def mark_installed_inis(ini_dir: Path, names) -> None:
    names = sorted({n for n in names} | {n for n in _read_marker(ini_dir)})
    (Path(ini_dir) / INSTALLED_INIS_MARKER).write_text(
        "".join(f"{n}\n" for n in names), encoding="utf-8")


def _read_marker(ini_dir: Path) -> "list[str]":
    try:
        text = (Path(ini_dir) / INSTALLED_INIS_MARKER).read_text(encoding="utf-8")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def read_installed_inis(ini_dir: Path) -> "set[str]":
    """Lower-cased names of the INIs in *ini_dir* still waiting for their
    first link."""
    return {n.lower() for n in _read_marker(ini_dir)}


def clear_installed_inis(ini_dir: Path) -> None:
    try:
        (Path(ini_dir) / INSTALLED_INIS_MARKER).unlink()
    except FileNotFoundError:
        pass


# Restore puts My Games' original INIs back (from <name>.bak). The next deploy
# must not take those for settings the game wrote and copy them over the
# profile's own INIs. This note in My Games records what restore put back, and
# its exact contents: an INI still identical to it is the original, not news.
RESTORED_INIS_NOTE = ".mosaic_restored_inis"


def _ini_digest(path: Path) -> "str | None":
    import hashlib
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _read_restored(mygames: Path) -> "dict[str, str]":
    try:
        text = (Path(mygames) / RESTORED_INIS_NOTE).read_text(encoding="utf-8")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        name, _, digest = line.partition("\t")
        if name and digest:
            out[name.lower()] = digest
    return out


def note_restored_ini(mygames: Path, path: Path) -> None:
    """Record that restore just put *path* (in *mygames*) back from its backup."""
    digest = _ini_digest(path)
    if digest is None:
        return
    entries = _read_restored(mygames)
    entries[Path(path).name.lower()] = digest
    (Path(mygames) / RESTORED_INIS_NOTE).write_text(
        "".join(f"{n}\t{d}\n" for n, d in sorted(entries.items())), encoding="utf-8")


def is_restored_original(mygames: Path, path: Path) -> bool:
    """True while *path* is exactly what restore put back: the original from
    before the profile's INIs were linked, which nothing has changed since."""
    want = _read_restored(mygames).get(Path(path).name.lower())
    return want is not None and _ini_digest(path) == want


def forget_restored_inis(mygames: Path) -> None:
    try:
        (Path(mygames) / RESTORED_INIS_NOTE).unlink()
    except FileNotFoundError:
        pass
