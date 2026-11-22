"""
loadout_install.py
Install a Modhold loadout into a profile.

A loadout is Modhold's equivalent of a Nexus Collection. This installer does the
loadout-specific part, and reports through the same ``CollectionInstallCallbacks`` /
``CollectionInstallControl`` the Nexus pipeline uses, so the app's progress overlay,
pause/cancel, cleanup and "these mods did not install" report all work unchanged.

How it runs:

* Files download in parallel (a few at a time) but install strictly in the loadout's
  order, one at a time, so a FOMOD wizard never overlaps another and the modlist
  ends up in the author's order.
* Items come from two places. Those hosted on Modhold are fetched with the player's
  Modhold key and checked against Modhold's SHA-256. Items hosted elsewhere that
  link to a Nexus mod page are fetched from Nexus (Premium, direct download) when
  the caller supplies a Nexus connection and the files chosen for them; they are
  stamped with their real Nexus identity so Nexus update checks work on them.
* A finished archive already in the download cache is reused with no network call.
* Anything that cannot be downloaded is left in ``control.unresolved_offsite`` (and
  written to the profile) with its link, for the user to get.
* Resume: with ``continue_existing`` the files already staged in the profile are
  kept and not fetched again.
* Pause and cancel stop at the next safe point; nothing is deleted here (the app's
  existing cancel cleanup owns that).
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from Modhold.browse_util import prebuilt_nexus_meta
from Modhold.loadout_manifest import Loadout, LoadoutFile, LoadoutItem, LoadoutPlan
from Modhold.modhold_download import (
    ModholdDownloader,
    ModholdDownloadResult,
    failure_reason,
    refusal_is_permanent,
)
from Modhold.modhold_meta import ModholdMeta, read_modhold_meta_fast
from Modhold.nexus_links import NexusPick
from Utils.atomic_write import write_atomic_text
from Utils.collections.collection_install import (
    CollectionInstallCallbacks,
    CollectionInstallControl,
    _audit_profile,
    _write_new_profile_modlist,
    build_install_report,
    write_install_report,
)
from Utils.mods.mod_install import install_collection_archive

LOADOUT_RECORD_NAME = "modhold_loadout.json"
MAX_DOWNLOADS = 3
_NAME_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class _Job:
    rank: int                        # the install order (item rank * 1000 + file index)
    item: LoadoutItem
    file: Optional[LoadoutFile]      # the Modhold file, for a Modhold job
    name: str                        # the staging folder name to install into
    pick: Optional[NexusPick] = None  # the chosen Nexus file, for a Nexus job
    status: str = "pending"          # pending | existing | installed | download_failed | ...
    detail: str = ""
    folder: str = ""
    path: str = ""                   # the verified archive
    prebuilt: object = None          # the meta handed to the installer
    # The label of this job's row in the progress overlay, and of its future. It travels
    # through Qt signals typed as C ints (the app's _col_row is one), so it must fit 32
    # bits: file ids from either site must never be used, a Nexus id plus an offset
    # overflows. build_jobs gives every job a small, unique number.
    key: int = 0

    @property
    def source(self) -> str:
        return "nexus" if self.pick is not None else "modhold"

    @property
    def mod_id(self) -> int:
        return int(self.pick.ref.mod_id) if self.pick is not None else int(self.item.mod_id or 0)

    @property
    def file_id(self) -> int:
        if self.pick is not None:
            return int(getattr(self.pick.file, "file_id", 0) or 0)
        return self.file.id if self.file is not None else 0


def _clean_name(text: str, fallback: str) -> str:
    name = _NAME_BAD.sub("_", (text or "").strip()).strip(". ")
    return (name or fallback)[:120]


def build_jobs(plan: LoadoutPlan,
               picks: "dict[int, NexusPick] | None" = None) -> list[_Job]:
    """One job per file, in loadout order, each with a folder name that is unique
    within the run (two items called the same thing never overwrite each other).

    A Nexus item becomes a job only when it has a usable chosen file in *picks*;
    one without it still gets a job (so it is reported as failed with a reason
    rather than silently vanishing)."""
    picks = picks or {}
    jobs: list[_Job] = []
    used: set[str] = set()

    def unique(base: str) -> str:
        name, n = base, 2
        while name.lower() in used:
            name = f"{base} ({n})"
            n += 1
        used.add(name.lower())
        return name

    for rank, item in enumerate(plan.ordered()):
        if item.hosted:
            for fi, f in enumerate(item.files):
                base = _clean_name(item.name, f"Modhold {item.mod_id}")
                if len(item.files) > 1:
                    base = f"{base} - {f.version or f.id}"
                jobs.append(_Job(rank=rank * 1000 + fi, item=item, file=f, name=unique(base)))
        else:
            pick = picks.get(item.order_index)
            if pick is None:
                pick = NexusPick(item.order_index, item.name, item.nexus_ref,
                                 reason="no Nexus file was chosen for it")
            jobs.append(_Job(rank=rank * 1000, item=item, file=None, pick=pick,
                             name=unique(_clean_name(item.name, f"Nexus {pick.ref.mod_id}"))))
    for n, job in enumerate(jobs, start=1):
        job.key = n
    return jobs


def meta_for(job: _Job, loadout: Loadout) -> ModholdMeta:
    """The meta.ini record stamped on an installed Modhold file."""
    item, f = job.item, job.file
    return ModholdMeta(
        mod_id=int(item.mod_id or 0), file_id=f.id, version=f.version or item.version,
        name=item.name, url=item.page, uploader=item.author, sha256=f.sha256,
        game=loadout.game_slug, locked=item.locked, loadout=str(loadout.id))


def staged_file_ids(staging: Path) -> dict[int, str]:
    """``{modhold file id: folder name}`` for every Modhold mod already staged."""
    found: dict[int, str] = {}
    try:
        for folder in staging.iterdir():
            if not folder.is_dir():
                continue
            meta = read_modhold_meta_fast(folder / "meta.ini")
            if meta is not None and meta.file_id > 0:
                found.setdefault(meta.file_id, folder.name)
    except OSError:
        pass
    return found


def staged_nexus_files(staging: Path) -> dict[tuple[int, int], str]:
    """``{(nexus mod id, file id): folder name}`` for every Nexus mod already staged."""
    found: dict[tuple[int, int], str] = {}
    try:
        from Nexus.nexus_meta import read_meta
        for folder in staging.iterdir():
            meta_path = folder / "meta.ini"
            if not folder.is_dir() or not meta_path.is_file():
                continue
            try:
                m = read_meta(meta_path)
            except Exception:
                continue
            if getattr(m, "mod_id", 0) and getattr(m, "file_id", 0):
                found.setdefault((int(m.mod_id), int(m.file_id)), folder.name)
    except OSError:
        pass
    return found


def items_for_unresolved(loadout: Loadout,
                         unresolved: "list[tuple[str, str]]") -> list[LoadoutItem]:
    """The loadout items behind a ``control.unresolved_offsite`` list, in loadout order
    (the list only carries names and links; the items know their rank and notes)."""
    wanted = {(n, u) for n, u in unresolved}
    out = [i for i in loadout.items if (i.name, i.link) in wanted]
    seen, unique = set(), []
    for item in out:
        key = (item.name, item.link)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def rebuild_profile_index(game, profile_dir: Path, modlist_path: Path,
                          log: Callable[[str], None]) -> bool:
    """Bring the profile's mod index up to date, as the Nexus collection pipeline does
    at the end of an install. Without it, a mod installed into a profile that already
    has an index is in ``modlist.txt`` but missing from the file map: its plugins don't
    list and its files don't deploy until the user presses Refresh. (A brand-new profile
    hides the problem, because the app builds the first index from scratch.)

    The index must sit next to the *effective* filemap, which is where the reload
    reads it. Returns True when the index was rebuilt; never raises."""
    try:
        log("Updating mod index…")
        from Nexus.nexus_meta import collect_root_flagged_mods
        from Utils.deploy.deploy import load_per_mod_strip_prefixes
        from Utils.filemap import rebuild_mod_index
        staging = game.get_effective_mod_staging_path()
        try:
            index_dir = game.get_effective_filemap_path().parent
        except Exception:
            index_dir = profile_dir
        try:
            root_flagged = collect_root_flagged_mods(modlist_path, staging, log_fn=log)
        except Exception:
            root_flagged = set()
        rebuild_mod_index(
            index_dir / "modindex.bin", staging,
            strip_prefixes=set(getattr(game, "mod_folder_strip_prefixes", None) or ()) or None,
            per_mod_strip_prefixes=load_per_mod_strip_prefixes(profile_dir),
            allowed_extensions=set(getattr(game, "mod_install_extensions", None) or ()) or None,
            root_folder_mods=set(root_flagged or ()) or None,
            normalize_folder_case=getattr(game, "normalize_folder_case", True))
        return True
    except Exception as exc:
        log(f"Mod index rebuild skipped: {exc}")
        return False


def write_loadout_record(profile_dir: Path, loadout: Loadout, plan: LoadoutPlan,
                         picks: "dict[int, NexusPick] | None" = None) -> None:
    """Save which loadout a profile came from, what came from Nexus, and the items the
    user must still fetch themselves, with their links (the profile's own to-do list)."""
    picks = picks or {}
    record = {
        "id": loadout.id, "name": loadout.name, "by": loadout.by, "page": loadout.page,
        "game": loadout.game_slug, "updated": loadout.updated,
        "nexus": [{"name": i.name, "url": i.link,
                   "file": getattr(getattr(picks.get(i.order_index), "file", None),
                                   "file_name", "")} for i in plan.nexus],
        "external": [{"name": i.name, "url": i.link, "note": i.note,
                      "optional": i.optional} for i in plan.external],
        "skipped_optional": [i.name for i in plan.skipped_optional],
    }
    try:
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        write_atomic_text(Path(profile_dir) / LOADOUT_RECORD_NAME,
                          json.dumps(record, indent=2))
    except OSError:
        pass


def run_loadout_install(
        *, game, loadout: Loadout, plan: LoadoutPlan, downloader: ModholdDownloader,
        profile_dir: Path, callbacks: Optional[CollectionInstallCallbacks] = None,
        control: Optional[CollectionInstallControl] = None,
        continue_existing: bool = False, max_downloads: int = MAX_DOWNLOADS,
        install_fn: Callable = install_collection_archive,
        nexus_picks: "dict[int, NexusPick] | None" = None,
        nexus_api=None, nexus_downloader=None) -> None:
    """Download and install *plan*'s items into *profile_dir*.

    Reports through *callbacks* (``on_done`` / ``on_paused`` / ``on_cancelled``) and
    fills *control* (``unresolved_offsite``, ``failed_mods``, ``audit_warnings``).
    *nexus_picks* / *nexus_api* / *nexus_downloader* enable the items in
    ``plan.nexus``; without them those items are reported as failed, never skipped
    silently. Never raises for a single item's failure."""
    cb = callbacks or CollectionInstallCallbacks()
    ctl = control or CollectionInstallControl()
    log = cb.on_log

    game.set_active_profile_dir(profile_dir)
    game.load_paths()
    modlist_path = profile_dir / "modlist.txt"
    staging = game.get_effective_mod_staging_path()
    staging_path = Path(staging) if staging is not None else None

    ctl.unresolved_offsite = [(i.name, i.link) for i in plan.external]
    write_loadout_record(profile_dir, loadout, plan, nexus_picks)

    jobs = build_jobs(plan, nexus_picks)
    total = len(jobs)
    cb.on_status(f"Installing {loadout.name}…")
    cb.on_progress(0.0)
    if total == 0:
        log("Loadout install: nothing to download.")
        cb.on_progress(None)
        cb.on_done(0, 0, 0, profile_dir.name)
        return

    if continue_existing and staging_path is not None:
        have_modhold = staged_file_ids(staging_path)
        have_nexus = staged_nexus_files(staging_path)
        for job in jobs:
            if job.pick is not None:
                folder = have_nexus.get((job.mod_id, job.file_id)) if job.pick.usable else None
            else:
                folder = have_modhold.get(job.file_id)
            if folder:
                job.status, job.folder = "existing", folder

    install_order: list[tuple[int, str]] = [
        (j.rank, j.folder) for j in jobs if j.status == "existing"]
    done = sum(1 for j in jobs if j.status == "existing")
    lock = threading.Lock()

    # ---- downloads (parallel, verified, cached) -------------------------------
    def fetch_modhold(job: _Job) -> ModholdDownloadResult:
        f = job.file
        return downloader.download_file(
            f.id, known_name=f.name, known_size=f.size, known_sha256=f.sha256,
            progress_cb=lambda d, t, _k=job.key: cb.on_dl_mod_update(_k, int(d), int(t)),
            cancel=ctl.stop)

    def fetch_nexus(job: _Job) -> ModholdDownloadResult:
        pick = job.pick
        if not pick.usable:
            return ModholdDownloadResult(False, error=f"Nexus: {pick.reason}")
        if nexus_downloader is None:
            return ModholdDownloadResult(
                False, error="Nexus: not connected, so it could not be downloaded.")
        nf = pick.file
        size = (getattr(nf, "size_in_bytes", 0) or 0) or (getattr(nf, "size_kb", 0) or 0) * 1024
        r = nexus_downloader.download_file(
            game_domain=pick.ref.domain, mod_id=pick.ref.mod_id, file_id=nf.file_id,
            known_file_name=getattr(nf, "file_name", ""), expected_size_bytes=size,
            progress_cb=lambda d, t, _k=job.key: cb.on_dl_mod_update(_k, int(d), int(t)),
            cancel=ctl.stop)
        if not r.success or r.file_path is None:
            msg = f"Nexus: {r.error or 'the download failed'}"
            gone = bool(getattr(r, "mod_removed", False))
            denied = getattr(r, "status_code", 0) in (401, 403)
            return ModholdDownloadResult(False, error=msg, gone=gone, not_entitled=denied)
        info = None
        if nexus_api is not None:
            try:
                info = nexus_api.get_mod(pick.ref.domain, pick.ref.mod_id)
            except Exception as e:                       # the meta is just less detailed
                log(f"Loadout install: no Nexus details for '{job.item.name}': {e}")
        try:
            from Nexus.nexus_meta import build_meta_from_download
            job.prebuilt = build_meta_from_download(
                game_domain=pick.ref.domain, mod_id=pick.ref.mod_id, file_id=nf.file_id,
                archive_name=Path(r.file_path).name, mod_info=info, file_info=nf)
        except Exception as e:
            log(f"Loadout install: could not build Nexus meta for '{job.item.name}': {e}")
        return ModholdDownloadResult(True, Path(r.file_path), Path(r.file_path).name,
                                     file_id=nf.file_id,
                                     from_cache=bool(getattr(r, "from_cache", False)))

    def fetch(job: _Job) -> ModholdDownloadResult:
        size = job.file.size if job.file is not None else int(
            getattr(job.pick.file, "size_in_bytes", 0) or 0)
        cb.on_dl_mod_start(job.key, job.item.name, size)
        try:
            return fetch_nexus(job) if job.pick is not None else fetch_modhold(job)
        except Exception as e:                       # one bad download never stops the rest
            return ModholdDownloadResult(False, error=str(e), file_id=job.file_id)
        finally:
            cb.on_dl_mod_finish(job.key)

    pool = ThreadPoolExecutor(max_workers=max(1, int(max_downloads)),
                              thread_name_prefix="loadout-dl")
    futures: dict[int, Future] = {}
    for job in jobs:
        if job.status == "pending":
            futures[job.key] = pool.submit(fetch, job)

    def stopped() -> bool:
        return ctl.stop.is_set() or ctl.cancel.is_set() or ctl.pause.is_set()

    # ---- install, in the loadout's order --------------------------------------
    try:
        for job in jobs:
            if job.status != "pending":
                cb.on_row_installed(job.key)
                continue
            if stopped():
                break
            res = futures[job.key].result()
            if res.cancelled or (stopped() and not res.success):
                break
            if not res.success or not res.file_path:
                # Asking again helps for a dropped connection, a file still being scanned
                # or a key that was removed; it does not for a file this account can't
                # get or a version the author replaced, so those are not "retryable".
                job.status = "install_failed" if refusal_is_permanent(res) else "download_failed"
                job.detail = failure_reason(res)
                log(f"Loadout install: '{job.item.name}' — {job.detail}")
                with lock:
                    done += 1
                cb.on_progress(done / total)
                continue
            job.path = str(res.file_path)
            prebuilt = (job.prebuilt if job.pick is not None
                        else prebuilt_nexus_meta(meta_for(job, loadout)))
            cb.on_extract_add(job.key, job.name)
            try:
                folder = install_fn(
                    job.path, game, profile_dir, log_fn=log,
                    progress_fn=lambda d, t, p=None, _k=job.key:
                        cb.on_extract_update(_k, int(d), int(t)),
                    prebuilt_meta=prebuilt,
                    preferred_name=job.name,
                    # A FOMOD the loadout records nothing for: ask the user, one at a time.
                    fomod_no_choice_action="wizard",
                    defer_interactive_fomod=False, defer_interactive_bain=False,
                    resolve_fomod=cb.resolve_fomod, resolve_bain=cb.resolve_bain,
                    skip_index_update=True, overwrite_existing=None,
                    cancel=ctl.stop, concurrent_workers=1)
            except Exception as e:
                folder = None
                job.detail = str(e)
                log(f"Loadout install: '{job.item.name}' failed to install: {e}")
            finally:
                cb.on_extract_remove(job.key)
            if folder:
                job.status, job.folder = "installed", str(folder)
                install_order.append((job.rank, job.folder))
                cb.on_row_installed(job.key)
            elif stopped():
                break                                  # paused/cancelled mid-extraction
            else:
                job.status = "stage_empty" if not job.detail else "install_failed"
                log(f"Loadout install: '{job.item.name}' produced no staged files.")
            with lock:
                done += 1
            cb.on_progress(done / total)
    finally:
        for fut in futures.values():
            fut.cancel()
        pool.shutdown(wait=False, cancel_futures=True)

    # What is in the profile now: the modlist reflects it even when paused, so a
    # paused profile is coherent and Continue picks up from it.
    if install_order and modlist_path is not None:
        _write_new_profile_modlist(profile_dir, modlist_path, list(install_order), log)

    installed = sum(1 for j in jobs if j.status in ("installed", "existing"))
    if ctl.cancel.is_set():
        cb.on_cancelled(profile_dir)
        return
    # Only mods installed in THIS run are new to the index (a Continue keeps the old ones).
    # A paused install is indexed too, so what is there shows up right away.
    if any(j.status == "installed" for j in jobs) and staging_path is not None:
        rebuild_profile_index(game, profile_dir, modlist_path, log)
    if ctl.pause.is_set() or ctl.stop.is_set():
        cb.on_paused(installed, profile_dir.name)
        return

    failed = [j for j in jobs if j.status not in ("installed", "existing")]
    missing = [(j.item.name, j.mod_id, j.file_id,
                j.status if j.status != "pending" else "unknown", j.detail) for j in failed]
    report = build_install_report(missing, total=total, slug=str(loadout.id))
    ctl.failed_mods = list(report["failed"])
    write_install_report(profile_dir, report)
    # A Nexus item that could not be fetched joins the user's list, with its link.
    ctl.unresolved_offsite = ctl.unresolved_offsite + [
        (j.item.name, j.item.link) for j in failed if j.pick is not None and j.item.link]
    if staging_path is not None:
        ctl.audit_warnings = _audit_profile(game, profile_dir, modlist_path, staging_path, log)
    cb.on_progress(None)
    cb.on_done(installed, total - installed, total, profile_dir.name)
