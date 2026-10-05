"""Dispatch an Archive's download ``State`` to the matching downloader.

Each implemented ``download_*`` function shares the shape
``(state, dest, *, progress_cb=None, cancel=None) -> WabbajackDownloadResult``,
except ``nexus_source.download_nexus`` which additionally needs a live
``NexusDownloader`` instance (it has to reuse Mosaic's authenticated Nexus
client, not make an anonymous request) -- callers special-case
``isinstance(state, NexusState)`` and call it directly rather than going
through :func:`resolve_downloader`.

Only HTTP and Wabbajack-CDN sources are implemented so far (GoogleDrive,
MediaFire, Mega and LoversLab are planned per the approved plan's download-
source build order but not built yet); :func:`resolve_downloader` returns
``None`` for anything not yet wired up so callers can report "unsupported
source" rather than guess.
"""
from __future__ import annotations

from ..wabbajack_manifest import ArchiveState, HttpState, UnknownState, WabbajackCDNState
from .http_source import WabbajackDownloadResult, download_http, download_wabbajack_cdn

__all__ = ["WabbajackDownloadResult", "resolve_downloader", "is_unsupported"]

_DISPATCH = {
    HttpState: download_http,
    WabbajackCDNState: download_wabbajack_cdn,
}


def resolve_downloader(state: ArchiveState):
    """Return a ``(state, dest, **kwargs) -> WabbajackDownloadResult``
    callable for ``state``, or ``None`` if nothing handles it yet."""
    return _DISPATCH.get(type(state))


def is_unsupported(state: ArchiveState) -> bool:
    return isinstance(state, UnknownState)
