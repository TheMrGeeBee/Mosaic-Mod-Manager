"""BG3 Nexus requirements satisfied by a pak UUID under another Nexus id.

CPCCE's Nexus page requires "ImpUI (ImprovedUI)"; the user runs "ImpUI P8
Fork" (a different Nexus mod id, same module UUID 26922ba9-...).  The Nexus
id check alone flagged CPCCE as missing ImpUI — a false alarm, because BG3
resolves dependencies by UUID.
"""
from __future__ import annotations

from Utils.mods import bg3_requirements
from Utils.mods.bg3_requirements import (
    LazyPakRequirementCheck,
    normalize_requirement_name,
    pak_satisfied_requirement_names,
)
from Utils.mods.modsettings import BG3ModInfo, parse_meta_lsx

IMPUI = "26922ba9-6018-5252-075d-7ff2ba6ed879"

_CPCCE_META = f"""<?xml version="1.0" encoding="UTF-8"?>
<save><region id="Config"><node id="root"><children>
  <node id="Dependencies"><children>
    <node id="ModuleShortDesc">
      <attribute id="Name" type="LSString" value="ImpUI (ImprovedUI)"/>
      <attribute id="UUID" type="guid" value="{IMPUI}"/>
    </node>
  </children></node>
  <node id="ModuleInfo">
    <attribute id="Name" type="LSString" value="CPCCE"/>
    <attribute id="Folder" type="LSString" value="CPCCE"/>
    <attribute id="UUID" type="FixedString" value="a2f25ffd-0c2f-4673-ad5d-bbe783e95564"/>
    <attribute id="Version64" type="int64" value="1"/>
  </node>
</children></node></region></save>"""


def _info(uuid, name, dep_names=None):
    dep_names = dep_names or {}
    return BG3ModInfo(uuid=uuid, name=name, folder=name, version64="1",
                      dependencies=list(dep_names), dependency_names=dep_names,
                      source_mod=name)


def test_parse_meta_lsx_records_dependency_names():
    info = parse_meta_lsx(_CPCCE_META)
    assert info.dependency_names == {IMPUI: "ImpUI (ImprovedUI)"}


def test_normalize_ignores_case_and_punctuation():
    assert normalize_requirement_name("ImpUI (ImprovedUI)") == \
        normalize_requirement_name("impui improvedui")


def _patch_scan(monkeypatch, infos):
    monkeypatch.setattr(bg3_requirements, "scan_mod_paks",
                        lambda *a, **k: {i.uuid: i for i in infos})


def test_fork_with_same_uuid_satisfies_requirement(tmp_path, monkeypatch):
    _patch_scan(monkeypatch, [
        _info("a2f25ffd", "CPCCE", {IMPUI: "ImpUI (ImprovedUI)"}),
        _info(IMPUI, "ImpUI_P8_Fork"),
    ])
    names = pak_satisfied_requirement_names(tmp_path, ["CPCCE", "ImpUI P8 Fork"])
    assert normalize_requirement_name("ImpUI (ImprovedUI)") in names


def test_declared_but_uninstalled_dependency_stays_missing(tmp_path, monkeypatch):
    _patch_scan(monkeypatch, [
        _info("a2f25ffd", "CPCCE", {IMPUI: "ImpUI (ImprovedUI)"}),
    ])
    names = pak_satisfied_requirement_names(tmp_path, ["CPCCE"])
    assert normalize_requirement_name("ImpUI (ImprovedUI)") not in names


def test_lazy_check_is_bg3_only_and_scans_once(tmp_path, monkeypatch):
    calls = []

    def fake_scan(*a, **k):
        calls.append(1)
        return {IMPUI: _info(IMPUI, "ImpUI (ImprovedUI)")}

    monkeypatch.setattr(bg3_requirements, "scan_mod_paks", fake_scan)

    other = LazyPakRequirementCheck("skyrimspecialedition", tmp_path, [])
    assert other("ImpUI (ImprovedUI)") is False
    assert calls == []

    bg3 = LazyPakRequirementCheck("baldursgate3", tmp_path, ["ImpUI"])
    assert bg3("ImpUI (ImprovedUI)") is True
    assert bg3("Some Other Mod") is False
    assert calls == [1]
