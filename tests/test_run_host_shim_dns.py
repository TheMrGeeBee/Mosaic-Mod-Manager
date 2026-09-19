"""The /run/host bwrap shim must not break name resolution inside the sandbox.

Real-world motivation: the shim replaces ``/run`` with an empty tmpfs (it has to,
to create the ``/run/host`` mountpoint). On a systemd-resolved host
``/etc/resolv.conf`` is a symlink to ``/run/systemd/resolve/stub-resolv.conf``,
so inside the shim it dangled and every DNS lookup failed (curl exit 6). A
Windows installer run through Mosaic's Run EXE — MulderLoad's Fallout 4
downgrader — reported "Failed:" for every download and left the game folder
half-modified, because its cleanup steps run whether or not the patches arrived.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from Utils.wine_proton import run_host_shim as shim


def _run_tree(tmp_path):
    run = tmp_path / "run"
    (run / "systemd" / "resolve").mkdir(parents=True)
    (run / "systemd" / "resolve" / "stub-resolv.conf").write_text("nameserver 127.0.0.53\n")
    return run


def _link(tmp_path, target):
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    conf = etc / "resolv.conf"
    conf.symlink_to(target)
    return conf


def test_systemd_resolved_directory_is_re_exposed(tmp_path):
    run = _run_tree(tmp_path)
    conf = _link(tmp_path, run / "systemd" / "resolve" / "stub-resolv.conf")
    d = str(run / "systemd" / "resolve")
    assert shim._resolver_binds(str(conf), str(run)) == ["--ro-bind", d, d]


def test_a_target_directly_in_run_binds_the_file_not_run_itself(tmp_path):
    """Binding /run itself would undo the tmpfs and defeat the shim."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "resolv.conf").write_text("nameserver 1.1.1.1\n")
    conf = _link(tmp_path, run / "resolv.conf")
    f = str(run / "resolv.conf")
    assert shim._resolver_binds(str(conf), str(run)) == ["--ro-bind", f, f]


def test_an_ordinary_resolv_conf_needs_nothing(tmp_path):
    (tmp_path / "etc").mkdir()
    conf = tmp_path / "etc" / "resolv.conf"
    conf.write_text("nameserver 9.9.9.9\n")
    assert shim._resolver_binds(str(conf), str(tmp_path / "run")) == []


def test_a_dangling_link_into_run_is_ignored(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    conf = _link(tmp_path, run / "gone" / "stub-resolv.conf")
    assert shim._resolver_binds(str(conf), str(run)) == []


def test_a_missing_resolv_conf_is_ignored(tmp_path):
    assert shim._resolver_binds(str(tmp_path / "nope"), str(tmp_path / "run")) == []


def test_the_shim_binds_the_resolver_after_the_run_tmpfs(monkeypatch):
    """Order matters to bwrap: a bind made before ``--tmpfs /run`` is hidden by it."""
    if shutil.which("bwrap") is None:
        pytest.skip("bwrap not installed")
    monkeypatch.setattr(shim, "_resolver_binds", lambda *a, **k: ["--ro-bind", "/MARK", "/MARK"])
    argv = shim._run_host_shim_prefix()
    assert argv.index("--tmpfs") < argv.index("/MARK") < argv.index("/run/host")


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
def test_resolv_conf_is_readable_inside_the_real_shim():
    """End to end, unmocked: on a host whose resolv.conf lives under /run this
    failed before the fix (`cat: /etc/resolv.conf: No such file or directory`)."""
    if not os.path.realpath("/etc/resolv.conf").startswith("/run/"):
        pytest.skip("this host's /etc/resolv.conf is not under /run")
    argv = shim._run_host_shim_prefix()
    res = subprocess.run(argv + ["cat", "/etc/resolv.conf"], capture_output=True, text=True, timeout=30)
    if res.returncode != 0 and "bwrap:" in res.stderr:
        pytest.skip(f"bwrap unusable here: {res.stderr.strip()[:80]}")
    assert res.returncode == 0 and "nameserver" in res.stdout, res.stderr
