"""A requirement the game's own setup provides (Bloodlines' Unofficial Patch base
layer) must not show up as missing: not in the mod-list flag, not in the panel."""
from __future__ import annotations

from pathlib import Path

from gui_qt.modlist.modlist_data import FLAG_MISSING_REQS, read_meta_for_entries
from Utils.mods.modlist import ModEntry
from Utils.wine_proton.framework_detect import game_provides_requirement

UP = "Vampire The Masquerade - Bloodlines Unofficial Patch"


class _Game:
    def __init__(self, root: Path, frameworks: dict, external=()):
        self._root = root
        self.frameworks = frameworks
        self.externally_managed_frameworks = list(external)

    def get_game_path(self):
        return self._root


def _marker(root: Path, rel="Unofficial_Patch/resource/gameui_english.txt"):
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x")


def test_present_framework_provides_a_requirement_with_its_label_in_the_name(tmp_path):
    g = _Game(tmp_path, {"Unofficial Patch": "Unofficial_Patch/resource/gameui_english.txt"})
    assert not game_provides_requirement(g)(UP)            # not installed yet
    _marker(tmp_path)
    provided = game_provides_requirement(g)
    assert provided(UP)
    assert provided("unofficial patch")                    # case-insensitive
    assert not provided("Some Other Mod")
    assert not provided("")


def test_path_lookup_is_case_insensitive(tmp_path):
    g = _Game(tmp_path, {"Unofficial Patch": "Unofficial_Patch/resource/gameui_english.txt"})
    _marker(tmp_path, "unofficial_patch/Resource/GameUI_English.TXT")
    assert game_provides_requirement(g)(UP)


def test_externally_managed_framework_counts_without_a_file(tmp_path):
    g = _Game(tmp_path, {"Native Mod Loader": "x.dll"}, external=["Native Mod Loader"])
    assert game_provides_requirement(g)("Native Mod Loader (standalone)")


def test_games_without_frameworks_provide_nothing(tmp_path):
    assert not game_provides_requirement(_Game(tmp_path, {}))(UP)
    assert not game_provides_requirement(None)(UP)


def _mod(staging: Path, name: str, missing: str):
    d = staging / name
    d.mkdir(parents=True)
    (d / "meta.ini").write_text(
        f"[General]\nmodid = 234\ninstalled = 2026-01-01\nmissingRequirements = {missing}\n",
        encoding="utf-8")


def _flags(staging, provided):
    _v, _i, flags, _c, _u, _f, _b, missing, *_r = read_meta_for_entries(
        [ModEntry(name="HD Mod", enabled=True, locked=False)], staging, provided=provided)
    return flags.get("HD Mod", 0), missing


def test_requirement_provided_by_the_game_no_longer_raises_the_flag(tmp_path):
    staging = tmp_path / "staging"
    _mod(staging, "HD Mod", f"80:{UP}")
    flag, missing = _flags(staging, provided=None)
    assert flag & FLAG_MISSING_REQS and "HD Mod" in missing          # before: flagged

    g = _Game(tmp_path, {"Unofficial Patch": "Unofficial_Patch/resource/gameui_english.txt"})
    _marker(tmp_path)
    flag, missing = _flags(staging, provided=game_provides_requirement(g))
    assert not (flag & FLAG_MISSING_REQS) and "HD Mod" not in missing


def test_a_genuinely_missing_requirement_still_flags_the_mod(tmp_path):
    staging = tmp_path / "staging"
    _mod(staging, "HD Mod", f"80:{UP};999:Some Real Dependency")
    g = _Game(tmp_path, {"Unofficial Patch": "Unofficial_Patch/resource/gameui_english.txt"})
    _marker(tmp_path)
    flag, missing = _flags(staging, provided=game_provides_requirement(g))
    assert flag & FLAG_MISSING_REQS and "HD Mod" in missing


def test_requirements_panel_says_provided_instead_of_not_installed(tmp_path):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from gui_qt.views.requirements_view import RequirementsView

    app = QApplication.instance() or QApplication([])
    staging = tmp_path / "staging"
    d = staging / "HD Mod"
    d.mkdir(parents=True)
    (d / "meta.ini").write_text(
        f"[General]\nmodid = 234\ninstalled = 2026-01-01\n"
        f"nexusRequirements = 80:{UP};999:Some Real Dependency\n", encoding="utf-8")

    def rows(provided_fn):
        view = RequirementsView(staging_fn=lambda: staging, on_close=None,
                                provided_fn=provided_fn)
        view.current_mods = ["HD Mod"]
        view._repopulate()
        out = [view._requires_list.item(i).text()
               for i in range(view._requires_list.count())]
        view.close()
        app.processEvents()
        return out

    before = rows(None)
    assert f"{UP}  (not installed)" in before
    after = rows(lambda name: "unofficial patch" in name.lower())
    assert f"{UP}  (provided by the game setup)" in after
    assert "Some Real Dependency  (not installed)" in after       # others unchanged
