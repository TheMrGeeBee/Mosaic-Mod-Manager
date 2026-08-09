"""
instance_lock.py
Cross-process single-instance enforcement via a POSIX advisory file lock
(fcntl.flock).

Unlike a PID file or the NXM IPC socket file, an flock is released by the
kernel automatically when the holding process exits for ANY reason (clean
exit, crash, SIGKILL) — no stale-lock cleanup to get wrong. That's exactly
the bug class NxmIPC.shutdown()'s unconditional path.unlink() used to fall
into for the IPC socket (a duplicate instance's belated close deleted the
live instance's socket file); don't repeat that shape here.

Linux-only (AppImage/Flatpak/AUR distribution, no Windows build), so no
cross-platform fallback is needed.
"""

from __future__ import annotations

import fcntl
import os

from Utils.config_paths import get_instance_lock_path


class InstanceLock:
    """Ensures only one Mosaic process holds the lock at a time.

    Call acquire() once at the very start of startup, before doing any
    other work. If it returns False, a live instance already exists —
    hand off via NxmIPC and exit rather than opening a second window.
    """

    _fd: int | None = None

    @classmethod
    def acquire(cls) -> bool:
        """Try to become the single running instance.

        Returns True if this process now holds the lock (proceed with
        full startup), False if another live process already holds it.
        """
        if cls._fd is not None:
            return True  # already held by this process
        path = get_instance_lock_path()
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        try:  # diagnostics only, never fatal
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
        except OSError:
            pass
        cls._fd = fd
        return True

    @classmethod
    def release(cls) -> None:
        """Release the lock, if held. Safe to call even if never acquired."""
        if cls._fd is None:
            return
        try:
            fcntl.flock(cls._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(cls._fd)
        except OSError:
            pass
        cls._fd = None
