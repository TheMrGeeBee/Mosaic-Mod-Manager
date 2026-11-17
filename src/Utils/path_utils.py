"""
Shared path utilities used by deploy, filemap, installers and game discovery.

Two groups:

- ``has_path_traversal`` -- path safety for untrusted archive entries.
- ``find_child_ci`` / ``resolve_ci`` -- case-insensitive lookup on a
  case-sensitive filesystem. Windows-era data (FOMOD paths, Wabbajack modlists,
  Steam depot manifests, plugin master names) spells paths in whatever case the
  author used; on Linux ``Data/Foo.esm`` and ``data/FOO.ESM`` are different
  files, so every consumer needs the same "find what is really on disk" walk.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from pathlib import Path, PurePath

# directory -> {lowercase name: [actual names, sorted]}; see ``listings`` below.
Listings = dict[Path, dict[str, list[str]]]


def has_path_traversal(path_str: str) -> bool:
    """Return True if *path_str* contains a ``..`` path segment.

    Checks individual path segments (split on ``/`` and ``\\``), so filenames
    like ``file..name.ext`` are allowed while ``foo/../bar`` is not.
    Also returns True if the path starts with an absolute ``/`` or ``\\``.
    """
    normalised = path_str.replace("\\", "/")
    if normalised.startswith("/"):
        return True
    return ".." in normalised.split("/")


def _listing(directory: Path, listings: "Listings | None") -> dict[str, list[str]]:
    """Map each lowercase name in *directory* to its real spellings (empty if
    the directory cannot be read). Memoised in *listings* when one is given."""
    if listings is not None:
        cached = listings.get(directory)
        if cached is not None:
            return cached
    mapping: dict[str, list[str]] = {}
    try:
        with os.scandir(directory) as it:
            for entry in it:
                mapping.setdefault(entry.name.lower(), []).append(entry.name)
    except OSError:
        pass
    for names in mapping.values():
        names.sort()
    if listings is not None:
        listings[directory] = mapping
    return mapping


def find_child_ci(directory: "str | Path", name: str, *,
                  listings: "Listings | None" = None) -> "Path | None":
    """Return the entry called *name* directly inside *directory*, ignoring case.

    An exact-case match always wins. If several entries differ from *name* only
    in case (possible on Linux), the one that sorts first is returned, so the
    answer never depends on directory listing order. Returns ``None`` if there
    is no such entry or *directory* cannot be read. ``"."`` and ``".."`` are
    never entries: the walk cannot leave *directory*, which matters because the
    names usually come from untrusted FOMOD, modlist or manifest data.

    Pass a shared *listings* dict when resolving many names in the same
    directories: each directory is then read from disk once.
    """
    if name in (".", ".."):
        return None
    directory = Path(directory)
    if listings is None:
        direct = directory / name
        if direct.exists():
            return direct
    names = _listing(directory, listings).get(name.lower())
    if not names:
        return None
    return directory / (name if name in names else names[0])


def _components(rel: "str | PurePath | Iterable[str]") -> list[str]:
    if isinstance(rel, PurePath):
        parts: Iterable[str] = rel.parts
    elif isinstance(rel, str):
        parts = rel.replace("\\", "/").split("/")
    else:
        parts = rel
    return [p for p in parts if p and p != "."]


def resolve_ci(root: "str | Path", rel: "str | PurePath | Iterable[str]", *,
               kind: str = "any", listings: "Listings | None" = None) -> "Path | None":
    """Resolve *rel* under *root*, matching every component ignoring case.

    *rel* may be a ``/`` or ``\\`` separated string, a ``Path``, or a list of
    components. Returns the real on-disk path, or ``None`` if any component is
    missing. *kind* is ``"file"`` or ``"dir"`` to also require what the final
    component is, or ``"any"`` (default) to accept either.

    Matching rules are those of :func:`find_child_ci` (exact case first, then a
    deterministic pick). *listings* is passed through to share directory reads
    across many lookups.
    """
    if kind not in ("any", "file", "dir"):
        raise ValueError(f"kind must be 'any', 'file' or 'dir', not {kind!r}")
    current = Path(root)
    for part in _components(rel):
        found = find_child_ci(current, part, listings=listings)
        if found is None:
            return None
        current = found
    if kind == "file" and not current.is_file():
        return None
    if kind == "dir" and not current.is_dir():
        return None
    return current


def free_bytes(path: "str | Path") -> int:
    """Free space, in bytes, on the filesystem that holds (or would hold) *path*.

    *path* need not exist yet: the nearest existing ancestor is measured, which
    is what a "will this fit?" check before creating a folder needs.
    """
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free
