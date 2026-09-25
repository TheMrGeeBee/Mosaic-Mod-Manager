"""An archive that IS a FOMOD, but whose ModuleConfig.xml will not parse, must be
installed verbatim - never probed as a BAIN package.

prepare_archive already kept BAIN mutually exclusive with FOMOD (it only probes
BAIN when no FOMOD was detected), but the collection installer's own branch chain
(``if prepared.is_fomod() ... elif supports_bain``) treated "FOMOD that failed to
parse" as "no FOMOD" and sent it to the BAIN picker: with an author selection
missing it prompted (or deferred) instead of installing what the archive holds.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from Utils.mods.mod_install import prepare_archive


class _FakeGame:
    supports_bain = True
    plugin_extensions = None

    def __init__(self, staging_dir: Path):
        self._staging_dir = staging_dir

    def get_effective_mod_staging_path(self):
        return self._staging_dir


def _archive(tmp_path: Path, *, config_xml: str) -> Path:
    """A FOMOD wrapper folder AND two BAIN-looking sub-packages side by side."""
    path = tmp_path / "mod.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("fomod/ModuleConfig.xml", config_xml)
        zf.writestr("00 Core/plugin.esp", "core")
        zf.writestr("01 Optional/plugin2.esp", "opt")
    return path


def _prepare(tmp_path, config_xml):
    logs = []
    prepared = prepare_archive(
        str(_archive(tmp_path, config_xml=config_xml)), _FakeGame(tmp_path / "mods"),
        tmp_path / "profile", log_fn=logs.append)
    assert prepared is not None, logs
    return prepared, logs


def test_unparseable_fomod_is_flagged_and_not_probed_as_bain(tmp_path):
    prepared, logs = _prepare(tmp_path, "<<< this is not xml")
    try:
        assert prepared.fomod_parse_failed is True
        assert not prepared.is_fomod()                      # it installs verbatim ...
        assert not getattr(prepared, "bain_subpkgs", None)  # ... and is not a BAIN package
        assert any("FOMOD parse failed" in m for m in logs)
    finally:
        prepared.cleanup()


def test_a_parseable_fomod_is_not_flagged(tmp_path):
    xml = ('<config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
           "<moduleName>M</moduleName></config>")
    prepared, _ = _prepare(tmp_path, xml)
    try:
        assert prepared.fomod_parse_failed is False
        assert prepared.is_fomod()
    finally:
        prepared.cleanup()


def test_a_plain_archive_is_not_flagged(tmp_path):
    path = tmp_path / "plain.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("plugin.esp", "x")
    prepared = prepare_archive(str(path), _FakeGame(tmp_path / "mods"), tmp_path / "profile",
                               log_fn=lambda _m: None)
    try:
        assert prepared.fomod_parse_failed is False
    finally:
        prepared.cleanup()


def test_collection_install_does_not_defer_an_unparseable_fomod_as_bain(tmp_path, monkeypatch):
    """The real regression: install_collection_archive sent it to the BAIN picker
    (returning the BAIN_DEFERRED sentinel to be prompted for at the end)."""
    import socket

    from Nexus.nexus_meta import NexusModMeta
    from Utils.installers.bain_installer import bain_unwrap_single_folder, detect_bain
    from Utils.mods.mod_install import BAIN_DEFERRED, install_collection_archive

    def no_network(*_a, **_k):
        raise AssertionError("this test must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)

    # Precondition: on its own, this layout really IS a BAIN package - so the test
    # only passes if the FOMOD flag is what keeps it out of the BAIN branch.
    probe = tmp_path / "probe"
    with zipfile.ZipFile(_archive(tmp_path, config_xml="x")) as zf:
        zf.extractall(probe)
    assert detect_bain(bain_unwrap_single_folder(str(probe))), "layout is no longer BAIN-shaped"

    logs = []
    result = install_collection_archive(
        str(_archive(tmp_path, config_xml="<<< not xml")), _FakeGame(tmp_path / "mods"),
        tmp_path / "profile", log_fn=logs.append, defer_interactive_bain=True,
        prebuilt_meta=NexusModMeta(mod_name="mod", mod_id=1, file_id=2, file_category="MAIN"))
    assert result is not BAIN_DEFERRED, logs
    assert not any("BAIN" in m for m in logs), logs
    assert result == "mod", logs                     # installed verbatim
