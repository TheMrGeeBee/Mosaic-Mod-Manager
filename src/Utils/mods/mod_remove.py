"""Toolkit-neutral full mod removal — delete a mod's deployed files + staging
folder + index/BSA entries + its plugins from plugins.txt/loadorder.txt.

Ported from the Tk ModListPanel._remove_mod / _remove_plugins_for_mods so the Qt
remove does the SAME complete cleanup (not just dropping the modlist line, which
leaves the files on disk → the mod still reads as installed in the Downloads tab,
and its files stay deployed). Pure stdlib + Utils.* — no GUI toolkit.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def remove_mods(game, profile_dir: Path, mod_names: list[str], log_fn=None) -> None:
    """Fully remove *mod_names* for *game* / *profile_dir*:

      1. undeploy their files from the game dir (before deleting staging, so
         leftover hardlinks/copies aren't misclassified as runtime files) and
         re-link any of those files that another enabled mod also provides,
      2. drop their plugins from plugins.txt + loadorder.txt,
      3. delete the staging folders,
      4. drop them from modindex.bin + bsa_index.bin.

    Does NOT touch modlist.txt — the caller removes the rows from the model
    (which saves the modlist). Mirrors Tk _remove_mod.
    """
    log = log_fn or (lambda _m: None)
    if game is None or not mod_names:
        return
    try:
        staging_root = game.get_effective_mod_staging_path()
    except Exception:
        return
    index_path = staging_root.parent / "modindex.bin"

    # 1. Undeploy deployed files first — but only when a deployment is
    #    actually active: after a restore the game folder holds the REAL
    #    game files, and a mod that shadows vanilla names (e.g. a patched
    #    FalloutNV.esm) would otherwise delete them.  undeploy_mod_files
    #    additionally verifies per file (via staging_root) that the deployed
    #    copy belongs to the mod before unlinking it.
    try:
        deploy_active = bool(game.get_deploy_active())
    except Exception:
        deploy_active = True
    if deploy_active:
        removed: list = []
        try:
            from Utils.deploy.deploy import undeploy_mod_files
            undeploy_mod_files(
                mod_names,
                game.get_mod_data_path(),
                game.get_game_path(),
                index_path,
                log_fn=log,
                staging_root=staging_root,
                removed_out=removed,
            )
        except Exception as exc:
            log(f"undeploy during remove failed: {exc}")
        if removed:
            try:
                relink_from_other_mods(profile_dir, staging_root, index_path,
                                       removed, mod_names, log)
            except Exception as exc:
                log(f"re-linking replacement files failed: {exc}")
    else:
        log("no deployment is active — skipping undeploy of removed mod(s).")

    # 2. Remove the mods' plugins from plugins.txt / loadorder.txt.
    try:
        _remove_plugins_for_mods(game, profile_dir, staging_root, mod_names, log)
    except Exception as exc:
        log(f"plugin cleanup during remove failed: {exc}")

    # 3. Delete staging folders.
    for name in mod_names:
        folder = staging_root / name
        if folder.is_dir():
            try:
                shutil.rmtree(folder)
            except OSError as exc:
                log(f"could not delete staging folder for '{name}': {exc}")

    # 4. Drop from the mod index + BSA index.
    try:
        from Utils.filemap import remove_from_mod_index
        remove_from_mod_index(index_path, mod_names)
    except Exception as exc:
        log(f"index cleanup during remove failed: {exc}")
    try:
        from Utils.archives.bsa_filemap import remove_from_bsa_index
        remove_from_bsa_index(index_path.parent / "bsa_index.bin", mod_names)
    except Exception:
        pass


def relink_from_other_mods(profile_dir: Path, staging_root: Path,
                           index_path: Path, removed: list,
                           removed_mods: list[str], log) -> int:
    """Fill the gaps a removal left in the game folder.

    *removed* holds ``(path, rel_key, area, link_kind)`` for every file just
    undeployed.  When another enabled mod ships the same file (same path
    inside the mod), its copy is linked at the same spot, the same way —
    so e.g. removing an old copy of a reinstalled mod doesn't leave the game
    without the file until the next Deploy.  The highest-priority enabled
    mod wins, as it would at deploy.  Returns how many files were linked.
    """
    from Utils.filemap import read_mod_index
    from Utils.mods.modlist import read_modlist

    index = read_mod_index(index_path) or {}
    gone = set(removed_mods)
    candidates = [e.name for e in read_modlist(profile_dir / "modlist.txt")
                  if e.enabled and not e.is_separator and e.name not in gone]
    linked = 0
    for path, rel_key, area, kind in removed:
        path = Path(path)
        if os.path.lexists(path):
            continue
        for mod in candidates:
            entry = index.get(mod)
            if entry is None:
                continue
            files = entry[1] if area == "r" else entry[0]
            rel_str = files.get(rel_key)
            if rel_str is None:
                continue
            src = staging_root / mod / rel_str
            if not src.is_file():
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if kind == "symlink":
                    os.symlink(src, path)
                elif kind == "hardlink":
                    try:
                        os.link(src, path)
                    except OSError:
                        shutil.copy2(src, path)
                else:
                    shutil.copy2(src, path)
                linked += 1
            except OSError as exc:
                log(f"  could not link {mod}'s copy of {path.name}: {exc}")
            break
    if linked:
        log(f"  Re-linked {linked} file(s) from other enabled mod(s) that "
            "provide them, so the game keeps them until the next Deploy.")
    return linked


def _remove_plugins_for_mods(game, profile_dir: Path, staging_root: Path,
                             mod_names: list[str], log) -> None:
    """Drop the mods' plugin files from plugins.txt + loadorder.txt."""
    plugin_exts = {e.lower() for e in (getattr(game, "plugin_extensions", []) or [])}
    if not plugin_exts:
        return
    plugins_path = profile_dir / "plugins.txt"
    if not plugins_path.is_file():
        return
    to_remove: set[str] = set()
    for name in mod_names:
        folder = staging_root / name
        if folder.is_dir():
            for f in folder.iterdir():
                if f.is_file() and f.suffix.lower() in plugin_exts:
                    to_remove.add(f.name.lower())
    if not to_remove:
        return
    from Utils.plugins.plugins import (
        read_plugins, write_plugins, read_loadorder, write_loadorder, PluginEntry,
    )
    star = bool(getattr(game, "plugins_use_star_prefix", True))
    existing = read_plugins(plugins_path, star_prefix=star)
    new_entries = [e for e in existing if e.name.lower() not in to_remove]
    if len(new_entries) < len(existing):
        write_plugins(plugins_path, new_entries, star_prefix=star)
    loadorder_path = profile_dir / "loadorder.txt"
    loadorder = read_loadorder(loadorder_path)
    new_lo = [n for n in loadorder if n.lower() not in to_remove]
    if len(new_lo) < len(loadorder):
        write_loadorder(loadorder_path,
                        [PluginEntry(name=n, enabled=True) for n in new_lo])
