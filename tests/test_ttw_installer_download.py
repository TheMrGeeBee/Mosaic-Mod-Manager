"""The TTW Linux installer's GitHub releases have had no files since 2026; its
author publishes it on Nexus (site mod 1657). Real case, 2026-11-25: the TTW
wizard stopped at "No Linux installer asset found in the latest TTW release
(0.2.0)"."""
from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace as NS

import pytest

from Utils.modding_tools import ttw_tools as tt


def _zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mpi-installer-linux-x86_64/mpi_installer", b"#!linux")
        z.writestr("mpi-installer-linux-x86_64/mpi_installer_gui", b"#!gui")
        z.writestr("mpi-installer-windows-x86_64/mpi_installer.exe", b"MZ")
    return buf.getvalue()


def _files():
    return [NS(file_id=7956, category_name="OLD_VERSION", version="0.2.0", uploaded_timestamp=1),
            NS(file_id=9521, category_name="MAIN", version="0.2.1", uploaded_timestamp=3),
            NS(file_id=9000, category_name="MAIN", version="0.2.0b", uploaded_timestamp=2)]


def test_the_newest_main_file_is_taken():
    assert tt.nexus_main_file(_files()).file_id == 9521
    assert tt.nexus_main_file([_files()[0]]) is None


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(tt, "applications_dir", lambda game: tmp_path / "TTW")
    monkeypatch.setattr(tt, "_github_installer_url", lambda log: None)
    data = _zip_bytes()
    monkeypatch.setattr("Utils.ca_bundle.download_file",
                        lambda url, dest: dest.write_bytes(data))
    return tmp_path


def test_no_github_build_falls_back_to_nexus(setup, monkeypatch):
    asked = []

    class Api:
        def get_mod_files(self, domain, mod_id):
            return NS(files=_files())

        def get_download_links(self, domain, mod_id, file_id):
            asked.append((domain, mod_id, file_id))
            return [NS(URI="https://cdn/bb/ab/33/uuid")]   # no extension
    monkeypatch.setattr("Utils.mods.mod_install._build_nexus_api", lambda: Api())
    exe = tt.download_installer(None)
    assert asked == [("site", 1657, 9521)]
    assert exe == setup / "TTW" / "mpi_installer"
    assert exe.read_bytes() == b"#!linux"
    assert (setup / "TTW" / "mpi_installer_gui").is_file()
    assert not (setup / "TTW" / "mpi_installer.exe").exists()


def test_without_a_nexus_login_it_says_what_to_do(setup, monkeypatch):
    monkeypatch.setattr("Utils.mods.mod_install._build_nexus_api", lambda: None)
    with pytest.raises(RuntimeError, match="Log in to Nexus"):
        tt.download_installer(None)


def test_a_free_account_gets_the_manual_route(setup, monkeypatch):
    class Api:
        def get_mod_files(self, domain, mod_id):
            return NS(files=_files())

        def get_download_links(self, *a):
            raise RuntimeError("403 Forbidden")
    monkeypatch.setattr("Utils.mods.mod_install._build_nexus_api", lambda: Api())
    with pytest.raises(RuntimeError, match="Premium"):
        tt.download_installer(None)
