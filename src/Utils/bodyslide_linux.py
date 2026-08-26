"""
GUI-neutral logic for the native-Linux BodySlide / Outfit Studio build.

Uses TheMrGeeBee/BodySlide-and-Outfit-Studio-Appimage — Mosaic's own fork of
ChrisDKN/BodySlide-and-Outfit-Studio-Appimage (itself a fork of
ousnius/BodySlide-and-Outfit-Studio), forked so Mosaic doesn't depend on a
third-party account's repo staying available. It carries the same
BSOS_TARGET_GAME / BSOS_GAME_DATA_PATH / BSOS_OUTPUT_DATA_PATH environment
variables, read on every launch and winning over the stored Config.xml every
time. That makes this integration a plain env-var launch — no Config.xml
patching needed, unlike the old Proton-based wizard
(Utils/modding_tools/bodyslide_tools.py).

One shared install (not per-game/per-profile): the env vars above are what
make the tool point at the right game each launch, so there's nothing
game-specific to keep separate on disk.

Flow: install_or_update() (idempotent, skips the download when already
current) -> the caller deploys the modlist -> launch_env() resolves the
launcher script + env for the Run step.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from Games.base_game import BaseGame

REPO_API_URL = (
    "https://api.github.com/repos/TheMrGeeBee/BodySlide-and-Outfit-Studio-Appimage"
    "/releases/latest"
)

# Every game the fork's BSOS_TARGET_GAME accepts, keyed by the real
# synthesis_registry_name each Games/Bethesda/*.py class sets (confirmed by
# reading the source directly — Utils.modding_tools.bodyslide_tools's own
# BODYSLIDE_GAMES has a latent bug: its "FalloutNewVegas" key never matches
# fallout_nv.py's actual "FalloutNV").
BSOS_TARGET_GAME = {
    "Fallout3": 0,
    "FalloutNV": 1,
    "Skyrim": 2,
    "Fallout4": 3,
    "Skyrim Special Edition": 4,
    "Fallout 4 VR": 5,
    "Skyrim VR": 6,
    "Fallout76": 7,
    "Oblivion": 8,
    "Starfield": 9,
}

_VERSION_FILE = "mosaic_bodyslide_meta.json"


def _noop(_msg: str) -> None:
    pass


def bsos_target_game(game: "BaseGame") -> int | None:
    return BSOS_TARGET_GAME.get(getattr(game, "synthesis_registry_name", None))


def install_dir() -> Path:
    from Utils.config_paths import get_bodyslide_linux_dir
    return get_bodyslide_linux_dir()


def installed_tag() -> str | None:
    path = install_dir() / _VERSION_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tag = data.get("tag")
        return tag if isinstance(tag, str) else None
    except (OSError, json.JSONDecodeError):
        return None


def save_installed_tag(tag: str) -> None:
    path = install_dir() / _VERSION_FILE
    try:
        path.write_text(json.dumps({"tag": tag}), encoding="utf-8")
    except OSError:
        pass


def find_launcher(exe_name: str) -> Path | None:
    """*exe_name* is "BodySlide" or "OutfitStudio" — the launcher scripts at
    the extracted root. Run these, not the real binaries under bin/: the
    launchers are what set up BSOS_APPDIR/BSOS_BINDIR/PATH for the bundle."""
    p = install_dir() / exe_name
    return p if p.is_file() else None


def install_or_update(log_fn: Callable[[str], None] = _noop) -> bool:
    """Fetch the fork's latest release tarball if not already installed.
    Call from a worker thread. Returns True on success (including "already
    up to date"), False on failure."""
    from Utils.ca_bundle import download_file
    from Utils.wizard_support.wizard_archives import (
        extract_archive, fetch_latest_github_asset,
    )

    try:
        tag, dl_url = fetch_latest_github_asset(REPO_API_URL, ["x86_64", "tar"])
    except Exception as exc:
        log_fn(f"BodySlide (Native Linux): could not check latest release — {exc}")
        return False

    if tag == installed_tag() and find_launcher("BodySlide") is not None:
        log_fn(f"BodySlide (Native Linux): already up to date ({tag}).")
        return True

    log_fn(f"BodySlide (Native Linux): downloading {tag}…")
    import shutil
    import tempfile
    dest = install_dir()
    suffix = "".join(Path(dl_url).suffixes[-2:]) or ".tar.zst"
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        download_file(dl_url, tmp_path)
        log_fn(f"BodySlide (Native Linux): extracting {tag}…")
        # Wipe any previous install first — re-extracting an update over a
        # prior one can collide on symlinks (bundled .so versioning symlinks
        # in particular): os.symlink/os.rename refuse to replace an existing
        # path the way a plain file overwrite would.
        if dest.is_dir():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        extract_archive(tmp_path, dest)
        tmp_path.unlink(missing_ok=True)
    except Exception as exc:
        log_fn(f"BodySlide (Native Linux): install failed — {exc}")
        return False

    if find_launcher("BodySlide") is None:
        log_fn("BodySlide (Native Linux): BodySlide launcher not found after extraction.")
        return False

    save_installed_tag(tag)
    log_fn(f"BodySlide (Native Linux): installed {tag}.")
    return True


def launch_env(exe_name: str, game: "BaseGame",
               output_mod_path: Path) -> tuple[Path, dict[str, str]] | None:
    """Resolve (launcher_path, env) for running *exe_name* against *game*.
    Returns None if the game isn't one BSOS_TARGET_GAME supports, or the
    launcher isn't installed. Caller owns the actual Popen/wait + UI
    signalling (mirrors BodySlideView._start_run's shape)."""
    import os

    launcher = find_launcher(exe_name)
    if launcher is None:
        return None
    target = bsos_target_game(game)
    if target is None:
        return None
    data_path = game.get_mod_data_path()
    if data_path is None:
        return None

    env = os.environ.copy()
    env["BSOS_TARGET_GAME"] = str(target)
    env["BSOS_GAME_DATA_PATH"] = str(data_path)
    env["BSOS_OUTPUT_DATA_PATH"] = str(output_mod_path)
    return launcher, env
