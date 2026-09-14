"""Deploy must warn (not stay silent) when an enabled mod has an un-ignored
Nexus "missing requirement" that is STILL actually missing — the same
yellow-exclamation flag already shown in the Mods tab (FLAG_MISSING_REQS),
surfaced via game.add_deploy_warning() so it reaches the user as a toast
after deploy, not just a passive icon.

The stored missing_requirements string is a stale snapshot, not live truth
(real report: a BG3 profile with ImpUI genuinely installed, and BG3SE/Native
Mod Loader installed as native frameworks rather than staged Nexus mods,
both still flagged "missing" because nothing ever re-checks the cached
flag) — so _warn_missing_requirements cross-checks against currently
installed mods and the game's own frameworks banner before trusting it.

Utils.deploy.deploy_pipeline._warn_missing_requirements is game-agnostic
(every game shares meta.ini's missing_requirements field, add_deploy_warning(),
and the frameworks property), tested here directly against a minimal fake game.
"""
from __future__ import annotations

from Nexus.nexus_meta import NexusModMeta, write_meta
from Utils.deploy.deploy_pipeline import _warn_missing_requirements
from Utils.mods.modlist import ModEntry, write_modlist
from Utils.profile.profile_state import write_ignored_missing_requirements


class _FakeGame:
    def __init__(self, staging_dir, game_root=None, frameworks=None):
        self._staging_dir = staging_dir
        self._game_root = game_root
        self.frameworks = frameworks or {}
        self.warnings: list[str] = []

    def get_effective_mod_staging_path(self):
        return self._staging_dir

    def get_game_path(self):
        return self._game_root

    def add_deploy_warning(self, message: str) -> None:
        self.warnings.append(message)


def _install(staging_root, folder_name: str, missing_requirements: str = "",
            mod_id: int = 0):
    mod_dir = staging_root / folder_name
    mod_dir.mkdir(parents=True)
    write_meta(mod_dir / "meta.ini",
              NexusModMeta(mod_name=folder_name, mod_id=mod_id,
                          missing_requirements=missing_requirements))


def test_warns_for_enabled_mod_with_missing_requirement(tmp_path):
    staging = tmp_path / "mods"
    _install(staging, "Lune's Idle Expressions - BG3SX Addon",
             missing_requirements="12345:BG3SX")
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    write_modlist(profile_dir / "modlist.txt", [
        ModEntry(name="Lune's Idle Expressions - BG3SX Addon",
                enabled=True, locked=False),
    ])
    game = _FakeGame(staging)

    _warn_missing_requirements(game, profile_dir, log_fn=lambda _m: None)

    assert len(game.warnings) == 1
    assert "Lune's Idle Expressions - BG3SX Addon" in game.warnings[0]
    assert "BG3SX" in game.warnings[0]


def test_disabled_mod_is_not_warned_about(tmp_path):
    staging = tmp_path / "mods"
    _install(staging, "Some Mod", missing_requirements="1:Framework")
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    write_modlist(profile_dir / "modlist.txt", [
        ModEntry(name="Some Mod", enabled=False, locked=False),
    ])
    game = _FakeGame(staging)

    _warn_missing_requirements(game, profile_dir, log_fn=lambda _m: None)

    assert game.warnings == []


def test_ignored_mod_is_not_warned_about(tmp_path):
    staging = tmp_path / "mods"
    _install(staging, "Some Mod", missing_requirements="1:Framework")
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    write_modlist(profile_dir / "modlist.txt", [
        ModEntry(name="Some Mod", enabled=True, locked=False),
    ])
    write_ignored_missing_requirements(profile_dir, {"Some Mod"})
    game = _FakeGame(staging)

    _warn_missing_requirements(game, profile_dir, log_fn=lambda _m: None)

    assert game.warnings == []


def test_no_missing_requirements_produces_no_warning(tmp_path):
    staging = tmp_path / "mods"
    _install(staging, "Clean Mod")
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    write_modlist(profile_dir / "modlist.txt", [
        ModEntry(name="Clean Mod", enabled=True, locked=False),
    ])
    game = _FakeGame(staging)

    _warn_missing_requirements(game, profile_dir, log_fn=lambda _m: None)

    assert game.warnings == []


