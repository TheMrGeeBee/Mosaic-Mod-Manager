"""OpenGL rendering for NIF meshes.

* ``MeshRenderer`` — draws a ``NifScene`` into whatever GL 3.3 core context is
  current (world-space shapes, Z-up as stored in the file). Looks: SOLID
  (shaded grey), WIRE, TEXTURED (the shape's diffuse image, when supplied).
* ``OrbitCamera`` — pure camera maths.
* ``MeshViewport`` — the interactive widget: orbit (left-drag), pan
  (right/middle-drag), zoom (scroll wheel), double-click to re-frame.
* ``render_offscreen`` — one frame into a QImage without a window (tests,
  thumbnails).
"""

from __future__ import annotations

import math
from array import array
from typing import Callable

from PySide6.QtCore import Qt, QPoint
from shiboken6 import VoidPtr
from PySide6.QtGui import (
    QColor, QImage, QMatrix4x4, QOffscreenSurface, QOpenGLContext, QSurfaceFormat,
    QVector3D,
)
from PySide6.QtOpenGL import (
    QOpenGLBuffer, QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat,
    QOpenGLFunctions_3_3_Core, QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture,
    QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from Utils.nif.nif_reader import NifScene, NifShape

SOLID, WIRE, TEXTURED = "solid", "wire", "textured"

_GL_TRIANGLES = 0x0004
_GL_UNSIGNED_SHORT = 0x1403
_GL_FLOAT = 0x1406
_GL_LINE, _GL_FILL, _GL_FRONT_AND_BACK = 0x1B01, 0x1B02, 0x0408
_GL_DEPTH_TEST = 0x0B71
_GL_COLOR_BUFFER_BIT, _GL_DEPTH_BUFFER_BIT = 0x4000, 0x0100
_GL_TEXTURE0 = 0x84C0

_VERT = """
#version 330 core
layout(location = 0) in vec3 aPos;
layout(location = 1) in vec3 aNormal;
layout(location = 2) in vec2 aUV;
uniform mat4 uMVP;
uniform mat4 uView;
out vec3 vN;
out vec2 vUV;
void main() {
    gl_Position = uMVP * vec4(aPos, 1.0);
    vN = mat3(uView) * aNormal;
    vUV = aUV;
}
"""

_FRAG = """
#version 330 core
in vec3 vN;
in vec2 vUV;
uniform sampler2D uTex;
uniform int uTextured;
uniform vec3 uColor;
out vec4 frag;
void main() {
    vec3 n = normalize(vN);
    if (!gl_FrontFacing) n = -n;                  // meshes are two-sided
    float d = max(dot(n, normalize(vec3(0.25, 0.35, 1.0))), 0.0);
    float light = 0.32 + 0.68 * d;
    vec4 base = (uTextured == 1) ? texture(uTex, vUV) : vec4(uColor, 1.0);
    if (uTextured == 1 && base.a < 0.35) discard;
    frag = vec4(base.rgb * light, 1.0);
}
"""


def compute_normals(pos: array, idx: array) -> array:
    """Smooth per-vertex normals from triangle geometry (for meshes that ship none)."""
    n = array("f", bytes(4 * len(pos)))
    for t in range(0, len(idx) - 2, 3):
        a, b, c = idx[t] * 3, idx[t + 1] * 3, idx[t + 2] * 3
        ux, uy, uz = pos[b] - pos[a], pos[b + 1] - pos[a + 1], pos[b + 2] - pos[a + 2]
        vx, vy, vz = pos[c] - pos[a], pos[c + 1] - pos[a + 1], pos[c + 2] - pos[a + 2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        for o in (a, b, c):
            n[o] += nx; n[o + 1] += ny; n[o + 2] += nz
    for i in range(0, len(n), 3):
        ln = math.sqrt(n[i] ** 2 + n[i + 1] ** 2 + n[i + 2] ** 2) or 1.0
        n[i] /= ln; n[i + 1] /= ln; n[i + 2] /= ln
    return n


class _Drawable:
    """GPU-side copy of one shape."""
    def __init__(self, shape: NifShape, image: "QImage | None"):
        self.shape = shape
        self.image = image
        self.vao = self.vbo = self.ibo = self.tex = None
        self.count = len(shape.indices)


class OrbitCamera:
    """Orbit camera around a target point, Z-up."""
    DEFAULT_YAW = math.radians(-35)
    DEFAULT_PITCH = math.radians(20)

    def __init__(self):
        self.target = QVector3D(0, 0, 0)
        self.radius = 1.0
        self.dist = 3.0
        self.yaw = self.DEFAULT_YAW
        self.pitch = self.DEFAULT_PITCH

    def frame(self, bounds):
        if bounds is None:
            self.target, self.radius = QVector3D(0, 0, 0), 1.0
        else:
            lo, hi = bounds
            self.target = QVector3D((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2,
                                    (lo[2] + hi[2]) / 2)
            self.radius = max(0.5 * math.dist(lo, hi), 1e-3)
        self.dist = self.radius * 2.6
        self.yaw, self.pitch = self.DEFAULT_YAW, self.DEFAULT_PITCH

    def orbit(self, dx: float, dy: float):
        self.yaw -= dx * 0.008
        self.pitch = max(-1.55, min(1.55, self.pitch + dy * 0.008))

    def zoom(self, steps: float):
        self.dist = max(self.radius * 0.05,
                        min(self.radius * 40, self.dist * (0.88 ** steps)))

    def eye(self) -> QVector3D:
        cp = math.cos(self.pitch)
        return self.target + self.dist * QVector3D(
            cp * math.cos(self.yaw), cp * math.sin(self.yaw), math.sin(self.pitch))

    def matrices(self, aspect: float):
        view = QMatrix4x4()
        view.lookAt(self.eye(), self.target, QVector3D(0, 0, 1))
        proj = QMatrix4x4()
        proj.perspective(40.0, max(aspect, 1e-3), max(self.radius * 0.01, 1e-3),
                         max(self.dist + self.radius * 6, 10.0))
        return view, proj

    def pan(self, view: QMatrix4x4, dx: float, dy: float):
        right = QVector3D(view.row(0).x(), view.row(0).y(), view.row(0).z())
        up = QVector3D(view.row(1).x(), view.row(1).y(), view.row(1).z())
        k = self.dist * 0.0016
        self.target += (-dx * k) * right + (dy * k) * up


class MeshRenderer:
    """Draws a NifScene into the current GL context. Every method needs that
    context current (the widget's paintGL, or render_offscreen)."""

    def __init__(self):
        self.gl: "QOpenGLFunctions_3_3_Core | None" = None
        self.prog: "QOpenGLShaderProgram | None" = None
        self.error = ""
        self.bg = QColor(34, 34, 34)
        self._scene: "NifScene | None" = None
        self._images: dict[int, "QImage | None"] = {}
        self._drawables: list[_Drawable] = []
        self._dirty = False

    def initialize(self, owner) -> bool:
        try:
            self.gl = QOpenGLFunctions_3_3_Core()
            if not self.gl.initializeOpenGLFunctions():
                raise RuntimeError("OpenGL 3.3 core functions unavailable")
            prog = QOpenGLShaderProgram(owner)
            if not (prog.addShaderFromSourceCode(QOpenGLShader.Vertex, _VERT)
                    and prog.addShaderFromSourceCode(QOpenGLShader.Fragment, _FRAG)
                    and prog.link()):
                raise RuntimeError("shader build failed: " + prog.log())
            self.prog = prog
            return True
        except Exception as exc:                       # pragma: no cover - driver specific
            self.error = str(exc)
            return False

    def set_scene(self, scene: "NifScene | None", images: "dict[int, QImage | None]"):
        self._scene, self._images, self._dirty = scene, images, True

    def release(self):
        for d in self._drawables:
            for o in (d.tex, d.vbo, d.ibo, d.vao):
                if o is not None:
                    o.destroy()
        self._drawables = []

    def _upload(self):
        self.release()
        self._dirty = False
        if self._scene is None:
            return
        p = self.prog
        for i, sh in enumerate(self._scene.shapes):
            if not sh.indices or not len(sh.positions):
                continue
            d = _Drawable(sh, self._images.get(i))
            nv = len(sh.positions) // 3
            normals = sh.normals if sh.normals is not None and len(sh.normals) == 3 * nv \
                else compute_normals(sh.positions, sh.indices)
            uvs = sh.uvs if sh.uvs is not None and len(sh.uvs) == 2 * nv \
                else array("f", bytes(8 * nv))
            inter = array("f")
            for v in range(nv):
                inter.extend(sh.positions[3 * v:3 * v + 3])
                inter.extend(normals[3 * v:3 * v + 3])
                inter.extend(uvs[2 * v:2 * v + 2])
            d.vao = QOpenGLVertexArrayObject()
            d.vao.create()
            d.vao.bind()
            d.vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
            d.vbo.create(); d.vbo.bind()
            d.vbo.allocate(inter.tobytes(), len(inter) * 4)
            d.ibo = QOpenGLBuffer(QOpenGLBuffer.IndexBuffer)
            d.ibo.create(); d.ibo.bind()
            d.ibo.allocate(sh.indices.tobytes(), len(sh.indices) * 2)
            for loc, off, size in ((0, 0, 3), (1, 12, 3), (2, 24, 2)):
                p.enableAttributeArray(loc)
                p.setAttributeBuffer(loc, _GL_FLOAT, off, size, 8 * 4)
            d.vao.release()
            if d.image is not None and not d.image.isNull():
                d.tex = QOpenGLTexture(d.image.convertToFormat(QImage.Format_RGBA8888))
                d.tex.setMinificationFilter(QOpenGLTexture.LinearMipMapLinear)
                d.tex.setMagnificationFilter(QOpenGLTexture.Linear)
                d.tex.setWrapMode(QOpenGLTexture.Repeat)
                d.tex.generateMipMaps()
            self._drawables.append(d)

    def render(self, view: QMatrix4x4, proj: QMatrix4x4, mode: str):
        gl, p = self.gl, self.prog
        if gl is None or p is None:
            return
        if self._dirty:
            self._upload()
        gl.glClearColor(self.bg.redF(), self.bg.greenF(), self.bg.blueF(), 1.0)
        gl.glClear(_GL_COLOR_BUFFER_BIT | _GL_DEPTH_BUFFER_BIT)
        gl.glEnable(_GL_DEPTH_TEST)
        p.bind()
        p.setUniformValue("uMVP", proj * view)
        p.setUniformValue("uView", view)
        # Ints via glUniform1i: setUniformValue(name, 0) picks the unsigned overload,
        # which GL rejects for a sampler (INVALID_OPERATION).
        gl.glUniform1i(p.uniformLocation("uTex"), 0)
        gl.glPolygonMode(_GL_FRONT_AND_BACK, _GL_LINE if mode == WIRE else _GL_FILL)
        for d in self._drawables:
            if d.shape.is_effect and mode != WIRE:
                continue                                  # glow/FX planes hide the mesh
            use_tex = mode == TEXTURED and d.tex is not None
            gl.glUniform1i(p.uniformLocation("uTextured"), 1 if use_tex else 0)
            p.setUniformValue("uColor", QVector3D(0.55, 0.85, 0.95) if mode == WIRE
                              else QVector3D(0.72, 0.74, 0.78))
            if use_tex:
                gl.glActiveTexture(_GL_TEXTURE0)
                d.tex.bind()
            d.vao.bind()
            # Offset 0 into the bound index buffer. It must be a shiboken VoidPtr: a
            # plain 0 is rejected and ctypes.c_void_p(0) silently draws nothing.
            gl.glDrawElements(_GL_TRIANGLES, d.count, _GL_UNSIGNED_SHORT, VoidPtr(0))
            d.vao.release()
            if use_tex:
                d.tex.release()
        gl.glPolygonMode(_GL_FRONT_AND_BACK, _GL_FILL)
        p.release()


class MeshViewport(QOpenGLWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        fmt.setDepthBufferSize(24)
        fmt.setSamples(4)
        self.setFormat(fmt)
        self.setMinimumSize(200, 200)
        self._renderer = MeshRenderer()
        self._camera = OrbitCamera()
        self._scene: "NifScene | None" = None
        self._mode = SOLID
        self._last: "QPoint | None" = None

    # -- public -----------------------------------------------------------------
    def set_scene(self, scene: "NifScene | None",
                  texture_for: "Callable[[NifShape], QImage | None] | None" = None):
        """Show *scene*; *texture_for* supplies each shape's diffuse image."""
        self._scene = scene
        images = {}
        if scene is not None and texture_for is not None:
            for i, sh in enumerate(scene.shapes):
                images[i] = texture_for(sh)
        self._renderer.set_scene(scene, images)
        self._camera.frame(scene.bounds() if scene else None)
        self.update()

    def set_mode(self, mode: str):
        if mode != self._mode:
            self._mode = mode
            self.update()

    def mode(self) -> str:
        return self._mode

    def error(self) -> str:
        """Non-empty if OpenGL setup failed (so the caller can explain/fall back)."""
        return self._renderer.error

    # -- interaction --------------------------------------------------------------
    def mousePressEvent(self, e):
        self._last = e.position().toPoint()

    def mouseReleaseEvent(self, e):
        self._last = None

    def mouseMoveEvent(self, e):
        if self._last is None:
            return
        pt = e.position().toPoint()
        dx, dy = pt.x() - self._last.x(), pt.y() - self._last.y()
        self._last = pt
        if e.buttons() & Qt.LeftButton:
            self._camera.orbit(dx, dy)
        elif e.buttons() & (Qt.RightButton | Qt.MiddleButton):
            view, _ = self._camera.matrices(self.width() / max(self.height(), 1))
            self._camera.pan(view, dx, dy)
        self.update()

    def wheelEvent(self, e):
        steps = e.angleDelta().y() / 120.0
        if steps:
            self._camera.zoom(steps)
            self.update()
            e.accept()

    def mouseDoubleClickEvent(self, e):
        self._camera.frame(self._scene.bounds() if self._scene else None)
        self.update()

    # -- GL -------------------------------------------------------------------------
    def initializeGL(self):
        self._renderer.initialize(self)
        ctx = self.context()
        if ctx is not None:
            ctx.aboutToBeDestroyed.connect(self._release)

    def _release(self):
        self.makeCurrent()
        self._renderer.release()
        self.doneCurrent()

    def paintGL(self):
        view, proj = self._camera.matrices(self.width() / max(self.height(), 1))
        self._renderer.render(view, proj, self._mode)


def render_offscreen(scene: NifScene, images: "dict[int, QImage | None]",
                     mode: str = SOLID, size: int = 512,
                     camera: "OrbitCamera | None" = None) -> "QImage | None":
    """Render one frame without a window. Returns None if no GL context is
    available. Needs a QGuiApplication."""
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    ctx = QOpenGLContext()
    ctx.setFormat(fmt)
    if not ctx.create():
        return None
    surf = QOffscreenSurface()
    surf.setFormat(ctx.format())
    surf.create()
    if not ctx.makeCurrent(surf):
        return None
    try:
        r = MeshRenderer()
        if not r.initialize(surf):
            return None
        ff = QOpenGLFramebufferObjectFormat()
        ff.setAttachment(QOpenGLFramebufferObject.CombinedDepthStencil)
        fbo = QOpenGLFramebufferObject(size, size, ff)
        fbo.bind()
        r.gl.glViewport(0, 0, size, size)
        cam = camera or OrbitCamera()
        if camera is None:
            cam.frame(scene.bounds())
        r.set_scene(scene, images)
        view, proj = cam.matrices(1.0)
        r.render(view, proj, mode)
        img = fbo.toImage()
        fbo.release()
        r.release()
        return img
    finally:
        ctx.doneCurrent()
