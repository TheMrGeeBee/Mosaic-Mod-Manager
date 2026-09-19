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


def _resolver_binds(resolv_conf: str = "/etc/resolv.conf",
                    run_root: str = "/run") -> "list[str]":
    """bwrap args that keep name resolution working under the shim's ``/run`` tmpfs.

    Most distros make ``/etc/resolv.conf`` a symlink into ``/run``
    (systemd-resolved's ``/run/systemd/resolve/stub-resolv.conf``,
    NetworkManager's, resolvconf's). The shim has to replace ``/run`` with an
    empty tmpfs to create ``/run/host``, which leaves that symlink dangling:
    every lookup inside the sandbox then fails (curl exit 6). Anything that
    downloads inside Wine — a Windows installer, a wizard fetching a runtime —
    silently gets nothing. Found 2026-09-19 running MulderLoad's Fallout 4
    downgrader, whose every download reported "Failed:".

    Re-exposes the directory holding the link's real target (or the file
    itself when it sits directly in *run_root*, since binding ``/run`` would
    undo the tmpfs). Empty when resolv.conf is an ordinary file, or points
    somewhere that doesn't exist.
    """
    try:
        target = os.path.realpath(resolv_conf)
    except OSError:
        return []
    root = run_root.rstrip("/") + "/"
    if not target.startswith(root) or not os.path.exists(target):
        return []
    parent = os.path.dirname(target)
    src = target if parent.rstrip("/") == run_root.rstrip("/") else parent
    return ["--ro-bind", src, src]


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
        *_resolver_binds(),
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