def test_requirement_satisfied_by_another_enabled_mod_is_not_warned_about(tmp_path):
    # "Better Map 0.7 scale" was installed before "ImpUI (ImprovedUI)" — its
    # stored flag says ImpUI is missing, but ImpUI is genuinely installed and
    # enabled now; the flag just never got refreshed. Cross-check by mod_id
    # must catch this without needing a fresh Nexus check.
    staging = tmp_path / "mods"
    _install(staging, "Better Map 0.7 scale",
             missing_requirements="999:ImpUI (ImprovedUI)")
    _install(staging, "ImpUI (ImprovedUI)", mod_id=999)
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    write_modlist(profile_dir / "modlist.txt", [
        ModEntry(name="Better Map 0.7 scale", enabled=True, locked=False),
        ModEntry(name="ImpUI (ImprovedUI)", enabled=True, locked=False),
    ])
    game = _FakeGame(staging)

    _warn_missing_requirements(game, profile_dir, log_fn=lambda _m: None)

    assert game.warnings == []


def test_requirement_satisfied_by_a_disabled_mod_still_warns(tmp_path):
    # Same as above, but the satisfying mod is disabled — it won't actually
    # be loaded, so the requirement is genuinely still unmet for this deploy.
    staging = tmp_path / "mods"
    _install(staging, "Better Map 0.7 scale",
             missing_requirements="999:ImpUI (ImprovedUI)")
    _install(staging, "ImpUI (ImprovedUI)", mod_id=999)
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    write_modlist(profile_dir / "modlist.txt", [
        ModEntry(name="Better Map 0.7 scale", enabled=True, locked=False),
        ModEntry(name="ImpUI (ImprovedUI)", enabled=False, locked=False),
    ])
    game = _FakeGame(staging)

    _warn_missing_requirements(game, profile_dir, log_fn=lambda _m: None)

    assert len(game.warnings) == 1


def test_native_framework_requirement_already_installed_is_not_warned_about(tmp_path):
    # "Mod Configuration Menu (MCM)" requires "Baldur's Gate 3 Script
    # Extender (BG3SE)" on Nexus, but Mosaic installs/verifies the Script
    # Extender itself as a native framework (bin/DWrite.dll), never as a
    # staged mod — it can never satisfy the mod_id cross-check, so the
    # frameworks banner check must catch it instead.
    staging = tmp_path / "mods"
    _install(staging, "Mod Configuration Menu (MCM)",
             missing_requirements="1:Baldur's Gate 3 Script Extender (BG3SE)")
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    write_modlist(profile_dir / "modlist.txt", [
        ModEntry(name="Mod Configuration Menu (MCM)", enabled=True, locked=False),
    ])
    game_root = tmp_path / "game"
    (game_root / "bin").mkdir(parents=True)
    (game_root / "bin" / "DWrite.dll").touch()
    game = _FakeGame(staging, game_root=game_root,
                     frameworks={"Script Extender": "bin/DWrite.dll"})

    _warn_missing_requirements(game, profile_dir, log_fn=lambda _m: None)

    assert game.warnings == []


def test_native_framework_requirement_genuinely_missing_still_warns(tmp_path):
    staging = tmp_path / "mods"
    _install(staging, "Mod Configuration Menu (MCM)",
             missing_requirements="1:Baldur's Gate 3 Script Extender (BG3SE)")
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    write_modlist(profile_dir / "modlist.txt", [
        ModEntry(name="Mod Configuration Menu (MCM)", enabled=True, locked=False),
    ])
    game_root = tmp_path / "game"
    game_root.mkdir()
    game = _FakeGame(staging, game_root=game_root,
                     frameworks={"Script Extender": "bin/DWrite.dll"})

    _warn_missing_requirements(game, profile_dir, log_fn=lambda _m: None)

    assert len(game.warnings) == 1
    assert "BG3SE" in game.warnings[0]


def test_multiple_affected_mods_combine_into_one_warning(tmp_path):
    staging = tmp_path / "mods"
    _install(staging, "Mod A", missing_requirements="1:Framework A")
    _install(staging, "Mod B", missing_requirements="2:Framework B")
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    write_modlist(profile_dir / "modlist.txt", [
        ModEntry(name="Mod A", enabled=True, locked=False),
        ModEntry(name="Mod B", enabled=True, locked=False),
    ])
    game = _FakeGame(staging)

    _warn_missing_requirements(game, profile_dir, log_fn=lambda _m: None)

    # One toast, not a flood — the log (not asserted here) still gets a
    # per-mod WARNING line for each.
    assert len(game.warnings) == 1
    assert "Mod A" in game.warnings[0] and "Mod B" in game.warnings[0]
