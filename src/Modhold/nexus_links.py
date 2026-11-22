"""
nexus_links.py
Turn the Nexus links in a Modhold loadout into downloads.

A loadout item hosted elsewhere carries only a link, almost always to a Nexus *mod
page*, not to one file. So Mosaic has to choose the file, and it does so
conservatively and tells the user before anything downloads:

1. the exact file, when the link names one (``?file_id=``);
2. else, when the game version is known, a file whose name or description names that
   version (an older SKSE build kept under "old versions" says "Compatible with ...
   1.6.1170"), preferring a main file, then the primary one, then the newest;
3. else the author's own *primary* main file;
4. else the newest main file (flagged "ambiguous" when there were several);
5. a mod with no main file is not guessed at: it stays on the user's list.

Qt-free; the Nexus API is injected so this is testable without a network.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

_PATH_RE = re.compile(r"^/([A-Za-z0-9_]+)/mods/(\d+)(?:/|$)")
_NEXUS_HOSTS = {"nexusmods.com", "www.nexusmods.com", "next.nexusmods.com"}


@dataclass(frozen=True)
class NexusRef:
    domain: str            # e.g. "skyrimspecialedition"
    mod_id: int
    file_id: int = 0       # set only when the link names a file


def parse_nexus_url(url: str) -> Optional[NexusRef]:
    """The Nexus mod (and file, if named) a link points at, or None for anything else."""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return None
    if (parsed.hostname or "").lower() not in _NEXUS_HOSTS:
        return None
    m = _PATH_RE.match(parsed.path)
    if not m:
        return None
    file_id = 0
    for key in ("file_id", "fileid"):
        vals = parse_qs(parsed.query).get(key)
        if vals and vals[0].isdigit():
            file_id = int(vals[0])
            break
    return NexusRef(domain=m.group(1).lower(), mod_id=int(m.group(2)), file_id=file_id)


@dataclass
class NexusChoice:
    file: object = None          # a NexusModFile, or None when nothing could be chosen
    ambiguous: bool = False      # several main files and none marked primary
    reason: str = ""             # why nothing was chosen
    version_matched: bool = False   # chosen because it names the game version


def _uploaded(f) -> int:
    return int(getattr(f, "uploaded_timestamp", 0) or 0)


# A match is ignored when the words just before it say the file does NOT work.
_NEGATIVE = re.compile(r"(not\s+compatible|incompatible|no\s+longer|does\s*n[o']t|do\s*n[o']t|won'?t|"
                       r"not\s+(?:work|support)|unsupported)[^.\n]{0,30}$", re.IGNORECASE)


def names_version(file, version: str) -> bool:
    """True when the file's name or description names *version* as a version number.

    ``1.6.1170`` does not match inside ``11.6.11700`` or ``0.1.6.1170``; a trailing
    ``.0`` (``1.7.104.0``) is the same version; "not compatible with 1.6.1170" does not count."""
    if not version:
        return False
    text = " ".join(str(getattr(file, attr, "") or "")
                    for attr in ("name", "file_name", "description"))
    pattern = re.compile(r"(?<![\d.])" + re.escape(version.strip()) + r"(?:\.0+)?(?![\d]|\.\d)")
    for m in pattern.finditer(text):
        if not _NEGATIVE.search(text[max(0, m.start() - 45):m.start()]):
            return True
    return False


def choose_file(files: list, wanted_file_id: int = 0, game_version: str = "") -> NexusChoice:
    """Pick the file to download for a mod page link (see the module docstring)."""
    if wanted_file_id:
        for f in files:
            if getattr(f, "file_id", 0) == wanted_file_id:
                return NexusChoice(file=f)
        return NexusChoice(reason="the file named in the link no longer exists")
    if game_version:
        named = [f for f in files
                 if str(getattr(f, "category_name", "")).upper() != "MISCELLANEOUS"
                 and names_version(f, game_version)]
        if named:
            best = max(named, key=lambda f: (str(getattr(f, "category_name", "")).upper() == "MAIN",
                                             bool(getattr(f, "is_primary", False)), _uploaded(f)))
            return NexusChoice(file=best, version_matched=True)
    mains = [f for f in files if str(getattr(f, "category_name", "")).upper() == "MAIN"]
    if not mains:
        return NexusChoice(reason="it has no main file")
    primary = [f for f in mains if getattr(f, "is_primary", False)]
    if primary:
        return NexusChoice(file=max(primary, key=_uploaded))
    return NexusChoice(file=max(mains, key=_uploaded), ambiguous=len(mains) > 1)


@dataclass
class NexusPick:
    """What will be downloaded from Nexus for one loadout item."""
    order_index: int
    name: str
    ref: NexusRef
    file: object = None
    ambiguous: bool = False
    reason: str = ""
    version_matched: bool = False    # chosen because it names the game version

    @property
    def usable(self) -> bool:
        return self.file is not None


def resolve_nexus_picks(api, items: list, *, workers: int = 4,
                        log: Optional[Callable[[str], None]] = None,
                        game_version: str = "") -> dict[int, NexusPick]:
    """For every item with a Nexus link, list that mod's files and choose one.

    *game_version* is the version the game must run (see :func:`choose_file`).

    One failing mod (removed, hidden, a network blip) only leaves that item
    unusable: it falls back to the user's list with the reason."""
    say = log or (lambda _m: None)
    todo = [i for i in items if getattr(i, "nexus_ref", None) is not None]

    def one(item) -> NexusPick:
        ref = item.nexus_ref
        try:
            listing = api.get_mod_files(ref.domain, ref.mod_id)
            choice = choose_file(list(getattr(listing, "files", []) or []), ref.file_id,
                                 game_version)
        except Exception as e:
            say(f"Nexus: could not list the files of '{item.name}': {e}")
            return NexusPick(item.order_index, item.name, ref, reason=f"Nexus said: {e}")
        return NexusPick(item.order_index, item.name, ref, choice.file,
                         choice.ambiguous, choice.reason, choice.version_matched)

    if not todo:
        return {}
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="nexus-pick") as pool:
        picks = list(pool.map(one, todo))
    return {p.order_index: p for p in picks}
