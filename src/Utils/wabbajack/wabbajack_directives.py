"""Applies a parsed Wabbajack ``Directive``, producing one installed file.

Each :func:`apply_directive` call writes exactly the file named by
``directive.to`` (resolved relative to ``dest_root``) and, where the
directive carries an expected ``Hash``, verifies the result against it
before reporting success -- a bad reconstruction (e.g. a corrupt patch, or
an unimplemented transform silently writing the wrong bytes) is caught here
as a hash mismatch rather than surfacing as an in-game crash much later.

``CreateBSA``/``CreateBA2`` packs the loose files earlier directives built
into ``TEMP_BSA_FILES/<TempID>/`` (see :mod:`.wabbajack_bsa`). Its output
is *not* required to match the directive's ``Hash``: Wabbajack compresses
with .NET libraries whose output differs from Python's zlib/LZ4, so a
correct rebuild is normally not byte-identical. Every file packed into it
was verified when it was built; a differing archive hash is reported as a
note, not a failure. ``TransformedTexture`` is an explicit v1 non-goal and
reports as unsupported.

``RemappedInlineFile`` (typically an INI or profile file that records
absolute paths) has Wabbajack's path placeholders replaced with the user's
real paths, from a table built by :func:`path_substitutions`. Its output is
*not* hash-verified: it depends on where this particular user installed
things, so it can't match a hash computed when the modlist was compiled.
The placeholder spellings (``{--||GAME_PATH_MAGIC_BACK||--}`` and its
``DOUBLE_BACK``/``FORWARD`` and ``MO2``/``DOWNLOAD`` siblings) follow
Wabbajack's own constants; ``GAME_PATH_MAGIC_DOUBLE_BACK``/``_FORWARD`` are
confirmed in real modlists.

``PatchedFromArchive`` patches are Octodiff deltas in real modlists (see
:mod:`.wabbajack_octodiff`); bsdiff (``BSDIFF40``) is still accepted.
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
from .wabbajack_bsa import ArchiveBuildError, build_archive
from .wabbajack_octodiff import apply_octodiff, is_octodiff
from .wabbajack_vfs import ArchiveIndex, VfsResolutionError


@dataclass
class DirectiveResult:
    success: bool
    path: "Path | None" = None
    error: str = ""
    unsupported: bool = False
    note: str = ""   # success, but worth logging (e.g. a non-identical rebuild)


# Directive kinds apply_directive() can't produce yet; preflight reports a
# modlist containing any of these instead of letting it install partially.
# (CreateBSA is supported, but some archive formats aren't -- see
# wabbajack_bsa.support_problem.)
UNSUPPORTED_DIRECTIVE_TYPES = (TransformedTextureDirective, UnknownDirective)


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


def _verify(directive: Directive, dest: Path, verify_hash: bool = True) -> DirectiveResult:
    expected = getattr(directive, "hash", "")
    if verify_hash and expected and not hashes_match(expected, hash_file(dest)):
        # Never leave a wrong file where an install would pick it up.
        dest.unlink(missing_ok=True)
        return DirectiveResult(
            success=False,
            error=f"hash mismatch writing {directive.to!r} (expected {expected})")
    return DirectiveResult(success=True, path=dest)


def _apply_patch(source: Path, patch_path: Path, dest: Path) -> None:
    """Patch ``source`` into ``dest`` with whichever format ``patch_path`` is:
    Octodiff (what real modlists ship) or bsdiff (BSDIFF40)."""
    with open(patch_path, "rb") as fh:
        head = fh.read(16)
    if is_octodiff(head):
        apply_octodiff(source, patch_path, dest)
    else:
        bsdiff4.file_patch(str(source), str(dest), str(patch_path))


def apply_directive(directive: Directive, *, dest_root: Path, wabbajack_path: "str | Path",
                     archive_index: ArchiveIndex,
                     substitutions: "dict[str, str] | None" = None,
                     bsa_temp_root: "Path | None" = None,
                     verify_hash: bool = True) -> DirectiveResult:
    """Apply one directive, writing ``dest_root / directive.to``.
    ``substitutions`` (from :func:`path_substitutions`) is used only by
    ``RemappedInlineFile``; ``bsa_temp_root`` (where the ``TEMP_BSA_FILES``
    directives were built) only by ``CreateBSA``. ``verify_hash=False`` skips
    the hash check, for a file Wabbajack itself stores with a stale hash (an
    MO2 profile's ``modlist.txt``). A failed directive leaves no file behind."""
    if isinstance(directive, UnknownDirective):
        return DirectiveResult(
            success=False, unsupported=True,
            error=f"unrecognized directive type {directive.type_name!r}")
    if isinstance(directive, UNSUPPORTED_DIRECTIVE_TYPES):
        return DirectiveResult(
            success=False, unsupported=True,
            error=f"{type(directive).__name__} is not implemented yet")

    dest = dest_root / directive.to
    if not dest.resolve().is_relative_to(Path(dest_root).resolve()):
        return DirectiveResult(
            success=False, error=f"refusing to write outside the install folder: {directive.to!r}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(directive, CreateBSADirective):
        if bsa_temp_root is None:
            return DirectiveResult(success=False, error="no archive input folder given")
        # Built under a temporary name so an interrupted run never leaves a
        # truncated archive that a resumed install would take for finished.
        part = dest.with_name(dest.name + ".part")
        try:
            build_archive(directive, bsa_temp_root, part)
            part.replace(dest)
        except ArchiveBuildError as exc:
            part.unlink(missing_ok=True)
            return DirectiveResult(success=False, error=f"couldn't build archive: {exc}")
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        if directive.hash and not hashes_match(directive.hash, hash_file(dest)):
            return DirectiveResult(
                success=True, path=dest,
                note=f"rebuilt {directive.to!r} isn't byte-identical to the curator's "
                     "archive (expected: different compressor); its files were verified")
        return DirectiveResult(success=True, path=dest)

    if isinstance(directive, FromArchiveDirective):
        try:
            source = archive_index.resolve(directive.archive_hash_path)
        except VfsResolutionError as exc:
            return DirectiveResult(success=False, error=str(exc))
        shutil.copyfile(source, dest)
        return _verify(directive, dest, verify_hash)

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
            _apply_patch(source, patch_path, dest)
        except Exception as exc:
            dest.unlink(missing_ok=True)
            return DirectiveResult(success=False, error=f"patch failed: {exc}")
        finally:
            patch_path.unlink(missing_ok=True)
        return _verify(directive, dest, verify_hash)

    if isinstance(directive, (InlineFileDirective, RemappedInlineFileDirective)):
        if not wabbajack_file.extract_inline_file(wabbajack_path, directive.source_data_id, dest):
            return DirectiveResult(
                success=False,
                error=f"inline data {directive.source_data_id!r} not found in container")
        if isinstance(directive, RemappedInlineFileDirective):
            _remap_file(dest, substitutions or {})
            return DirectiveResult(success=True, path=dest)
        return _verify(directive, dest, verify_hash)

    return DirectiveResult(success=False, unsupported=True,
                            error=f"no handler for {type(directive).__name__}")
