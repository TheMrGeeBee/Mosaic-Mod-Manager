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


import pytest


@pytest.fixture(autouse=True)
def _offline_bg3_known_rules(tmp_path, monkeypatch):
    """Keep tests off the network and away from the user's real config dir:
    the known-rules loader would otherwise start a background refresh from
    GitHub and read ~/.config/MosaicModManager/bg3_known_rules.json."""
    try:
        from Utils.mods import bg3_known_rules as K
    except Exception:
        return
    monkeypatch.setattr(K, "start_refresh", lambda log_fn=None: None)
    monkeypatch.setattr(K, "_cache_path", lambda: tmp_path / "bg3_known_rules.cache.json")

