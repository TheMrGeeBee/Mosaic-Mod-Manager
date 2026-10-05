"""End-to-end tests for Utils.wabbajack.wabbajack_install against a fully
synthetic modlist: a real .wabbajack zip, real archives on disk, the real
VFS / directive engine / profile writing -- only the network is faked, by
swapping the downloader the dispatcher hands back."""
from __future__ import annotations

import json
import shutil
import threading
import zipfile

from Utils.mods.modlist import read_modlist
from Utils.profile.profile_state import read_wabbajack_modlist_info
from Utils.wabbajack import wabbajack_install as wi
from Utils.wabbajack.downloaders.http_source import WabbajackDownloadResult
from Utils.wabbajack.wabbajack_directives import path_substitutions
from Utils.wabbajack.wabbajack_hash import hash_file
from Utils.wabbajack.wabbajack_manifest import parse_modlist


class FakeGame:
    def __init__(self, staging):
        self.staging = staging

    def set_active_profile_dir(self, path):
        self.profile = path

    def load_paths(self):
        pass

    def get_effective_mod_staging_path(self):
        return self.staging


def _inline(to, data_id, kind="InlineFile"):
    return {"$type": f"{kind}, Wabbajack.Lib", "To": to, "Hash": "", "Size": 1,
            "SourceDataID": data_id}


def _from_archive(to, archive_hash, member, file_hash):
    return {"$type": "FromArchive, Wabbajack.Lib", "To": to, "Hash": file_hash, "Size": 1,
            "ArchiveHashPath": [archive_hash, member]}


def _build(tmp_path, *, archive_state="HttpDownloader", extra=None):
    """A source archive on 'the internet' plus a .wabbajack referencing it."""
    src = tmp_path / "internet" / "ModA.zip"
    src.parent.mkdir()
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("plugin.esp", b"ESP BYTES")
        zf.writestr("textures/a.dds", b"DDS BYTES")
    a_hash = hash_file(src)

    from Utils.wabbajack.wabbajack_hash import hash_bytes
    inline = {
        "cfg": b'{"setting": 1}',
        "mo2-modlist": b"+ModB\n+ModA\n*DLC: Dawnguard\n+NotBuilt\n",
        "plugins": b"*ModA.esp\n",
        "loadorder": b"Skyrim.esm\nModA.esp\n",
        "ini": b"sPath={--||GAME_PATH_MAGIC_BACK||--}\\Data\n",
        "mo2ini": b"[General]\n",
        "other": b"*Other.esp\n",
    }
    modlist = {
        "Name": "Test List", "Author": "Curator", "Version": "1.2", "GameType": "SkyrimSpecialEdition",
        "Archives": [{"Hash": a_hash, "Name": "ModA.zip", "Size": src.stat().st_size,
                      "State": {"$type": f"{archive_state}, Wabbajack.Lib",
                                "Url": "https://example.com/ModA.zip"}}],
        "Directives": [
            *(extra(a_hash) if extra else []),
            _from_archive("mods\\ModA\\ModA.esp", a_hash, "plugin.esp", hash_bytes(b"ESP BYTES")),
            _from_archive("mods\\ModA\\textures\\a.dds", a_hash, "textures/a.dds",
                          hash_bytes(b"DDS BYTES")),
            _inline("mods\\ModB\\config.json", "cfg"),
            _inline("profiles\\Main\\modlist.txt", "mo2-modlist"),
            _inline("profiles\\Main\\plugins.txt", "plugins"),
            _inline("profiles\\Main\\loadorder.txt", "loadorder"),
            _inline("profiles\\Main\\Skyrim.ini", "ini", kind="RemappedInlineFile"),
            _inline("ModOrganizer.ini", "mo2ini"),
            _inline("profiles\\Other\\plugins.txt", "other"),
        ],
    }
    wj = tmp_path / "list.wabbajack"
    with zipfile.ZipFile(wj, "w") as zf:
        zf.writestr("modlist", json.dumps(modlist))
        for k, v in inline.items():
            zf.writestr(k, v)
    return wj, parse_modlist(modlist), src


def _fake_http(src, calls=None):
    def download(state, dest, *, progress_cb=None, cancel=None):
        if calls is not None:
            calls.append(state.url)
        shutil.copyfile(src, dest)
        return WabbajackDownloadResult(success=True, file_path=dest)
    return download


