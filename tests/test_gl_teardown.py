"""Regression: a segfault when a view's OpenGL context died before its textures.

Found from a real crash dump (2026-09-27): closing a Character tab and opening it
again freed the old view's QOpenGLTexture wrappers while another context was
current; Qt's destructor dereferenced the dead context. The scenario runs in a
subprocess so a regression fails this test instead of killing the test run.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

SCRIPT = textwrap.dedent('''
    import os, sys, gc
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, {src!r})
    from array import array
    from PySide6.QtGui import (QGuiApplication, QImage, QColor, QOpenGLContext,
                               QOffscreenSurface, QSurfaceFormat)
    from Utils.nif.nif_reader import NifScene, NifShape
    from gui_qt.nif_viewer.gl_viewport import MeshRenderer, OrbitCamera, TEXTURED, _LIVE_TEXTURES
    app = QGuiApplication([])

    def make_ctx():
        fmt = QSurfaceFormat(); fmt.setVersion(3, 3); fmt.setProfile(QSurfaceFormat.CoreProfile)
        c = QOpenGLContext(); c.setFormat(fmt)
        if not c.create():
            print("SKIP"); sys.exit(0)
        s = QOffscreenSurface(); s.setFormat(c.format()); s.create()
        if not c.makeCurrent(s):
            print("SKIP"); sys.exit(0)
        return c, s

    img = QImage(4, 4, QImage.Format_RGBA8888); img.fill(QColor("red"))
    shape = NifShape("t", array("f", [0, 0, 0, 1, 0, 0, 0, 1, 0]), None,
                     array("f", [0, 0, 1, 0, 0, 1]), array("H", [0, 1, 2]))
    ctx_a, surf_a = make_ctx()
    r = MeshRenderer()
    assert r.initialize(surf_a)
    r.set_scene(NifScene([shape]), {{0: img}})
    cam = OrbitCamera(); cam.frame(((0, 0, 0), 1.0))
    view, proj = cam.matrices(1.0)
    r.render(view, proj, TEXTURED)
    assert len(_LIVE_TEXTURES) == 1                # the texture is registered while it lives

    # A view's context dies WITHOUT the renderer having released anything, and a
    # different context is current when the renderer is finally garbage collected.
    ctx_a.doneCurrent(); del ctx_a, surf_a
    ctx_b, surf_b = make_ctx()
    del r; gc.collect()
    assert len(_LIVE_TEXTURES) == 1                # kept, not freed behind Qt's back

    # The normal path still cleans up: released while its own context is current.
    r2 = MeshRenderer()
    assert r2.initialize(surf_b)
    r2.set_scene(NifScene([shape]), {{0: img}}); r2.render(view, proj, TEXTURED)
    assert len(_LIVE_TEXTURES) == 2
    r2.release()
    assert len(_LIVE_TEXTURES) == 1                # destroyed for real, so unregistered
    print("survived")
''').format(src=str(SRC))


def test_texture_wrappers_never_outlive_their_context_unsafely():
    proc = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=120)
    out = proc.stdout.strip().splitlines()
    if out and out[-1] == "SKIP":
        import pytest
        pytest.skip("no OpenGL 3.3 core context available")
    assert proc.returncode == 0, f"crashed (exit {proc.returncode}):\n{proc.stderr[-2000:]}"
    assert out and out[-1] == "survived", proc.stdout + proc.stderr[-2000:]
