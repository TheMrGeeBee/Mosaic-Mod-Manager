"""MainWindow's method-group modules must keep the ``MainWindow`` tr context.

``self.tr()`` looks up translations under the runtime class name (MainWindow)
but ``pyside6-lupdate`` reads the context from the enclosing ``class``
statement.  A mixin with any other name would file its strings under the
wrong context and orphan every existing (and upstream-synced) translation.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

PARTS = Path(__file__).resolve().parent.parent / "src" / "gui_qt" / "app_parts"
MODULES = sorted(p for p in PARTS.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_part_defines_single_class_named_mainwindow(path):
    classes = [n.name for n in ast.parse(path.read_text(encoding="utf-8")).body
               if isinstance(n, ast.ClassDef)]
    assert classes == ["MainWindow"], classes


def test_parts_are_wired_into_mainwindow(monkeypatch):
    import sys
    sys.path.insert(0, str(PARTS.parent.parent))
    from gui_qt.app import MainWindow
    mixins = {c.__module__ for c in MainWindow.__mro__}
    for p in MODULES:
        assert f"gui_qt.app_parts.{p.stem}" in mixins, p.name
