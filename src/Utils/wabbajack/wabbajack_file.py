"""Read access to a ``.wabbajack`` modlist container.

A ``.wabbajack`` file is a plain zip. Two things live inside it:
  - a ``modlist`` entry (occasionally seen as ``modlist.json``) holding the
    JSON manifest, parsed by :mod:`Utils.wabbajack.wabbajack_manifest`
  - "inline" file payloads referenced by ``InlineFile``/``RemappedInlineFile``
    directives (``SourceDataID``) and by ``PatchedFromArchive`` directives
    (``PatchID``), stored as zip members named by that id/hash

Pure I/O, no GUI imports -- mirrors Utils/collections/collection_manifest.py's
shape (toolkit-neutral, degrades to a reportable error on anything
unexpected) so a corrupt/truncated ``.wabbajack`` download is a clean message
instead of a stack trace.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from .wabbajack_manifest import ModList, parse_modlist

_MODLIST_NAMES = ("modlist", "modlist.json")


class WabbajackFileError(Exception):
    """A ``.wabbajack`` container could not be opened or has no modlist."""


def read_modlist(path: "str | Path") -> ModList:
    """Parse the ``modlist`` JSON out of a ``.wabbajack`` file.

    Raises :class:`WabbajackFileError` (never a raw zip/json exception) so
    callers can show one clean message instead of a stack trace.
    """
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            target = next((n for n in names if n in _MODLIST_NAMES), None)
            if target is None:
                # tolerate a stray leading slash / case difference some
                # compiler versions have been seen to produce
                target = next(
                    (n for n in names if n.lstrip("/").lower() in _MODLIST_NAMES), None)
            if target is None:
                raise WabbajackFileError(f"{path}: no modlist entry found in archive")
            raw = zf.read(target)
    except zipfile.BadZipFile as exc:
        raise WabbajackFileError(f"{path}: not a valid .wabbajack file ({exc})") from exc
    except FileNotFoundError as exc:
        raise WabbajackFileError(f"{path}: file not found") from exc

    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WabbajackFileError(f"{path}: modlist entry is not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise WabbajackFileError(f"{path}: modlist JSON is not an object")
    return parse_modlist(data)


def extract_inline_file(path: "str | Path", data_id: str, dest: "str | Path") -> bool:
    """Extract one inline-file payload (by its ``SourceDataID``/``PatchID``)
    from the container to ``dest``. Returns ``False`` (never raises) if the
    id isn't present -- the caller treats that as a reportable directive
    failure, not a crash, since one bad entry shouldn't take the whole
    install down.
    """
    if not data_id:
        return False
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = set(zf.namelist())
            candidates = (data_id, data_id.lstrip("/"), f"/{data_id}")
            target = next((n for n in candidates if n in names), None)
            if target is None:
                return False
            dest_path = Path(dest)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(target) as src, open(dest_path, "wb") as out:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
            return True
    except (zipfile.BadZipFile, FileNotFoundError, OSError):
        return False
