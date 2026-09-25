"""An empty <dependencies operator="Or"> is FALSE (any-of-nothing), an empty And is
true — as in MO2 and Vortex.

Real case: Glowing Mushroom Collision Fixes (Nexus 69558) gives its Mari option a
<dependencyType> whose only pattern is an EMPTY Or that would make it NotUsable.
Mosaic treated the empty Or as satisfied, so every option of that group was
NotUsable, the collection author's recorded choice was refused (the Origins
Reborn guard), no flags were set, every later step was hidden and the mod staged
nothing - deterministically, so 'press Install again' could never fix it.
"""
from __future__ import annotations

from pathlib import Path

from Utils.installers.fomod_installer import evaluate_dependency, resolve_files
from Utils.installers.fomod_parser import Dependency, parse_module_config


def _dep(operator, subs=()):
    return Dependency(dep_type="composite", operator=operator, flag_name="", flag_value="",
                      file_name="", file_state="Active", sub_deps=list(subs))


def test_empty_or_is_false_and_empty_and_is_true():
    assert evaluate_dependency(_dep("Or"), {}, set()) is False
    assert evaluate_dependency(_dep("or"), {}, set()) is False
    assert evaluate_dependency(_dep("And"), {}, set()) is True


XML = """<config>
  <moduleName>T</moduleName>
  <installSteps>
    <installStep name="Patches"><optionalFileGroups>
      <group name="Patches" type="SelectExactlyOne"><plugins>
        <plugin name="Mari"><description/>
          <conditionFlags><flag name="Mari">On</flag></conditionFlags>
          <typeDescriptor><dependencyType>
            <defaultType name="Optional"/>
            <patterns><pattern>
              <dependencies operator="Or"></dependencies>
              <type name="NotUsable"/>
            </pattern></patterns>
          </dependencyType></typeDescriptor>
        </plugin>
        <plugin name="Other"><description/>
          <conditionFlags><flag name="Other">On</flag></conditionFlags>
          <typeDescriptor><type name="Optional"/></typeDescriptor>
        </plugin>
      </plugins></group>
    </optionalFileGroups></installStep>
    <installStep name="Colour">
      <visible><flagDependency flag="Mari" value="On"/></visible>
      <optionalFileGroups><group name="Colour" type="SelectExactlyOne"><plugins>
        <plugin name="Blue"><description/>
          <conditionFlags><flag name="Blue">On</flag></conditionFlags>
          <typeDescriptor><type name="Optional"/></typeDescriptor>
        </plugin>
      </plugins></group></optionalFileGroups>
    </installStep>
  </installSteps>
  <conditionalFileInstalls><patterns><pattern>
    <dependencies operator="And">
      <flagDependency flag="Mari" value="On"/><flagDependency flag="Blue" value="On"/>
    </dependencies>
    <files><folder source="Mari\\Blue" destination="meshes" priority="0"/></files>
  </pattern></patterns></conditionalFileInstalls>
</config>"""


def test_a_recorded_choice_behind_an_empty_or_gate_is_replayed(tmp_path):
    cfg_path = tmp_path / "ModuleConfig.xml"
    cfg_path.write_text(XML, encoding="utf-8")
    cfg = parse_module_config(Path(cfg_path))
    selections = {"0": {"Patches": ["Mari"]}, "1": {"Colour": ["Blue"]}}
    files = resolve_files(cfg, selections, set(), set(), set())
    assert [(dst, is_folder) for _src, dst, is_folder in files] == [("meshes", True)]
    assert files[0][0].replace("\\", "/") == "Mari/Blue"


def test_a_genuinely_unusable_option_is_still_refused(tmp_path):
    """The Origins Reborn guard must keep working: a NotUsable that comes from a
    real (non-empty) condition still blocks a recorded choice."""
    xml = XML.replace('<dependencies operator="Or"></dependencies>',
                      '<dependencies operator="Or"><fileDependency file="Missing.esp" state="Missing"/>'
                      '<flagDependency flag="Never" value="On"/></dependencies>')
    # 'Missing.esp' is absent, so the Or holds -> NotUsable -> the choice is refused.
    cfg_path = tmp_path / "ModuleConfig.xml"
    cfg_path.write_text(xml, encoding="utf-8")
    cfg = parse_module_config(Path(cfg_path))
    selections = {"0": {"Patches": ["Mari"]}, "1": {"Colour": ["Blue"]}}
    assert resolve_files(cfg, selections, set(), set(), set()) == []
