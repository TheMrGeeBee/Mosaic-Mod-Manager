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
from Utils.nif import asset_catalog as ac  # noqa: E402
from gui_qt.nif_viewer import asset_loader, character_view as cv  # noqa: E402
from test_character import shape  # noqa: E402

FILES = {
    "meshes/armor/iron/f/cuirass_1.nif": b"CUIRASS_F",
    "meshes/armor/iron/m/cuirass_1.nif": b"CUIRASS_M",
    "meshes/armor/iron/f/boots_1.nif": b"BOOTS",
    "meshes/armor/iron/f/cuirass_0.nif": b"CUIRASS_F0",               # the slim version of cuirass_1
    "meshes/armor/iron/cuirass_alt.nif": b"CUIRASS_ALT",
    "meshes/actors/character/character assets/hair/female/hair01.nif": b"HAIR",
    "meshes/clutter/mug.nif": b"PROP",
    "meshes/armor/le/le_cuirass.nif": b"LE",
    "textures/armor/iron_d.dds": b"TEX",
    "meshes/armor/blades/bladesarmor.nif": b"DISPLAY",              # an item's display model
    "meshes/armor/blades/bladesarmor_1.nif": b"CUIRASS_M",
    "meshes/armor/blades/bladesarmorf_1.nif": b"CUIRASS_F",
    "meshes/armor/blades/bladeshelmet.nif": b"HELM",
}


def fake_read_nif(data, include_nodes=False):
    scenes = {
        b"CUIRASS_F": NifScene([shape("cuirass_f", [32, 34], [2, 1])]),
        b"CUIRASS_F0": NifScene([shape("cuirass_f0", [32, 34], [2, 1])]),
        b"CUIRASS_M": NifScene([shape("cuirass_m", [32, 34], [2, 1])]),
        b"CUIRASS_ALT": NifScene([shape("cuirass_alt", [32], [2])]),
        b"BOOTS": NifScene([shape("boots", [37, 38], [2, 1])]),
        b"HAIR": NifScene([shape("hair", [131, 141], [2, 1])]),
        b"HELM": NifScene([shape("helm", [131], [2])]),
        b"PROP": NifScene([shape("mug", skinned=False, slots=[])]),
        b"DISPLAY": NifScene([shape("display", skinned=False, slots=[])]),
    }
    if data == b"LE":
        raise NifUnsupported("BS version 83", 0x14020007, 83)
    return scenes[data]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


FAKE_SLOTS = {b"CUIRASS_F": {32, 34}, b"CUIRASS_M": {32, 34}, b"CUIRASS_ALT": {32}, b"CUIRASS_F0": {32, 34},
              b"BOOTS": {37, 38}, b"HAIR": {131, 141}, b"HELM": {131}}


@pytest.fixture(autouse=True)
def fake_body_slots(monkeypatch):
    """The catalog's slots-only reader sees fake marker bytes; give them slots."""
    def fake(data):
        if data == b"LE":
            raise NifUnsupported("BS version 83", 0x14020007, 83)
        return frozenset(FAKE_SLOTS.get(data, ()))
    monkeypatch.setattr(ac, "read_body_slots", fake)


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
def _picker(app, catalog, group, gender=None):
    dlg = cv.PickMeshDialog(catalog, group, gender)
    _wait(app, lambda: dlg.scanned)
    return dlg


def _paths(dlg):
    return [e.path for e in dlg._entries]


def test_picker_lists_only_meshes_that_fit_the_slot(app, catalog):
    catalog.mark_incompatible(entry(catalog, "meshes/armor/le/le_cuirass.nif"), "Skyrim LE")
    body = _paths(_picker(app, catalog, "body"))
    assert body == ["meshes/armor/blades/bladesarmor_1.nif", "meshes/armor/blades/bladesarmorf_1.nif",
                    "meshes/armor/iron/cuirass_alt.nif", "meshes/armor/iron/f/cuirass_1.nif",
                    "meshes/armor/iron/m/cuirass_1.nif"]
    #  not listed: boots/helmet/hair (other slots), the mug and the display model (no slots), the
    #  LE mesh (other format), the texture (not a mesh) — and cuirass_0 is folded into cuirass_1.
    assert _paths(_picker(app, catalog, "feet")) == ["meshes/armor/iron/f/boots_1.nif"]
    assert _paths(_picker(app, catalog, "head")) == ["meshes/armor/blades/bladeshelmet.nif"]
    assert _paths(_picker(app, catalog, "hair")) == \
        ["meshes/actors/character/character assets/hair/female/hair01.nif"]
    assert _paths(_picker(app, catalog, "legs")) == []                      # nothing fits: an empty list, not everything


