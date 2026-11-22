"""Skyrim SE 1.7.104: the bundled Steam build entry and the SKSE wizard's choices."""
from __future__ import annotations

from Games.Bethesda.skyrim_se import SkyrimSE
from Utils.steam import depot_versions as dv


def test_bundled_table_lists_1_7_104_with_the_installed_build_manifests():
    table = dv.load_table("489830")
    entry = table.get("1.7.104")
    assert entry is not None
    assert entry.exe_version == "1.7.104.0"
    assert entry.depots == {"489831": "4940892828028256588",
                            "489832": "5728778377666085157",
                            "489833": "4886117324142477814"}
    assert "build 24914197" in entry.title
    # Builds stay in ascending order, so the picker reads old to new.
    assert [e.label for e in table.versions] == ["1.5.97", "1.6.640", "1.6.1170", "1.7.104"]


def test_the_older_builds_keep_their_manifests():
    table = dv.load_table("489830")
    assert table.get("1.6.1170").depots["489833"] == "1914580699073641964"
    assert "SKSE64 2.2.x" in table.get("1.6.1170").title


def _skse_versions():
    tool = next(t for t in SkyrimSE().wizard_tools if t.id == "install_se_skyrimse")
    return tool.extra["versions"]


def test_skse_wizard_offers_1_7_104_first_through_its_nexus_page():
    versions = _skse_versions()
    first = versions[0]
    assert "1.7.104" in first["label"]
    # SKSE for 1.7.104 is only published on Nexus: no GitHub or direct URL to fetch,
    # so the wizard falls back to its open-the-page-and-locate flow.
    assert not first.get("github_api_url") and not first.get("direct_download_url")
    assert first["download_url"] == "https://www.nexusmods.com/skyrimspecialedition/mods/30379?tab=files"
    # Nexus names the file "...(SKSE64)-30379-2-3-1-<stamp>.7z".
    name = "skyrim script extender (skse64)-30379-2-3-1-1780000000.7z"
    assert all(k in name for k in first["archive_keywords"])
    assert not all(k in "skyrim script extender (skse64)-30379-2-2-6-1705522967.7z"
                   for k in first["archive_keywords"])


def test_skse_wizard_keeps_the_older_builds_and_no_longer_calls_1_6_1170_current():
    versions = _skse_versions()
    labels = [v["label"] for v in versions]
    assert any("1.6.1170" in lb and "older" in lb for lb in labels)
    assert not any("1.6.1170" in lb and "current" in lb for lb in labels)
    assert any("GOG 1.6.1179" in lb for lb in labels)
    assert any("1.5.97" in lb for lb in labels)
    gh = next(v for v in versions if "1.6.1170" in v["label"])
    assert gh["github_api_url"].endswith("/releases/latest")
