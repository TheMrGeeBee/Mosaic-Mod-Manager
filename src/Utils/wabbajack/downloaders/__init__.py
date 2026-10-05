"""Dispatch an Archive's download ``State`` to the matching downloader.

Each implemented ``download_*`` function shares the shape
``(state, dest, *, progress_cb=None, cancel=None) -> WabbajackDownloadResult``,
except ``nexus_source.download_nexus`` which additionally needs a live
``NexusDownloader`` instance (it has to reuse Mosaic's authenticated Nexus
client, not make an anonymous request) -- callers special-case
``isinstance(state, NexusState)`` and call it directly rather than going
through :func:`resolve_downloader`.

``ManualDownloader`` archives (a page the user must download from by hand)
and unrecognized ``$type``s have no downloader; :func:`resolve_downloader`
returns ``None`` for them so callers can route them to the manual-download
flow or report them as unsupported rather than guess. A LoversLab download
without a valid login comes back with ``needs_auth=True``.
"""
from __future__ import annotations

from ..wabbajack_manifest import (
    ArchiveState,
    GoogleDriveState,
    HttpState,
    LoversLabState,
    MediaFireState,
    MegaState,
    UnknownState,
    WabbajackCDNState,
)
from .google_drive_source import download_google_drive
from .http_source import WabbajackDownloadResult, download_http, download_wabbajack_cdn
from .loverslab_source import download_loverslab
from .mediafire_source import download_mediafire
from .mega_source import download_mega

__all__ = ["WabbajackDownloadResult", "resolve_downloader", "is_unsupported"]

_DISPATCH = {
    HttpState: download_http,
    WabbajackCDNState: download_wabbajack_cdn,
    GoogleDriveState: download_google_drive,
    MediaFireState: download_mediafire,
    MegaState: download_mega,
    LoversLabState: download_loverslab,
}


def resolve_downloader(state: ArchiveState):
    """Return a ``(state, dest, **kwargs) -> WabbajackDownloadResult``
    callable for ``state``, or ``None`` if nothing handles it yet."""
    return _DISPATCH.get(type(state))


def is_unsupported(state: ArchiveState) -> bool:
    return isinstance(state, UnknownState)