def test_picker_shows_a_progress_line_then_the_count(app, catalog):
    dlg = cv.PickMeshDialog(catalog, "feet")
    assert "Checking which meshes fit" in dlg._count.text() and not dlg._ok.isEnabled()
    _wait(app, lambda: dlg.scanned)
    assert dlg._count.text() == "1 mesh(es) fit" and dlg._ok.isEnabled()


def test_picker_offers_only_the_characters_gender_and_a_box_to_widen_it(app, catalog):
    dlg = _picker(app, catalog, "body", "male")
    assert _paths(dlg) == ["meshes/armor/blades/bladesarmor_1.nif",        # its f-pair marks it male
                           "meshes/armor/iron/cuirass_alt.nif",              # unspecified: kept
                           "meshes/armor/iron/m/cuirass_1.nif"]
    assert dlg._only_gender.isChecked() and "male body" in dlg._only_gender.text()
    dlg._only_gender.setChecked(False)
    assert len(dlg._entries) == 5
    female = _picker(app, catalog, "body", "female")
    assert "meshes/armor/iron/m/cuirass_1.nif" not in _paths(female)
    assert "meshes/armor/blades/bladesarmorf_1.nif" in _paths(female)


def test_picker_search_and_choice(app, catalog):
    dlg = _picker(app, catalog, "body")
    dlg._search.setText("blades")
    assert dlg._list.count() == 2 and "bladesarmor_1.nif" in dlg._list.item(0).text()
    dlg._search.setText("modx")                                              # by mod name too
    assert dlg._list.count() == len(dlg._entries)
    dlg._search.setText("nothing-matches")
    assert dlg._list.count() == 0 and not dlg._ok.isEnabled()
    dlg._search.setText("cuirass_alt")
    dlg._accept()
    assert dlg.chosen().path.endswith("cuirass_alt.nif")


def test_closing_the_picker_stops_the_scan(app, catalog):
    dlg = cv.PickMeshDialog(catalog, "body")
    dlg.reject()
    assert dlg._closing


def test_the_slot_check_is_cached_per_mesh(catalog, monkeypatch):
    calls = []
    real = ac.read_body_slots
    monkeypatch.setattr(ac, "read_body_slots", lambda d: calls.append(d) or real(d))
    e = entry(catalog, "meshes/armor/iron/f/boots_1.nif")
    assert catalog.slots_of(e) == {37, 38} and catalog.slots_of(e) == {37, 38}
    assert len(calls) == 1
    assert catalog.slots_of(entry(catalog, "meshes/armor/le/le_cuirass.nif")) is None       # unreadable → None, cached
    assert catalog.slots_of(entry(catalog, "meshes/armor/le/le_cuirass.nif")) is None
    assert len(calls) == 2


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


def _capture_builds(view):
    got = []
    view._build_ready.connect(lambda gen, res: got.append(res))
    return got


def test_worn_pieces_follow_the_body_weight(app, view, catalog):
    _ready(app, view)
    got = _capture_builds(view)
    view.equip(entry(catalog, "meshes/armor/iron/f/cuirass_1.nif"))        # equipped as the heavy version
    view.equip(entry(catalog, "meshes/armor/iron/f/boots_1.nif"))           # has no slim version
    _wait(app, lambda: len(view._pieces) == 2)
    _wait(app, lambda: got and "boots" in [s.name for s in got[-1]["scene"].shapes])
    names = lambda: [s.name for s in got[-1]["scene"].shapes]              # noqa: E731
    assert "cuirass_f" in names() and not got[-1]["matched"]                # Heavy: as equipped

    n = len(got)
    view._weight.setCurrentIndex(1)                                        # Light (_0)
    _wait(app, lambda: len(got) > n and "cuirass_f0" in names())
    assert "cuirass_f" not in names()
    assert "boots" in names()                                              # no boots_0: keeps the _1
    assert got[-1]["matched"] == ["cuirass_0.nif"]
    assert "matched to body weight: cuirass_0.nif" in view._info.text()
    assert view._slot_labels["body"].text() == "cuirass_1.nif"             # the slot still shows what was chosen

    n = len(got)
    view._weight.setCurrentIndex(0)                                        # back to Heavy
    _wait(app, lambda: len(got) > n and "cuirass_f" in names() and "cuirass_f0" not in names())
    assert got[-1]["matched"] == []


def test_the_first_piece_sets_the_gender_from_its_f_suffix_pair(app, view, catalog):
    _ready(app, view)
    assert view._gender.currentData() == "female"
    view.equip(entry(catalog, "meshes/armor/blades/bladesarmor_1.nif"))     # male: bladesarmorf_1 sits beside it
    _wait(app, lambda: "body" in view._pieces)
    assert view._gender.currentData() == "male"
    assert view._piece_gender["body"] == "male"


