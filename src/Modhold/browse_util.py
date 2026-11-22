"""
browse_util.py
Qt-free helpers for the Modhold browse tab: badges, file choice, meta building.
"""

from __future__ import annotations

from Modhold.modhold_meta import ModholdMeta

SORTS: list[tuple[str, str]] = [
    ("updated", "Recently updated"),
    ("new", "Newest"),
    ("downloads", "Most downloaded"),
    ("vouched", "Most vouched"),
    ("rising", "Rising"),
    ("name", "Name"),
]


def format_size(num_bytes: int) -> str:
    n = float(max(0, int(num_bytes or 0)))
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"   # pragma: no cover


def format_price(price_cents: int) -> str:
    return f"${int(price_cents or 0) / 100:.2f}"


def is_gated(mod: dict) -> bool:
    """A release that needs more than a plain account: beta, paid or supporter-only."""
    return bool(mod.get("beta") or int(mod.get("price_cents") or 0) > 0
                or (mod.get("access") or "free") != "free"
                or mod.get("supporters_until"))


def mod_badges(mod: dict) -> list[str]:
    """Short labels for a card, e.g. ["Beta", "$4.00", "Adult"]."""
    out: list[str] = []
    if mod.get("beta"):
        out.append("Beta")
    price = int(mod.get("price_cents") or 0)
    if price > 0:
        out.append(format_price(price))
    access = (mod.get("access") or "free")
    if access != "free" and price <= 0:
        out.append(access.replace("_", " ").title())
    if mod.get("supporters_until"):
        out.append("Supporters early access")
    if mod.get("adult"):
        out.append("Adult")
    return out


def numbers_line(mod: dict) -> str:
    n = mod.get("numbers") or {}
    parts = [f"{int(n.get('downloads') or 0):,} downloads"]
    score, scores = n.get("score"), int(n.get("scores") or 0)
    if score is not None and scores:
        parts.append(f"score {score:g}/10 ({scores})")
    vouches = int(n.get("vouches") or 0)
    if vouches:
        parts.append(f"{vouches} vouches")
    return " · ".join(parts)


def default_file_selection(files: list[dict]) -> list[dict]:
    """The files to install by default: every main file, or the first file
    when none is marked main. Optional files stay unchecked."""
    mains = [f for f in files if f.get("kind") == "main"]
    if mains:
        return mains
    return files[:1]


def needs_chooser(files: list[dict]) -> bool:
    """Ask which files to install only when there is a real choice."""
    return len(files) > 1


def meta_from_detail(detail: dict, file: dict, slug: str,
                     loadout: str = "") -> ModholdMeta:
    """The meta.ini record for installing *file* of the mod described by *detail*."""
    author = detail.get("author") or {}
    return ModholdMeta(
        mod_id=int(detail.get("id") or 0),
        file_id=int(file.get("id") or 0),
        version=str(file.get("version") or detail.get("version") or ""),
        name=str(detail.get("name") or ""),
        url=str(detail.get("page") or ""),
        uploader=str(author.get("name") or author.get("handle") or ""),
        sha256=str(file.get("sha256") or "").lower(),
        game=slug,
        locked="beta" if detail.get("beta") else "",
        loadout=loadout,
        file_label=str(file.get("label") or ""),
        file_kind=str(file.get("kind") or ""),
    )


def prebuilt_nexus_meta(info: ModholdMeta):
    """A blank NexusModMeta carrying the Modhold record. Handed to the installer as
    the prebuilt meta, it skips the Nexus filename lookup and stamps the Modhold
    keys (see Utils/mods/mod_install.py::_write_install_meta)."""
    from Nexus.nexus_meta import NexusModMeta
    meta = NexusModMeta()
    meta.modhold_info = info
    return meta


def page_count(total: int, per_page: int) -> int:
    per_page = max(1, int(per_page or 50))
    return max(1, -(-int(total or 0) // per_page))
