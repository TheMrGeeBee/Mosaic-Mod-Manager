"""Dataclasses for a parsed Wabbajack modlist (the ``modlist`` JSON inside a
``.wabbajack`` container).

Wabbajack's JSON uses .NET-style polymorphic ``$type`` discriminators for
``Archive.State`` (where/how to download an archive) and for each
``Directive`` (how to produce one installed file). Every dataclass here keeps
the original dict as ``.raw`` so a field this module doesn't recognize yet
never silently disappears -- callers can always fall back to ``.raw``, and
this file can grow incrementally as real modlists get checked against it
(see the plan's "confirm against real .wabbajack files" note).

Field names mirror Wabbajack's C# models (PascalCase in the JSON); this
module exposes them as snake_case Python attributes. Parsing never raises:
an unrecognized ``$type`` becomes an ``Unknown*`` entry carrying the raw
dict rather than failing the whole modlist over one directive.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Archive download states ($type discriminators under Archive.State)
# ---------------------------------------------------------------------------


@dataclass
class NexusState:
    game_name: str = ""
    mod_id: int = 0
    file_id: int = 0
    name: str = ""
    author: str = ""
    version: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class HttpState:
    url: str = ""
    headers: "list[str]" = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class WabbajackCDNState:
    url: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class GoogleDriveState:
    file_id: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class MediaFireState:
    url: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class MegaState:
    url: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class LoversLabState:
    url: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class ManualState:
    url: str = ""
    prompt: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class GameFileSourceState:
    """A file taken from the user's own game install. ``game_file`` is
    relative to the game root, with Windows separators and the ``Data\\``
    prefix (e.g. ``Data\\Dawnguard.esm``); ``game_version`` is the game
    version the curator's copy came from."""
    game: str = ""
    game_file: str = ""
    game_version: str = ""
    hash: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class UnknownState:
    """Any ``$type`` this module doesn't recognize yet. ``downloaders``
    reports these as an unsupported source instead of guessing at one."""
    type_name: str = ""
    raw: dict = field(default_factory=dict)


ArchiveState = (
    NexusState | HttpState | WabbajackCDNState | GoogleDriveState
    | MediaFireState | MegaState | LoversLabState | ManualState | GameFileSourceState
    | UnknownState
)


def _type_key(type_str: str) -> str:
    """Normalize a ``$type`` string (e.g. ``"NexusDownloader, Wabbajack.Lib"``
    or ``"NexusDownloader+State, Wabbajack.Lib"``) down to a bare lowercase
    name, tolerant of the assembly-qualified suffix and the nested ``+State``
    class name some downloaders use."""
    head = (type_str or "").split(",", 1)[0]
    head = head.split("+", 1)[0]
    return head.strip().lower()


_STATE_FACTORIES = {
    "nexusdownloader": lambda d: NexusState(
        game_name=d.get("GameName", ""), mod_id=int(d.get("ModID") or 0),
        file_id=int(d.get("FileID") or 0), name=d.get("Name", ""),
        author=d.get("Author", ""), version=d.get("Version", ""), raw=d),
    "httpdownloader": lambda d: HttpState(
        url=d.get("Url", ""), headers=list(d.get("Headers") or []), raw=d),
    "wabbajackcdndownloader": lambda d: WabbajackCDNState(url=d.get("Url", ""), raw=d),
    "googledrivedownloader": lambda d: GoogleDriveState(file_id=d.get("Id", ""), raw=d),
    "mediafiredownloader": lambda d: MediaFireState(url=d.get("Url", ""), raw=d),
    "megadownloader": lambda d: MegaState(url=d.get("Url", ""), raw=d),
    "loverslabdownloader": lambda d: LoversLabState(url=d.get("Url", ""), raw=d),
    "manualdownloader": lambda d: ManualState(
        url=d.get("Url", ""), prompt=d.get("Prompt", ""), raw=d),
    "gamefilesourcedownloader": lambda d: GameFileSourceState(
        game=d.get("Game", ""), game_file=d.get("GameFile", ""),
        game_version=str(d.get("GameVersion", "") or ""), hash=d.get("Hash", ""), raw=d),
}


def parse_archive_state(d: dict) -> ArchiveState:
    d = d or {}
    factory = _STATE_FACTORIES.get(_type_key(d.get("$type", "")))
    if factory is not None:
        return factory(d)
    return UnknownState(type_name=d.get("$type", ""), raw=d)


@dataclass
class Archive:
    hash: str = ""
    meta: str = ""
    name: str = ""
    size: int = 0
    state: ArchiveState = field(default_factory=UnknownState)
    raw: dict = field(default_factory=dict)


def parse_archive(d: dict) -> Archive:
    d = d or {}
    return Archive(
        hash=d.get("Hash", ""), meta=d.get("Meta", ""), name=d.get("Name", ""),
        size=int(d.get("Size") or 0), state=parse_archive_state(d.get("State") or {}),
        raw=d,
    )


# ---------------------------------------------------------------------------
# Directives ($type discriminators under each Directives[] entry)
# ---------------------------------------------------------------------------


