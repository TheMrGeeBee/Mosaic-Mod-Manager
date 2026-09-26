"""Headless render checks for the NIF viewport (real OpenGL, no window).

Skipped when no GL 3.3 core context can be created. Uses a hand-made triangle
so no game files are needed.
"""
from __future__ import annotations

import os
from array import array

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QGuiApplication, QImage  # noqa: E402

from Utils.nif.nif_reader import NifScene, NifShape  # noqa: E402
from gui_qt.nif_viewer.gl_viewport import (  # noqa: E402
    SOLID, TEXTURED, WIRE, OrbitCamera, compute_normals, render_offscreen,
)

BG = QColor(34, 34, 34).rgb()


@pytest.fixture(scope="module")
def app():
    return QGuiApplication.instance() or QGuiApplication([])


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
    cam.frame(scene.bounds())
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


def test_effect_shapes_are_hidden_unless_wireframe(app):
    scene = NifScene([_tri(is_effect=True)])
    assert _covered(_render(scene, mode=SOLID)) == 0
    assert _covered(_render(scene, mode=WIRE)) > 0


def test_compute_normals_points_along_the_face():
    n = compute_normals(array("f", [0, 0, 0, 1, 0, 0, 0, 1, 0]), array("H", [0, 1, 2]))
    assert [round(v, 3) for v in n[:3]] == [0.0, 0.0, 1.0]


def test_orbit_camera_zoom_is_clamped_and_frame_resets():
    cam = OrbitCamera()
    cam.frame(((0, 0, 0), (2, 2, 2)))
    d0 = cam.dist
    cam.zoom(1000)
    assert cam.dist >= cam.radius * 0.05
    cam.zoom(-1000)
    assert cam.dist <= cam.radius * 40
    cam.orbit(10_000, 10_000)
    assert -1.56 < cam.pitch < 1.56              # pitch never flips over the pole
    cam.frame(((0, 0, 0), (2, 2, 2)))
    assert cam.dist == d0
