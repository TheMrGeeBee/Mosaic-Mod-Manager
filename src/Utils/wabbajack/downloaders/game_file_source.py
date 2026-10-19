"""Archives a modlist takes from the user's own game install
(``Archive.State`` ``$type`` ``GameFileSourceDownloader``), e.g. the DLC
masters ``Data\\Dawnguard.esm`` or Creation Club plugins.

Nothing is downloaded or copied: the file is used where it is, after
checking it against the modlist's hash. A mismatch almost always means a
different game version than the curator's, so the error names the version
the modlist expects. The file is never modified or deleted.
"""
from __future__ import annotations

from pathlib import Path

from ..wabbajack_hash import hash_file, hashes_match
from ..wabbajack_manifest import GameFileSourceState
from .http_source import WabbajackDownloadResult


def _resolve_under(base: Path, game_file: str) -> "Path | None":
    current = base
    for part in game_file.replace("\\", "/").strip("/").split("/"):
        if not part:
            continue
        exact = current / part
        if exact.exists():
            current = exact
            continue
        try:
            current = next(c for c in current.iterdir() if c.name.lower() == part.lower())
        except (StopIteration, OSError):
            return None
    return current if current.is_file() else None


def runtime_swap_originals(game_name: str) -> "list[Path]":
    """The folder holding the game files Mosaic's runtime swap replaced, as
    they were before the swap (mirrors the game folder: ``Data/Dawnguard.esm``),
    or ``[]`` when no swap is applied. A modlist built from those files must
    read these originals, not the patched ones in the game folder."""
    try:
        from Utils.config_paths import get_game_config_dir
        from Utils.modding_tools import skyrim_runtime as sr
        state_dir = get_game_config_dir(game_name)
        state = sr.load_state(state_dir)
        backup = sr.backup_dir(state_dir)
        return [backup] if state and state.get("applied") and backup.is_dir() else []
    except Exception:
        return []


def resolve_game_file(game_root: "str | Path", game_file: str,
                      originals: "list[Path] | None" = None) -> "Path | None":
    """The on-disk path of ``game_file`` (``Data\\Foo.esm``) under
    ``game_root``, matching each path part case-insensitively -- Windows
    paths in the modlist, a case-sensitive filesystem here. ``None`` if
    it isn't there.

    While mods are deployed, ``Data/`` holds the mods' files (a cleaned
    ``Dawnguard.esm`` where the game's own was) and the game's originals are
    parked in ``Data_Core/``; that is where the original is read from then.
    Real case (LoreRim, 2026-10-19): 50 "different" game files were really
    the deployed cleaned plugins.

    ``originals`` are further folders (see :func:`runtime_swap_originals`)
    holding the untouched version of files that were patched in place; they
    come first, because a patched file may also be what ``Data_Core`` holds."""
    root = Path(game_root)
    for folder in originals or ():
        found = _resolve_under(Path(folder), game_file)
        if found is not None:
            return found
    parts = game_file.replace("\\", "/").strip("/").split("/")
    if parts and parts[0].lower() == "data":
        core = _resolve_under(root / "Data_Core", "/".join(parts[1:]))
        if core is not None:
            return core
    return _resolve_under(root, game_file)


def fetch_game_file(state: GameFileSourceState, game_root: "str | Path | None",
                    expected_hash: str,
                    originals: "list[Path] | None" = None) -> WabbajackDownloadResult:
    if not game_root:
        return WabbajackDownloadResult(success=False, error="the game's install folder isn't set")
    path = resolve_game_file(game_root, state.game_file, originals)
    if path is None:
        return WabbajackDownloadResult(
            success=False, error=f"{state.game_file} isn't in your game folder")
    if not hashes_match(expected_hash or state.hash, hash_file(path)):
        version = f" (it expects game version {state.game_version})" if state.game_version else ""
        return WabbajackDownloadResult(
            success=False,
            error=f"your {state.game_file} differs from the modlist's{version}")
    return WabbajackDownloadResult(success=True, file_path=path)
