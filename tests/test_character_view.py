"""Character tab (equip / unequip / slots), the mesh picker and the NIF viewer's
'Add to character' button — headless, with fake meshes so no game is needed."""
from __future__ import annotations

import os
import time
from array import array

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from Utils.nif.asset_catalog import AssetCatalog  # noqa: E402
from Utils.nif.nif_reader import NifScene, NifShape, NifUnsupported  # noqa: E402
from gui_qt.nif_viewer import asset_loader, character_view as cv  # noqa: E402
from test_character import shape  # noqa: E402

FILES = {
    "meshes/armor/iron/f/cuirass_1.nif": b"CUIRASS_F",
    "meshes/armor/iron/m/cuirass_1.nif": b"CUIRASS_M",
    "meshes/armor/iron/f/boots_1.nif": b"BOOTS",
    "meshes/armor/iron/cuirass_alt.nif": b"CUIRASS_ALT",
    "meshes/actors/character/character assets/hair/female/hair01.nif": b"HAIR",
    "meshes/clutter/mug.nif": b"PROP",
    "meshes/armor/le/le_cuirass.nif": b"LE",
    "textures/armor/iron_d.dds": b"TEX",
    "meshes/armor/blades/bladesarmor.nif": b"DISPLAY",              # an item's display model
    "meshes/armor/blades/bladesarmor_1.nif": b"CUIRASS_M",
    "meshes/armor/blades/bladesarmorf_1.nif": b"CUIRASS_F",
    "meshes/armor/blades/bladeshelmet.nif": b"BOOTS",
}


def fake_read_nif(data, include_nodes=False):
    scenes = {
        b"CUIRASS_F": NifScene([shape("cuirass_f", [32, 34], [2, 1])]),
        b"CUIRASS_M": NifScene([shape("cuirass_m", [32, 34], [2, 1])]),
        b"CUIRASS_ALT": NifScene([shape("cuirass_alt", [32], [2])]),
        b"BOOTS": NifScene([shape("boots", [37, 38], [2, 1])]),
        b"HAIR": NifScene([shape("hair", [131, 141], [2, 1])]),
        b"PROP": NifScene([shape("mug", skinned=False, slots=[])]),
        b"DISPLAY": NifScene([shape("display", skinned=False, slots=[])]),
    }
    if data == b"LE":
        raise NifUnsupported("BS version 83", 0x14020007, 83)
    return scenes[data]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _wait(app, cond, sec=5.0):
    end = time.time() + sec
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.01)
    assert cond(), "timed out"


@pytest.fixture
def catalog(tmp_path):
    mod = tmp_path / "modX"
    for rel, data in FILES.items():
        p = mod / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    cat = AssetCatalog(
        base_name="Game", base_archives=[], mod_order=["modX"],
        loose={"modX": {k: k for k in FILES}}, bsas={}, loose_winner={}, bsa_winner={},
        mod_dir_for=lambda m: tmp_path / m, expected_nif_format=(0x14020007, 100))
    yield cat
    cat.close()


@pytest.fixture
def view(app, catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "read_nif", fake_read_nif)
    monkeypatch.setattr(asset_loader, "read_nif", fake_read_nif)
    monkeypatch.setattr(cv, "build_catalog", lambda *_a: catalog)
    v = cv.CharacterView(object(), tmp_path, tmp_path)
    yield v
    v.deleteLater()


def entry(cat, path):
    return cat.resolve(path)


def _ready(app, v):
    _wait(app, lambda: v._catalog is not None)


def test_equipping_before_the_catalog_is_ready_is_queued(app, view, catalog):
    # The catalog arrives through a queued signal, so right after construction it is not there yet.
    assert view._catalog is None
    view.equip(entry(catalog, "meshes/armor/iron/f/cuirass_1.nif"))
    assert len(view._pending) == 1 and view._pieces == {}
    _ready(app, view)
    _wait(app, lambda: "body" in view._pieces)
    assert view._pieces["body"].path.endswith("f/cuirass_1.nif")
    assert view._pending == []


def test_pieces_go_to_the_slot_their_mesh_belongs_to(app, view, catalog):
    _ready(app, view)
    for path, group in (("meshes/armor/iron/f/cuirass_1.nif", "body"),
                        ("meshes/armor/iron/f/boots_1.nif", "feet"),
                        ("meshes/actors/character/character assets/hair/female/hair01.nif", "hair")):
        view.equip(entry(catalog, path))
        _wait(app, lambda g=group: g in view._pieces)
    assert sorted(view._pieces) == ["body", "feet", "hair"]
    assert view._slot_labels["feet"].text() == "boots_1.nif"
    assert view._slot_clear["feet"].isEnabled() and not view._slot_clear["head"].isEnabled()
    assert view._remove_all.isEnabled()


def test_equipping_the_same_group_replaces_the_piece(app, view, catalog):
    _ready(app, view)
    view.equip(entry(catalog, "meshes/armor/iron/f/cuirass_1.nif"))
    _wait(app, lambda: "body" in view._pieces)
    view.equip(entry(catalog, "meshes/armor/iron/cuirass_alt.nif"))
    _wait(app, lambda: view._pieces["body"].path.endswith("cuirass_alt.nif"))
    assert len(view._pieces) == 1


def test_a_prop_or_an_unconverted_mesh_is_refused_with_a_reason(app, view, catalog):
    _ready(app, view)
    view.equip(entry(catalog, "meshes/clutter/mug.nif"))
    _wait(app, lambda: "no body slots" in view._info.text())
    assert view._pieces == {}
    view.equip(entry(catalog, "meshes/armor/le/le_cuirass.nif"))
    _wait(app, lambda: "Skyrim LE format" in view._info.text())
    assert view._pieces == {}