def _run(tmp_path, wj, modlist, monkeypatch, *, downloader, callbacks=None, control=None):
    monkeypatch.setattr(wi, "resolve_downloader", lambda state: downloader)
    rebuilt = []
    monkeypatch.setattr(wi, "_rebuild_index", lambda game, profile_dir, log: rebuilt.append(1))
    staging = tmp_path / "staging" / "mods"
    profile = tmp_path / "staging" / "profiles" / "Test"
    profile.mkdir(parents=True)
    report = wi.run_wabbajack_install(
        wabbajack_path=wj, modlist=modlist, game=FakeGame(staging), profile_dir=profile,
        download_dir=tmp_path / "downloads",
        substitutions=path_substitutions(game_path="Z:\\Games\\Skyrim"),
        callbacks=callbacks, control=control)
    return report, staging, profile, rebuilt


# ---------------------------------------------------------------------------
# classify_directives
# ---------------------------------------------------------------------------

def test_classify_normalises_separators_and_picks_profile_with_modlist(tmp_path):
    _, modlist, _ = _build(tmp_path)
    plan = wi.classify_directives(modlist)
    assert sorted(d.to for d in plan.mod_directives) == [
        "ModA/ModA.esp", "ModA/textures/a.dds", "ModB/config.json"]
    assert plan.profile_name == "Main"
    assert sorted(d.to for d in plan.profile_directives) == [
        "Skyrim.ini", "loadorder.txt", "modlist.txt", "plugins.txt"]
    assert sorted(plan.unplaced) == ["ModOrganizer.ini", "profiles/Other/plugins.txt"]
    assert len(plan.archive_hashes()) == 1


# ---------------------------------------------------------------------------
# full install
# ---------------------------------------------------------------------------

def test_full_install_builds_mods_profile_and_provenance(tmp_path, monkeypatch):
    wj, modlist, src = _build(tmp_path)
    report, staging, profile, rebuilt = _run(
        tmp_path, wj, modlist, monkeypatch, downloader=_fake_http(src))

    assert report.ok, (report.failed_archives, report.failed_directives)
    assert report.installed_mods == ["ModA", "ModB"]
    assert (staging / "ModA" / "ModA.esp").read_bytes() == b"ESP BYTES"
    assert (staging / "ModA" / "textures" / "a.dds").read_bytes() == b"DDS BYTES"
    assert (staging / "ModB" / "config.json").read_bytes() == b'{"setting": 1}'

    # Curator's order kept; unmanaged DLC and never-built mods dropped.
    assert [e.name for e in read_modlist(profile / "modlist.txt")] == ["ModB", "ModA"]
    assert (profile / "plugins.txt").read_bytes() == b"*ModA.esp\n"
    assert (profile / "loadorder.txt").read_bytes() == b"Skyrim.esm\nModA.esp\n"

    # Other profile files are kept aside (with paths substituted), not applied.
    held = profile / wi.PROFILE_FILES_DIR / "Skyrim.ini"
    assert held.read_bytes() == b"sPath=Z:\\Games\\Skyrim\\Data\n"
    assert report.held_profile_files == ["Skyrim.ini"]
    assert sorted(report.unplaced_files) == ["ModOrganizer.ini", "profiles/Other/plugins.txt"]

    assert "modlist = Test List" in (staging / "ModA" / "meta.ini").read_text()
    assert read_wabbajack_modlist_info(profile)["name"] == "Test List"
    assert not (profile / ".wabbajack_work").exists()
    assert rebuilt == [1]


def test_cached_archive_is_reused_without_downloading(tmp_path, monkeypatch):
    wj, modlist, src = _build(tmp_path)
    (tmp_path / "downloads").mkdir()
    shutil.copyfile(src, tmp_path / "downloads" / "ModA.zip")
    calls = []
    report, *_ = _run(tmp_path, wj, modlist, monkeypatch, downloader=_fake_http(src, calls))
    assert report.ok
    assert calls == []


def test_hash_mismatch_falls_back_to_manual_download(tmp_path, monkeypatch):
    wj, modlist, src = _build(tmp_path)

    def corrupt(state, dest, **kw):
        dest.write_bytes(b"truncated")
        return WabbajackDownloadResult(success=True, file_path=dest)

    asked = []

    def manual(archive, why):
        asked.append((archive.name, why))
        return src  # the user picks the real file

    report, staging, *_ = _run(
        tmp_path, wj, modlist, monkeypatch, downloader=corrupt,
        callbacks=wi.WabbajackInstallCallbacks(request_manual_download=manual))
    assert asked == [("ModA.zip", "downloaded file doesn't match the modlist's hash")]
    assert report.ok
    assert (staging / "ModA" / "ModA.esp").is_file()


