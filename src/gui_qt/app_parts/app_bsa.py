"""BSA/BA2 pack, unpack and preview, and BSA conflict scanning.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

import threading
from pathlib import Path
from pathlib import Path as _P


class MainWindow:

    def _open_bsa_preview_tab(self, path, rel_str):
        """Open a BSA/BA2 archive's contents as a MODLIST-PANEL-SCOPED tab: it
        shows in the modlist region (in the shared top tab bar) while the Mod
        Files tree in the plugins panel stays live. Reuses one preview tab —
        clicking another archive swaps it in place. Replaces the old Tk Archive
        tab; the TOC is read without decompressing any file data."""
        from gui_qt.bsa.bsa_preview import BsaPreview
        name = rel_str.replace("\\", "/").rsplit("/", 1)[-1]
        existing = getattr(self, "_bsa_preview_widget", None)
        if existing is not None and self._tabs.has_key("mf_bsa_preview"):
            existing.set_archive(_P(path), name)
            self._tabs.focus_key("mf_bsa_preview")
            self._tabs.set_tab_title("mf_bsa_preview", name)
            return
        widget = BsaPreview(_P(path), name,
                            conflict_fn=self._bsa_preview_conflicts)
        widget.close_requested.connect(
            lambda: self._tabs.close_tab("mf_bsa_preview"))
        self._bsa_preview_widget = widget
        self._tabs.open_scoped_tab(
            widget, name, self._modlist_panel_stack, key="mf_bsa_preview")

    def _bsa_preview_conflicts(self, archive_path):
        """Per-file conflict codes for the archive preview: {contained_path:
        +1 (this mod's copy wins) / -1 (loses)} judged for the archive's
        owning mod against every enabled mod's archives — the same winner map
        the modlist icons and Show Conflicts tab use. Runs on the preview's
        daemon thread. Returns {} for unowned paths (overwrite/), disabled
        mods, or any failure — the preview just shows no tints then."""
        try:
            from pathlib import Path as _P
            g = self._gs.game
            staging = self._gs.staging_dir()
            ml = self._gs.modlist_path()
            if g is None or staging is None or ml is None or not ml.is_file():
                return {}
            rel = _P(archive_path).resolve().relative_to(staging.resolve())
            mod_name = rel.parts[0]

            from Utils.archives.bsa_filemap import (
                compute_bsa_winner_map,
                read_bsa_index,
            )
            from Utils.archives.ue_pak_reader import UE_ARCHIVE_EXTENSIONS
            from Utils.mods.modlist import read_modlist
            index = read_bsa_index(staging.parent / "bsa_index.bin") or {}
            enabled = [e for e in read_modlist(ml)
                       if not e.is_separator and e.enabled]
            if mod_name not in {e.name for e in enabled}:
                return {}
            prio = [e.name for e in reversed(enabled)]
            exts = frozenset(
                getattr(g, "archive_extensions", frozenset()) or frozenset())
            if getattr(g, "archive_plugin_ordering", True):
                from Utils.plugins.plugins import read_loadorder
                pdir = self._gs.profile_dir()
                plugin_order = (read_loadorder(pdir / "loadorder.txt")
                                if pdir is not None else None)
                plugin_exts = frozenset(
                    getattr(g, "plugin_extensions", []) or [])
            else:
                plugin_order = None
                plugin_exts = None
            is_ue = bool(exts & UE_ARCHIVE_EXTENSIONS)
            winner, losers = compute_bsa_winner_map(
                index, prio, plugin_order or None, plugin_exts or None,
                staging.parent / "modindex.bin", is_ue)
            codes = {}
            for fp, lose_list in losers.items():
                if winner.get(fp) == mod_name:
                    codes[fp] = 1
                elif mod_name in lose_list:
                    codes[fp] = -1
            if not is_ue:
                my_bsa_paths = {
                    fp
                    for _bsa, _mt, paths in index.get(mod_name, [])
                    for fp in paths
                }
                if my_bsa_paths:
                    from Utils.filemap import read_mod_index
                    loose_index = read_mod_index(
                        staging.parent / "modindex.bin") or {}
                    for cand in prio:
                        if cand == mod_name:
                            continue
                        entry = loose_index.get(cand)
                        if not entry:
                            continue
                        normal, _root = entry
                        for rel_key in normal:
                            if rel_key in my_bsa_paths:
                                # A higher-priority loose file wins outright; a
                                # lower-priority one still beats the BSA (engine
                                # loads BSAs first, then loose on top).
                                codes[rel_key] = -1
            return codes
        except Exception:
            return {}

    # -- BSA / BA2 pack + unpack -------------------------------------------
    def _bsa_mod_dir(self):
        """Resolve the on-disk staging folder for the Mod Files tab's mod."""
        mv = self._mod_files_view
        game = getattr(mv, "game", None)
        mod_name = getattr(mv, "_mod_name", None)
        if game is None or not mod_name:
            return None
        try:
            base = Path(game.get_effective_mod_staging_path())
        except Exception:
            return None
        d = base / mod_name
        return d if d.is_dir() else None

    def _bsa_plugin_exts(self):
        game = getattr(self._mod_files_view, "game", None)
        return getattr(game, "plugin_extensions", None) or (".esp", ".esm", ".esl")

    def _on_pack_bsa(self):
        import Utils.archives.bsa_pack_ops as ops
        from gui_qt.bsa.bsa_pack_overlay import BsaPackOverlay

        if self._bsa_op_running:
            self._notify(self.tr("An archive operation is already running."), "warning")
            return
        mv = self._mod_files_view
        game = getattr(mv, "game", None)
        mod_name = getattr(mv, "_mod_name", None)
        profile_dir = getattr(mv, "profile_dir", None)
        kind = ops.archive_kind_for_game(game)
        if kind is None or not ops.is_packable_mod(mod_name):
            return
        mod_dir = self._bsa_mod_dir()
        if mod_dir is None:
            self._notify(self.tr("Mod folder not found."), "warning")
            return
        if ops.is_profile_deployed(game, profile_dir):
            self._notify(
                self.tr("Profile is deployed — run Restore first, then pack the "
                "{0}.").format(kind.upper()), "warning")
            return

        plan = ops.plan_pack(game, mod_dir, mod_name, kind, self._bsa_plugin_exts())

        def on_done(opts):
            if opts is None:
                return
            self._start_pack_bsa(plan, opts)

        BsaPackOverlay.show_over(
            self, archive_name=plan.archive_path.name,
            existing=plan.existing_any, kind=kind, on_done=on_done)

    def _start_pack_bsa(self, plan, opts):
        import Utils.archives.bsa_pack_ops as ops
        from gui_qt.safe_emit import safe_emit

        mv = self._mod_files_view
        profile_dir = getattr(mv, "profile_dir", None)
        index_path = getattr(mv, "index_path", None)
        mod_name = plan.mod_name
        delete_loose = bool(opts.get("delete_loose"))
        split_textures = bool(opts.get("split_textures"))
        skip_winners = bool(opts.get("skip_winners"))

        excluded = ops.read_excluded_for_mod(profile_dir, mod_name)
        if skip_winners:
            excluded |= ops.compute_skip_winners(index_path, profile_dir, mod_name)
        excluded_now = frozenset(excluded)

        self._bsa_op_running = True
        self._op_title = f"Pack {plan.kind.upper()}"
        self._ensure_feedback()
        self._notify(self.tr("Packing {0}…").format(mod_name), "info")

        def worker():
            def progress(done, total, current):
                safe_emit(self._op_progress, done, total,
                          f"{done} / {total}  —  {current[-50:]}")
            try:
                res = ops.run_pack(
                    plan, excluded_keys=excluded_now,
                    split_textures=split_textures,
                    progress=progress, cancel=None)
            except ops.PackCancelled:
                safe_emit(self._bsa_op_done, {"kind": "pack", "cancelled": True})
                return
            except Exception as exc:
                safe_emit(self._bsa_op_done,
                          {"kind": "pack", "error": str(exc)})
                return
            safe_emit(self._bsa_op_done, {
                "kind": "pack", "plan": plan, "result": res,
                "delete_loose": delete_loose, "mod_name": mod_name,
            })

        threading.Thread(target=worker, daemon=True).start()

    def _on_unpack_bsa(self):
        import Utils.archives.bsa_pack_ops as ops
        from gui_qt.bsa.bsa_unpack_overlay import BsaUnpackOverlay

        if self._bsa_op_running:
            self._notify(self.tr("An archive operation is already running."), "warning")
            return
        mv = self._mod_files_view
        game = getattr(mv, "game", None)
        mod_name = getattr(mv, "_mod_name", None)
        profile_dir = getattr(mv, "profile_dir", None)
        if ops.archive_kind_for_game(game) is None or not ops.is_packable_mod(mod_name):
            return
        mod_dir = self._bsa_mod_dir()
        if mod_dir is None:
            return
        if ops.is_profile_deployed(game, profile_dir):
            self._notify(
                self.tr("Profile is deployed — run Restore first, then unpack."),
                "warning")
            return

        def on_done(archives):
            if not archives:
                return
            self._start_unpack_bsa(mod_dir, archives)

        BsaUnpackOverlay.show_over(
            self, mod_name=mod_name, mod_dir=mod_dir,
            plugin_exts=self._bsa_plugin_exts(), on_done=on_done)

    def _start_unpack_bsa(self, mod_dir, archive_paths):
        import Utils.archives.bsa_pack_ops as ops
        from gui_qt.safe_emit import safe_emit

        mod_name = getattr(self._mod_files_view, "_mod_name", None)
        kind_upper = ops.unpack_kind_label(archive_paths)

        self._bsa_op_running = True
        self._op_title = f"Unpack {kind_upper}"
        self._ensure_feedback()
        self._notify(self.tr("Unpacking {0} archive(s)…").format(len(archive_paths)), "info")

        def worker():
            def progress(done, total, current):
                safe_emit(self._op_progress, done, total,
                          f"{done} / {total}  —  {current[-50:]}")
            try:
                count, written = ops.run_unpack(
                    archive_paths, mod_dir, progress=progress, cancel=None)
            except ops.UnpackCancelled:
                safe_emit(self._bsa_op_done, {"kind": "unpack", "cancelled": True})
                return
            except Exception as exc:
                safe_emit(self._bsa_op_done,
                          {"kind": "unpack", "error": str(exc)})
                return
            safe_emit(self._bsa_op_done, {
                "kind": "unpack", "mod_dir": mod_dir, "mod_name": mod_name,
                "archives": archive_paths, "count": count, "written": written,
            })

        threading.Thread(target=worker, daemon=True).start()

    def _on_bsa_op_done(self, info: dict):
        """UI-thread completion for pack/unpack workers (see _bsa_op_done)."""
        import Utils.archives.bsa_pack_ops as ops

        self._bsa_op_running = False
        if self._progress_popup is not None:
            self._schedule_op_clear(800)

        if info.get("cancelled"):
            self._notify(self.tr("Cancelled."), "info")
            return
        err = info.get("error")
        if err:
            verb = "Pack" if info["kind"] == "pack" else "Unpack"
            self._notify(self.tr("{0} failed: {1}").format(verb, err), "error")
            return

        mv = self._mod_files_view
        profile_dir = getattr(mv, "profile_dir", None)
        mod_name = info.get("mod_name")

        if info["kind"] == "pack":
            plan = info["plan"]
            res = info["result"]
            if info["delete_loose"]:
                deleted = ops.delete_loose_files(plan.mod_dir, res.packed_keys)
                tail = f" ({deleted} loose file(s) deleted)" if deleted else ""
            else:
                ops.auto_disable_packed_files(profile_dir, mod_name, res.packed_keys)
                tail = " (packed files disabled)"
            parts = []
            if res.main_count:
                parts.append(f"{res.main_count} → {plan.archive_path.name}")
            if res.tex_count and plan.archive_textures_path is not None:
                parts.append(f"{res.tex_count} → {plan.archive_textures_path.name}")
            summary = "; ".join(parts) or "no files packed"
            stub = " + stub .esp" if plan.stub_plugin_path is not None else ""
            self._notify(self.tr("Packed {0}{1}{2}").format(summary, stub, tail), "success")
        else:  # unpack
            for ap in info["archives"]:
                try:
                    ap.unlink()
                except OSError:
                    pass
            stem = ops.shared_archive_stem(info["archives"])
            stub, is_ours = ops.stub_for_unpack(info["mod_dir"], stem)
            if is_ours:
                try:
                    stub.unlink()
                except OSError:
                    pass
            ops.clear_excluded_for_unpack(profile_dir, mod_name, info["written"])
            self._notify(
                self.tr("Unpacked {0} file(s) from "
                "{1} archive(s)").format(info['count'], len(info['archives'])), "success")

        # Rebuild conflicts/index (Qt equivalent of the Tk filemap rebuild) and
        # refresh the Mod Files tree so the new state shows.
        self._rebuild_conflicts_async(rescan_index=True)
        try:
            if mod_name is not None:
                mv.show_mod(mod_name)
        except Exception:
            pass
        self._update_mf_footer_buttons()

    def _bsa_backend_codes(self, cd) -> dict:
        """BSA codes from ConflictData are 1/-1/2 (win/lose/mixed). Map to
        backend codes for the engine (mixed→PARTIAL)."""
        from Utils.filemap import CONFLICT_LOSES, CONFLICT_PARTIAL, CONFLICT_WINS
        m = {1: CONFLICT_WINS, -1: CONFLICT_LOSES, 2: CONFLICT_PARTIAL}
        return {n: m[c] for n, c in (cd.bsa_codes or {}).items() if c in m}

    def _recompute_bsa_conflicts_async(self):
        """A plugin toggle/reorder changed the plugin load order. BSAs load at
        their plugin's position, so BSA conflict winners may have changed — but
        the deployed file set (filemap.txt / modindex.bin), loose conflicts,
        plugin ownership and framework states are all unaffected. So recompute
        ONLY the BSA conflicts off-thread (re-reads the freshly-written
        loadorder.txt) and repaint just the BSA icons on the modlist — instead
        of the full _rebuild_conflicts_async, which rebuilds the filemap and
        reloads both the modlist and the Plugins panel. Tk parity: plugin
        toggle → recompute_bsa_conflicts (BSA-only), NOT a filemap rebuild."""
        g = self._gs.game
        if g is None or not self._gs.profile:
            return
        gen = getattr(self, "_bsa_conflict_gen", 0) + 1
        self._bsa_conflict_gen = gen

        def worker():
            try:
                bsa_codes, bsa_over, bsa_overby = self._gs._build_bsa_conflicts(
                    g, lambda _m: None)
            except Exception as exc:
                print(f"[gui_qt] BSA recompute failed: {exc}", flush=True)
                return
            self._bsa_conflicts_ready.emit(gen, bsa_codes, bsa_over, bsa_overby)

        threading.Thread(target=worker, daemon=True).start()

    def _on_bsa_conflicts_ready(self, gen, bsa_codes, bsa_over, bsa_overby):
        if gen != getattr(self, "_bsa_conflict_gen", 0):
            return   # superseded by a newer plugin toggle
        # Keep the cached conflict data's BSA maps in sync so a later full
        # rebuild path / highlight lookup sees the current winners.
        if getattr(self, "_conflict_data", None) is not None:
            self._conflict_data.bsa_codes = bsa_codes
            self._conflict_data.bsa_overrides = bsa_over
            self._conflict_data.bsa_overridden_by = bsa_overby
        self._modlist_model.set_bsa_conflicts(bsa_codes)
        # Cross-panel highlights follow the BSA override maps (loose maps unchanged).
        loose_over = loose_overby = None
        if getattr(self, "_conflict_data", None) is not None:
            loose_over = self._conflict_data.overrides
            loose_overby = self._conflict_data.overridden_by
        self._modlist_view.set_conflict_maps(
            loose_over or {}, loose_overby or {}, bsa_over, bsa_overby)
