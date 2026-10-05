"""Applies a parsed Wabbajack ``Directive``, producing one installed file.

Each :func:`apply_directive` call writes exactly the file named by
``directive.to`` (resolved relative to ``dest_root``) and, where the
directive carries an expected ``Hash``, verifies the result against it
before reporting success -- a bad reconstruction (e.g. a corrupt patch, or
an unimplemented transform silently writing the wrong bytes) is caught here
as a hash mismatch rather than surfacing as an in-game crash much later.

``CreateBSA``/``CreateBA2`` and ``TransformedTexture`` are not implemented
yet and report as unsupported rather than being attempted: BSA/BA2 writer
byte-for-byte fidelity against Wabbajack's expected output hash needs
dedicated validation against a real modlist first (see the plan's "key open
risks" note), and texture transforms are an explicit v1 non-goal.

``RemappedInlineFile`` (typically an INI or profile file that records
absolute paths) has Wabbajack's path placeholders replaced with the user's
real paths, from a table built by :func:`path_substitutions`. Its output is
*not* hash-verified: it depends on where this particular user installed
things, so it can't match a hash computed when the modlist was compiled.
The placeholder spellings (``{--||GAME_PATH_MAGIC_BACK||--}`` and its
``DOUBLE_BACK``/``FORWARD`` and ``MO2``/``DOWNLOAD`` siblings) follow
Wabbajack's own constants but haven't been checked against a real modlist
from this sandbox.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import bsdiff4

from . import wabbajack_file
from .wabbajack_hash import hash_file, hashes_match
from .wabbajack_manifest import (
    CreateBSADirective,
    Directive,
    FromArchiveDirective,
    InlineFileDirective,
    PatchedFromArchiveDirective,
    RemappedInlineFileDirective,
    TransformedTextureDirective,
    UnknownDirective,
)
from .wabbajack_vfs import ArchiveIndex, VfsResolutionError


@dataclass
class DirectiveResult:
    success: bool
    path: "Path | None" = None
    error: str = ""
    unsupported: bool = False


# Directive kinds apply_directive() can't produce yet; preflight reports a
# modlist containing any of these instead of letting it install partially.
UNSUPPORTED_DIRECTIVE_TYPES = (CreateBSADirective, TransformedTextureDirective, UnknownDirective)


def path_substitutions(*, game_path: "str | None" = None, install_path: "str | None" = None,
                       download_path: "str | None" = None) -> "dict[str, str]":
    """Placeholder -> replacement table for ``RemappedInlineFile`` content.

    Paths are given as the game will see them -- under Proton, Windows-style
    (``Z:\\home\\...``). Each root gets the three spellings Wabbajack uses:
    backslashes, doubled backslashes (for escaped strings), and forward
    slashes.
    """
    table: "dict[str, str]" = {}
    for root, path in (("GAME", game_path), ("MO2", install_path), ("DOWNLOAD", download_path)):
        if not path:
            continue
        back = str(path).replace("/", "\\")
        table[f"{{--||{root}_PATH_MAGIC_BACK||--}}"] = back
        table[f"{{--||{root}_PATH_MAGIC_DOUBLE_BACK||--}}"] = back.replace("\\", "\\\\")
        table[f"{{--||{root}_PATH_MAGIC_FORWARD||--}}"] = back.replace("\\", "/")
    return table


def _remap_file(path: Path, substitutions: "dict[str, str]") -> None:
    data = path.read_bytes()
    for placeholder, replacement in substitutions.items():
        data = data.replace(placeholder.encode("ascii"), replacement.encode("utf-8"))
    path.write_bytes(data)


def _verify(directive: Directive, dest: Path) -> DirectiveResult:
    expected = getattr(directive, "hash", "")
    if expected and not hashes_match(expected, hash_file(dest)):
        return DirectiveResult(
            success=False, path=dest,
            error=f"hash mismatch writing {directive.to!r} (expected {expected})")
    return DirectiveResult(success=True, path=dest)


def apply_directive(directive: Directive, *, dest_root: Path, wabbajack_path: "str | Path",
                     archive_index: ArchiveIndex,
                     substitutions: "dict[str, str] | None" = None) -> DirectiveResult:
    """Apply one directive, writing ``dest_root / directive.to``.
    ``substitutions`` (from :func:`path_substitutions`) is used only by
    ``RemappedInlineFile``."""
    if isinstance(directive, UnknownDirective):
        return DirectiveResult(
            success=False, unsupported=True,
            error=f"unrecognized directive type {directive.type_name!r}")
    if isinstance(directive, UNSUPPORTED_DIRECTIVE_TYPES):
        return DirectiveResult(
            success=False, unsupported=True,
            error=f"{type(directive).__name__} is not implemented yet")

    dest = dest_root / directive.to
    dest.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(directive, FromArchiveDirective):
        try:
            source = archive_index.resolve(directive.archive_hash_path)
        except VfsResolutionError as exc:
            return DirectiveResult(success=False, error=str(exc))
        shutil.copyfile(source, dest)
        return _verify(directive, dest)

    if isinstance(directive, PatchedFromArchiveDirective):
        try:
            source = archive_index.resolve(directive.archive_hash_path)
        except VfsResolutionError as exc:
            return DirectiveResult(success=False, error=str(exc))
        patch_path = dest.parent / f"{dest.name}.wj-patch"
        if not wabbajack_file.extract_inline_file(wabbajack_path, directive.patch_id, patch_path):
            return DirectiveResult(
                success=False, error=f"patch data {directive.patch_id!r} not found in container")
        try:
            bsdiff4.file_patch(str(source), str(dest), str(patch_path))
        except Exception as exc:
            return DirectiveResult(success=False, error=f"bsdiff patch failed: {exc}")
        finally:
            patch_path.unlink(missing_ok=True)
        return _verify(directive, dest)

    if isinstance(directive, (InlineFileDirective, RemappedInlineFileDirective)):
        if not wabbajack_file.extract_inline_file(wabbajack_path, directive.source_data_id, dest):
            return DirectiveResult(
                success=False,
                error=f"inline data {directive.source_data_id!r} not found in container")
        if isinstance(directive, RemappedInlineFileDirective):
            _remap_file(dest, substitutions or {})
            return DirectiveResult(success=True, path=dest)
        return _verify(directive, dest)

    return DirectiveResult(success=False, unsupported=True,
                            error=f"no handler for {type(directive).__name__}")
