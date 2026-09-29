"""Test for Games/Baldur's Gate 3/modio_meta.py's ModuleInfo attribute
extraction decoding XML entities.

Caught live: a mod named "Better Hotbar [Vova's Edition]" displayed
everywhere as the literal "Better Hotbar [Vova&apos;s Edition]". BG3's own
LSX serializer escapes attribute values, so meta.lsx's ModuleInfo Name
attribute contains "&apos;" for a real apostrophe -- the regex-based
extractor returned that raw string without decoding it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_bg3(stem: str):
    mod_name = f"{stem}_bg3"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    bg3_dir = Path(__file__).resolve().parent.parent / "src" / "Games" / "Baldur's Gate 3"
    spec = importlib.util.spec_from_file_location(mod_name, str(bg3_dir / f"{stem}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_publish_handle_decodes_xml_entities_in_name():
    modio_meta = _load_bg3("modio_meta")
    meta_xml = (
        '<node id="ModuleInfo">'
        '<attribute id="PublishHandle" type="int64" value="4343518"/>'
        '<attribute id="Name" type="LSString" value="Better Hotbar [Vova&apos;s Edition]"/>'
        '<children></children>'
        '</node>'
    )

    handle, name = modio_meta.parse_publish_handle(meta_xml)

    assert handle == 4343518
    assert name == "Better Hotbar [Vova's Edition]"


def test_parse_publish_handle_decodes_other_common_entities():
    modio_meta = _load_bg3("modio_meta")
    meta_xml = (
        '<node id="ModuleInfo">'
        '<attribute id="PublishHandle" type="int64" value="1"/>'
        '<attribute id="Name" type="LSString" value="A &amp; B &quot;Mod&quot; &lt;Test&gt;"/>'
        '<children></children>'
        '</node>'
    )

    _handle, name = modio_meta.parse_publish_handle(meta_xml)

    assert name == 'A & B "Mod" <Test>'
