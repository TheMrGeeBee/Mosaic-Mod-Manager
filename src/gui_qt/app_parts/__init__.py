"""Method groups split out of ``gui_qt.app.MainWindow``.

Each module defines the group as a class literally named ``MainWindow`` and
``app.py`` imports it under an alias (``... import MainWindow as _XMixin``).
The name is deliberate: ``self.tr()`` resolves its translation context from the
runtime class (``MainWindow``) while ``pyside6-lupdate`` takes it from the
enclosing ``class`` statement, so a differently named mixin would file its
strings under the wrong context and orphan every existing translation.
Qt ``Signal`` declarations, ``__init__`` and shared state stay in ``app.py``.
"""
