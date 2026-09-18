"""
blood_of_dawnwalker.py
Game handler for The Blood of Dawnwalker (Unreal Engine 5 / UE4SS).

Promoted from a custom-game JSON definition (deploy_type "ue5") to a real,
first-class Games/ module so game-specific deploy bugs can be fixed directly
in Python instead of patched around the generic custom-game engine.

Mod structure
-------------
Mods ship files destined for multiple locations in the game root:

  Engine/Game/Input/etc. .ini → Wine prefix AppData Saved/Config/Windows/
  .pak / .csv (+ .ucas/.utoc) → Content/Paks/~mods/
  LogicMods/ folder           → Content/Paks/LogicMods/
  ue4ss.dll / ue4ss-settings.ini
  dwmapi.dll / xinput1_x.dll  → Binaries/Win64/ (UE4SS loader, two install methods)
  ue4ss/ folder               → Binaries/Win64/ue4ss/
  Scripts/ + dlls/ folders    → Binaries/Win64/ue4ss/Mods/<ModName>/
  Bare .lua (no Scripts/)     → Binaries/Win64/ue4ss/Mods/<ModName>/
  enabled.txt / mods.txt      → Binaries/Win64/ue4ss/Mods/<ModName>/
  ReShade.ini + loader dll    → Binaries/Win64/
  reshade-shaders/ folder     → Binaries/Win64/reshade-shaders/
  Loose .ini (ReShade preset) → Binaries/Win64/
  Loose .dll (other plugins)  → Binaries/Win64/
  Windows/ folder             → Mods/Windows/ (save-adjacent Windows saves dir)

The game root itself lives inside the Steam/GOG/Epic install directory:
  <install_dir>/Dawnwalker/

Rule ordering matters — CustomRule matching is first-match-wins, so
more specific rules (exact ue4ss binaries, ReShade loader filenames) are
declared before the generic loose-.ini/.dll fallbacks that exist only to
catch whatever nothing else claimed.
"""

from __future__ import annotations

from pathlib import Path

from Games.ue5_game import UE5Game
from Utils.deploy.deploy import CustomRule

# Game root subfolder inside the Steam/GOG/Epic install directory
_GAME_SUBDIR = "Dawnwalker"

# Engine/editor .ini files that must land in the Wine prefix's virtual
# AppData Saved/Config/Windows folder, not the game install root.
_PREFIX_CONFIG_INI_NAMES = [
    "Engine.ini", "Game.ini", "Input.ini", "DeviceProfiles.ini",
    "GameUserSettings.ini", "GameUserFramegen.ini", "Scalability.ini",
    "RuntimeOptions.ini", "InstallBundle.ini", "Hardware.ini",
    "GameplayTags.ini", "Editor.ini", "EditorPerProjectUserSettings.ini",
    "EditorSettings.ini", "EditorKeyBindings.ini", "EditorLayout.ini",
    "Compat.ini", "Lightmass.ini",
]

_PREFIX_CONFIG_DEST = (
    "drive_c/users/steamuser/AppData/Local/Dawnwalker/Saved/Config/Windows"
)


