"""
modio_quick_update.py  (Baldur's Gate 3)

Resolve mod.io Quick Update targets — the mod.io analog of
``Utils.exe_launch.quick_update`` (Nexus). Simpler than the Nexus version:
there's no name-match fork to replicate, since every mod carrying
FLAG_MODIO_UPDATE already has a confirmed newer file
(``modio_update_checker.check_for_updates`` wrote ``modioLatestFileId`` into
meta.ini) — resolution here is just "fetch that file's download URL".

This module holds only the pure resolve logic; the front-end
(``gui_qt/app.py``) owns the download + install pipeline and all UI.

Loaded by file path (the BG3 folder name has spaces), so it imports only
from packages on ``sys.path`` (``Utils.*``) and loads its siblings with
:func:`_load_sibling`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_sibling(stem: str):
    mod_name = f"{stem}_bg3"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    sibling = Path(__file__).resolve().parent / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(mod_name, str(sibling))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def resolve_modio_quick_update_target(api, staging_root: Path,
                                      mod_name: str) -> tuple[str, object]:
    """Resolve one mod's mod.io update to a downloadable file.

    Returns ``("queued", payload)`` where *payload* is
    ``(mod_name, meta, file)`` — *meta* is the mod's current ``ModioMeta``,
    *file* is a ``ModioFile`` with a populated ``binary_url`` — ready for
    download, or ``("skipped", reason)`` with a human-readable reason.
    """
    modio_meta = _load_sibling("modio_meta")
    meta_path = Path(staging_root) / mod_name / "meta.ini"
    if not meta_path.is_file():
        return ("skipped", "no mod.io metadata")
    meta = modio_meta.read_modio_meta(meta_path)
    if meta.mod_id <= 0:
        return ("skipped", "no mod.io mod id in metadata")
    if meta.latest_file_id <= 0:
        return ("skipped", "latest file unknown")
    if meta.latest_file_id == meta.file_id:
        return ("skipped", "already up to date")
    try:
        file = api.get_file(meta.mod_id, meta.latest_file_id)
    except Exception as exc:
        return ("skipped", f"could not fetch file details ({exc})")
    if file is None or not file.binary_url:
        return ("skipped", "could not resolve download URL")
    return ("queued", (mod_name, meta, file))
