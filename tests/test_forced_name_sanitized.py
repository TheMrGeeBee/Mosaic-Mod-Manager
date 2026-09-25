"""A name forced by the caller (collections use the author's mod title) must be as
Wine-addressable as every other naming route. "C.O.I.N." was created as a folder
with a trailing dot; a later refresh renamed it to "C.O.I.N" - and because the
profile was deployed as symlinks, the links kept pointing at the old name: 141
dangling files on Gate To Sovngarde, the start plugin among them."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from Utils.mods.mod_install import prepare_archive


class _Game:
    supports_bain = False
    plugin_extensions = None

    def __init__(self, staging: Path):
        self._s = staging

    def get_effective_mod_staging_path(self):
        return self._s


@pytest.mark.parametrize("forced,expected", [
    ("C.O.I.N.", "C.O.I.N"),
    ("M.I.N.T.", "M.I.N.T"),
    ("Alternate Perspective - Voiced Addon for AP 3.1.1.", "Alternate Perspective - Voiced Addon for AP 3.1.1"),
    ("A.S.S. for B.O.O.B.I.E.S.", "A.S.S. for B.O.O.B.I.E.S"),
    ("Trailing space ", "Trailing space"),
    ("Fine Name", "Fine Name"),
])
def test_a_forced_name_is_sanitized_like_every_other_route(tmp_path, forced, expected):
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("plugin.esp", "x")
    prepared = prepare_archive(str(archive), _Game(tmp_path / "mods"), tmp_path / "profile",
                               log_fn=lambda _m: None, preferred_name=forced)
    try:
        assert prepared.mod_name == expected
    finally:
        prepared.cleanup()