@dataclass
class FromArchiveDirective:
    to: str = ""
    hash: str = ""
    size: int = 0
    archive_hash_path: "list[str]" = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class PatchedFromArchiveDirective:
    to: str = ""
    hash: str = ""
    size: int = 0
    archive_hash_path: "list[str]" = field(default_factory=list)
    patch_id: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class InlineFileDirective:
    to: str = ""
    hash: str = ""
    size: int = 0
    source_data_id: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class RemappedInlineFileDirective:
    to: str = ""
    hash: str = ""
    size: int = 0
    source_data_id: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class CreateBSADirective:
    to: str = ""
    hash: str = ""
    size: int = 0
    temp_id: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class TransformedTextureDirective:
    to: str = ""
    hash: str = ""
    size: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class UnknownDirective:
    """An unrecognized ``$type``, surfaced as an explicit "unsupported
    directive" by preflight/install rather than silently skipped."""
    type_name: str = ""
    to: str = ""
    raw: dict = field(default_factory=dict)


Directive = (
    FromArchiveDirective | PatchedFromArchiveDirective | InlineFileDirective
    | RemappedInlineFileDirective | CreateBSADirective | TransformedTextureDirective
    | UnknownDirective
)


def parse_directive(d: dict) -> Directive:
    d = d or {}
    key = _type_key(d.get("$type", ""))
    to = d.get("To", "")
    hash_ = d.get("Hash", "")
    size = int(d.get("Size") or 0)
    if key == "fromarchive":
        return FromArchiveDirective(
            to=to, hash=hash_, size=size,
            archive_hash_path=list(d.get("ArchiveHashPath") or []), raw=d)
    if key == "patchedfromarchive":
        return PatchedFromArchiveDirective(
            to=to, hash=hash_, size=size,
            archive_hash_path=list(d.get("ArchiveHashPath") or []),
            patch_id=d.get("PatchID", ""), raw=d)
    if key == "inlinefile":
        return InlineFileDirective(
            to=to, hash=hash_, size=size, source_data_id=d.get("SourceDataID", ""), raw=d)
    if key == "remappedinlinefile":
        return RemappedInlineFileDirective(
            to=to, hash=hash_, size=size, source_data_id=d.get("SourceDataID", ""), raw=d)
    if key in ("createbsa", "createba2"):
        return CreateBSADirective(to=to, hash=hash_, size=size, temp_id=d.get("TempID", ""), raw=d)
    if key == "transformedtexture":
        return TransformedTextureDirective(to=to, hash=hash_, size=size, raw=d)
    return UnknownDirective(type_name=d.get("$type", ""), to=to, raw=d)


# ---------------------------------------------------------------------------
# GameType -> Mosaic's internal game identifiers (the extensibility seam)
# ---------------------------------------------------------------------------

# Wabbajack's GameType enum names -> Mosaic's own game names (the ``name``
# each handler under src/Games/ returns; test_wabbajack_manifest_parse checks
# every value here against the real handlers).
# Only Bethesda-engine games are wired end-to-end in v1; a GameType absent
# here is rejected at preflight with a clear message instead of being
# attempted and silently mishandled. Extending to BG3/Cyberpunk 2077 later is
# "add an entry here, plus whatever game-specific directive handling it
# needs in wabbajack_directives.py" -- the VFS, downloaders and orchestrator
# don't need to change.
WABBAJACK_GAME_MAP: "dict[str, str]" = {
    "Skyrim": "Skyrim",
    "SkyrimSpecialEdition": "Skyrim Special Edition",
    "SkyrimVR": "Skyrim VR",
    "Enderal": "Enderal",
    "EnderalSpecialEdition": "Enderal SE",
    "Fallout4": "Fallout 4",
    "Fallout4VR": "Fallout 4 VR",
    "FalloutNewVegas": "Fallout New Vegas",
    "Fallout3": "Fallout 3",
    "Oblivion": "Oblivion",
    "Morrowind": "Morrowind",
}


def mosaic_game_for(game_type: str) -> "str | None":
    """Map a Wabbajack ``GameType`` string to Mosaic's internal game name, or
    ``None`` if this GameType isn't supported yet."""
    return WABBAJACK_GAME_MAP.get((game_type or "").strip())


# ---------------------------------------------------------------------------
# Top-level ModList
# ---------------------------------------------------------------------------


@dataclass
class ModList:
    name: str = ""
    author: str = ""
    description: str = ""
    game_type: str = ""
    version: str = ""
    wabbajack_version: str = ""
    website: str = ""
    readme: str = ""
    image: str = ""
    is_nsfw: bool = False
    archives: "list[Archive]" = field(default_factory=list)
    directives: "list[Directive]" = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def total_archive_size(self) -> int:
        return sum(a.size for a in self.archives)

    @property
    def total_install_size(self) -> int:
        return sum(getattr(d, "size", 0) for d in self.directives)

    @property
    def unsupported_directives(self) -> "list[UnknownDirective]":
        return [d for d in self.directives if isinstance(d, UnknownDirective)]

    @property
    def unsupported_archives(self) -> "list[Archive]":
        return [a for a in self.archives if isinstance(a.state, UnknownState)]


def parse_modlist(d: dict) -> ModList:
    d = d or {}
    return ModList(
        name=d.get("Name", ""), author=d.get("Author", ""),
        description=d.get("Description", ""), game_type=d.get("GameType", ""),
        version=str(d.get("Version", "") or ""),
        wabbajack_version=str(d.get("WabbajackVersion", "") or ""),
        website=d.get("Website", ""), readme=d.get("Readme", ""), image=d.get("Image", ""),
        is_nsfw=bool(d.get("IsNSFW", False)),
        archives=[parse_archive(a) for a in (d.get("Archives") or [])],
        directives=[parse_directive(dd) for dd in (d.get("Directives") or [])],
        raw=d,
    )
