"""NIF viewer tree model and tab flow (headless Qt; no OpenGL needed).

The 3D drawing itself is covered by test_nif_viewport; here the viewport widget
is constructed but never rendered (Qt's offscreen platform has no GL widgets).
"""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from Utils.nif.asset_catalog import AssetCatalog  # noqa: E402
from gui_qt.nif_viewer import nif_viewer_view  # noqa: E402
from gui_qt.nif_viewer.asset_tree import AssetTreeModel, EntryRole, SourceRole  # noqa: E402
from test_asset_catalog import MESH, NEW_IN_MOD, ONLY_BASE, TEX, world  # noqa: E402,F401
from test_nif_reader import _simple  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _names(model, parent=None):
    from PySide6.QtCore import QModelIndex
    parent = parent or QModelIndex()
    return [model.index(r, 0, parent).data() for r in range(model.rowCount(parent))]


def _find(model, *path):
    from PySide6.QtCore import QModelIndex
    idx = QModelIndex()
    for name in path:
        if model.canFetchMore(idx):
            model.fetchMore(idx)
        for r in range(model.rowCount(idx)):
            c = model.index(r, 0, idx)
            if c.data() == name:
                idx = c
                break
        else:
            raise AssertionError(f"{name!r} not found under {idx.data()!r}")
    return idx


# -- tree model --------------------------------------------------------------------------
def test_roots_are_base_then_mods_in_load_order(app, world):
    m = AssetTreeModel()
    m.set_catalog(world)
    assert _names(m) == ["Game", "modA", "modB"]


def test_children_are_lazy_folders_first_names_ascending(app, world):
    m = AssetTreeModel()
    m.set_catalog(world)
    base = m.index(0, 0)
    assert m.hasChildren(base) and m.rowCount(base) == 0 and m.canFetchMore(base)
    m.fetchMore(base)
    assert _names(m, base) == ["meshes", "textures"]
    meshes = _find(m, "Game", "meshes")
    assert _names(m, meshes) == ["armor", "clutter"]           # folders, a → z
    assert not m.canFetchMore(base)


def test_file_tags_and_overridden_dimming(app, world):
    m = AssetTreeModel()
    m.set_catalog(world)
    base_mesh = _find(m, "Game", "meshes", "armor", "iron", "cuirass.nif")
    assert base_mesh.data(SourceRole) == "Skyrim - Meshes0.bsa"
    assert base_mesh.data(EntryRole).is_winner is False
    assert base_mesh.data(Qt.ForegroundRole) is not None       # dimmed: a mod overrides it
    assert "Overridden by modA" in base_mesh.data(Qt.ToolTipRole)
    winner = _find(m, "modA", "meshes", "armor", "iron", "cuirass.nif")
    assert winner.data(SourceRole) in ("", None)               # loose file: no archive tag
    assert winner.data(Qt.ForegroundRole) is None
    bsa_file = _find(m, "modB", "meshes", "new", "thing.nif")
    assert bsa_file.data(SourceRole) == "b.bsa"


def test_filter_hides_roots_without_matches_and_clears(app, world):
    m = AssetTreeModel()
    m.set_catalog(world)
    assert m.set_filter("pot.nif") == 1
    assert _names(m) == ["Game"]
    assert m.set_filter("iron_d") == 3                          # base + modA(bsa) + modB
    assert _names(m) == ["Game", "modA", "modB"]
    assert m.set_filter("modb") == 3                            # mod name match → all its files
    assert _names(m) == ["modB"]
    assert m.set_filter("nothing-matches") == 0 and _names(m) == []
    assert m.set_filter("") == -1 and _names(m) == ["Game", "modA", "modB"]


# -- tab flow --------------------------------------------------------------------------------
def _png(color: str) -> bytes:
    img = QImage(4, 4, QImage.Format_RGBA8888)
    img.fill(QColor(color))
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


