"""proton_run_command() must apply the /run/host bwrap shim itself.

Real-world motivation: a Proton prefix whose system32 was only ever touched by
a real Steam launch (SteamLinuxRuntime/pressure-vessel) has every Wine DLL
symlinked through a sandbox-only ``/run/host/...`` path. A bare ``proton
runinprefix`` from outside the sandbox then dies with ``wine: could not load
kernel32.dll, status c0000135`` (exit 53). That was fixed one call site at a
time (wizard tools, VC++ installer, .NET installer, registry writer) and each
new site that built its own ``proton_run_command`` argv reintroduced it — Run
EXE was the latest, hit launching FO4Down.exe / f4se_loader.exe. Applying the
shim inside proton_run_command means no caller can forget it.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from Utils.wine_proton import run_host_shim, steam_finder

PROTON = Path("/opt/proton/proton")
BASE = ["python3", str(PROTON), "runinprefix", "a.exe"]


def _make_prefix(tmp_path: Path, name: str, *, sandbox_symlink: bool) -> Path:
    compat = tmp_path / name
    sys32 = compat / "pfx" / "drive_c" / "windows" / "system32"
    sys32.mkdir(parents=True)
    kernel32 = sys32 / "kernel32.dll"
    if sandbox_symlink:
        kernel32.symlink_to(
            "/run/host/usr/share/steam/compatibilitytools.d/x/files/lib/wine/"
            "x86_64-windows/kernel32.dll")
    else:
        kernel32.write_bytes(b"MZ")
    return compat


@pytest.fixture
def sandbox_prefix(tmp_path):
    return _make_prefix(tmp_path, "sandboxed", sandbox_symlink=True)


@pytest.fixture
def plain_prefix(tmp_path):
    return _make_prefix(tmp_path, "plain", sandbox_symlink=False)


@pytest.fixture(autouse=True)
def _host_environment(monkeypatch):
    """Pin the environment: plain host (no flatpak), bwrap present, python3."""
    monkeypatch.setattr(steam_finder, "_in_flatpak_sandbox", lambda: False)
    monkeypatch.setattr(steam_finder, "_host_python", lambda: "python3")
    monkeypatch.setattr(
        shutil, "which",
        lambda name, *a, **k: f"/usr/bin/{name}" if name in ("bwrap", "flatpak-spawn") else None)


@pytest.fixture
def logged(monkeypatch):
    msgs: list[str] = []
    monkeypatch.setattr(steam_finder, "app_log", msgs.append)
    return msgs


def _cmd(prefix: Path | None) -> list[str]:
    env = {"STEAM_COMPAT_DATA_PATH": str(prefix)} if prefix is not None else None
    return steam_finder.proton_run_command(PROTON, "runinprefix", "a.exe", env=env)


def test_wraps_when_prefix_needs_shim(sandbox_prefix, logged):
    cmd = _cmd(sandbox_prefix)
    assert cmd[0] == "/usr/bin/bwrap"
    assert cmd[cmd.index("--") + 1:] == BASE
    assert ["--bind", "/", "/run/host"] == cmd[cmd.index("/run/host") - 2:cmd.index("/run/host") + 1]
    assert len(logged) == 1 and "wrapping via bwrap" in logged[0]


def test_normal_prefix_is_not_wrapped(plain_prefix, logged):
    assert _cmd(plain_prefix) == BASE
    assert logged == []


def test_no_env_means_no_wrap(logged):
    # Without STEAM_COMPAT_DATA_PATH there's no prefix to inspect.
    assert _cmd(None) == BASE
    assert logged == []


def test_missing_bwrap_falls_back_to_unwrapped(sandbox_prefix, monkeypatch, logged):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    assert _cmd(sandbox_prefix) == BASE
    assert len(logged) == 1 and "bwrap isn't available" in logged[0]


def test_existing_per_site_calls_do_not_double_wrap(sandbox_prefix, logged):
    """Call sites that still run _apply_run_host_shim on proton_run_command's
    output must be no-ops, and must not log a second time."""
    wrapped = _cmd(sandbox_prefix)
    site_msgs: list[str] = []
    again = run_host_shim._apply_run_host_shim(
        wrapped, sandbox_prefix / "pfx", "Site", site_msgs.append)
    assert again == wrapped
    assert again.count("/usr/bin/bwrap") == 1
    assert site_msgs == []
    assert len(logged) == 1


def test_apply_shim_still_wraps_an_unwrapped_command(sandbox_prefix):
    """The idempotence guard must not stop the helper wrapping a fresh command."""
    out = run_host_shim._apply_run_host_shim(
        ["wineserver", "-k"], sandbox_prefix / "pfx", "Site", lambda _m: None)
    assert out[0] == "/usr/bin/bwrap" and out[-2:] == ["wineserver", "-k"]


def test_flatpak_branch_is_left_alone(sandbox_prefix, monkeypatch, logged):
    monkeypatch.setattr(steam_finder, "_in_flatpak_sandbox", lambda: True)
    cmd = _cmd(sandbox_prefix)
    assert cmd[0] == "flatpak-spawn"
    assert "/usr/bin/bwrap" not in cmd
    assert logged == []


def test_plain_wine_binary_branch_is_left_alone(sandbox_prefix, logged):
    cmd = steam_finder.proton_run_command(
        Path("/opt/lutris/wine64"), "run", "a.exe",
        env={"STEAM_COMPAT_DATA_PATH": str(sandbox_prefix)})
    assert cmd == ["/opt/lutris/wine64", "a.exe"]
    assert logged == []
