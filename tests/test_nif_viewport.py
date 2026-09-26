"""Headless render checks for the NIF viewport (real OpenGL, no window).

Skipped when no GL 3.3 core context can be created. Uses a hand-made triangle
so no game files are needed.
"""
from __future__ import annotations

import os
from array import array

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from Utils.nif.nif_reader import NifNode, NifScene, NifShape  # noqa: E402
from gui_qt.nif_viewer.gl_viewport import (  # noqa: E402
    SOLID, TEXTURED, WIRE, OrbitCamera, compute_normals, fit_inputs, render_offscreen,
)

BG = QColor(34, 34, 34).rgb()


@pytest.fixture(scope="module")
def app():
    # QApplication (not QGuiApplication): other test modules build widgets and
    # must be able to share the one application whichever module runs first.
    return QApplication.instance() or QApplication([])


def _tri(**kw) -> NifShape:
    return NifShape(
        name="t", positions=array("f", [-1, 0, -1, 1, 0, -1, 0, 0, 1]),
        normals=None, uvs=array("f", [0, 0, 1, 0, 0.5, 1]),
        indices=array("H", [0, 1, 2]), **kw)


def _covered(img: QImage) -> int:
    return sum(1 for x in range(0, img.width(), 4) for y in range(0, img.height(), 4)
               if img.pixel(x, y) != BG)


def _render(scene, images=None, mode=SOLID):
    img = render_offscreen(scene, images or {}, mode, 128, _front_camera(scene))
    if img is None:
        pytest.skip("no OpenGL 3.3 core context available")
    return img


def _front_camera(scene):
    cam = OrbitCamera()
    cam.frame(scene.bounding_sphere(), scene.sample_points())
    cam.yaw, cam.pitch = -1.5707963, 0.0        # look along +Y at the XZ triangle
    return cam


def test_solid_draws_the_shape(app):
    img = _render(NifScene([_tri()]))
    assert _covered(img) > 50
    assert img.pixel(1, 1) == BG                 # background untouched in the corner


def test_wire_covers_less_than_solid(app):
    scene = NifScene([_tri()])
    assert 0 < _covered(_render(scene, mode=WIRE)) < _covered(_render(scene, mode=SOLID))


def test_textured_uses_the_diffuse_image(app):
    red = QImage(2, 2, QImage.Format_RGBA8888)
    red.fill(QColor(255, 0, 0))
    img = _render(NifScene([_tri()]), {0: red}, TEXTURED)
    c = QColor(img.pixel(64, 80))
    assert c.red() > 120 and c.green() < 40 and c.blue() < 40


def test_textured_without_an_image_falls_back_to_grey(app):
    c = QColor(_render(NifScene([_tri()]), {}, TEXTURED).pixel(64, 80))
    assert abs(c.red() - c.green()) < 25 and c.red() > 60


def test_effect_shapes_are_drawn_translucent_not_hidden(app):
    """Editor markers are made only of effect-shader shapes: they must show up."""
    scene = NifScene([_tri(is_effect=True)])
    assert _covered(_render(scene, mode=SOLID)) > 50
    assert _covered(_render(scene, mode=WIRE)) > 0
    c = QColor(_render(scene, mode=SOLID).pixel(64, 80))
    assert c.blue() > c.red()                         # tinted blue…
    assert c.red() > 34                               # …but blended, not opaque


def _bbox(img: QImage):
    xs, ys = [], []
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixel(x, y) != BG:
                xs.append(x); ys.append(y)
    return (min(xs), max(xs), min(ys), max(ys)) if xs else None


@pytest.mark.parametrize("scale", [0.01, 1.0, 5000.0])
def test_default_view_fills_the_frame_at_any_object_size(app, scale):
    """Tiny markers and huge structures must both be framed to fill the view."""
    sh = _tri()
    for i in range(len(sh.positions)):
        sh.positions[i] *= scale
    scene = NifScene([sh])
    img = render_offscreen(scene, {}, SOLID, (400, 400))
    if img is None:
        pytest.skip("no OpenGL 3.3 core context available")
    x0, x1, y0, y1 = _bbox(img)
    fill = max(x1 - x0, y1 - y0) / 400
    assert 0.6 < fill <= 1.0, fill


