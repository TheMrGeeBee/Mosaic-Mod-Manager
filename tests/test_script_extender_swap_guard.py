"""The script-extender swap must never destroy the game executable.

``swap_launcher`` renamed whatever sat at Starfield.exe to Starfield.bak and
copied the loader into its place. Run it a second time over an already-swapped
folder and the rename moves the LOADER on top of the backup — the real game
binary is gone, silently, with the log still reporting an ordinary swap. That
happened on 2026-10-28 and cost a Steam file verify to recover.

Also covered: turning the swap off has to clear the RuntimeName override a
previous swap wrote, or the loader is left pointing at a .bak that no longer
exists and stops with "Couldn't retrieve EXE version information".
"""
from __future__ import annotations

from pathlib import Path

GAME = b"REAL GAME EXECUTABLE" * 64
LOADER = b"sfse_loader stub" * 16


def _game(root: Path, swap: bool = True):
    """A real Starfield instance with only the fields swap_launcher touches.

    object.__new__ skips the heavy __init__; exe_name, exe_name_alts and
    _script_extender_exe are class properties, so they already answer
    correctly. A real instance matters because swap_launcher uses zero-arg
    super(), which a stand-in object cannot satisfy.
    """
    from Games.Bethesda.starfield import Starfield
    g = object.__new__(Starfield)
    g._game_path = root
    g._script_extender_swap = swap
    g.log = []
    return g


def logged(g) -> str:
    return "\n".join(g.log)


def _swap(g):
    from Games.Bethesda.starfield import Starfield
    Starfield.swap_launcher(g, g.log.append)


def _mkgame(tmp_path, *, exe: "bytes | None" = GAME, bak: "bytes | None" = None,
            swap: bool = True):
    if exe is not None:
        (tmp_path / "Starfield.exe").write_bytes(exe)
    if bak is not None:
        (tmp_path / "Starfield.bak").write_bytes(bak)
    (tmp_path / "sfse_loader.exe").write_bytes(LOADER)
    return _game(tmp_path, swap=swap)


def test_a_first_swap_backs_up_the_real_executable(tmp_path):
    g = _mkgame(tmp_path)
    _swap(g)
    assert (tmp_path / "Starfield.bak").read_bytes() == GAME
    assert (tmp_path / "Starfield.exe").read_bytes() == LOADER


def test_a_second_swap_does_not_destroy_the_backed_up_executable(tmp_path):
    """The regression. Swap twice; the real binary must survive."""
    g = _mkgame(tmp_path)
    _swap(g)
    g2 = _game(tmp_path)          # fresh instance, same already-swapped folder
    _swap(g2)
    assert (tmp_path / "Starfield.bak").read_bytes() == GAME, (
        "the second swap overwrote the backup with the loader — "
        "the game executable is gone")
    assert (tmp_path / "Starfield.exe").read_bytes() == LOADER
    assert "already" in logged(g2).lower()


def test_loader_in_place_with_no_backup_is_refused(tmp_path):
    """Exe is the loader and no .bak exists: the real binary is already lost.
    Renaming would only produce a second copy of the loader."""
    g = _mkgame(tmp_path, exe=LOADER)
    _swap(g)
    assert not (tmp_path / "Starfield.bak").exists()
    assert (tmp_path / "Starfield.exe").read_bytes() == LOADER
    assert "WARNING" in logged(g) and "verify" in logged(g).lower()


def test_a_backup_beside_a_different_executable_is_left_alone(tmp_path):
    """Two plausible real binaries and no way to tell them apart — touch
    neither rather than guess."""
    other = b"SOME OTHER BINARY" * 32
    g = _mkgame(tmp_path, exe=other, bak=GAME)
    _swap(g)
    assert (tmp_path / "Starfield.exe").read_bytes() == other
    assert (tmp_path / "Starfield.bak").read_bytes() == GAME
    assert "WARNING" in logged(g)


def test_a_disabled_swap_touches_nothing(tmp_path):
    g = _mkgame(tmp_path, swap=False)
    _swap(g)
    assert (tmp_path / "Starfield.exe").read_bytes() == GAME
    assert not (tmp_path / "Starfield.bak").exists()


# --------------------------------------------------------------------------
# sfse.ini RuntimeName must follow the swap setting
# --------------------------------------------------------------------------
def _write_ini(root: Path, runtime: str) -> Path:
    ini = root / "Data" / "SFSE" / "sfse.ini"
    ini.parent.mkdir(parents=True, exist_ok=True)
    ini.write_text(f"[Loader]\nRuntimeName={runtime}\n", encoding="utf-8")
    return ini


def test_disabling_the_swap_resets_a_stale_runtime_name(tmp_path):
    """Left pointing at Starfield.bak, the loader launches a file that is no
    longer there and dies with 'Couldn't retrieve EXE version information'."""
    g = _mkgame(tmp_path, swap=False)
    ini = _write_ini(tmp_path, "Starfield.bak")
    _swap(g)
    assert "RuntimeName=Starfield.exe" in ini.read_text()
    assert "sfse.ini" in logged(g)


def test_disabling_the_swap_leaves_an_already_correct_ini_alone(tmp_path):
    g = _mkgame(tmp_path, swap=False)
    ini = _write_ini(tmp_path, "Starfield.exe")
    before = ini.read_text()
    _swap(g)
    assert ini.read_text() == before
    assert "sfse.ini" not in logged(g)


def test_disabling_the_swap_creates_no_ini_when_there_is_none(tmp_path):
    g = _mkgame(tmp_path, swap=False)
    _swap(g)
    assert not (tmp_path / "Data" / "SFSE" / "sfse.ini").exists()


def test_an_enabled_swap_still_points_the_loader_at_the_backup(tmp_path):
    g = _mkgame(tmp_path)
    _swap(g)
    ini = tmp_path / "Data" / "SFSE" / "sfse.ini"
    assert "RuntimeName=Starfield.bak" in ini.read_text()


# --------------------------------------------------------------------------
# a transfer must not be skipped just because its parent directory is absent
# --------------------------------------------------------------------------
def test_a_transfer_creates_a_missing_destination_parent(tmp_path):
    """A missing parent used to surface as a WARN buried in the log while the
    deploy still reported success, so the mod file silently never arrived."""
    from Utils.deploy.deploy_shared import _do_link_ex
    from Utils.deploy.deploy_shared import LinkMode

    src = tmp_path / "src.bk2"
    src.write_bytes(b"video")
    dst = tmp_path / "Data" / "Video" / "src.bk2"      # parent does not exist
    mode, err = _do_link_ex(str(src), str(dst), LinkMode.SYMLINK)
    assert err is None, f"transfer failed instead of creating the parent: {err}"
    assert mode is LinkMode.SYMLINK
    assert dst.is_symlink() and dst.resolve() == src.resolve()


def test_a_genuinely_missing_source_is_still_reported(tmp_path):
    """The retry must not paper over a real failure."""
    from Utils.deploy.deploy_shared import _do_link_ex
    from Utils.deploy.deploy_shared import LinkMode

    dst = tmp_path / "Data" / "Video" / "gone.bk2"
    mode, err = _do_link_ex(str(tmp_path / "nope.bk2"), str(dst), LinkMode.HARDLINK)
    assert mode is None and err is not None
