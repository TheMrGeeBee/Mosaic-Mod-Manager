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
    QVector3D, QVector4D,
)
from PySide6.QtOpenGL import (
    QOpenGLBuffer, QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat,
    QOpenGLFunctions_3_3_Core, QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture,
    QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from Utils.nif.nif_reader import NifNode, NifScene, NifShape
from Utils.nif.skeleton import bone_segments, is_bone

SOLID, WIRE, TEXTURED = "solid", "wire", "textured"

_GL_TRIANGLES = 0x0004
_GL_UNSIGNED_SHORT = 0x1403
_GL_FLOAT = 0x1406
_GL_LINE, _GL_FILL, _GL_FRONT_AND_BACK = 0x1B01, 0x1B02, 0x0408
_GL_DEPTH_TEST = 0x0B71
_GL_BLEND = 0x0BE2
_GL_SRC_ALPHA, _GL_ONE_MINUS_SRC_ALPHA = 0x0302, 0x0303
_GL_COLOR_BUFFER_BIT, _GL_DEPTH_BUFFER_BIT = 0x4000, 0x0100
_GL_TEXTURE0 = 0x84C0
_GL_POINTS, _GL_LINES = 0x0000, 0x0001
_GL_PROGRAM_POINT_SIZE = 0x8642

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
uniform float uAlpha;
out vec4 frag;
void main() {
    vec3 n = normalize(vN);
    if (!gl_FrontFacing) n = -n;                  // meshes are two-sided
    float d = max(dot(n, normalize(vec3(0.25, 0.35, 1.0))), 0.0);
    float light = 0.32 + 0.68 * d;
    vec4 base = (uTextured == 1) ? texture(uTex, vUV) : vec4(uColor, 1.0);
    if (uTextured == 1 && base.a < 0.35) discard;
    frag = vec4(base.rgb * light, uAlpha);
}
"""


# Flat-colour lines and points (the skeleton), drawn over the meshes.
_LINE_VERT = """
#version 330 core
layout(location = 0) in vec3 aPos;
uniform mat4 uMVP;
uniform float uPoint;
void main() {
    gl_Position = uMVP * vec4(aPos, 1.0);
    gl_PointSize = uPoint;
}
"""

_LINE_FRAG = """
#version 330 core
uniform vec4 uColor;
out vec4 frag;
void main() { frag = uColor; }
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


# QOpenGLTexture's destructor asks whether the *current* context shares with the
# one the texture was created in — through a raw pointer that dangles once that
# context is gone. A widget's context dies when its tab closes, and by then Qt has
# already invalidated the Python widget, so our cleanup can't run; the texture
# wrapper is then freed later by the garbage collector (often while another
# view's context is current) and the destructor dereferences the dead context:
# a segfault. So a texture wrapper is never left to the garbage collector while
# it still owns a GPU texture: it is kept here until destroy() has really run
# (textureId() == 0). If its context is already gone it stays here for good —
# a small Python-side leak, while the driver frees the GPU texture itself.
_LIVE_TEXTURES: "set[QOpenGLTexture]" = set()


def _destroy_texture(tex: "QOpenGLTexture | None"):
    """Destroy *tex* if its context is current; otherwise leave it registered."""
    if tex is None:
        return
    if QOpenGLContext.currentContext() is not None:
        tex.destroy()
    if tex.textureId() == 0:                     # really gone: safe to let go
        _LIVE_TEXTURES.discard(tex)


class _Drawable:
    """GPU-side copy of one shape."""
    def __init__(self, shape: NifShape, image: "QImage | None"):
        self.shape = shape
        self.image = image
        self.vao = self.vbo = self.ibo = self.tex = None
        self.count = len(shape.indices)


class OrbitCamera:
    """Orbit camera around a target point, Z-up.

    The distance is never stored: it is derived every frame from the object's
    size and the viewport's aspect ratio, times ``zoom_factor``. So "1.0" always
    means the object just fills the view at the default angle — whatever its
    size (a 4-unit marker or a 14,000-unit landscape piece) and however the
    window is shaped — and scroll-zoom is relative to that.

    With the object's vertices the fit is tight (the largest extent touches the
    margin, centred on what is visible); without them it falls back to the
    bounding sphere."""
    DEFAULT_YAW = math.radians(-35)
    DEFAULT_PITCH = math.radians(20)
    FOV = 40.0                       # vertical, degrees
    MARGIN = 1.04                    # breathing room around the fitted object
    MIN_ZOOM, MAX_ZOOM = 0.02, 4.0

    def __init__(self):
        self.target = QVector3D(0, 0, 0)
        self.radius = 1.0
        self.zoom_factor = 1.0
        self.yaw = self.DEFAULT_YAW
        self.pitch = self.DEFAULT_PITCH
        self._pts: list = []
        self._fit_cache: dict[float, float] = {}

    def frame(self, sphere, points=None):
        """Fit to *sphere* = ((x, y, z), radius) — or None to reset to a unit
        sphere — and reset zoom and angle. *points* (vertex samples) enable the
        tight fit and re-centre the target on the visible extent."""
        if sphere is None:
            self.target, self.radius = QVector3D(0, 0, 0), 1.0
            points = None
        else:
            (x, y, z), r = sphere
            self.target, self.radius = QVector3D(x, y, z), max(r, 1e-6)
        self.zoom_factor = 1.0
        self.yaw, self.pitch = self.DEFAULT_YAW, self.DEFAULT_PITCH
        self._pts = list(points) if points else []
        self._fit_cache = {}
        if self._pts:
            right, up, toward = self._basis()
            xs = [(p[0] - self.target.x()) * right[0] + (p[1] - self.target.y()) * right[1]
                  + (p[2] - self.target.z()) * right[2] for p in self._pts]
            ys = [(p[0] - self.target.x()) * up[0] + (p[1] - self.target.y()) * up[1]
                  + (p[2] - self.target.z()) * up[2] for p in self._pts]
            cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
            self.target = self.target + QVector3D(
                cx * right[0] + cy * up[0], cx * right[1] + cy * up[1],
                cx * right[2] + cy * up[2])

    def _basis(self):
        """(right, up, toward-camera) unit vectors at the default angle."""
        cp = math.cos(self.DEFAULT_PITCH)
        e = (cp * math.cos(self.DEFAULT_YAW), cp * math.sin(self.DEFAULT_YAW),
             math.sin(self.DEFAULT_PITCH))
        # right = normalize(cross(forward, worldUp)) with forward = -e, worldUp = +Z
        rx, ry = -e[1], e[0]
        n = math.hypot(rx, ry) or 1.0
        right = (rx / n, ry / n, 0.0)
        up = (e[1] * right[2] - e[2] * right[1], e[2] * right[0] - e[0] * right[2],
              e[0] * right[1] - e[1] * right[0])   # cross(toward, right)
        return right, up, e

    def fit_distance(self, aspect: float) -> float:
        """Camera distance at which the object just fits the view."""
        v = math.radians(self.FOV) / 2
        tan_v = math.tan(v)
        tan_h = max(aspect, 1e-3) * tan_v
        if not self._pts:
            return self.MARGIN * self.radius / math.sin(min(v, math.atan(tan_h)))
        key = round(aspect, 2)
        cached = self._fit_cache.get(key)
        if cached is None:
            right, up, toward = self._basis()
            t = self.target
            d = 0.0
            for p in self._pts:
                dx, dy, dz = p[0] - t.x(), p[1] - t.y(), p[2] - t.z()
                x = dx * right[0] + dy * right[1] + dz * right[2]
                y = dx * up[0] + dy * up[1] + dz * up[2]
                z = dx * toward[0] + dy * toward[1] + dz * toward[2]
                need = z + max(abs(x) / tan_h, abs(y) / tan_v)
                if need > d:
                    d = need
            # Never put the camera inside the object.
            cached = max(self.MARGIN * d, 1.5 * self.radius)
            self._fit_cache[key] = cached
        return cached

    def dist(self, aspect: float) -> float:
        return self.fit_distance(aspect) * self.zoom_factor

    def orbit(self, dx: float, dy: float):
        self.yaw -= dx * 0.008
        self.pitch = max(-1.55, min(1.55, self.pitch + dy * 0.008))

    def zoom(self, steps: float):
        self.zoom_factor = max(self.MIN_ZOOM, min(self.MAX_ZOOM,
                                                  self.zoom_factor * (0.88 ** steps)))

    def eye(self, aspect: float) -> QVector3D:
        cp = math.cos(self.pitch)
        return self.target + self.dist(aspect) * QVector3D(
            cp * math.cos(self.yaw), cp * math.sin(self.yaw), math.sin(self.pitch))

    def matrices(self, aspect: float):
        view = QMatrix4x4()
        view.lookAt(self.eye(aspect), self.target, QVector3D(0, 0, 1))
        proj = QMatrix4x4()
        d = self.dist(aspect)
        proj.perspective(self.FOV, max(aspect, 1e-3), max(self.radius * 0.004, 1e-4),
                         d + self.radius * 4)
        return view, proj

    def pan(self, view: QMatrix4x4, aspect: float, dx: float, dy: float):
        right = QVector3D(view.row(0).x(), view.row(0).y(), view.row(0).z())
        up = QVector3D(view.row(1).x(), view.row(1).y(), view.row(1).z())
        k = self.dist(aspect) * 0.0016
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
        self.line_prog: "QOpenGLShaderProgram | None" = None
        self._skeleton: "list[NifNode] | None" = None
        self._skel_dirty = False
        self._skel_gpu: list = []          # [(vao, vbo, vertex_count, mode)]
        self.show_skeleton = False

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
            lp = QOpenGLShaderProgram(owner)
            if not (lp.addShaderFromSourceCode(QOpenGLShader.Vertex, _LINE_VERT)
                    and lp.addShaderFromSourceCode(QOpenGLShader.Fragment, _LINE_FRAG)
                    and lp.link()):
                raise RuntimeError("line shader build failed: " + lp.log())
            self.line_prog = lp
            return True
        except Exception as exc:                       # pragma: no cover - driver specific
            self.error = str(exc)
            return False

    def set_scene(self, scene: "NifScene | None", images: "dict[int, QImage | None]"):
        self._scene, self._images, self._dirty = scene, images, True

    def set_skeleton(self, nodes: "list[NifNode] | None"):
        """Bones to draw over the meshes (None clears). See show_skeleton."""
        self._skeleton, self._skel_dirty = nodes, True

    def _free_skeleton(self):
        for vao, vbo, _n, _m in self._skel_gpu:
            vbo.destroy()
            vao.destroy()
        self._skel_gpu = []

    def _upload_skeleton(self):
        self._free_skeleton()
        self._skel_dirty = False
        if not self._skeleton:
            return
        lines, joints = bone_segments(self._skeleton)
        for data, mode in ((lines, _GL_LINES), (joints, _GL_POINTS)):
            if not len(data):
                continue
            vao = QOpenGLVertexArrayObject()
            vao.create()
            vao.bind()
            vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
            vbo.create(); vbo.bind()
            vbo.allocate(data.tobytes(), len(data) * 4)
            self.line_prog.enableAttributeArray(0)
            self.line_prog.setAttributeBuffer(0, _GL_FLOAT, 0, 3, 12)
            vao.release()
            self._skel_gpu.append((vao, vbo, len(data) // 3, mode))

    def release(self):
        self._free_skeleton()
        for d in self._drawables:
            _destroy_texture(d.tex)
            for o in (d.vbo, d.ibo, d.vao):
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
                _LIVE_TEXTURES.add(d.tex)
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
        # Solid shapes first; effect shapes (glows, editor markers, FX planes)
        # after, translucent, so they tint what is behind them instead of
        # hiding it — and a mesh made only of them (a marker) is still visible.
        for effect in (False, True):
            if effect:
                gl.glEnable(_GL_BLEND)
                gl.glBlendFunc(_GL_SRC_ALPHA, _GL_ONE_MINUS_SRC_ALPHA)
                gl.glDepthMask(False)
            for d in self._drawables:
                if d.shape.is_effect != effect:
                    continue
                use_tex = mode == TEXTURED and d.tex is not None and not effect
                gl.glUniform1i(p.uniformLocation("uTextured"), 1 if use_tex else 0)
                if mode == WIRE:
                    col, alpha = QVector3D(0.55, 0.85, 0.95), 1.0
                elif effect:
                    col, alpha = QVector3D(0.45, 0.72, 1.0), 0.6
                else:
                    col, alpha = QVector3D(0.72, 0.74, 0.78), 1.0
                p.setUniformValue("uColor", col)
                gl.glUniform1f(p.uniformLocation("uAlpha"), alpha)
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
            if effect:
                gl.glDepthMask(True)
                gl.glDisable(_GL_BLEND)
        gl.glPolygonMode(_GL_FRONT_AND_BACK, _GL_FILL)
        p.release()
        self._render_skeleton(view, proj)

    def _render_skeleton(self, view: QMatrix4x4, proj: QMatrix4x4):
        gl, lp = self.gl, self.line_prog
        if self._skel_dirty:
            self._upload_skeleton()
        if not self.show_skeleton or not self._skel_gpu or lp is None:
            return
        # Over everything, so the bones show through armour and skin.
        gl.glDisable(_GL_DEPTH_TEST)
        gl.glEnable(_GL_BLEND)
        gl.glBlendFunc(_GL_SRC_ALPHA, _GL_ONE_MINUS_SRC_ALPHA)
        gl.glEnable(_GL_PROGRAM_POINT_SIZE)
        lp.bind()
        lp.setUniformValue("uMVP", proj * view)
        for vao, _vbo, n, mode in self._skel_gpu:
            pts = mode == _GL_POINTS
            gl.glUniform1f(lp.uniformLocation("uPoint"), 7.0 if pts else 1.0)
            lp.setUniformValue("uColor", QVector4D(1.0, 0.55, 0.1, 1.0) if pts
                               else QVector4D(1.0, 0.85, 0.2, 0.95))
            vao.bind()
            gl.glDrawArrays(mode, 0, n)
            vao.release()
        lp.release()
        gl.glDisable(_GL_PROGRAM_POINT_SIZE)
        gl.glDisable(_GL_BLEND)
        gl.glEnable(_GL_DEPTH_TEST)


def _release_quietly(renderer: "MeshRenderer"):
    try:
        renderer.release()
    except Exception:
        pass


def fit_inputs(scene: "NifScene | None", skeleton: "list[NifNode] | None"):
    """(sphere, points) for OrbitCamera.frame(): the meshes plus, when given, the
    skeleton's bones — so a character's head bones aren't cut off by a headless
    body. (None, None) when there is nothing to frame."""
    pts = list(scene.sample_points()) if scene else []
    if skeleton:
        pts += [n.position for n in skeleton if is_bone(n)]
    if not pts:
        return None, None
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    c = tuple((lo[i] + hi[i]) / 2 for i in range(3))
    r = max(math.dist(c, p) for p in pts)
    return (c, max(r, 1e-6)), pts


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
        self._skeleton: "list[NifNode] | None" = None
        self._mode = SOLID
        self._last: "QPoint | None" = None

    # -- public -----------------------------------------------------------------
    def set_scene(self, scene: "NifScene | None",
                  texture_for: "Callable[[NifShape], QImage | None] | None" = None,
                  skeleton: "list[NifNode] | None" = None, reframe: bool = True):
        """Show *scene*; *texture_for* supplies each shape's diffuse image;
        *skeleton* (a skeleton NIF's nodes) is drawn as bones over the meshes and
        included in the framing. Either may be empty — a skeleton NIF on its own
        has no shapes. With *reframe* False the camera stays where the user put it
        (swapping a piece on a character shouldn't reset the view)."""
        self._scene = scene
        images = {}
        if scene is not None and texture_for is not None:
            for i, sh in enumerate(scene.shapes):
                images[i] = texture_for(sh)
        self._renderer.set_scene(scene, images)
        self._skeleton = skeleton or None
        self._renderer.set_skeleton(self._skeleton)
        self._renderer.show_skeleton = bool(self._skeleton)
        if reframe:
            self._frame_scene()
        self.update()

    def _frame_scene(self):
        """Fit the camera to the meshes and, when shown, the skeleton's bones."""
        sphere, pts = fit_inputs(self._scene, self._skeleton)
        self._camera.frame(sphere, pts)

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
            aspect = self.width() / max(self.height(), 1)
            view, _ = self._camera.matrices(aspect)
            self._camera.pan(view, aspect, dx, dy)
        self.update()

    def wheelEvent(self, e):
        steps = e.angleDelta().y() / 120.0
        if steps:
            self._camera.zoom(steps)
            self.update()
            e.accept()

    def mouseDoubleClickEvent(self, e):
        self._frame_scene()
        self.update()

    # -- GL -------------------------------------------------------------------------
    def initializeGL(self):
        self._renderer.initialize(self)
        ctx = self.context()
        if ctx is not None:
            # Deliberately NOT a bound method of the widget: by the time the
            # context dies (the tab closed) Qt has invalidated the Python widget,
            # and calling makeCurrent() on it only raises. The renderer alone
            # cleans up; textures it can't destroy stay registered (see
            # _LIVE_TEXTURES) instead of crashing later.
            renderer = self._renderer
            ctx.aboutToBeDestroyed.connect(lambda: _release_quietly(renderer))

    def paintGL(self):
        view, proj = self._camera.matrices(self.width() / max(self.height(), 1))
        self._renderer.render(view, proj, self._mode)


def render_offscreen(scene: NifScene, images: "dict[int, QImage | None]",
                     mode: str = SOLID, size: "int | tuple[int, int]" = 512,
                     camera: "OrbitCamera | None" = None,
                     skeleton: "list[NifNode] | None" = None) -> "QImage | None":
    """Render one frame without a window. *size* is a square edge or (w, h).
    Returns None if no GL context is available. Needs a QGuiApplication."""
    w, h = (size, size) if isinstance(size, int) else size
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
        fbo = QOpenGLFramebufferObject(w, h, ff)
        fbo.bind()
        r.gl.glViewport(0, 0, w, h)
        cam = camera or OrbitCamera()
        if camera is None:
            cam.frame(*fit_inputs(scene, skeleton))
        r.set_scene(scene, images)
        if skeleton:
            r.set_skeleton(skeleton)
            r.show_skeleton = True
        view, proj = cam.matrices(w / h)
        r.render(view, proj, mode)
        img = fbo.toImage()
        fbo.release()
        r.release()
        return img
    finally:
        ctx.doneCurrent()
