"""Test for CollectionDetailView's off-site preview classification.

Real bug: collection_install.py was fixed to auto-resolve a mod.io-sourced
"browse" entry and install it automatically (see
Utils/collections/collection_install.py's _resolve_modio_browse_url) --
confirmed working end-to-end on a real install (the mod landed in
modlist.txt, staged, and deployed). But the Import tab's pre-install
preview panel (collection_detail_view.py's _populate_from_local_manifest)
has its own, separate classification that decides "Off-site mods --
download manually" vs "downloaded automatically", and it never learned
about the mod.io resolution -- so it kept showing the mod as needing a
manual download even though the actual install no longer needed one."""
from __future__ import annotations

from gui_qt.collections.collection_detail_view import (
    _is_modio_auto_offsite, _split_offsite_with_modio)

_URL = "https://mod.io/g/baldursgate3/m/better-hotbar-vovas-edition"


def test_modio_page_url_on_bg3_is_auto():
    assert _is_modio_auto_offsite(_URL, "baldursgate3") is True


def test_modio_page_url_on_a_different_game_is_not_auto():
    assert _is_modio_auto_offsite(_URL, "skyrimspecialedition") is False


def test_a_genuine_third_party_url_is_not_auto():
    assert _is_modio_auto_offsite(
        "https://github.com/someone/somemod/releases", "baldursgate3") is False


def test_empty_url_is_not_auto():
    assert _is_modio_auto_offsite("", "baldursgate3") is False


def test_split_offsite_with_modio_reclassifies_browse_type_modio_url():
    """Regression: _start_manifest_fetch's network path classified offsite
    mods via extract_offsite_split alone, which has no mod.io awareness --
    only the local-import preview got the fix above. A mod.io "browse"
    entry fetched over the network must land in 'automatic', matching what
    collection_install.py actually does at install time."""
    manifest = {"mods": [
        {"name": "Better Hotbar", "source": {"type": "browse", "url": _URL}},
        {"name": "Some Manual Mod", "source": {
            "type": "browse", "url": "https://github.com/x/y/releases"}},
        {"name": "A Direct Mod", "source": {
            "type": "direct", "url": "https://example.com/file.zip"}},
    ]}
    manual, automatic = _split_offsite_with_modio(manifest, "baldursgate3")
    assert manual == [("Some Manual Mod", "https://github.com/x/y/releases")]
    assert ("Better Hotbar", _URL) in automatic
    assert ("A Direct Mod", "https://example.com/file.zip") in automatic


def test_split_offsite_with_modio_leaves_non_bg3_browse_manual():
    manifest = {"mods": [
        {"name": "Better Hotbar", "source": {"type": "browse", "url": _URL}},
    ]}
    manual, automatic = _split_offsite_with_modio(manifest, "skyrimspecialedition")
    assert manual == [("Better Hotbar", _URL)]
    assert automatic == []