def test_unavailable_archive_fails_its_files_but_not_the_rest(tmp_path, monkeypatch):
    wj, modlist, _ = _build(tmp_path)
    report, staging, profile, _ = _run(
        tmp_path, wj, modlist, monkeypatch,
        downloader=lambda state, dest, **kw: WabbajackDownloadResult(success=False, error="404"),
        callbacks=wi.WabbajackInstallCallbacks(request_manual_download=lambda a, why: None))
    assert not report.ok
    assert report.failed_archives == [("ModA.zip", "404")]
    assert {to for to, _ in report.failed_directives} == {"ModA/ModA.esp", "ModA/textures/a.dds"}
    assert report.installed_mods == ["ModB"]
    assert [e.name for e in read_modlist(profile / "modlist.txt")] == ["ModB"]


def test_loverslab_login_requested_then_retried(tmp_path, monkeypatch):
    wj, modlist, src = _build(tmp_path, archive_state="LoversLabDownloader")
    attempts = []

    def needs_login_first(state, dest, **kw):
        attempts.append(1)
        if len(attempts) == 1:
            return WabbajackDownloadResult(success=False, error="log in", needs_auth=True)
        shutil.copyfile(src, dest)
        return WabbajackDownloadResult(success=True, file_path=dest)

    logins = []
    report, *_ = _run(
        tmp_path, wj, modlist, monkeypatch, downloader=needs_login_first,
        callbacks=wi.WabbajackInstallCallbacks(
            request_loverslab_login=lambda: logins.append(1) or True))
    assert report.ok
    assert logins == [1]
    assert len(attempts) == 2


def test_cancel_before_start_returns_cancelled(tmp_path, monkeypatch):
    wj, modlist, src = _build(tmp_path)
    control = wi.WabbajackInstallControl(cancel=threading.Event())
    control.cancel.set()
    report, staging, *_ = _run(
        tmp_path, wj, modlist, monkeypatch, downloader=_fake_http(src), control=control)
    assert report.cancelled
    assert not (staging / "ModA").exists()


def test_install_rebuilds_bsa_from_temp_files(tmp_path, monkeypatch):
    """CreateBSA listed *before* its inputs still runs after them; the
    rebuilt archive's hash differs from the curator's (different
    compressor), which is logged, not a failure."""
    from Utils.archives.bsa_extract import extract_bsa
    from Utils.wabbajack.wabbajack_hash import hash_bytes

    def extra(a_hash):
        return [
            {"$type": "CreateBSA, Wabbajack.Lib", "To": "mods\\ModC\\ModC.bsa",
             "Hash": "curator-archive-hash==", "Size": 1, "TempID": "abc123",
             "State": {"$type": "BSAState, Compression.BSA", "Magic": "BSA\u0000",
                       "Version": 105, "ArchiveFlags": 0x7, "FileFlags": 0x3},
             "FileStates": [
                 {"$type": "BSAFileState, Compression.BSA", "Path": "meshes\\x.nif",
                  "Index": 0, "FlipCompression": False},
                 {"$type": "BSAFileState, Compression.BSA", "Path": "textures\\y.dds",
                  "Index": 1, "FlipCompression": True}]},
            _from_archive("TEMP_BSA_FILES\\abc123\\meshes\\x.nif", a_hash, "plugin.esp",
                          hash_bytes(b"ESP BYTES")),
            _from_archive("TEMP_BSA_FILES\\abc123\\textures\\y.dds", a_hash,
                          "textures/a.dds", hash_bytes(b"DDS BYTES")),
        ]

    wj, modlist, src = _build(tmp_path, extra=extra)
    logs = []
    report, staging, profile, _ = _run(
        tmp_path, wj, modlist, monkeypatch, downloader=_fake_http(src),
        callbacks=wi.WabbajackInstallCallbacks(on_log=logs.append))

    assert report.ok, (report.failed_archives, report.failed_directives)
    assert report.installed_mods == ["ModA", "ModB", "ModC"]
    assert not any("TEMP_BSA_FILES" in u for u in report.unplaced_files)
    extract_bsa(staging / "ModC" / "ModC.bsa", tmp_path / "out")
    assert (tmp_path / "out" / "meshes" / "x.nif").read_bytes() == b"ESP BYTES"
    assert (tmp_path / "out" / "textures" / "y.dds").read_bytes() == b"DDS BYTES"
    assert any("isn't byte-identical" in line for line in logs)
    assert not (profile / ".wabbajack_work").exists()
