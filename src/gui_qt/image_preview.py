"""Panel-scoped image preview widget for the Mod Files tab.

Loads via Pillow (so .dds/.tga/.tiff decode — QPixmap can't) and shows the image
fit-to-panel over a checkerboard backdrop (transparency visible). Scrollwheel
zooms (anchored under the cursor), left-drag pans, double-click resets to fit.
Used as a modlist-panel-scoped tab: the Mod Files tree stays live in the plugins
panel while the preview occupies the modlist region.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QPointF, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QBrush
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy, QComboBox,
    QPushButton,
)

from gui_qt.theme.theme_qt import active_palette, _c

PREVIEW_EXTS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp",
    ".tga", ".tif", ".tiff", ".ico", ".dds",
}


def _load_qimage(path: Path) -> QImage | None:
    """Load *path* to a QImage. Tries QImage first (fast for common formats),
    falls back to Pillow for .dds/.tga/etc."""
    img = QImage(str(path))
    if not img.isNull():
        return img
    try:
        from PIL import Image as PilImage
        with PilImage.open(path) as im:
            im = im.convert("RGBA")
            data = im.tobytes("raw", "RGBA")
            qi = QImage(data, im.width, im.height, QImage.Format_RGBA8888)
            return qi.copy()   # detach from the freed buffer
    except Exception:
        return None


def load_qimage_bytes(data: bytes) -> QImage | None:
    """Decode an image held in memory (e.g. read out of a BSA): QImage first,
    Pillow for .dds/.tga. Safe off the GUI thread."""
    img = QImage.fromData(data)
    if not img.isNull():
        return img
    try:
        import io
        from PIL import Image as PilImage
        with PilImage.open(io.BytesIO(data)) as im:
            im = im.convert("RGBA")
            qi = QImage(im.tobytes("raw", "RGBA"), im.width, im.height,
                        QImage.Format_RGBA8888)
            return qi.copy()
    except Exception:
        return None


class _ImageCanvas(QLabel):
    """Paints the image over a checkerboard with free zoom (scrollwheel) and
    pan (left-drag). Zoom is anchored under the cursor; double-click resets to
    fit-to-window."""

    _MIN_SCALE = 0.05
    _MAX_SCALE = 40.0
    _ZOOM_STEP = 1.15  # per wheel notch

    # Emitted after the USER changes zoom/pan/fit (never from apply_view_state),
    # so two canvases can be wired to each other without feedback loops.
    view_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(1, 1)
        self.setMouseTracking(True)
        self._pm: QPixmap | None = None
        self._scale = 1.0        # user-applied zoom on top of fit scale
        self._fit_scale = 1.0    # scale that makes the image fit the widget
        self._offset = QPointF(0.0, 0.0)  # top-left of image in widget coords
        self._fitting = True     # follow the fit scale until the user zooms/pans
        self._drag_from: QPointF | None = None
        self._drag_offset0 = QPointF(0.0, 0.0)
        self.setCursor(Qt.OpenHandCursor)

    def set_image(self, pm: QPixmap | None, keep_view: bool = False):
        """Show *pm*. With *keep_view* the current zoom/pan carries over (in
        image-relative terms, so it also holds when the new image has a
        different resolution) — flipping between two versions of a texture
        then lands on the same spot."""
        state = self.view_state() if keep_view and self._pm is not None else None
        self._pm = pm
        self._fitting = True
        self._recompute_fit()
        if state is not None:
            self.apply_view_state(state)
        self.update()

    # -- view sync ------------------------------------------------------------
    def view_state(self) -> tuple[bool, float, float, float]:
        """(fitting, zoom relative to fit, cx, cy) with (cx, cy) the image point
        under the widget centre in 0..1 image coordinates. Resolution-independent,
        so a 1K and a 4K version of the same texture show the same region."""
        if self._fitting or self._pm is None or self._pm.isNull():
            return (True, 1.0, 0.5, 0.5)
        s = self._eff_scale()
        iw, ih = self._pm.width() * s, self._pm.height() * s
        cx = (self.width() / 2.0 - self._offset.x()) / iw if iw else 0.5
        cy = (self.height() / 2.0 - self._offset.y()) / ih if ih else 0.5
        return (False, self._scale, cx, cy)

    def apply_view_state(self, state: tuple[bool, float, float, float]):
        """Adopt a view_state() from another canvas. Does not emit view_changed."""
        fitting, scale, cx, cy = state
        if self._pm is None or self._pm.isNull():
            return
        if fitting:
            self._fitting = True
            self._recompute_fit()
        else:
            self._fitting = False
            self._scale = scale
            s = self._eff_scale()
            iw, ih = self._pm.width() * s, self._pm.height() * s
            self._offset = QPointF(self.width() / 2.0 - cx * iw,
                                   self.height() / 2.0 - cy * ih)
            self._clamp_offset()
        self.update()

    # -- geometry helpers ---------------------------------------------------
    def _recompute_fit(self):
        """Compute the fit scale and, while in fitting mode, centre the image."""
        if self._pm is None or self._pm.isNull():
            return
        w, h = self._pm.width(), self._pm.height()
        if w <= 0 or h <= 0:
            return
        self._fit_scale = min(self.width() / w, self.height() / h)
        if self._fitting:
            self._scale = 1.0
            self._center()

    def _eff_scale(self) -> float:
        return self._fit_scale * self._scale

    def _center(self):
        if self._pm is None or self._pm.isNull():
            return
        s = self._eff_scale()
        iw, ih = self._pm.width() * s, self._pm.height() * s
        self._offset = QPointF((self.width() - iw) / 2.0,
                               (self.height() - ih) / 2.0)

    def _clamp_offset(self):
        """Keep the image sensibly placed: centre it on each axis when it is
        smaller than the viewport, otherwise stop it leaving empty gaps."""
        if self._pm is None or self._pm.isNull():
            return
        s = self._eff_scale()
        iw, ih = self._pm.width() * s, self._pm.height() * s
        ox, oy = self._offset.x(), self._offset.y()
        if iw <= self.width():
            ox = (self.width() - iw) / 2.0
        else:
            ox = min(0.0, max(self.width() - iw, ox))
        if ih <= self.height():
            oy = (self.height() - ih) / 2.0
        else:
            oy = min(0.0, max(self.height() - ih, oy))
        self._offset = QPointF(ox, oy)

    # -- interaction --------------------------------------------------------
    def wheelEvent(self, e):
        if self._pm is None or self._pm.isNull():
            return
        delta = e.angleDelta().y()
        if delta == 0:
            return
        factor = self._ZOOM_STEP ** (delta / 120.0)
        old_eff = self._eff_scale()
        new_eff = max(self._MIN_SCALE, min(self._MAX_SCALE, old_eff * factor))
        if new_eff == old_eff:
            return
        # Anchor the zoom under the cursor: keep the image point beneath the
        # pointer fixed on screen.
        cursor = e.position()
        img_pt = (cursor - self._offset) / old_eff
        self._fitting = False
        self._scale = new_eff / self._fit_scale
        self._offset = cursor - img_pt * self._eff_scale()
        self._clamp_offset()
        self.update()
        self.view_changed.emit()
        e.accept()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self._pm is not None:
            self._drag_from = e.position()
            self._drag_offset0 = QPointF(self._offset)
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self._drag_from is not None:
            self._fitting = False
            self._offset = self._drag_offset0 + (e.position() - self._drag_from)
            self._clamp_offset()
            self.update()
            self.view_changed.emit()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._drag_from is not None:
            self._drag_from = None
            self.setCursor(Qt.OpenHandCursor)

    def mouseDoubleClickEvent(self, e):
        self._fitting = True
        self._recompute_fit()
        self.update()
        self.view_changed.emit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._recompute_fit()
        if not self._fitting:
            self._clamp_offset()

    def paintEvent(self, _e):
        p = QPainter(self)
        self._paint_checker(p)
        if self._pm is None or self._pm.isNull():
            p.setPen(QColor("#aaa"))
            p.drawText(self.rect(), Qt.AlignCenter, "Image could not be loaded")
            p.end()
            return
        s = self._eff_scale()
        iw, ih = self._pm.width() * s, self._pm.height() * s
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawPixmap(int(round(self._offset.x())), int(round(self._offset.y())),
                     int(round(iw)), int(round(ih)), self._pm)
        p.end()

    def _paint_checker(self, p: QPainter):
        tile = 12
        c1, c2 = QColor("#353535"), QColor("#454545")
        p.fillRect(self.rect(), QBrush(c1))
        for y in range(0, self.height(), tile):
            for x in range(0, self.width(), tile):
                if ((x // tile) + (y // tile)) % 2:
                    p.fillRect(x, y, tile, tile, c2)


def _image_info(path: Path, qi: QImage | None) -> str:
    """One-line facts for a pane: DDS header details, else size and file size."""
    from Utils.dds_info import format_size, read_dds_info
    if path.suffix.lower() == ".dds":
        info = read_dds_info(path)
        if info is not None:
            return info.summary()
    try:
        size = format_size(path.stat().st_size)
    except OSError:
        size = ""
    dims = f"{qi.width()}×{qi.height()}" if qi is not None else ""
    return " · ".join(x for x in (dims, size) if x)


class _Pane(QWidget):
    """One image + its info line, with an optional picker of the mods that
    provide this file."""

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        pal = active_palette()
        self.picker = QComboBox()
        self.picker.setVisible(False)
        v.addWidget(self.picker)
        self.canvas = _ImageCanvas()
        self.canvas.setStyleSheet(f"background:{_c(pal, 'BG_DEEP')};")
        v.addWidget(self.canvas, 1)
        self.info = QLabel()
        self.info.setStyleSheet(
            f"background:{_c(pal, 'BG_HEADER')}; color:{_c(pal, 'TEXT_MAIN')};"
            " padding:4px 10px;")
        self.info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        v.addWidget(self.info)

    def show_file(self, path: Path | None, keep_view: bool = False):
        qi = _load_qimage(path) if path is not None else None
        self.canvas.set_image(QPixmap.fromImage(qi) if qi is not None else None,
                              keep_view=keep_view)
        self.info.setText(_image_info(path, qi) if path is not None else "")
        self.info.setToolTip(str(path) if path is not None else "")


class ImagePreview(QWidget):
    """A panel-scoped image preview: header (file name) + canvas. Fit by default;
    scrollwheel zooms (anchored under the cursor), left-drag pans, double-click
    resets to fit.

    When several mods ship the same file (*providers*), a picker per pane
    switches between their copies and a Compare toggle lines two of them up side
    by side with zoom/pan kept in sync."""

    def __init__(self, path: Path, display_name: str = "", parent=None,
                 providers=None, current_mod: str | None = None):
        super().__init__(parent)
        self.setObjectName("ImagePreview")
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        pal = active_palette()
        bar = QWidget()
        bar.setStyleSheet(
            f"background:{_c(pal, 'BG_HEADER')}; color:{_c(pal, 'TEXT_MAIN')};")
        h = QHBoxLayout(bar)
        h.setContentsMargins(10, 4, 6, 4)
        self._header = QLabel(display_name or path.name)
        self._header.setObjectName("ImagePreviewHeader")
        self._header.setStyleSheet("font-weight:600;")
        self._header.setToolTip(self.tr("Scroll to zoom · drag to pan · double-click to fit"))
        h.addWidget(self._header, 1)
        self._compare_btn = QPushButton(self.tr("Compare"))
        self._compare_btn.setCheckable(True)
        self._compare_btn.setToolTip(self.tr(
            "Show two mods' copies of this file side by side (zoom and pan stay in sync)"))
        self._compare_btn.setStyleSheet(
            f"QPushButton:checked {{ background:{_c(pal, 'ACCENT')};"
            f" color:{_c(pal, 'TEXT_ON_ACCENT')}; }}")
        self._compare_btn.toggled.connect(self._on_compare_toggled)
        h.addWidget(self._compare_btn)
        bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        v.addWidget(bar)

        panes = QHBoxLayout()
        panes.setContentsMargins(0, 0, 0, 0)
        panes.setSpacing(2)
        self._a, self._b = _Pane(), _Pane()
        self._b.setVisible(False)
        panes.addWidget(self._a, 1)
        panes.addWidget(self._b, 1)
        v.addLayout(panes, 1)

        # Keep the two views locked together; apply_view_state never re-emits.
        self._a.canvas.view_changed.connect(
            lambda: self._b.canvas.apply_view_state(self._a.canvas.view_state()))
        self._b.canvas.view_changed.connect(
            lambda: self._a.canvas.apply_view_state(self._b.canvas.view_state()))
        self._a.picker.currentIndexChanged.connect(lambda i: self._on_pick(self._a, i))
        self._b.picker.currentIndexChanged.connect(lambda i: self._on_pick(self._b, i))

        self._providers: list = []
        self.set_image(path, display_name, providers, current_mod)

    # -- public ---------------------------------------------------------------
    def set_image(self, path: Path, display_name: str = "", providers=None,
                  current_mod: str | None = None):
        """Swap the previewed file in place (browsing between files)."""
        if display_name:
            self._header.setText(display_name)
        self._providers = list(providers or [])
        multi = len(self._providers) >= 2
        self._compare_btn.setVisible(multi)
        if not multi and self._compare_btn.isChecked():
            self._compare_btn.setChecked(False)   # -> hides pane B
        self._fill_picker(self._a, self._initial_a(current_mod))
        self._fill_picker(self._b, self._initial_b())
        self._a.picker.setVisible(multi)
        self._b.picker.setVisible(multi)
        self._a.show_file(path)
        if self._compare_btn.isChecked():
            self._show_b()

    # -- providers ------------------------------------------------------------
    def _label(self, p) -> str:
        return p.mod_name + (self.tr("  (wins)") if p.is_winner else "")

    def _initial_a(self, current_mod) -> int:
        for i, p in enumerate(self._providers):
            if p.mod_name == current_mod:
                return i
        return 0

    def _initial_b(self) -> int:
        """Pane B defaults to the mod that wins — comparing "this copy" against
        "what actually deploys"; if A is already the winner, the next mod."""
        a = self._a.picker.currentIndex()
        for i, p in enumerate(self._providers):
            if p.is_winner and i != a:
                return i
        return next((i for i in range(len(self._providers)) if i != a), 0)

    def _fill_picker(self, pane: _Pane, index: int):
        cb = pane.picker
        cb.blockSignals(True)
        cb.clear()
        for p in self._providers:
            cb.addItem(self._label(p))
            if p.disk_path is None:
                cb.model().item(cb.count() - 1).setEnabled(False)
        if self._providers:
            cb.setCurrentIndex(index)
        cb.blockSignals(False)

    def _on_pick(self, pane: _Pane, index: int):
        if not 0 <= index < len(self._providers):
            return
        p = self._providers[index]
        if p.disk_path is not None:
            pane.show_file(p.disk_path, keep_view=True)
            if pane is self._a:
                self._header.setToolTip(str(p.disk_path))

    def _show_b(self):
        i = self._b.picker.currentIndex()
        if 0 <= i < len(self._providers):
            self._b.show_file(self._providers[i].disk_path, keep_view=False)
            # Pane B may not have been laid out yet (just made visible), and a
            # zoomed view is positioned from the widget size — sync next tick.
            QTimer.singleShot(0, lambda: self._b.canvas.apply_view_state(
                self._a.canvas.view_state()))

    def _on_compare_toggled(self, on: bool):
        self._b.setVisible(on)
        if on:
            self._fill_picker(self._b, self._initial_b())
            self._show_b()