def test_fit_follows_the_viewport_shape(app):
    """A wide window fits the height, a tall one the width — nothing is clipped."""
    scene = NifScene([_tri()])
    for size in ((640, 240), (240, 640)):
        img = render_offscreen(scene, {}, SOLID, size)
        if img is None:
            pytest.skip("no OpenGL 3.3 core context available")
        x0, x1, y0, y1 = _bbox(img)
        assert x0 > 0 and y0 > 0 and x1 < size[0] - 1 and y1 < size[1] - 1   # fully inside
        assert max((x1 - x0) / size[0], (y1 - y0) / size[1]) > 0.6


def test_compute_normals_points_along_the_face():
    n = compute_normals(array("f", [0, 0, 0, 1, 0, 0, 0, 1, 0]), array("H", [0, 1, 2]))
    assert [round(v, 3) for v in n[:3]] == [0.0, 0.0, 1.0]


def test_orbit_camera_zoom_is_relative_to_the_fit_and_clamped():
    cam = OrbitCamera()
    cam.frame(((0, 0, 0), 5.0))
    fit = cam.dist(1.5)
    cam.zoom(1)
    assert cam.dist(1.5) < fit                     # scroll up zooms in
    cam.zoom(-1000)
    assert cam.zoom_factor == OrbitCamera.MAX_ZOOM
    cam.zoom(1000)
    assert cam.zoom_factor == OrbitCamera.MIN_ZOOM
    cam.orbit(10_000, 10_000)
    assert -1.56 < cam.pitch < 1.56                # pitch never flips over the pole
    cam.frame(((0, 0, 0), 5.0))                    # re-framing resets zoom and angle
    assert cam.zoom_factor == 1.0 and cam.dist(1.5) == pytest.approx(fit)


def test_camera_fit_scales_with_the_object_and_never_enters_it():
    small, big = OrbitCamera(), OrbitCamera()
    small.frame(((0, 0, 0), 2.0))
    big.frame(((0, 0, 0), 2000.0))
    assert big.fit_distance(1.0) == pytest.approx(1000 * small.fit_distance(1.0))
    pts = [(-1, 0, 0), (1, 0, 0), (0, 0, 1)]       # tiny object, big sphere
    tight = OrbitCamera()
    tight.frame(((0, 0, 0.3), 100.0), pts)
    assert tight.fit_distance(1.0) >= 1.5 * 100.0  # floor: camera stays outside the sphere


# -- skeleton overlay -----------------------------------------------------------------------------
def _bones():
    return [NifNode("skel.nif", -1, (0, 0, 0)),
            NifNode("NPC Root [Root]", 0, (0, 0, -1)),
            NifNode("NPC Spine [Spn0]", 1, (0, 0, 0)),
            NifNode("NPC Head [Head]", 2, (0, 0, 1))]


def _orange(img: QImage) -> int:
    n = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = QColor(img.pixel(x, y))
            if c.red() > 200 and 100 < c.green() < 190 and c.blue() < 90:
                n += 1
    return n


def test_skeleton_is_drawn_over_the_mesh(app):
    scene = NifScene([_tri()])
    without = _render(scene)
    assert _orange(without) == 0
    img = render_offscreen(scene, {}, SOLID, 128, _front_camera(scene), skeleton=_bones())
    assert _orange(img) > 10                                   # joints/bones over the grey mesh


def test_a_skeleton_alone_is_framed_and_drawn(app):
    empty = NifScene([])
    sphere, pts = fit_inputs(empty, _bones())
    assert sphere is not None and len(pts) == 3                # the helper node is not a bone
    img = render_offscreen(empty, {}, SOLID, 200, skeleton=_bones())
    if img is None:
        pytest.skip("no OpenGL 3.3 core context available")
    assert _orange(img) > 10
    x0, x1, y0, y1 = _bbox(img)
    assert x0 >= 0 and y0 >= 0 and x1 < 199 and y1 < 199       # nothing clipped


def test_fit_inputs_include_bones_beyond_the_mesh():
    scene = NifScene([_tri()])                                  # z in [-1, 1]
    tall = [NifNode("NPC Head [Head]", -1, (0, 0, 50))]
    (c, r), pts = fit_inputs(scene, tall)
    assert c[2] == pytest.approx(24.5) and r > 25              # framing grew to hold the head
    assert fit_inputs(None, None) == (None, None)
