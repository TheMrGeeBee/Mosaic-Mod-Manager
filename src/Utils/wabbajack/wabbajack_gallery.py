"""The public Wabbajack modlist gallery: listing, caching and fetching the
``.wabbajack`` file for one entry. Toolkit-neutral (the Qt browser lives in
``gui_qt/wabbajack/wabbajack_browser_view.py``).

The gallery has no single feed with full metadata. Like the Wabbajack app,
this reads ``repositories.json`` (name -> URL of that repository's
``modlists.json``) and fetches every repository's list in parallel. The
merged result is cached on disk for an hour; when the network fails, a
stale cache is still used.

Observed shape (2026-10-07, 148 repositories, 226 lists): every entry has
``links.image/readme/download/machineURL`` and a ``download_metadata`` with
``Hash/Size/NumberOfArchives/SizeOfArchives/NumberOfInstalledFiles/
SizeOfInstalledFiles``; ``game`` is a lowercase-ish Wabbajack game name
(``skyrimspecialedition``, but also ``Fallout4``), so it's matched without
regard to case. Download links are Wabbajack's CDN (222), GitHub or the build
server (3, plain HTTP) and Google Drive (1).
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .wabbajack_hash import hash_file, hashes_match
from .wabbajack_manifest import (
    WABBAJACK_GAME_MAP,
    GoogleDriveState,
    HttpState,
    WabbajackCDNState,
)

REPOSITORIES_URL = ("https://raw.githubusercontent.com/wabbajack-tools/mod-lists/"
                    "master/repositories.json")
GALLERY_WEBSITE = "https://www.wabbajack.org/gallery"
CACHE_MAX_AGE = 3600          # seconds
_FETCH_WORKERS = 16
_TIMEOUT = 20

_GAME_BY_KEY = {k.lower(): v for k, v in WABBAJACK_GAME_MAP.items()}


class GalleryError(Exception):
    """The gallery couldn't be loaded and no cached copy exists."""


@dataclass
class GalleryModlist:
    title: str = ""
    author: str = ""
    description: str = ""
    game: str = ""                 # Wabbajack's name, as the gallery spells it
    version: str = ""
    date_updated: str = ""         # ISO date as published, may be empty
    nsfw: bool = False
    official: bool = False
    utility: bool = False
    force_down: bool = False
    tags: "list[str]" = field(default_factory=list)
    maintainers: "list[str]" = field(default_factory=list)
    machine_url: str = ""          # unique per list; used as its id
    repository: str = ""
    image_url: str = ""
    readme_url: str = ""
    website_url: str = ""
    discord_url: str = ""
    download_url: str = ""
    file_hash: str = ""            # Wabbajack hash of the .wabbajack file
    file_size: int = 0
    archive_count: int = 0
    archives_size: int = 0
    install_size: int = 0

    @property
    def total_size(self) -> int:
        """Disk space while installing: downloads plus the installed files."""
        return self.archives_size + self.install_size

    @property
    def mosaic_game(self) -> "str | None":
        """Mosaic's name for this list's game, or None if unsupported."""
        return mosaic_game_for_gallery(self.game)


def mosaic_game_for_gallery(game: str) -> "str | None":
    return _GAME_BY_KEY.get((game or "").strip().lower())


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_entry(d: dict, repository: str = "") -> GalleryModlist:
    links = d.get("links") or {}
    meta = d.get("download_metadata") or {}
    return GalleryModlist(
        title=str(d.get("title") or ""), author=str(d.get("author") or ""),
        description=str(d.get("description") or ""), game=str(d.get("game") or ""),
        version=str(d.get("version") or ""), date_updated=str(d.get("dateUpdated") or ""),
        nsfw=bool(d.get("nsfw")), official=bool(d.get("official")),
        utility=bool(d.get("utility_list")), force_down=bool(d.get("force_down")),
        tags=[str(t) for t in (d.get("tags") or []) if t],
        maintainers=[str(m) for m in (d.get("maintainers") or []) if m],
        machine_url=str(links.get("machineURL") or ""), repository=repository,
        image_url=str(links.get("image") or ""), readme_url=str(links.get("readme") or ""),
        website_url=str(links.get("websiteURL") or links.get("website") or ""),
        discord_url=str(links.get("discordURL") or ""),
        download_url=str(links.get("download") or ""),
        file_hash=str(meta.get("Hash") or ""), file_size=_int(meta.get("Size")),
        archive_count=_int(meta.get("NumberOfArchives")),
        archives_size=_int(meta.get("SizeOfArchives")),
        install_size=_int(meta.get("SizeOfInstalledFiles")),
    )


def _get_json(url: str):
    import requests
    from Utils.ca_bundle import resolve_ca_bundle
    resp = requests.get(url, timeout=_TIMEOUT, verify=resolve_ca_bundle() or True)
    resp.raise_for_status()
    return resp.json()