def test_the_first_piece_sets_the_gender_later_ones_do_not(app, view, catalog):
    _ready(app, view)
    assert view._gender.currentData() == "female"
    view.equip(entry(catalog, "meshes/armor/iron/m/cuirass_1.nif"))
    _wait(app, lambda: "body" in view._pieces)
    assert view._gender.currentData() == "male"
    view.equip(entry(catalog, "meshes/armor/iron/f/boots_1.nif"))          # 'f' path, but not the first piece
    _wait(app, lambda: "feet" in view._pieces)
    assert view._gender.currentData() == "male"


def test_unequip_and_remove_all(app, view, catalog):
    _ready(app, view)
    for p in ("meshes/armor/iron/f/cuirass_1.nif", "meshes/armor/iron/f/boots_1.nif"):
        view.equip(entry(catalog, p))
    _wait(app, lambda: len(view._pieces) == 2)
    view._slot_clear["body"].click()
    assert sorted(view._pieces) == ["feet"] and view._slot_labels["body"].text().startswith("—")
    view._remove_all.click()
    assert view._pieces == {} and not view._remove_all.isEnabled()


def test_the_scene_is_built_from_the_pieces_and_stale_builds_are_dropped(app, view, catalog):
    _ready(app, view)
    view.equip(entry(catalog, "meshes/armor/iron/f/cuirass_1.nif"))
    _wait(app, lambda: "1 piece(s) worn" in view._info.text())
    view._gen += 1                                                          # a newer build is under way
    view._on_build_ready(view._gen - 1, {"error": "stale"})
    assert "stale" not in view._message.text()


def test_settings_changes_rebuild(app, view, catalog):
    _ready(app, view)
    view.equip(entry(catalog, "meshes/armor/iron/f/cuirass_1.nif"))
    _wait(app, lambda: "1 piece(s) worn" in view._info.text())
    gen = view._gen
    view._weight.setCurrentIndex(1)
    view._skel.setChecked(True)
    assert view._gen == gen + 2


# -- picker ---------------------------------------------------------------------------------------------------
def test_picker_lists_wearable_winners_only(app, catalog):
    catalog.mark_incompatible(entry(catalog, "meshes/armor/le/le_cuirass.nif"), "Skyrim LE")
    dlg = cv.PickMeshDialog(catalog, None)
    paths = [e.path for e in dlg._entries]
    assert "meshes/clutter/mug.nif" not in paths and "textures/armor/iron_d.dds" not in paths
    assert "meshes/armor/le/le_cuirass.nif" not in paths                    # incompatible: hidden
    assert "meshes/armor/iron/f/cuirass_1.nif" in paths and len(dlg._entries) == 9
    assert paths == sorted(paths)


def test_picker_splits_hair_from_the_rest(app, catalog):
    hair = cv.PickMeshDialog(catalog, "hair")
    assert [e.path for e in hair._entries] == \
        ["meshes/actors/character/character assets/hair/female/hair01.nif"]
    body = cv.PickMeshDialog(catalog, "body")
    assert not any("/hair/" in e.path for e in body._entries)


def test_picker_search_and_choice(app, catalog):
    dlg = cv.PickMeshDialog(catalog, "body")
    dlg._search.setText("boots")
    assert dlg._list.count() == 1 and "boots_1.nif" in dlg._list.item(0).text()
    dlg._search.setText("modx")                                              # by mod name too
    assert dlg._list.count() == len(dlg._entries)
    dlg._search.setText("nothing-matches")
    assert dlg._list.count() == 0 and not dlg._ok.isEnabled()
    dlg._search.setText("boots")
    dlg._accept()
    assert dlg.chosen().path.endswith("boots_1.nif")


# -- 'Add to character' in the NIF Viewer -----------------------------------------------------------------------
def test_add_to_character_button_follows_the_selection(app, tmp_path, monkeypatch, catalog):
    from gui_qt.nif_viewer import nif_viewer_view as nv
    from test_nif_viewer_ui import _find
    monkeypatch.setattr(nv, "build_catalog", lambda *_a: catalog)
    v = nv.NifViewerView(object(), tmp_path, tmp_path)
    _wait(app, lambda: v._catalog is not None)
    got = []
    v.equip_requested.connect(got.append)
    assert not v._equip_btn.isEnabled()
    v._tree.setCurrentIndex(_find(v._model, "modX", "meshes", "armor", "iron", "f", "boots_1.nif"))
    assert v._equip_btn.isEnabled()
    v._equip_btn.click()
    assert len(got) == 1 and got[0].path.endswith("boots_1.nif")
    assert "Sent boots_1.nif to the Character tab" in v._info.text()
    v._tree.setCurrentIndex(_find(v._model, "modX", "textures", "armor", "iron_d.dds"))
    assert not v._equip_btn.isEnabled()
    v.deleteLater()


def test_a_display_model_is_refused_with_its_worn_siblings_named(app, view, catalog):
    _ready(app, view)
    view.equip(entry(catalog, "meshes/armor/blades/bladesarmor.nif"))
    _wait(app, lambda: "display model" in view._info.text())
    text = view._info.text()
    assert "bladesarmor_1.nif" in text and "bladesarmorf_1.nif" in text
    assert "bladeshelmet.nif" not in text                       # a different item, not a sibling by name
    assert view._pieces == {}
    view.equip(entry(catalog, "meshes/clutter/mug.nif"))        # no similarly-named files → the plain message
    _wait(app, lambda: "props, weapons and shields" in view._info.text())
