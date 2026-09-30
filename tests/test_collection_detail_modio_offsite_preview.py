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

from gui_qt.collections.collection_detail_view import _is_modio_auto_offsite

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
