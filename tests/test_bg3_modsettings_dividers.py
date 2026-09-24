"""BG3 modsettings.lsx: load-order divider paks stay out of the game's load order.

Divider packs (e.g. Astra's Load Order Dividers) are paks holding nothing but
a meta.lsx — markers for BG3MM / volo's curated sorting.  Listing them in
modsettings.lsx makes the game's Mod Verification report every one of them
as a "New Mod Detected".  Pure override paks stay out too (unchanged).
"""
from __future__ import annotations

from Utils.mods.modlist import ModEntry, write_modlist
from Utils.mods.modsettings import (
    BG3ModInfo,
    _is_meta_only_pak,
    load_order_eligible,
    write_modsettings,
)


def _info(uuid: str, name: str, *, meta_only: bool = False,
          override_only: bool = False, deps: tuple[str, ...] = ()) -> BG3ModInfo:
    return BG3ModInfo(uuid=uuid, name=name, folder=name, version64="1",
                      source_mod=name, is_meta_only=meta_only,
                      is_override_only=override_only, dependencies=list(deps))


def test_is_meta_only_pak():
    assert _is_meta_only_pak(["Mods/VOLOS_Dividers_Late_Compat/meta.lsx"])
    assert not _is_meta_only_pak(["Mods/X/meta.lsx", "Public/X/Stats/a.txt"])
    assert not _is_meta_only_pak([])


def test_dividers_and_overrides_are_not_eligible():
    infos = {
        "div": _info("div", "✒︎ 001 · UI ❧", meta_only=True),
        "ovr": _info("ovr", "Waterproof Shadowheart", override_only=True),
        "mod": _info("mod", "CPCCE"),
    }
    assert set(load_order_eligible(infos)) == {"mod"}


def test_divider_kept_when_another_mod_depends_on_it():
    infos = {
        "div": _info("div", "Marker", meta_only=True),
        "mod": _info("mod", "Needs Marker", deps=("div",)),
    }
    assert set(load_order_eligible(infos)) == {"div", "mod"}


def test_dividers_not_written_to_modsettings(tmp_path, monkeypatch):
    modlist = tmp_path / "modlist.txt"
    write_modlist(modlist, [
        ModEntry(name="Astra's Load Order Dividers", enabled=True, locked=False),
        ModEntry(name="CPCCE", enabled=True, locked=False),
    ])
    infos = {
        "uuid-div": _info("uuid-div", "✒︎ 001 · UI ❧", meta_only=True),
        "uuid-cpcce": _info("uuid-cpcce", "CPCCE"),
    }
    monkeypatch.setattr("Utils.mods.modsettings.scan_mod_paks",
                        lambda *a, **k: dict(infos))

    out = tmp_path / "modsettings.lsx"
    count = write_modsettings(out, modlist, tmp_path / "mods", patch_version=8)

    xml = out.read_text(encoding="utf-8")
    assert count == 1
    assert 'value="uuid-cpcce"' in xml
    assert "uuid-div" not in xml
