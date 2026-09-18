"""``include_siblings`` must drag the real per-mod container folder, not
whatever sits at the very top of the mod's relative path.

Reproduces the Combat Camera - Configurable bug on The Blood of Dawnwalker:
its archive is laid out as ``Data/CombatCamera/{Scripts,dlls,mod_settings.ini,
enabled.txt}`` — the real per-mod folder is "CombatCamera", one level below
an extra "Data" packaging wrapper. ``_sibling_container`` used to take only
the first path segment ("Data") as the container, deploying everything to
``ue4ss/Mods/Data/CombatCamera/...`` instead of the correct
``ue4ss/Mods/CombatCamera/...`` — verified against the real Vortex extension
for this game, whose equivalent rule (``take: "parent.parent"``) lands at
the un-nested path. DawnwalkerModMenu's in-game menu discovers a
configurable mod's directory assuming the standard ``ue4ss/Mods/<ModName>/``
layout, so the extra nesting made it fail to find Combat Camera's files at
all (``choices.lua: existing config file required``).
"""
from __future__ import annotations

from Games.ue5_game import UE5Game
from Utils.deploy.deploy import CustomRule


class _StubUE5Game(UE5Game):
    @property
    def name(self) -> str:
        return "Stub UE5 Game"

    @property
    def game_id(self) -> str:
        return "stub_ue5_game"

    @property
    def exe_name(self) -> str:
        return "Stub/Binaries/Win64/Stub.exe"

    @property
    def custom_routing_rules(self) -> list[CustomRule]:
        return [
            CustomRule(dest="Binaries/Win64/ue4ss/Mods",
                       folders=["scripts", "dlls"], include_siblings=True),
        ]

    # Isolate to just the custom rule under test so unrelated default rules
    # (shared pre-rules, binaries/content passthrough) can't claim entries.
    @property
    def ue5_routing_rules(self):
        return self._custom_rules_as_ue5_rules()


def _resolve(entries):
    game = _StubUE5Game()
    resolved = game._resolve_filemap_entries(entries)
    return {staged: (dest, final) for staged, _mod, dest, final in resolved}


def test_nested_wrapper_folder_is_dropped():
    """Combat Camera's exact layout: Data/CombatCamera/{Scripts,dlls,...}."""
    mod = "Combat Camera - Configurable"
    entries = [
        ("Data/CombatCamera/Scripts/main.lua", mod),
        ("Data/CombatCamera/dlls/main.dll", mod),
        ("Data/CombatCamera/mod_settings.ini", mod),
        ("Data/CombatCamera/enabled.txt", mod),
    ]
    resolved = _resolve(entries)

    for staged, (dest, final) in resolved.items():
        assert dest == "Binaries/Win64/ue4ss/Mods", staged
        assert final.startswith("CombatCamera/"), (staged, final)
        assert not final.startswith("Data/"), (staged, final)

    assert resolved["Data/CombatCamera/Scripts/main.lua"][1] == \
        "CombatCamera/Scripts/main.lua"
    assert resolved["Data/CombatCamera/mod_settings.ini"][1] == \
        "CombatCamera/mod_settings.ini"


def test_flat_layout_falls_back_to_mod_name():
    """A mod with Scripts/ truly at its own archive root (no wrapper) must
    still land under a folder named after the mod — the pre-existing,
    already-correct behaviour for this shape must not regress."""
    mod = "FlatMod"
    entries = [
        ("Scripts/main.lua", mod),
        ("mod_settings.ini", mod),
    ]
    resolved = _resolve(entries)

    for staged, (dest, final) in resolved.items():
        assert dest == "Binaries/Win64/ue4ss/Mods", staged
        assert final.startswith("FlatMod/"), (staged, final)

    assert resolved["Scripts/main.lua"][1] == "FlatMod/Scripts/main.lua"
    assert resolved["mod_settings.ini"][1] == "FlatMod/mod_settings.ini"
