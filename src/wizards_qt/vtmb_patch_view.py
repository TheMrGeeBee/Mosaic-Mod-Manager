"""Bloodlines Unofficial Patch wizard — installs the Unofficial_Patch/ layer.

The patch (Nexus ``vampirebloodlines`` mod 80, an Inno Setup ``VTMBup*.exe``)
is the base layer Bloodlines mods are built against. The installer also carries
replacement files for the vanilla ``Vampire/`` folder and ``Bin/*.dll``, which
Mosaic never writes. It unpacks ``Unofficial_Patch/``, ``Patch_Extras/`` and the
Loader (``Loader.exe``, ``Bin/loader/``), and replaces Steam's ``vampire.exe``
with the 4 GB-patched one, as the official installer does under Wine
(see ``Utils.modding_tools.vtmb_patch``).

Pages: Check → Download → Apply.
"""

from __future__ import annotations

import html
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QPushButton, QWidget

from gui_qt.safe_emit import safe_emit
from gui_qt.theme.theme_qt import err_text, ok_text
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_CHECK, _PG_DOWNLOAD, _PG_APPLY = range(3)


class VtmbPatchView(WizardViewBase):
    """Download and unpack the Bloodlines Unofficial Patch."""

    _done_enable_sig = Signal()
    _file_id_sig = Signal(int)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Unofficial Patch — {0}").format(game.name))
        from Utils.config_paths import list_all_cache_dirs
        from Utils.modding_tools import vtmb_patch
        self._core = vtmb_patch
        self._game_root = game.get_game_path()
        self._search_dirs = list_all_cache_dirs(game.name)

        self._done_enable_sig.connect(self._guard(
            lambda: self._done_btn.setEnabled(True)))
        self._file_id_sig.connect(self._guard(self._on_file_id))

        self._stack.addWidget(self._build_check_page())
        self._stack.addWidget(self._build_manual_download_page(
            self.tr("Step 2: Download the patch"),
            self.tr("Premium accounts download the installer automatically — just wait.\n\n"
                    "Otherwise open the Nexus page and use MANUAL download on the main\n"
                    "file (VTMBup….exe, about 600 MB), then press Next. Don't use “Mod\n"
                    "Manager Download” — Mosaic would try to install it as a mod."),
            vtmb_patch.NEXUS_URL,
            self._scan_and_apply,
            button_text=self.tr("Open Nexus Mods Page")))
        self._stack.addWidget(self._build_run_page(self.tr("Step 3: Install")))
        self._goto(_PG_CHECK)

    # ---- page 0: check ------------------------------------------------------------
    def _build_check_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Check"))
        self._make_note(lay, self.tr(
            "The Unofficial Patch is the base layer almost every Bloodlines mod\n"
            "builds on. It is unpacked into an Unofficial_Patch/ folder next to\n"
            "Vampire/, together with the Loader. Like the official installer, it\n"
            "also replaces Steam's vampire.exe with the 4 GB-patched one. The\n"
            "vanilla Vampire/ folder and the Bin/ DLLs are never modified."))
        self._check_label = QLabel("")
        self._check_label.setTextFormat(Qt.RichText)
        self._check_label.setWordWrap(True)
        self._check_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        lay.addSpacing(8)
        lay.addWidget(self._check_label)
        self._check_status = self._make_status(lay)
        lay.addStretch(1)

        row = QWidget()
        rh = QHBoxLayout(row)
        rh.setContentsMargins(0, 8, 0, 0)
        rh.setSpacing(8)
        rh.addStretch(1)
        self._browse_btn = QPushButton(self.tr("Use an installer I have…"))
        self._browse_btn.setCursor(Qt.PointingHandCursor)
        self._browse_btn.clicked.connect(self._on_browse)
        rh.addWidget(self._browse_btn)
        self._next_btn = self._accent_btn(self.tr("Install / update →"))
        self._next_btn.clicked.connect(lambda: self._goto(_PG_DOWNLOAD))
        rh.addWidget(self._next_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    @staticmethod
    def _row(ok: bool, text: str) -> str:
        color = ok_text() if ok else err_text()
        return (f'<span style="color:{color}">{"✓" if ok else "✗"}</span> '
                f"{html.escape(text)}")

    def _compare_row(self, installed: str) -> str:
        """Installed vs. the newest installer already on disk (no download)."""
        found = self._core.find_installer(self._search_roots())
        latest = self._core.installer_version(found) if found else None
        if not latest:
            return self._row(True, self.tr(
                "No installer downloaded yet to compare against — Install / update "
                "fetches the latest."))
        cmp_installed = self._core.version_tuple(installed)
        cmp_latest = self._core.version_tuple(latest)
        if cmp_installed >= cmp_latest:
            return self._row(True, self.tr(
                "Up to date: the downloaded installer is {0}.").format(latest))
        return self._row(False, self.tr(
            "A newer patch is available: the downloaded installer is {0}.").format(latest))

    def _refresh_check(self):
        rows = []
        usable = self._game_root is not None
        if not usable:
            rows.append(self._row(False, self.tr("Game path is not configured.")))
        else:
            a = self._core.assess(self._game_root)
            if not a["game_found"]:
                usable = False
                rows.append(self._row(False, self.tr(
                    "No Vampire/ folder in the game path — check the game path.")))
            if a["patch_version"]:
                where = a["patch_version_in"]
                rows.append(self._row(True, self.tr(
                    "Unofficial Patch {0} is installed (in {1}/).").format(
                        a["patch_version"], where)))
                rows.append(self._compare_row(a["patch_version"]))
            else:
                rows.append(self._row(a["patch_installed"], self.tr(
                    "Unofficial_Patch/ is installed, but its version couldn't be read.")
                    if a["patch_installed"] else self.tr(
                    "The Unofficial Patch is not installed yet.")))
            rows.append(self._row(a["loader_present"], self.tr(
                "Loader (Loader.exe, Bin/loader/) is present.")
                if a["loader_present"] else self.tr(
                "Loader (Loader.exe, Bin/loader/) is not installed yet.")))
            if not a["exe_found"]:
                usable = False
                rows.append(self._row(False, self.tr(
                    "vampire.exe was not found in the game folder.")))
            else:
                rows.append(self._row(a["exe_patched"], self.tr(
                    "vampire.exe is the 4 GB-patched exe (supports -game).")
                    if a["exe_patched"] else self.tr(
                    "vampire.exe is Steam's stock exe (no 4 GB patch) — it crashes on "
                    "large mod sets. The wizard replaces it with the patched one.")))
        if self._core.find_innoextract() is None:
            usable = False
            rows.append(self._row(False, self.tr(
                "innoextract is not installed (needed to unpack the installer). "
                "Install it with your package manager, e.g. `sudo pacman -S "
                "innoextract` or `sudo apt install innoextract`.")))
        self._check_label.setText("<br>".join(rows))
        self._set_status(self._check_status, "")
        self._next_btn.setEnabled(usable)
        self._browse_btn.setEnabled(usable)

    def _on_browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Select the Unofficial Patch installer"),
            str(Path.home() / "Downloads"), self.tr("Installers (*.exe)"))
        if path:
            self._archive_path = Path(path)
            self._goto(_PG_APPLY)

    # ---- navigation ---------------------------------------------------------------
    def _search_roots(self) -> list[Path]:
        from Utils.wizard_support.wizard_archives import get_downloads_dir
        return [*self._search_dirs, get_downloads_dir()]

    def _goto(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_CHECK:
            self._refresh_check()
        elif idx == _PG_DOWNLOAD:
            cached = self._core.find_installer(self._search_roots())
            if cached is not None:
                # Already on disk — an old copy still installs the older patch,
                # so the user can re-download from the Nexus page if it's stale.
                self._log(f"Unofficial Patch Wizard: using the installer already "
                          f"downloaded: {cached.name}")
                self._archive_path = cached
                self._goto(_PG_APPLY)
                return
            self._resolve_latest_file_id()
        elif idx == _PG_APPLY:
            self._done_btn.setEnabled(False)
            self._set_status(self._run_status, self.tr("Unpacking the patch…"))
            threading.Thread(target=self._apply_worker, daemon=True,
                             name="vtmb-patch").start()

    def _scan_and_apply(self):
        found = self._core.find_installer(self._search_roots())
        if found is None:
            self._set_status(self._dl_status, self.tr(
                "No VTMBup….exe found in Downloads or Mosaic's download cache yet."),
                RED)
            return
        self._archive_path = found
        self._goto(_PG_APPLY)

    # ---- download: pick the newest MAIN file, then premium fetch / watch ---------
    def _resolve_latest_file_id(self):
        api_fn = getattr(self._ctx, "nexus_api", None)
        api = None
        if api_fn is not None:
            try:
                api = api_fn()
            except Exception:
                api = None
        if api is None:
            return      # manual page stays; Next scans the download folders

        def worker():
            fid = 0
            try:
                files = api.get_mod_files(self._core.NEXUS_DOMAIN,
                                          self._core.NEXUS_MOD_ID).files
                main = [f for f in files if f.is_primary] or list(files)
                if main:
                    fid = max(main, key=lambda f: f.uploaded_timestamp).file_id
            except Exception as exc:
                self._log(f"Unofficial Patch Wizard: could not list Nexus files: {exc}")
            safe_emit(self._file_id_sig, int(fid))

        threading.Thread(target=worker, daemon=True, name="vtmb-file-id").start()

    def _on_file_id(self, file_id: int):
        if not file_id or self._stack.currentIndex() != _PG_DOWNLOAD:
            return
        roots = self._search_roots
        self._nexus_auto_fetch(
            url=self._core.NEXUS_URL, file_id=file_id,
            keywords=[self._core.INSTALLER_PREFIX],
            label=self.tr("the Unofficial Patch"),
            pages=(_PG_DOWNLOAD,),
            on_archive=lambda _p: self._goto(_PG_APPLY),
            find_fn=lambda: self._core.find_installer([roots()[-1]]))

    # ---- worker -------------------------------------------------------------------
    def _apply_worker(self):
        core = self._core
        try:
            installer = self._archive_path
            if installer is None or not installer.is_file():
                raise core.PatchError(self.tr("Installer not found."))
            core.install_patch(
                installer, self._game_root,
                log_fn=lambda m: self._log(f"Unofficial Patch Wizard: {m}"),
                progress_fn=lambda d, t: safe_emit(
                    self._run_status_sig,
                    self.tr("Unpacking the patch… {0}%").format(int(d * 100 / max(t, 1))),
                    ""))
            self._ran = True
            safe_emit(self._run_status_sig, self.tr(
                "The Unofficial Patch is installed in Unofficial_Patch/.\n\n"
                "Mods now deploy into a Mosaic_<profile>/ folder layered on top of it, "
                "and Play launches with -game for that folder. vampire.exe is now the "
                "4 GB-patched exe; the vanilla Vampire/ folder was not modified.\n\nClick Done to close."), GREEN)
        except core.PatchError as exc:
            self._fail(str(exc))
        except Exception as exc:                       # noqa: BLE001 — surface, don't crash the worker
            self._fail(self.tr("Unexpected error: {0}").format(exc))
        finally:
            safe_emit(self._done_enable_sig)

    def _fail(self, message: str):
        self._log(f"Unofficial Patch Wizard: {message}")
        safe_emit(self._run_status_sig, message, RED)
