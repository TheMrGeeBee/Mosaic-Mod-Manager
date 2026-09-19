"""
run_host_shim.py
Helpers for running bare Proton/wine against a Steam-created prefix.

Lives here (not in ``Utils.exe_launch.exe_launch``) so ``steam_finder`` can use
it without a circular import; ``exe_launch`` re-exports the same names.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _needs_run_host_shim(prefix_dir: "Path | None") -> bool:
    """Whether *prefix_dir*'s Wine DLLs are symlinked through a
    SteamLinuxRuntime sandbox-only ``/run/host/...`` path.

    A prefix whose ``system32`` was only ever touched by a real Steam game
    launch (routed through SteamLinuxRuntime/pressure-vessel) gets its Wine
    DLLs symlinked to wherever Proton saw itself running FROM — which,
    inside that sandbox, is ``/run/host/usr/...``. That path doesn't exist
    outside the sandbox, so a bare ``proton run`` (what wizard tools use for
    "the game's own prefix" mode) hits a dangling symlink and fails with
    "could not load kernel32.dll". Checked via kernel32.dll alone — the
    whole system32 tree gets symlinked the same way in one pass, so it's a
    reliable signal without walking every DLL.
    """
    if prefix_dir is None:
        return False
    try:
        target = os.readlink(Path(prefix_dir) / "drive_c/windows/system32/kernel32.dll")
    except OSError:
        return False
    return target.startswith("/run/host/")


def _run_host_shim_prefix() -> "list[str] | None":
    """``bwrap`` argv prefix that re-creates SteamLinuxRuntime's ``/run/host``
    view for a bare Proton/wine invocation, or None if bwrap isn't available.

    Rebinds the whole filesystem at ``/run/host`` — matching what
    SteamLinuxRuntime itself provides to a real game launch — while
    preserving the real ``XDG_RUNTIME_DIR`` (X11/Wayland/DBus sockets) under
    the fresh ``/run`` tmpfs, since ``/run`` has to be replaced to create the
    ``/run/host`` mountpoint at all. Unprivileged; bwrap is already a hard
    dependency of Steam's own runtime, so it's reliably present alongside it.
    """
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return None
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return [
        bwrap,
        "--bind", "/", "/",
        "--tmpfs", "/run",
        "--bind", runtime_dir, runtime_dir,
        "--bind", "/", "/run/host",
        "--dev-bind", "/dev", "/dev",
        "--proc", "/proc",
        "--",
    ]


def _apply_run_host_shim(cmd: list, prefix_dir: "Path | None",
                         label: str, log_fn) -> list:
    """Prepend the ``/run/host`` bwrap shim to *cmd* when *prefix_dir* needs it.

    Idempotent: ``proton_run_command`` applies the shim itself, so call sites
    that still run this on its output get *cmd* back unchanged rather than a
    second (nested) bwrap.
    """
    if cmd and Path(str(cmd[0])).name == "bwrap" and "/run/host" in cmd:
        return cmd
    if not _needs_run_host_shim(prefix_dir):
        return cmd
    shim = _run_host_shim_prefix()
    if shim is None:
        log_fn(f"{label}: prefix DLLs need the sandbox's /run/host view but "
               "bwrap isn't available — launch may fail to load kernel32.dll.")
        return cmd
    log_fn(f"{label}: prefix DLLs were symlinked by a real Steam launch "
           "(sandbox-only /run/host paths) — wrapping via bwrap to match.")
    return shim + cmd
