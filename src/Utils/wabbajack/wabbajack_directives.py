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

``RemappedInlineFile`` is extracted as-is, without substituting the
game-install-path placeholders real Wabbajack modlists use -- the exact
placeholder token format isn't confirmed against a real modlist yet. In
practice this means a real ``RemappedInlineFile`` directive will currently
fail its own hash check (the directive's ``Hash`` is almost certainly the
*remapped* output's hash, not the raw template's), which is the correct,
loud failure mode for an unfinished feature rather than silently installing
a wrong file.
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


def _verify(directive: Directive, dest: Path) -> DirectiveResult:
    expected = getattr(directive, "hash", "")
    if expected and not hashes_match(expected, hash_file(dest)):
        return DirectiveResult(
            success=False, path=dest,
            error=f"hash mismatch writing {directive.to!r} (expected {expected})")
    return DirectiveResult(success=True, path=dest)


def apply_directive(directive: Directive, *, dest_root: Path, wabbajack_path: "str | Path",
                     archive_index: ArchiveIndex) -> DirectiveResult:
    """Apply one directive, writing ``dest_root / directive.to``."""
    if isinstance(directive, (CreateBSADirective, TransformedTextureDirective)):
        return DirectiveResult(
            success=False, unsupported=True,
            error=f"{type(directive).__name__} is not implemented yet")
    if isinstance(directive, UnknownDirective):
        return DirectiveResult(
            success=False, unsupported=True,
            error=f"unrecognized directive type {directive.type_name!r}")

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
        return _verify(directive, dest)

    return DirectiveResult(success=False, unsupported=True,
                            error=f"no handler for {type(directive).__name__}")
