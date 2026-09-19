"""Fallout 4 Downgrade wizard — Anniversary Edition (1.11.240) → Old-Gen (1.10.163).

Old-Gen mod collections (F4SE 0.6.23, "OG" F4SE plugins, Previsibines 1.10.163)
don't load on the Anniversary Edition Steam serves today. This applies the
community xdelta patch natively — no Wine — to the three root files that differ;
the transactional core (backup, verify, rollback, revert) lives in
``Utils.modding_tools.fo4_downgrade``.

Pages: Check → Download → Locate → Apply. The Check page also offers Restore
(the patch needs Steam's real launcher, which deploy swaps out) and Revert.
"""

from __future__ import annotations

import html
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from gui_qt.safe_emit import safe_emit
from gui_qt.theme.theme_qt import err_text, ok_text
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_NEXUS_URL = "https://www.nexusmods.com/fallout4/mods/98059?tab=files"
_NEXUS_MOD_ID = 98059
_NEXUS_FILE_ID = 407852         # the patch's main file (zip: Fallout4exe/Fallout4Launcherexe/SteamAPI64 .xdelta)
# The mod id is in every Nexus-named download of this mod, whatever the file is called.
_ARCHIVE_KEYWORDS = ["98059"]

_PG_CHECK, _PG_DOWNLOAD, _PG_LOCATE, _PG_APPLY = range(4)


