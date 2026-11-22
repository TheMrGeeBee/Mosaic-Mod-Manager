"""
modhold_update.py
Prepare one installed Modhold mod's update: find its newer file, download and
verify it, and build the meta the installer stamps. Qt-free; the app queues the
results through its normal install path (silently replacing the old folder).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from Modhold.browse_util import meta_from_detail
from Modhold.modhold_api import ModholdAPI, ModholdError
from Modhold.modhold_download import ModholdDownloader, failure_reason
from Modhold.modhold_meta import ModholdMeta, read_modhold_meta
from Modhold.modhold_update_checker import find_counterpart, is_update


@dataclass
class UpdateJob:
    mod_name: str               # the staging folder to replace
    archive: str                # the verified download
    info: ModholdMeta           # the new file's meta record


def prepare_update(api: ModholdAPI, downloader: ModholdDownloader, staging: Path,
                   mod_name: str, slug: str,
                   progress_cb: Optional[Callable[[int, int], None]] = None,
                   cancel: Optional[threading.Event] = None
                   ) -> tuple[Optional[UpdateJob], str]:
    """``(job, "")`` when the newer file is downloaded and verified, else
    ``(None, reason)``. Never raises for an ordinary failure."""
    installed = read_modhold_meta(staging / mod_name / "meta.ini")
    if installed.mod_id <= 0:
        return None, "it is not a Modhold mod"
    try:
        detail = api.get_mod(installed.mod_id)
    except ModholdError as e:
        return None, str(e)
    files = [f for f in (detail.get("files") or []) if f.get("id")]
    counter = find_counterpart(installed, files)
    if counter is None:
        return None, "its file is no longer on Modhold"
    if not is_update(installed, counter):
        return None, "already up to date"
    if detail.get("can_download") is False:
        return None, "this account cannot download it (Pass, purchase or beta access)"
    return prepare_file(downloader, detail, counter, installed.game or slug,
                        loadout=installed.loadout, mod_name=mod_name,
                        progress_cb=progress_cb, cancel=cancel)


def prepare_file(downloader: ModholdDownloader, detail: dict, file: dict, slug: str,
                 *, mod_name: str, loadout: str = "",
                 progress_cb: Optional[Callable[[int, int], None]] = None,
                 cancel: Optional[threading.Event] = None
                 ) -> tuple[Optional[UpdateJob], str]:
    """Download and verify one chosen *file* of the mod in *detail*, ready to be
    installed over *mod_name*'s folder. Used by updates and by Change Version."""
    res = downloader.download_file(
        int(file["id"]), known_name=str(file.get("name") or ""),
        known_size=int(file.get("size") or 0),
        known_sha256=str(file.get("sha256") or ""),
        progress_cb=progress_cb, cancel=cancel)
    if not res.success or not res.file_path:
        return None, failure_reason(res)
    info = meta_from_detail(detail, file, slug, loadout=loadout)
    return UpdateJob(mod_name, str(res.file_path), info), ""
