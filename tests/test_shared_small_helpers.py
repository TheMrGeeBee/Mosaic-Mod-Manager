"""app_log.noop_log and path_utils.free_bytes -- small helpers that replaced
many private copies."""
from __future__ import annotations

import shutil

from Utils import app_log
from Utils.path_utils import free_bytes


# -- noop_log ----------------------------------------------------------------

def test_noop_log_accepts_any_call_and_returns_none():
    assert app_log.noop_log("message") is None
    assert app_log.noop_log() is None
    assert app_log.noop_log("a", "b", level="warn") is None


def test_safe_log_falls_back_to_the_noop_and_keeps_real_loggers():
    seen = []

    assert app_log.safe_log(None) is app_log.noop_log
    assert app_log.safe_log(seen.append) == seen.append
    app_log.safe_log(None)("ignored")
    assert seen == []


# -- free_bytes --------------------------------------------------------------

def test_free_bytes_of_existing_directory(tmp_path):
    assert free_bytes(tmp_path) == shutil.disk_usage(tmp_path).free


def test_free_bytes_of_a_path_that_does_not_exist_yet(tmp_path):
    future = tmp_path / "not" / "created" / "yet"

    assert not future.exists()
    assert free_bytes(future) == shutil.disk_usage(tmp_path).free


def test_free_bytes_accepts_str(tmp_path):
    assert free_bytes(str(tmp_path / "x")) == shutil.disk_usage(tmp_path).free


def test_free_bytes_of_a_file_measures_its_filesystem(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"x")

    assert free_bytes(f) == shutil.disk_usage(tmp_path).free


def test_free_bytes_of_the_filesystem_root_terminates():
    assert free_bytes("/definitely/not/a/real/path") > 0
