"""Make ``src/`` importable so tests can ``import Utils...`` / ``import Nexus...``.

The project has no installable package config — ``run_qt.sh`` runs from inside
``src/``, so the test suite reproduces that import root instead of introducing
packaging just for tests.

Run with the repo-root ``.venv`` (created with ``--system-site-packages`` so it
inherits keyring/requests/PySide6 from the system interpreter). Do NOT use
``src/.venv`` — that one is stale and missing ``keyring``. See
``requirements-dev.txt`` for the exact commands.
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
