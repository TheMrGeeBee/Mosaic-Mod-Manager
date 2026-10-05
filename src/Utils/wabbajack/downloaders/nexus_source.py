"""Adapter from a Wabbajack Archive's ``NexusState`` to the existing
``Nexus.nexus_download.NexusDownloader``.

Wabbajack-hosted-on-Nexus archives resolve through the exact same signed-CDN
mechanism Collections already use, so this is a thin translation layer (map
Wabbajack's ``GameName``/``ModID``/``FileID`` onto Mosaic's existing,
authenticated Nexus client), not a new downloader.

Needs a live ``NexusDownloader`` instance (unlike the other sources in this
package), since it has to reuse Mosaic's own authenticated client rather than
make anonymous requests -- see ``downloaders.resolve_downloader``'s docstring
for why it isn't in that dispatch table.
"""
from __future__ import annotations

import threading
from pathlib import Path

from Nexus.nexus_download import NexusDownloader

from ..wabbajack_manifest import NexusState
from .http_source import WabbajackDownloadResult

# Wabbajack's Archive.State.GameName strings -> Nexus game domain slugs. Most
# games are just a lowercase of the name, but a few diverge -- cross-check
# this list (and the keys below) against a real modlist's GameName values
# before relying on it; see the plan's "key open risks" note.
_NEXUS_DOMAIN_OVERRIDES = {
    "FalloutNewVegas": "newvegas",
}


def nexus_domain_for(game_name: str) -> str:
    override = _NEXUS_DOMAIN_OVERRIDES.get(game_name or "")
    if override:
        return override
    return (game_name or "").strip().lower()


def download_nexus(state: NexusState, downloader: NexusDownloader, dest_dir: Path, *,
                    progress_cb=None,
                    cancel: "threading.Event | None" = None) -> WabbajackDownloadResult:
    """Download a Nexus-sourced archive via the existing ``NexusDownloader``."""
    domain = nexus_domain_for(state.game_name)
    if not domain or not state.mod_id or not state.file_id:
        return WabbajackDownloadResult(
            success=False,
            error=f"incomplete Nexus reference (domain={domain!r}, "
                  f"mod={state.mod_id}, file={state.file_id})")
    result = downloader.download_file(
        domain, state.mod_id, state.file_id, dest_dir=dest_dir,
        progress_cb=progress_cb, cancel=cancel, known_file_name=state.name)
    if not result.success:
        return WabbajackDownloadResult(success=False, error=result.error)
    return WabbajackDownloadResult(
        success=True, file_path=result.file_path, bytes_downloaded=result.bytes_downloaded)
