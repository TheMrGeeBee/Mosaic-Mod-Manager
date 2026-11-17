"""Tests for the shared read-only mosaic.ini parse cache in Utils.ui_config."""

import configparser
import os
import time

import pytest

from Utils import ui_config


@pytest.fixture(autouse=True)
def _clean_cache():
    ui_config._ini_cache.clear()
    yield
    ui_config._ini_cache.clear()


def _age(path, seconds=60):
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_missing_file_gives_empty_parser(tmp_path):
    parser = ui_config._read_ini(tmp_path / "nope.ini")
    assert parser.sections() == []


def test_old_file_is_parsed_once(tmp_path, monkeypatch):
    path = tmp_path / "a.ini"
    path.write_text("[ui]\nscale = 1.5\n", encoding="utf-8")
    _age(path)
    first = ui_config._read_ini(path)
    second = ui_config._read_ini(path)
    assert first is second
    assert first.get("ui", "scale") == "1.5"


def test_recently_modified_file_is_never_cached(tmp_path):
    path = tmp_path / "a.ini"
    path.write_text("[ui]\nscale = 1.5\n", encoding="utf-8")
    first = ui_config._read_ini(path)
    path.write_text("[ui]\nscale = 2.5\n", encoding="utf-8")  # same size
    assert ui_config._read_ini(path).get("ui", "scale") == "2.5"
    assert first.get("ui", "scale") == "1.5"
    assert path not in ui_config._ini_cache


def test_change_of_old_file_is_picked_up(tmp_path):
    path = tmp_path / "a.ini"
    path.write_text("[ui]\nscale = 1.5\n", encoding="utf-8")
    _age(path)
    assert ui_config._read_ini(path).get("ui", "scale") == "1.5"
    path.write_text("[ui]\nscale = 2.5\n", encoding="utf-8")
    _age(path, 30)
    assert ui_config._read_ini(path).get("ui", "scale") == "2.5"


def test_malformed_file_raises_like_a_fresh_parse(tmp_path):
    path = tmp_path / "bad.ini"
    path.write_text("no section header\n", encoding="utf-8")
    _age(path)
    with pytest.raises(configparser.Error):
        ui_config._read_ini(path)
    assert path not in ui_config._ini_cache


def test_duplicate_options_still_load(tmp_path):
    path = tmp_path / "dup.ini"
    path.write_text("[s]\nkey = 1\nkey = 2\n", encoding="utf-8")
    _age(path)
    assert ui_config._read_ini(path).get("s", "key") == "2"
