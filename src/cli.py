#!/usr/bin/env python3
"""
cli.py
Command-line interface for Mosaic Mod Manager.

Usage:
    python cli.py --deploy <game_id_or_name> <profile_name>
    python cli.py --restore <game_id_or_name>
    python cli.py --list-games
    python cli.py --list-profiles <game_id_or_name>
    python cli.py --clear-credentials
    python cli.py audit-collection-fomod <collection.json|profile_dir> [--cache DIR]

game_id_or_name can be either the game's game_id (e.g. 'skyrim_se') or its
full display name (e.g. 'Skyrim Special Edition').  Matching is case-insensitive.
"""

from __future__ import annotations

import argparse
import sys


def _setup_path():
    """Ensure src/ is on sys.path so Utils/Games/etc can be imported."""
    import app_bootstrap

    app_bootstrap.setup_environment()


def _find_game(games: dict, key: str):
    """Return a game instance matching key by name, game_id, or Steam app ID (case-insensitive)."""
    key_lower = key.lower()
    # Exact name match first
    for name, game in games.items():
        if name.lower() == key_lower:
            return game
    # game_id match
    for game in games.values():
        if getattr(game, "game_id", "").lower() == key_lower:
            return game
    # Steam ID match (primary + alts)
    for game in games.values():
        sid = getattr(game, "steam_id", "")
        alt_ids = getattr(game, "alt_steam_ids", [])
        if key_lower == str(sid).lower() or key_lower in [str(a).lower() for a in alt_ids]:
            return game
    return None


def _log(msg: str):
    print(msg, flush=True)


def cmd_list_games(games: dict):
    if not games:
        print("No games discovered.")
        return
    print(f"{'Game Name':<40} {'game_id':<30} {'Configured'}")
    print("-" * 80)
    for name, game in sorted(games.items()):
        configured = "yes" if game.is_configured() else "no"
        gid = getattr(game, "game_id", "")
        print(f"{name:<40} {gid:<30} {configured}")


def cmd_list_profiles(games: dict, key: str):
    game = _find_game(games, key)
    if game is None:
        print(f"Error: game '{key}' not found.", file=sys.stderr)
        sys.exit(1)
    profile_root = game.get_profile_root()
    profiles_dir = profile_root / "profiles"
    if not profiles_dir.is_dir():
        print(f"No profiles directory found at: {profiles_dir}")
        return
    profiles = sorted(p.name for p in profiles_dir.iterdir() if p.is_dir())
    if not profiles:
        print("No profiles found.")
    else:
        for p in profiles:
            print(p)


def cmd_deploy(games: dict, key: str, profile: str):
    game = _find_game(games, key)
    if game is None:
        print(f"Error: game '{key}' not found.", file=sys.stderr)
        sys.exit(1)
    if not game.is_configured():
        print(f"Error: game '{game.name}' is not configured (game path not set).", file=sys.stderr)
        sys.exit(1)

    from Utils.deploy.deploy_pipeline import run_deploy_pipeline

    profile_dir = game.get_profile_root() / "profiles" / profile
    if not profile_dir.is_dir():
        print(f"Error: profile '{profile}' does not exist at {profile_dir}", file=sys.stderr)
        sys.exit(1)

    success = run_deploy_pipeline(game, profile, log_fn=_log)
    if not success:
        sys.exit(1)

    _log(f"Deploy complete: {game.name} / {profile}")


def cmd_clear_credentials():
    from Nexus.nexus_api import clear_api_key
    from Nexus.nexus_oauth import clear_oauth_tokens
    clear_api_key()
    clear_oauth_tokens()
    print("Nexus credentials cleared.")


def cmd_restore(games: dict, key: str):
    game = _find_game(games, key)
    if game is None:
        print(f"Error: game '{key}' not found.", file=sys.stderr)
        sys.exit(1)
    if not game.is_configured():
        print(f"Error: game '{game.name}' is not configured (game path not set).", file=sys.stderr)
        sys.exit(1)

    from Utils.deploy.deploy import restore_root_folder

    game_root = game.get_game_path()
    profile_root = game.get_profile_root()

    last_deployed = game.get_last_deployed_profile()
    if last_deployed:
        game.set_active_profile_dir(profile_root / "profiles" / last_deployed)
        # Reload so the last-deployed profile's path overrides drive the restore.
        game.load_paths()
        game_root = game.get_game_path()

    if hasattr(game, "restore"):
        game.restore(log_fn=_log)
    else:
        print(f"Game '{game.name}' does not support restore.")

    root_folder_dir = game.get_effective_root_folder_path()
    if root_folder_dir.is_dir() and game_root:
        restore_root_folder(root_folder_dir, game_root, log_fn=_log)

    _log(f"Restore complete: {game.name}")


def _find_7z():
    import shutil
    return (shutil.which("7zzs") or shutil.which("7zz")
            or shutil.which("7z") or shutil.which("7za"))


