"""Every package in requirements-vendor.txt must also be in the Flatpak's
generated pypi-dependencies.yaml, or the Flatpak ships without it (the
AppImage pip-installs the requirements file directly, so only Flatpak breaks).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_flatpak_pypi_deps_cover_vendor_requirements():
    required = set()
    for line in (ROOT / "src" / "requirements-vendor.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            required.add(_norm(re.split(r"[=<>~!\[; ]", line, maxsplit=1)[0]))
    yaml_text = (ROOT / "flatpak" / "pypi-dependencies.yaml").read_text()
    present = {_norm(m) for m in re.findall(r"^  - name: python3-(\S+)", yaml_text, re.M)}
    assert required - present == set()