def test_a_piece_for_the_other_gender_is_flagged(app, view, catalog):
    _ready(app, view)
    got = _capture_builds(view)
    view.equip(entry(catalog, "meshes/armor/blades/bladesarmor_1.nif"))     # male
    _wait(app, lambda: view._gender.currentData() == "male" and "body" in view._pieces)
    view.equip(entry(catalog, "meshes/armor/iron/f/boots_1.nif"))           # a female (f/ folder) piece on the male body
    _wait(app, lambda: "feet" in view._pieces)
    _wait(app, lambda: "made for the female body: boots_1.nif" in view._info.text())
    view._unequip("feet")
    _wait(app, lambda: "made for" not in view._info.text() and "1 piece(s) worn" in view._info.text())
    assert "feet" not in view._piece_gender


# -- feedback and the right-click menu ------------------------------------------------------------------------------
@pytest.fixture
def nif_view(app, tmp_path, monkeypatch, catalog):
    from gui_qt.nif_viewer import nif_viewer_view as nv
    monkeypatch.setattr(nv, "build_catalog", lambda *_a: catalog)
    v = nv.NifViewerView(object(), tmp_path, tmp_path)
    _wait(app, lambda: v._catalog is not None)
    yield v
    v.deleteLater()


def _row(view, *path):
    from test_nif_viewer_ui import _find
    return _find(view._model, *path)


def test_pressing_add_to_character_visibly_acknowledges_the_click(app, nif_view):
    v = nif_view
    v._equip_flash.setInterval(60)
    v._tree.setCurrentIndex(_row(v, "modX", "meshes", "armor", "iron", "f", "boots_1.nif"))
    idle = v._equip_btn.text()
    assert idle == "Add to character" and v._equip_btn.styleSheet() == ""
    v._equip_btn.click()
    assert v._equip_btn.text() == "✓ Added" and "background" in v._equip_btn.styleSheet()
    _wait(app, lambda: v._equip_btn.text() == idle)                       # flashes, then goes back
    assert v._equip_btn.styleSheet() == ""


def test_the_right_click_menu_offers_add_to_character_for_meshes_only(app, nif_view):
    v = nif_view
    boots = _row(v, "modX", "meshes", "armor", "iron", "f", "boots_1.nif")
    menu = v._build_tree_menu(boots)
    assert [a.text() for a in menu.actions()] == ["Add to character"]
    assert v._build_tree_menu(_row(v, "modX", "textures", "armor", "iron_d.dds")) is None   # a texture
    assert v._build_tree_menu(_row(v, "modX", "meshes")) is None                            # a folder
    assert v._build_tree_menu(_row(v, "modX")) is None                                      # a mod
    from PySide6.QtCore import QModelIndex
    assert v._build_tree_menu(QModelIndex()) is None                                        # empty space


def test_right_click_equips_that_row_without_selecting_or_flashing(app, nif_view):
    v = nif_view
    got = []
    v.equip_requested.connect(got.append)
    v._tree.setCurrentIndex(_row(v, "modX", "meshes", "armor", "iron", "f", "cuirass_1.nif"))
    selected = v._entry
    other = _row(v, "modX", "meshes", "armor", "iron", "f", "boots_1.nif")
    v._build_tree_menu(other).actions()[0].trigger()
    assert [e.path for e in got] == ["meshes/armor/iron/f/boots_1.nif"]
    assert v._entry == selected                                            # the selection (and its load) untouched
    assert v._equip_btn.text() == "Add to character"                       # the button is about the selection
    assert "Sent boots_1.nif" in v._info.text()
    v._build_tree_menu(_row(v, "modX", "meshes", "armor", "iron", "f", "cuirass_1.nif")).actions()[0].trigger()
    assert v._equip_btn.text() == "✓ Added"                                # right-clicking the selected row flashes it


def test_the_character_tab_reports_each_equip_for_a_toast(app, view, catalog):
    _ready(app, view)
    got = []
    view.equip_result.connect(lambda msg, ok: got.append((msg, ok)))
    view.equip(entry(catalog, "meshes/armor/iron/f/boots_1.nif"))
    _wait(app, lambda: got)
    assert got[-1] == ("Added boots_1.nif to the character (Feet)", True)
    view.equip(entry(catalog, "meshes/clutter/mug.nif"))
    _wait(app, lambda: len(got) == 2)
    assert got[-1][1] is False and "no body slots" in got[-1][0]
