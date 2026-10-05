"""LoversLab archive downloads.

Covers ``Archive.State``'s ``LoversLabDownloader`` ``$type``. Downloads need
a logged-in session (see :mod:`.loverslab_auth`); with no stored session, or
one LoversLab no longer accepts, the result comes back with
``needs_auth=True`` so the UI can show the login form and retry.

The state's URL is usually an IPS4 file page, which answers with an HTML
download page rather than the file. That page's ``?do=download`` link (with
its per-session ``csrfKey``) is followed to get the archive itself; a URL
that already serves the file is streamed directly.

Only the ``Url`` field of the state is used; newer Wabbajack versions may
describe LoversLab files differently, so the state shape should be checked
against a real modlist that uses LoversLab (``LoversLabState.raw`` keeps the
full original). Not exercised against the live site from this sandbox.
"""
from __future__ import annotations

import html
import re
import threading
from pathlib import Path
from urllib.parse import urljoin

import requests

from Utils.ca_bundle import resolve_ca_bundle
from Utils.downloads import bandwidth_limit

from ..wabbajack_manifest import LoversLabState
from . import loverslab_auth
from .http_source import DownloadCancelled, WabbajackDownloadResult

_CHUNK_SIZE = 256 * 1024
_DOWNLOAD_LINK_RE = re.compile(r'href="([^"]*do=download[^"]*)"')


def _needs_login(resp) -> bool:
    return "/login" in (resp.url or "") or 'name="_processLogin"' in (resp.text or "")


def _not_logged_in(msg: str) -> WabbajackDownloadResult:
    return WabbajackDownloadResult(success=False, error=msg, needs_auth=True)


def download_loverslab(state: LoversLabState, dest: Path, *, progress_cb=None,
                        cancel: "threading.Event | None" = None,
                        cookies: "dict[str, str] | None" = None) -> WabbajackDownloadResult:
    """Download a LoversLab-hosted archive using the stored login session
    (or ``cookies``, if given)."""
    if not state.url:
        return WabbajackDownloadResult(success=False, error="no URL in LoversLabDownloader state")
    cookies = cookies if cookies is not None else loverslab_auth.load_session()
    if not cookies:
        return _not_logged_in("Log in to LoversLab to download this file.")

    verify = resolve_ca_bundle() or True
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers["User-Agent"] = loverslab_auth.USER_AGENT
    try:
        resp = session.get(state.url, stream=True, timeout=60, verify=verify)
        resp.raise_for_status()
        if "text/html" in resp.headers.get("Content-Type", ""):
            if _needs_login(resp):
                return _not_logged_in("Your LoversLab login has expired. Log in again.")
            m = _DOWNLOAD_LINK_RE.search(resp.text)
            if not m:
                return WabbajackDownloadResult(
                    success=False,
                    error="couldn't find a download link on the LoversLab page")
            resp.close()
            resp = session.get(urljoin(state.url, html.unescape(m.group(1))),
                               stream=True, timeout=60, verify=verify)
            resp.raise_for_status()
            if "text/html" in resp.headers.get("Content-Type", ""):
                if _needs_login(resp):
                    return _not_logged_in("Your LoversLab login has expired. Log in again.")
                return WabbajackDownloadResult(
                    success=False, error="LoversLab returned a page instead of the file")

        with resp:
            dest.parent.mkdir(parents=True, exist_ok=True)
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(_CHUNK_SIZE):
                    if cancel and cancel.is_set():
                        raise DownloadCancelled()
                    fh.write(chunk)
                    downloaded += len(chunk)
                    bandwidth_limit.throttle(len(chunk), cancel)
                    if progress_cb:
                        progress_cb(downloaded, total)
        return WabbajackDownloadResult(success=True, file_path=dest, bytes_downloaded=downloaded)
    except DownloadCancelled:
        dest.unlink(missing_ok=True)
        return WabbajackDownloadResult(success=False, error="cancelled")
    except (requests.RequestException, OSError) as exc:
        dest.unlink(missing_ok=True)
        return WabbajackDownloadResult(success=False, error=str(exc))
    finally:
        session.close()
