"""Off-site mods come in two kinds. "browse" is a web page the user must open and
download from by hand. "direct" is a real file URL with a size and md5: the
installer downloads, verifies and installs it itself before the rest of the
collection. The detail view listed both under "download manually", so Gate To
Sovngarde's MaxsuBlockOverhaul - already installed by the collection install -
still looked like something the user had to do."""
from __future__ import annotations

from Utils.collections.collection_manifest import extract_offsite_mods, extract_offsite_split

MANIFEST = {"mods": [
    {"name": "Nexus Mod", "source": {"type": "nexus", "modId": 1, "fileId": 2}},
    {"name": "Manual One", "source": {"type": "browse", "url": "https://example.com/page"}},
    {"name": "Auto One.7z", "source": {"type": "direct", "url": "https://github.com/x/y/a.7z"}},
    {"name": "No Url", "source": {"type": "direct"}},
    {"name": "Bundled", "source": {"type": "bundle"}},
    {"name": "Auto Two", "source": {"type": "DIRECT", "fileUrl": "https://example.com/b.zip"}},
]}


def test_split_separates_manual_from_automatic():
    manual, auto = extract_offsite_split(MANIFEST)
    assert manual == [("Manual One", "https://example.com/page")]
    assert auto == [("Auto One.7z", "https://github.com/x/y/a.7z"), ("Auto Two", "https://example.com/b.zip")]


def test_split_is_tolerant():
    assert extract_offsite_split({}) == ([], [])
    assert extract_offsite_split(None) == ([], [])


def test_the_old_helper_still_returns_everything():
    assert len(extract_offsite_mods(MANIFEST)) == 3
