"""Root-flagged mods whose staging folders differ in case from the filemap's."""
from __future__ import annotations

from Utils.deploy.deploy_root import deploy_root_flagged_mods
from Utils.deploy.deploy_shared import LinkMode


def test_root_flagged_file_found_when_staging_case_differs(tmp_path):
    """Real case (LoreRim 2026-10-19): the mod holds Data/skse/plugins/ but the
    filemap names Data/SKSE/Plugins/, so EngineFixes.dll was never deployed."""
    staging = tmp_path / "mods"
    src = staging / "Engine Fixes" / "Data" / "skse" / "plugins" / "EngineFixes.dll"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"dll")
    game = tmp_path / "game"
    game.mkdir()
    filemap = tmp_path / "filemap_root.txt"
    filemap.write_text("Data/SKSE/Plugins/EngineFixes.dll\tEngine Fixes\n", encoding="utf-8")
    logs = []

    n = deploy_root_flagged_mods(filemap, game, staging, mode=LinkMode.COPY, log_fn=logs.append)

    assert n == 1
    assert (game / "Data" / "SKSE" / "Plugins" / "EngineFixes.dll").read_bytes() == b"dll"
    assert not any("source not found" in line for line in logs)