def _fetch_raw(get_json=_get_json) -> "tuple[list[dict], list[str]]":
    """Every repository's entries (each tagged with ``_repository``), and a
    message per repository that failed."""
    repos = get_json(REPOSITORIES_URL)
    if not isinstance(repos, dict):
        raise GalleryError("the gallery's repository list isn't in the expected format")

    def one(item):
        name, url = item
        try:
            data = get_json(url)
        except Exception as exc:
            return name, [], f"{name}: {exc}"
        if not isinstance(data, list):
            return name, [], f"{name}: unexpected format"
        return name, [dict(e, _repository=name) for e in data if isinstance(e, dict)], ""

    with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
        results = list(pool.map(one, repos.items()))
    entries = [e for _, es, _ in results for e in es]
    return entries, [err for _, _, err in results if err]


def _entries_from(raw: "list[dict]") -> "list[GalleryModlist]":
    seen: "set[str]" = set()
    out = []
    for d in raw:
        m = parse_entry(d, d.get("_repository", ""))
        key = m.machine_url or m.title
        if not m.title or key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def load_gallery(cache_path: "str | Path", *, max_age: float = CACHE_MAX_AGE,
                 force: bool = False, get_json=_get_json,
                 log=lambda _m: None) -> "list[GalleryModlist]":
    """The whole gallery, from the cache when it's fresh enough, else from
    the network (falling back to a stale cache if that fails)."""
    cache_path = Path(cache_path)
    cached = None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    fresh = (isinstance(cached, dict) and isinstance(cached.get("entries"), list)
             and time.time() - float(cached.get("fetched_at") or 0) < max_age)
    if fresh and not force:
        return _entries_from(cached["entries"])
    try:
        raw, errors = _fetch_raw(get_json)
    except Exception as exc:
        if isinstance(cached, dict) and isinstance(cached.get("entries"), list):
            log(f"Wabbajack gallery: refresh failed ({exc}); using the cached copy")
            return _entries_from(cached["entries"])
        raise GalleryError(f"couldn't load the Wabbajack gallery: {exc}") from exc
    for err in errors:
        log(f"Wabbajack gallery: skipped a repository ({err})")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": time.time(), "entries": raw}),
                       encoding="utf-8")
        tmp.replace(cache_path)
    except OSError as exc:
        log(f"Wabbajack gallery: couldn't save the cache ({exc})")
    return _entries_from(raw)


# ---------------------------------------------------------------------------
# Fetching one list's .wabbajack file
# ---------------------------------------------------------------------------

_DRIVE_ID = re.compile(r"/file/d/([^/]+)|[?&]id=([^&]+)")


def download_state_for(entry: GalleryModlist):
    """The archive-downloader state that fetches ``entry``'s file."""
    url = entry.download_url
    host = urlsplit(url).netloc.lower()
    if host == "authored-files.wabbajack.org":
        return WabbajackCDNState(url=url)
    if host in ("drive.google.com", "docs.google.com"):
        m = _DRIVE_ID.search(url)
        if m:
            return GoogleDriveState(file_id=m.group(1) or m.group(2))
    return HttpState(url=url)


def local_file_for(entry: GalleryModlist, folder: "str | Path") -> Path:
    name = re.sub(r"[^\w.\-]+", "_", entry.machine_url or entry.title).strip("._") or "modlist"
    return Path(folder) / f"{name}.wabbajack"


def fetch_modlist_file(entry: GalleryModlist, folder: "str | Path", *,
                       progress_cb=None, cancel: "threading.Event | None" = None) -> Path:
    """``entry``'s ``.wabbajack`` file in ``folder``: the copy already there
    if it matches the gallery's hash, else a fresh, hash-checked download.
    Raises :class:`GalleryError`."""
    from .downloaders.google_drive_source import download_google_drive
    from .downloaders.http_source import download_http, download_wabbajack_cdn

    if not entry.download_url:
        raise GalleryError(f"{entry.title} has no download link")
    dest = local_file_for(entry, folder)
    if dest.is_file() and entry.file_hash and hashes_match(entry.file_hash, hash_file(dest)):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    state = download_state_for(entry)
    fetch = {WabbajackCDNState: download_wabbajack_cdn, GoogleDriveState: download_google_drive,
             HttpState: download_http}[type(state)]
    result = fetch(state, part, progress_cb=progress_cb, cancel=cancel)
    if not result.success:
        part.unlink(missing_ok=True)
        raise GalleryError(f"couldn't download {entry.title}: {result.error}")
    got = Path(result.file_path or part)
    if entry.file_hash and not hashes_match(entry.file_hash, hash_file(got)):
        got.unlink(missing_ok=True)
        raise GalleryError(f"the downloaded {entry.title} file doesn't match the gallery's hash")
    got.replace(dest)
    return dest