class Fallout4DowngradeView(WizardViewBase):
    """Downgrade Fallout 4 so Old-Gen collections and F4SE 0.6.23 work."""

    _done_enable_sig = Signal()

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Downgrade Fallout 4 — {0}").format(game.name))
        from Utils.config_paths import get_game_config_dir
        self._game_root = game.get_game_path()
        self._state_dir = get_game_config_dir(game.name)
        self._assessment = None
        # An nxm download is fetched by Mosaic into its cache, not ~/Downloads.
        from Utils.config_paths import list_all_cache_dirs
        self._locate_extra_dirs = list_all_cache_dirs(game.name)

        self._done_enable_sig.connect(self._guard(
            lambda: self._done_btn.setEnabled(True)))

        self._stack.addWidget(self._build_check_page())
        self._stack.addWidget(self._build_manual_download_page(
            self.tr("Step 2: Download the patch"),
            self.tr("Premium accounts download the patch automatically — just wait.\n\n"
                    "Otherwise open the Nexus page and use MANUAL download on the main\n"
                    "file of “AnniversaryEdition(1.11.240) to LastGen(1.10.163)\n"
                    "downgrade patch”, then press Next. Don't use “Mod Manager\n"
                    "Download” — Mosaic would install the patch as a mod."),
            _NEXUS_URL,
            lambda: self._goto(_PG_LOCATE),
            button_text=self.tr("Open Nexus Mods Page")))
        self._stack.addWidget(self._build_locate_page(
            self.tr("Step 3: Locate the archive"), with_next=True))
        self._stack.addWidget(self._build_run_page(self.tr("Step 4: Apply")))
        self._goto(_PG_CHECK)

    # ---- page 0: check ------------------------------------------------------------
    def _build_check_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Check"))
        self._make_note(lay, self.tr(
            "Old-Gen collections need Fallout 4 1.10.163, but Steam serves the\n"
            "Anniversary Edition. This applies the community patch to Fallout4.exe,\n"
            "Fallout4Launcher.exe and steam_api64.dll. Your originals are backed up\n"
            "first and can be restored from here."))
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
        self._recheck_btn = QPushButton(self.tr("Re-check"))
        self._recheck_btn.setCursor(Qt.PointingHandCursor)
        self._recheck_btn.clicked.connect(self._refresh_check)
        rh.addWidget(self._recheck_btn)
        self._restore_btn = self._orange_btn(self.tr("Restore game files"))
        self._restore_btn.clicked.connect(self._on_restore_clicked)
        rh.addWidget(self._restore_btn)
        self._revert_btn = self._orange_btn(self.tr("Revert downgrade"))
        self._revert_btn.clicked.connect(self._on_revert_clicked)
        rh.addWidget(self._revert_btn)
        self._next_btn = self._accent_btn(self.tr("Next →"))
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

    def _refresh_check(self):
        from Utils.exe_launch.exe_launch import _wine_process_alive
        from Utils.modding_tools import fo4_downgrade as fo4
        from Utils.wizard_support.pe_version import format_version

        if self._game_root is None:
            self._check_label.setText(self._row(False, self.tr("Game path is not configured.")))
            for b in (self._restore_btn, self._revert_btn, self._next_btn):
                b.setVisible(False)
            return

        a = fo4.assess(self._game_root, self._state_dir,
                       xdelta3=fo4.find_xdelta3(),
                       game_running=_wine_process_alive("Fallout4.exe"))
        self._assessment = a
        ver = format_version(a.version) if a.version else self.tr("unreadable")
        if a.build == "anniversary":
            rows = [self._row(True, self.tr(
                "Fallout 4 {0} (Anniversary Edition) — can be downgraded to {1}.")
                .format(ver, format_version(fo4.TO_VERSION)))]
        elif a.downgraded:
            rows = [self._row(True, self.tr(
                "Fallout 4 {0} — downgraded by Mosaic. The originals are backed up "
                "and can be restored.").format(ver))]
        elif a.build == "oldgen":
            rows = [self._row(True, self.tr(
                "Fallout 4 is already {0} — nothing to do.").format(ver))]
        else:
            rows = [self._row(False, self.tr(
                "Fallout4.exe is {0}; this wizard only handles {1} → {2}.").format(
                    ver, format_version(fo4.FROM_VERSION), format_version(fo4.TO_VERSION)))]
        blockers = a.blockers if a.build == "anniversary" else a.revert_blockers
        rows += [self._row(False, b) for b in blockers]
        self._check_label.setText("<br>".join(rows))
        self._set_status(self._check_status, "")

        deployed = fo4.launcher_swapped(self._game_root)
        self._restore_btn.setVisible(deployed)
        self._revert_btn.setVisible(a.downgraded)
        self._revert_btn.setEnabled(a.can_revert)
        self._next_btn.setVisible(a.build == "anniversary")
        self._next_btn.setEnabled(a.can_downgrade)

    def _on_restore_clicked(self):
        run_restore = getattr(self._ctx, "run_restore", None)
        if run_restore is None:
            self._set_status(self._check_status, self.tr(
                "Restore is unavailable here — use Restore in the main window, "
                "then press Re-check."), RED)
            return
        self._restore_btn.setEnabled(False)
        self._set_status(self._check_status, self.tr("Restoring…"))

        def _done(ok: bool):
            if self._closing:
                return
            self._restore_btn.setEnabled(True)
            self._refresh_check()
            if not ok:
                self._set_status(self._check_status,
                                 self.tr("Restore failed — see the log."), RED)

        if not run_restore(_done):
            self._restore_btn.setEnabled(True)
            self._set_status(self._check_status, self.tr(
                "Couldn't start Restore right now (a deploy or restore may be running). "
                "Try again in a moment."), RED)

    # ---- navigation ---------------------------------------------------------------
    def _goto(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_CHECK:
            self._refresh_check()
        elif idx == _PG_DOWNLOAD:
            cached = self._find_cached_patch()
            if cached is not None:
                # Already on disk (e.g. Mosaic's nxm handler fetched it) — no
                # second download, no waiting.
                self._log(f"Downgrade Wizard: using the patch already downloaded: {cached.name}")
                self._archive_path = cached
                self._goto(_PG_APPLY)
                return
            # Premium: API download, no browser and no mod install.
            # Everyone else: the download folders are watched for the archive.
            self._nexus_auto_fetch(
                url=_NEXUS_URL, file_id=_NEXUS_FILE_ID,
                keywords=_ARCHIVE_KEYWORDS, label=self.tr("the downgrade patch"),
                pages=(_PG_DOWNLOAD, _PG_LOCATE),
                on_archive=lambda _p: self._goto(_PG_APPLY))
        elif idx == _PG_LOCATE:
            self._enter_locate(
                _ARCHIVE_KEYWORDS,
                self.tr("Select the Fallout 4 downgrade patch archive"),
                self.tr("Archive not found in Downloads.\n"
                        "Make sure you downloaded the patch, then press Try Again,\n"
                        "or use Browse to select it manually."),
                lambda _p: self._goto(_PG_APPLY))
        elif idx == _PG_APPLY:
            self._start_worker(self._apply_worker, self.tr("Extracting the patch…"))

    def _find_cached_patch(self) -> Path | None:
        """The patch archive if it's already on disk, matched exactly by its
        ``.fileid`` sidecar (not by name) in Mosaic's download cache — where an
        nxm download lands, and which the premium fetch doesn't look in — or in
        Downloads. Returns None if there's no complete copy."""
        from Nexus.nexus_download import _find_cached_archive
        from Utils.wizard_support.wizard_archives import get_downloads_dir
        for directory in (*self._locate_extra_dirs, get_downloads_dir()):
            found, complete = _find_cached_archive(
                directory, "", 0, _NEXUS_MOD_ID, _NEXUS_FILE_ID)
            if found is not None and complete:
                return found
        return None

    def _on_revert_clicked(self):
        self._stack.setCurrentIndex(_PG_APPLY)
        self._start_worker(self._revert_worker, self.tr("Restoring the original files…"))

    def _start_worker(self, target, status: str):
        self._done_btn.setEnabled(False)
        self._set_status(self._run_status, status)
        threading.Thread(target=target, daemon=True, name="fo4-downgrade").start()

    # ---- workers -------------------------------------------------------------------
    def _apply_worker(self):
        from Utils.modding_tools import fo4_downgrade as fo4
        from Utils.wizard_support.wizard_archives import extract_to_dir
        try:
            archive = self._archive_path
            if archive is None or not archive.is_file():
                raise fo4.DowngradeError(self.tr("Archive not found."))
            with tempfile.TemporaryDirectory(prefix="mosaic-fo4-downgrade-") as tmp:
                self._log(f"Downgrade Wizard: extracting {archive.name}")
                extract_to_dir(archive, Path(tmp))
                patches = fo4.find_patches(tmp)
                safe_emit(self._run_status_sig,
                          self.tr("Applying the patch — this takes a few seconds…"), "")
                fo4.apply_downgrade(
                    self._game_root, patches, self._state_dir,
                    xdelta3=fo4.find_xdelta3(),
                    log_fn=lambda m: self._log(f"Downgrade Wizard: {m}"))
            newer = fo4.count_newer_format_archives(self._game_root)
            text = self.tr(
                "Fallout 4 is now 1.10.163.\n\n"
                "Next: install the Old-Gen script extender (F4SE 0.6.23 — the "
                "collection's own file), then Deploy.\n")
            if newer:
                text += self.tr(
                    "\nImportant: {0} of this install's game archives are in the newer "
                    "Anniversary/Next-Gen format, which Old-Gen Fallout 4 cannot read "
                    "by itself. Also install \u201c{1}\u201d (Nexus mod {2}) and "
                    "\u201c{3}\u201d (Nexus mod {4} — the 1.10.163 file) next to "
                    "F4SE 0.6.23. Without them the game shows a black screen and exits "
                    "with no error.\n").format(
                        newer, fo4.BACKPORTED_BA2_MOD[0], fo4.BACKPORTED_BA2_MOD[1],
                        fo4.ADDRESS_LIBRARY_MOD[0], fo4.ADDRESS_LIBRARY_MOD[1])
            text += self.tr(
                "\nIf Steam updates Fallout 4 it will replace these files; run this "
                "wizard again afterwards.\n\nClick Done to close.")
            safe_emit(self._run_status_sig, text, GREEN)
        except fo4.DowngradeError as exc:
            self._fail(str(exc))
        except Exception as exc:                       # noqa: BLE001 — surface, don't crash the worker
            self._fail(self.tr("Unexpected error: {0}").format(exc))
        finally:
            safe_emit(self._done_enable_sig)

    def _revert_worker(self):
        from Utils.modding_tools import fo4_downgrade as fo4
        try:
            fo4.revert_downgrade(
                self._game_root, self._state_dir,
                log_fn=lambda m: self._log(f"Downgrade Wizard: {m}"))
            safe_emit(self._run_status_sig, self.tr(
                "Fallout 4 is back to the Anniversary Edition.\n\nClick Done to close."),
                GREEN)
        except fo4.DowngradeError as exc:
            self._fail(str(exc))
        except Exception as exc:                       # noqa: BLE001
            self._fail(self.tr("Unexpected error: {0}").format(exc))
        finally:
            safe_emit(self._done_enable_sig)

    def _fail(self, message: str):
        self._log(f"Downgrade Wizard: {message}")
        safe_emit(self._run_status_sig, message, RED)
