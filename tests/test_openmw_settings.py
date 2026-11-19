"""Line-based editing of OpenMW's settings.cfg."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from Games.Morrowind.openmw_settings import (  # noqa: E402
    BACKUP_SUFFIX,
    read_settings,
    set_values,
    settings_cfg_path,
)

SAMPLE = """# comment
[Video]
resolution x = 3440

[Groundcover]
enabled = false
stomp mode = 2

[Saves]
character = Charlie
"""


def test_update_in_place_keeps_everything_else(tmp_path):
    f = tmp_path / "settings.cfg"
    f.write_text(SAMPLE)
    changed = set_values(f, {("Groundcover", "enabled"): "true"})
    assert changed == ["[Groundcover] enabled = true"]
    assert f.read_text() == SAMPLE.replace("enabled = false", "enabled = true")


def test_backup_made_once(tmp_path):
    f = tmp_path / "settings.cfg"
    f.write_text(SAMPLE)
    set_values(f, {("Groundcover", "enabled"): "true"})
    bak = tmp_path / ("settings.cfg" + BACKUP_SUFFIX)
    assert bak.read_text() == SAMPLE
    set_values(f, {("Groundcover", "enabled"): "false"})
    assert bak.read_text() == SAMPLE


def test_missing_key_added_to_existing_section(tmp_path):
    f = tmp_path / "settings.cfg"
    f.write_text(SAMPLE)
    set_values(f, {("Groundcover", "density"): "0.5"})
    lines = f.read_text().splitlines()
    i = lines.index("stomp mode = 2")
    assert lines[i + 1] == "density = 0.5"
    assert read_settings(f)[("groundcover", "density")] == "0.5"


def test_missing_section_appended(tmp_path):
    f = tmp_path / "settings.cfg"
    f.write_text(SAMPLE)
    set_values(f, {("Terrain", "distant terrain"): "true"})
    assert f.read_text().endswith("\n[Terrain]\ndistant terrain = true\n")


def test_creates_file_when_absent(tmp_path):
    f = tmp_path / "sub" / "settings.cfg"
    set_values(f, {("Groundcover", "enabled"): "true"})
    assert f.read_text() == "[Groundcover]\nenabled = true\n"
    assert not (tmp_path / "sub" / ("settings.cfg" + BACKUP_SUFFIX)).exists()


def test_no_change_means_no_write_and_no_backup(tmp_path):
    f = tmp_path / "settings.cfg"
    f.write_text(SAMPLE)
    assert set_values(f, {("Groundcover", "enabled"): "false"}) == []
    assert not (tmp_path / ("settings.cfg" + BACKUP_SUFFIX)).exists()


def test_crlf_is_preserved(tmp_path):
    f = tmp_path / "settings.cfg"
    f.write_bytes(SAMPLE.replace("\n", "\r\n").encode())
    set_values(f, {("Groundcover", "enabled"): "true"})
    raw = f.read_bytes()
    assert b"\r\nenabled = true\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n")


def test_section_and_key_match_ignores_case(tmp_path):
    f = tmp_path / "settings.cfg"
    f.write_text("[groundcover]\nEnabled = false\n")
    set_values(f, {("Groundcover", "enabled"): "true"})
    assert f.read_text() == "[groundcover]\nEnabled = true\n"


def test_settings_path_is_beside_openmw_cfg(tmp_path):
    assert settings_cfg_path(tmp_path / "openmw" / "openmw.cfg") == tmp_path / "openmw" / "settings.cfg"
