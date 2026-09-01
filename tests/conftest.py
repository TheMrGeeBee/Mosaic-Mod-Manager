"""Make ``src/`` importable so tests can ``import Utils...`` / ``import Nexus...``.

The project has no installable package config — ``run_qt.sh`` runs from inside
``src/``, so the test suite reproduces that import root instead of introducing
packaging just for tests.

Run with the project-root ``.venv`` — the same venv ``run_qt.sh`` uses (its
``VENV="../.venv"``), which already carries PySide6, keyring and requests. Do
NOT use ``src/.venv``: that is the old Tk app's venv, has no PySide6 and is
missing keyring. See ``requirements-dev.txt`` for the exact commands.
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