def cmd_audit_collection_fomod(target: str, cache_dir: "str | None") -> int:
    """Check whether a collection's recorded FOMOD choices survive a replay.

    *target* is a ``collection.json`` or a collection profile directory (which
    also gives us the ``fomod/`` mirror of what the install actually applied).
    *cache_dir* is a download cache to read each mod's real ``ModuleConfig.xml``
    from; without it only the checks that need no archive run, which is still
    enough to compare a finished profile against its manifest.
    """
    import json
    import subprocess
    import tempfile
    from pathlib import Path

    from Utils.collections.collection_fomod_audit import (
        audit_collection_manifest, format_audit, convert_choices)
    from Utils.installers.fomod_parser import parse_module_config

    tpath = Path(target).expanduser()
    profile_dir = tpath if tpath.is_dir() else None
    manifest_path = (profile_dir / "collection.json") if profile_dir else tpath
    if not manifest_path.is_file():
        print(f"No collection.json at {manifest_path}")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # fileId -> archive, from the download cache's .fileid sidecars.
    fid_to_archive: dict = {}
    if cache_dir:
        cdir = Path(cache_dir).expanduser()
        for sidecar in cdir.glob("*.fileid"):
            try:
                fid = int(sidecar.read_text().strip())
            except (OSError, ValueError):
                continue
            archive = sidecar.with_suffix("")
            if archive.is_file():
                fid_to_archive.setdefault(fid, archive)
        print(f"Download cache: {len(fid_to_archive)} archive(s) in {cdir}")

    sevenzip = _find_7z()

    def parse_fomod(mod):
        fid = int((mod.get("source") or {}).get("fileId") or 0)
        archive = fid_to_archive.get(fid)
        if archive is None or not sevenzip:
            return None
        # Only ModuleConfig.xml comes out, so a 7 GB texture pack costs a seek
        # rather than a full extract.
        with tempfile.TemporaryDirectory() as td:
            subprocess.run([sevenzip, "e", "-y", f"-o{td}", str(archive),
                            "-r", "ModuleConfig.xml"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            xml = Path(td) / "ModuleConfig.xml"
            if not xml.is_file():
                return None
            try:
                return parse_module_config(str(xml))
            except Exception:
                return None

    def applied_selections(mod):
        if profile_dir is None:
            return None
        src = mod.get("source") or {}
        for key in filter(None, (src.get("logicalFilename"), mod.get("name"))):
            f = profile_dir / "fomod" / f"{key}.json"
            if f.is_file():
                try:
                    return json.loads(f.read_text(encoding="utf-8") or "{}")
                except (OSError, ValueError):
                    return None
        return None

    results = audit_collection_manifest(
        manifest,
        parse_fomod=parse_fomod if fid_to_archive else None,
        applied_selections=applied_selections if profile_dir else None)

    # Mods the install ran a FOMOD for that the manifest says nothing about —
    # only knowable from a finished profile's fomod/ mirror.
    no_choice: list = []
    if profile_dir is not None and (profile_dir / "fomod").is_dir():
        recorded = set()
        for mod in manifest.get("mods") or []:
            if (mod.get("choices") or {}).get("type") == "fomod":
                src = mod.get("source") or {}
                recorded.update(filter(None, (src.get("logicalFilename"),
                                              mod.get("name"))))
        no_choice = [f.stem for f in (profile_dir / "fomod").glob("*.json")
                     if f.stem not in recorded]

    info = manifest.get("info") or {}
    print(f"Collection: {info.get('name') or manifest_path.parent.name} "
          f"({len(manifest.get('mods') or [])} mods)")
    print(format_audit(results, no_choice_names=no_choice))
    return 1 if any(r.warnings or r.applied_matches is False for r in results) else 0


def main():
    _setup_path()

    parser = argparse.ArgumentParser(
        prog="mosaic",
        description="Mosaic Mod Manager — CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-games", help="List all discovered games and whether they are configured")

    lp = subparsers.add_parser("list-profiles", help="List profiles for a game")
    lp.add_argument("game", help="game_id or display name (case-insensitive)")

    dp = subparsers.add_parser("deploy", help="Build filemap and deploy mods for a profile")
    dp.add_argument("game", help="game_id or display name (case-insensitive)")
    dp.add_argument("profile", help="Profile name")

    rp = subparsers.add_parser("restore", help="Restore the game directory (undo last deploy)")
    rp.add_argument("game", help="game_id or display name (case-insensitive)")

    subparsers.add_parser("clear-credentials", help="Remove stored Nexus Mods API key and OAuth tokens")

    ap = subparsers.add_parser(
        "audit-collection-fomod",
        help="Check that a collection's recorded FOMOD choices still resolve")
    ap.add_argument("target", help="a collection.json, or a collection profile directory")
    ap.add_argument("--cache", default=None,
                    help="download cache dir holding the mods' archives; without "
                         "it only the checks that need no archive run")

    args = parser.parse_args()

    if args.command == "clear-credentials":
        cmd_clear_credentials()
        return

    # Reads files it is pointed at; no game discovery needed.
    if args.command == "audit-collection-fomod":
        sys.exit(cmd_audit_collection_fomod(args.target, args.cache))

    from Utils.exe_launch.game_loader import discover_games
    games = discover_games()

    if args.command == "list-games":
        cmd_list_games(games)
    elif args.command == "list-profiles":
        cmd_list_profiles(games, args.game)
    elif args.command == "deploy":
        cmd_deploy(games, args.game, args.profile)
    elif args.command == "restore":
        cmd_restore(games, args.game)


if __name__ == "__main__":
    main()
