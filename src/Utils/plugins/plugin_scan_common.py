"""
Shared plugin-scan surface used by both the SkyGen and Plugin Audit wizards.

Both wizards resolve the active profile, read its load order, find plugin
files across the staging tree and build the cross-mod BOS/SkyPatcher patch
index.  The concrete implementations live in Utils.modding_tools.skygen_core (the richer
parser) and are re-exported here as the shared surface; Utils.plugins.plugin_audit_core
keeps its own copies of the header/new-record scanners it needs on top.
"""

from __future__ import annotations

from Utils.modding_tools.skygen_core import (  # noqa: F401
    BASE_GAME_PLUGINS,
    GLOBAL_IGNORE_PLUGINS,
)
from Utils.modding_tools.skygen_core import (
    _build_patch_index as build_patch_index,
)
from Utils.modding_tools.skygen_core import (
    _find_mod_folder as find_mod_folder,
)
from Utils.modding_tools.skygen_core import (
    _find_plugin_file as find_plugin_file,
)
from Utils.modding_tools.skygen_core import (
    _profile_dir as profile_dir,
)
from Utils.modding_tools.skygen_core import (
    _read_active_profile as read_active_profile,
)
from Utils.modding_tools.skygen_core import (
    _read_loadorder as read_loadorder,
)