class BloodOfDawnwalker(UE5Game):

    # -------------------------------------------------------------------
    # Identity
    # -------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "The Blood of Dawnwalker"

    @property
    def game_id(self) -> str:
        return "blood_of_dawnwalker"

    @property
    def exe_name(self) -> str:
        return "Dawnwalker/Binaries/Win64/Dawnwalker.exe"

    @property
    def steam_id(self) -> str:
        return "3751260"

    @property
    def nexus_game_domain(self) -> str:
        return "thebloodofdawnwalker"

    # -------------------------------------------------------------------
    # Mod-handling properties
    # -------------------------------------------------------------------

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return {"dawnwalker", "content", "paks", "~mods", "binaries",
                "win64", "mods"}

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"license", "*.md", "*read*.txt", "*.png", "*.html"}

    @property
    def filemap_casing_pins(self) -> dict[str, str]:
        # UE4SS's own "scripts" -> "Scripts" pin (UE5Game default) plus
        # Dawnwalker's top-level "mods" -> "Mods" folder under the game root
        # (distinct from Binaries/Win64/ue4ss/Mods, which UE4SS itself owns).
        pins = dict(super().filemap_casing_pins)
        pins["mods"] = "Mods"
        return pins

    # -------------------------------------------------------------------
    # Routing rules
    # -------------------------------------------------------------------

    @property
    def custom_routing_rules(self) -> list[CustomRule]:
        return [
            # Engine/editor config -> Wine prefix Saved/Config/Windows.
            CustomRule(dest=_PREFIX_CONFIG_DEST,
                       filenames=_PREFIX_CONFIG_INI_NAMES,
                       flatten=True, to_prefix=True),

            # Paks / streaming data -> Content/Paks/~mods.
            CustomRule(dest="Content/Paks/~mods", extensions=[".pak", ".csv"],
                       companion_extensions=[".ucas", ".utoc"],
                       include_siblings=True),

            # LogicMods (Blueprint paks) -> Content/Paks/LogicMods.
            CustomRule(dest="Content/Paks", folders=["LogicMods"],
                       flatten=True),

            # UE4SS loader, direct-DLL install method.
            CustomRule(dest="Binaries/Win64/ue4ss",
                       filenames=["ue4ss.dll", "ue4ss-settings.ini"],
                       flatten=True),

            # UE4SS loader, proxy-DLL injection install method (alternative
            # to the direct ue4ss.dll form above — some packages use this
            # instead; both land in the same place).
            CustomRule(dest="Binaries/Win64",
                       filenames=["dwmapi.dll", "xinput1_3.dll",
                                  "xinput1_4.dll"],
                       flatten=True),

            # A whole ue4ss/ folder shipped as-is -> Binaries/Win64/ue4ss.
            CustomRule(dest="Binaries/Win64", folders=["ue4ss"],
                       flatten=True),

            # UE4SS Lua mods: Scripts/ and/or dlls/ folders -> their own
            # named subfolder under ue4ss/Mods, dragging siblings
            # (mod_settings.ini, enabled.txt, etc.) along with them.
            CustomRule(dest="Binaries/Win64/ue4ss/Mods",
                       folders=["scripts", "dlls"], include_siblings=True),

            # Bare .lua files with no Scripts/ wrapper -> ue4ss/Mods, same
            # as above. Only reached for mods the previous rule didn't
            # already claim (i.e. no real Scripts/ folder exists).
            CustomRule(dest="Binaries/Win64/ue4ss/Mods",
                       extensions=[".lua"], include_siblings=True),

            # Loose enabled.txt / mods.txt not already dragged in above.
            CustomRule(dest="Binaries/Win64/ue4ss/Mods",
                       filenames=["enabled.txt", "mods.txt"],
                       include_siblings=True),

            # Windows/ folder (save-adjacent data) -> Mods/Windows.
            CustomRule(dest="Mods", folders=["Windows"],
                       include_siblings=True),

            # ReShade: its own ini plus whichever graphics-API DLL it's
            # loaded as -> Binaries/Win64, flat (no per-mod wrapper — these
            # must sit exactly where ReShade's own installer would put them).
            CustomRule(dest="Binaries/Win64",
                       filenames=["ReShade.ini", "dxgi.dll", "d3d11.dll",
                                  "d3d10.dll", "d3d9.dll", "opengl32.dll",
                                  "dinput8.dll"],
                       flatten=True),

            # ReShade's shader/texture package -> Binaries/Win64/reshade-shaders,
            # preserving the folder name ReShade's own file scan expects.
            CustomRule(dest="Binaries/Win64", folders=["reshade-shaders"],
                       flatten=True),

            # Any other loose top-level .ini not claimed above (e.g. a
            # ReShade preset's extra config) -> Binaries/Win64. loose_only
            # so it can't intercept a mod-specific .ini meant to ride along
            # with a Scripts/dlls sibling-drag above.
            CustomRule(dest="Binaries/Win64", extensions=[".ini"],
                       loose_only=True, flatten=True),

            # Any other native plugin DLL not claimed above -> Binaries/Win64.
            CustomRule(dest="Binaries/Win64", extensions=[".dll"],
                       flatten=True),
        ]

    # -------------------------------------------------------------------
    # Paths
    # -------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        """The actual game root is the Dawnwalker/ subfolder inside the
        Steam/GOG/Epic install directory. Looked up case-insensitively so it
        works on both Windows (via Proton) and Linux test setups."""
        if self._game_path is None:
            return None
        sub = self._game_path / _GAME_SUBDIR
        if sub.is_dir():
            return sub
        needle = _GAME_SUBDIR.lower()
        try:
            for child in self._game_path.iterdir():
                if child.is_dir() and child.name.lower() == needle:
                    return child
        except OSError:
            pass
        # Fallback: user pointed directly at the subfolder.
        return self._game_path
