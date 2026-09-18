"""``vortex_override_instructions.json`` must never reach the filemap.

The file is Vortex's own per-mod installer-instructions format (pre-baked
``attribute``/``setmodtype`` directives its generic installer consumes at
install time) — never read by any game or mod loader, and never copied into
a real Vortex deploy. Mosaic had no awareness of it, so it fell through to
ordinary file handling and landed at a shared destination every mod
providing one collided on (e.g. "Better Journal 1.0 overrides Combat Camera"
on The Blood of Dawnwalker, even though the two mods are unrelated).
"""
from __future__ import annotations

from Utils.filemap import _scan_dir


def test_scan_dir_excludes_vortex_override_instructions(tmp_path):
    mod_dir = tmp_path / "Some Mod"
    mod_dir.mkdir()
    (mod_dir / "vortex_override_instructions.json").write_text(
        '[{"type": "setmodtype", "value": "dawnwalker-ue4ss"}]',
    )
    (mod_dir / "real_file.txt").write_text("content")

    _name, normal, _root, invalid = _scan_dir("Some Mod", str(mod_dir))

    assert "vortex_override_instructions.json" not in normal
    assert "real_file.txt" in normal
    assert invalid == []
