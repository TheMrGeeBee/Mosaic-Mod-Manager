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


def resolve_game_file(game_root: "str | Path", game_file: str) -> "Path | None":
    """The on-disk path of ``game_file`` (``Data\\Foo.esm``) under
    ``game_root``, matching each path part case-insensitively -- Windows
    paths in the modlist, a case-sensitive filesystem here. ``None`` if
    it isn't there."""
    current = Path(game_root)
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


def fetch_game_file(state: GameFileSourceState, game_root: "str | Path | None",
                    expected_hash: str) -> WabbajackDownloadResult:
    if not game_root:
        return WabbajackDownloadResult(success=False, error="the game's install folder isn't set")
    path = resolve_game_file(game_root, state.game_file)
    if path is None:
        return WabbajackDownloadResult(
            success=False, error=f"{state.game_file} isn't in your game folder")
    if not hashes_match(expected_hash or state.hash, hash_file(path)):
        version = f" (it expects game version {state.game_version})" if state.game_version else ""
        return WabbajackDownloadResult(
            success=False,
            error=f"your {state.game_file} differs from the modlist's{version}")
    return WabbajackDownloadResult(success=True, file_path=path)