@pytest.fixture
def viewer(app, tmp_path, monkeypatch):
    mod = tmp_path / "modX"
    (mod / "meshes").mkdir(parents=True)
    (mod / "textures/armor").mkdir(parents=True)
    (mod / "meshes/thing.nif").write_bytes(_simple())           # → textures/armor/iron_d.dds
    (mod / "meshes/broken.nif").write_bytes(b"not a nif")
    (mod / "textures/armor/iron_d.dds").write_bytes(_png("red"))
    cat = AssetCatalog(
        base_name="Game", base_archives=[], mod_order=["modX"],
        loose={"modX": {"meshes/thing.nif": "meshes/thing.nif",
                        "meshes/broken.nif": "meshes/broken.nif",
                        "textures/armor/iron_d.dds": "textures/armor/iron_d.dds"}},
        bsas={}, loose_winner={}, bsa_winner={}, mod_dir_for=lambda m: tmp_path / m)
    monkeypatch.setattr(nif_viewer_view, "build_catalog", lambda *_a: cat)
    v = nif_viewer_view.NifViewerView(object(), tmp_path, tmp_path)
    _wait(app, lambda: v._catalog is not None)
    yield v
    v.deleteLater()


def _wait(app, cond, sec=5.0):
    end = time.time() + sec
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.01)
    assert cond(), "timed out"


def _select(viewer, *path):
    viewer._tree.setCurrentIndex(_find(viewer._model, *path))


def test_catalog_status_and_first_root(viewer):
    assert "1 mod(s)" in viewer._tree_status.text()
    assert _names(viewer._model) == ["Game", "modX"]


def test_mesh_selection_loads_shapes_and_textures(app, viewer):
    _select(viewer, "modX", "meshes", "thing.nif")
    _wait(app, lambda: viewer._last is not None)
    assert viewer._stack.currentIndex() == nif_viewer_view._PAGE_MESH
    assert len(viewer._last["scene"].shapes) == 1
    assert set(viewer._last["images"]) == {0}                   # diffuse resolved via the catalog
    assert viewer._last["missing"] == []
    assert "1 shape(s)" in viewer._info.text() and "textures: 1 found" in viewer._info.text()


def test_texture_only_source_shows_the_image(app, viewer):
    viewer._sources.setCurrentIndex(1)                          # Textures (.dds)
    _select(viewer, "modX", "meshes", "thing.nif")
    _wait(app, lambda: viewer._last is not None)
    assert viewer._stack.currentIndex() == nif_viewer_view._PAGE_IMAGE


def test_switching_to_textures_refetches_when_they_were_skipped(app, viewer):
    viewer._sources.setCurrentIndex(0)                          # Mesh only: no textures loaded
    _select(viewer, "modX", "meshes", "thing.nif")
    _wait(app, lambda: viewer._last is not None)
    assert viewer._last["textures_loaded"] is False
    viewer._sources.setCurrentIndex(2)
    _wait(app, lambda: viewer._last is not None and viewer._last["textures_loaded"])
    assert set(viewer._last["images"]) == {0}


def test_dds_selection_shows_image(app, viewer):
    _select(viewer, "modX", "textures", "armor", "iron_d.dds")
    _wait(app, lambda: viewer._stack.currentIndex() == nif_viewer_view._PAGE_IMAGE)
    assert "iron_d.dds" in viewer._info.text()


def test_corrupt_nif_shows_a_message_not_a_crash(app, viewer):
    _select(viewer, "modX", "meshes", "broken.nif")
    _wait(app, lambda: viewer._stack.currentIndex() == nif_viewer_view._PAGE_MESSAGE
          and "not a NIF" in viewer._message.text())


def test_stale_results_are_dropped(app, viewer):
    viewer._gen = 5
    viewer._on_asset_ready(4, {"kind": "error", "text": "old result"})
    assert "old result" not in viewer._message.text()


def test_search_filters_the_tree(app, viewer):
    viewer._search.setText("iron_d")
    _wait(app, lambda: "match" in viewer._tree_status.text())
    assert _names(viewer._model) == ["modX"]
