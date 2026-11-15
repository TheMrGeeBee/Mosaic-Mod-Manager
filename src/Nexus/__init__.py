"""
Nexus Mods integration package.

Provides API access, NXM protocol handling, and download management
for the Nexus Mods ecosystem.
"""

from .nexus_api import NexusAPI, NexusModUpdateInfo
from .nexus_download import NexusDownloader
from .nexus_meta import (
           NexusModMeta,
           build_meta_from_download,
           read_meta,
           scan_installed_mods,
           write_meta,
)
from .nexus_requirements import (
           MissingRequirementInfo,
           check_missing_requirements,
           check_requirements_from_gql,
)
from .nexus_update_checker import UpdateInfo, check_for_updates
from .nxm_handler import NxmHandler, NxmIPC, NxmLink

__all__ = ["NexusAPI", "NexusModUpdateInfo", "NxmHandler", "NxmLink", "NxmIPC", "NexusDownloader",
           "NexusModMeta", "read_meta", "write_meta", "build_meta_from_download",
           "scan_installed_mods", "check_for_updates", "UpdateInfo",
           "check_missing_requirements", "check_requirements_from_gql", "MissingRequirementInfo"]
