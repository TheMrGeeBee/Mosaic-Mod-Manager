"""Bloodlines Unofficial Patch extraction: the allow-list must never let the
installer's replacement Vampire/, Vampire.exe or Bin/*.dll files through."""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from Utils.modding_tools import vtmb_patch as v

_REAL = Path("/home/mrgeebee/Downloads/VTMBup120.6 80 12.0 2026-10-15T19-52Z sPCTRqnU.exe")

_PAYLOAD = [
    "app/Unofficial_Patch/python/fileutil.py",
    "app/Unofficial_Patch/cfg/config.cfg",
    "app/Patch_Extras/Player Mods/readme.txt",
    "app/Loader.exe",
    "app/Bin/loader/loader.ini",
    "app/Vampire.exe",                # patched 4 GB exe
    # must be ignored:
    "app/Vampire.exe.12",
    "app/Vampire/pack102.vpk",
    "app/Vampire/dlls/vampire.dll",
    "app/Bin/engine.dll",
    "app/VTMBup-readme.txt",
]


@pytest.mark.parametrize("rel,ok", [
    ("Unofficial_Patch/python/x.py", True),
    ("unofficial_patch/Python/x.py", True),
    ("Patch_Extras/a/b.txt", True),
    ("Loader.exe", True),
    ("Bin/loader/loader.ini", True),
    ("Bin/engine.dll", False),
    ("Bin/loader_evil/x", False),
    ("Vampire.exe", False),           # handled by install_exe, not copied
    ("vampire.exe", False),
    ("Vampire.exe.12", False),
    ("Vampire/pack102.vpk", False),
    ("vampire/dlls/vampire.dll", False),
    ("Unofficial_Patch/../Vampire/x", False),
    ("", False),
])
def test_is_allowed(rel, ok):
    assert v.is_allowed(rel) is ok


def test_find_installer_picks_newest_vtmbup_exe(tmp_path):
    a = tmp_path / "VTMBup120.5.exe"
    b = tmp_path / "VTMBup120.6 80 12.0 2026 hash.exe"
    (tmp_path / "other.exe").write_text("x")
    (tmp_path / "VTMBup-notes.txt").write_text("x")
    a.write_text("a")
    b.write_text("b")
    os.utime(a, (time.time() - 100, time.time() - 100))
    assert v.find_installer([tmp_path, tmp_path / "missing"]) == b


def _pe(laa: bool, tag: bytes = b"") -> bytes:
    """Minimal PE header with (or without) IMAGE_FILE_LARGE_ADDRESS_AWARE."""
    head = bytearray(256)
    head[0:2] = b"MZ"
    head[0x3C:0x40] = (0x80).to_bytes(4, "little")
    head[0x80:0x84] = b"PE\0\0"
    head[0x84:0x86] = (0x14C).to_bytes(2, "little")          # i386
    head[0x84 + 18:0x84 + 20] = (0x0102 | (0x20 if laa else 0)).to_bytes(2, "little")
    return bytes(head) + tag


def _fake_innoextract(tmp_path: Path) -> str:
    patched = tmp_path / "patched-installer-exe.bin"
    patched.write_bytes(_pe(True, b"advanced-exe"))
    listing = "\n".join(f' - "{p}" (1 B)' for p in _PAYLOAD)
    script = tmp_path / "innoextract"
    script.write_text(f"""#!/bin/sh
case "$*" in
  *--list*) cat <<'EOF'
Listing "Fake Patch"
{listing}
EOF
  exit 0;;
esac
dest=""; incs=""
while [ $# -gt 0 ]; do
  case "$1" in -d) dest="$2"; shift;; -I) incs="$incs $2"; shift;; esac
  shift
done
# A real innoextract only writes files matching an -I filter; honour it.
for p in {" ".join(f"'{p}'" for p in _PAYLOAD)}; do
  keep=0
  for i in $incs; do
    case "$p" in "$i"|"$i"/*) keep=1;; esac
  done
  if [ $keep = 1 ]; then
    mkdir -p "$dest/$(dirname "$p")"
    if [ "$p" = "app/Vampire.exe" ]; then cp '{patched}' "$dest/$p"; else echo "$p" > "$dest/$p"; fi
    echo " - \\"$p\\" (1 B)"
  fi
done
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _game_root(tmp_path: Path) -> Path:
    root = tmp_path / "game"
    (root / "Vampire").mkdir(parents=True)
    (root / "Vampire" / "pack102.vpk").write_text("vanilla")
    (root / "vampire.exe").write_bytes(_pe(False, b"steam-stub"))   # Steam's own
    return root


def test_install_patch_writes_only_allow_listed_files(tmp_path):
    root = _game_root(tmp_path)
    installer = tmp_path / "VTMBup120.6.exe"
    installer.write_text("x")
    n = v.install_patch(installer, root, _fake_innoextract(tmp_path))
    assert n == 6
    assert (root / "Unofficial_Patch" / "python" / "fileutil.py").is_file()
    assert (root / "Patch_Extras" / "Player Mods" / "readme.txt").is_file()
    assert (root / "Loader.exe").is_file()
    assert (root / "Bin" / "loader" / "loader.ini").is_file()
    # vanilla and patched-engine files stay untouched / absent
    assert (root / "Vampire" / "pack102.vpk").read_text() == "vanilla"
    assert not (root / "Vampire" / "dlls").exists()
    # Steam's exe is replaced IN PLACE (same name); no backup, no sibling copy
    exe = root / "vampire.exe"
    assert v.is_large_address_aware(exe) and exe.read_bytes().endswith(b"advanced-exe")
    assert sorted(p.name for p in root.iterdir() if "vampire.exe" in p.name.lower()) \
        == ["vampire.exe"]
    assert not (root / "Vampire.exe.12").exists()
    assert not list(root.glob("*.mosaic-new"))
    assert not (root / "Bin" / "engine.dll").exists()
    assert not (root / "VTMBup-readme.txt").exists()
    # staging directory cleaned up
    assert not [p for p in root.iterdir() if p.name.startswith(".mosaic-vtmb-")]


def test_install_patch_rerun_overwrites_in_place(tmp_path):
    root = _game_root(tmp_path)
    tool = _fake_innoextract(tmp_path)
    installer = tmp_path / "VTMBup120.6.exe"
    installer.write_text("x")
    target = root / "Unofficial_Patch" / "python" / "fileutil.py"
    target.parent.mkdir(parents=True)
    target.write_text("old")
    v.install_patch(installer, root, tool)
    assert target.read_text().strip() == "app/Unofficial_Patch/python/fileutil.py"


def test_install_patch_requires_game_folder_and_tool(tmp_path):
    installer = tmp_path / "VTMBup.exe"
    installer.write_text("x")
    with pytest.raises(v.PatchError):
        v.install_patch(installer, tmp_path, _fake_innoextract(tmp_path))
    with pytest.raises(v.PatchError):
        v.install_patch(installer, _game_root(tmp_path), tool="/nonexistent/innoextract")


def test_second_run_leaves_an_identical_patched_exe_alone(tmp_path):
    root = _game_root(tmp_path)
    installer = tmp_path / "VTMBup.exe"
    installer.write_text("x")
    tool = _fake_innoextract(tmp_path)
    v.install_patch(installer, root, tool)
    before = (root / "vampire.exe").stat().st_mtime_ns
    v.install_patch(installer, root, tool)
    assert (root / "vampire.exe").stat().st_mtime_ns == before   # "unchanged": not rewritten
    assert (root / "vampire.exe").read_bytes().endswith(b"advanced-exe")


def test_install_exe_refuses_a_non_4gb_replacement(tmp_path):
    root = _game_root(tmp_path)
    bad = tmp_path / "bad.exe"
    bad.write_bytes(_pe(False, b"not-patched"))
    with pytest.raises(v.PatchError, match="4 GB"):
        v.install_exe(bad, root)
    assert (root / "vampire.exe").read_bytes() == _pe(False, b"steam-stub")


def test_install_exe_updates_an_already_patched_exe(tmp_path):
    root = _game_root(tmp_path)
    (root / "vampire.exe").write_bytes(_pe(True, b"older-advanced"))
    newer = tmp_path / "new.exe"
    newer.write_bytes(_pe(True, b"newer-advanced"))
    assert v.install_exe(newer, root) == "updated"
    assert (root / "vampire.exe").read_bytes().endswith(b"newer-advanced")


def test_is_large_address_aware(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    a.write_bytes(_pe(True)); b.write_bytes(_pe(False)); c.write_bytes(b"not a pe")
    assert v.is_large_address_aware(a)
    assert not v.is_large_address_aware(b)
    assert not v.is_large_address_aware(c)
    assert not v.is_large_address_aware(tmp_path / "missing")


def test_assess(tmp_path):
    root = _game_root(tmp_path)
    a = v.assess(root)
    assert a == {"game_found": True, "patch_version": None,
                 "patch_version_in": None, "patch_installed": False,
                 "loader_present": False, "exe_found": True,
                 "exe_patched": False}
    (root / "Unofficial_Patch").mkdir()
    (root / "Unofficial_Patch" / "x").write_text("1")
    (root / "Loader.exe").write_text("1")
    (root / "Bin" / "loader").mkdir(parents=True)
    a = v.assess(root)
    assert a["patch_installed"] and a["loader_present"]


@pytest.mark.skipif(not (_REAL.is_file() and v.find_innoextract()),
                    reason="real Unofficial Patch installer / innoextract not present")
def test_real_installer_allow_list_excludes_vanilla_payload():
    files = v._list_files(_REAL, v.find_innoextract())
    allowed = [f for f in files if v.is_allowed(f)]
    assert sum(f.lower().startswith("unofficial_patch/") for f in allowed) > 10000
    assert "Loader.exe" in allowed
    assert "Vampire.exe" in files and "Vampire.exe" not in allowed   # via install_exe
    assert not any(f.lower().startswith("vampire") for f in allowed)
    assert not any(f.lower().startswith("bin/") and not f.lower().startswith("bin/loader/")
                   for f in allowed)
    # the vanilla-overwriting files really are in the installer (and skipped)
    assert any(f.lower().startswith("vampire/") for f in files)


def _write_version_file(folder: Path, text: str, encoding: str):
    f = folder / "resource" / "gameui_english.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(text.encode(encoding))


_UI = ('"lang"\r\n{\r\n"Language" "English"\r\n"Tokens"\r\n{\r\n'
       '"GameUI_Patch"\t\t"Unofficial Patch 12.0"\t\t// added by wesp\r\n}\r\n}\r\n')


@pytest.mark.parametrize("encoding", ["utf-16", "utf-8", "utf-8-sig"])
def test_read_installed_version_handles_utf16_and_utf8(tmp_path, encoding):
    root = _game_root(tmp_path)
    _write_version_file(root / "Unofficial_Patch", _UI, encoding)
    assert v.read_installed_version(root) == ("12.0", "Unofficial_Patch")
    assert v.assess(root)["patch_version"] == "12.0"


def test_version_found_in_vanilla_folder_for_a_prepatched_install(tmp_path):
    """A prepatched (GOG-style) install may carry the patch inside Vampire/."""
    root = _game_root(tmp_path)
    _write_version_file(root / "Vampire", _UI.replace("12.0", "11.8"), "utf-16")
    assert v.read_installed_version(root) == ("11.8", "Vampire")


def test_unofficial_patch_folder_wins_over_vampire_folder(tmp_path):
    root = _game_root(tmp_path)
    _write_version_file(root / "Vampire", _UI.replace("12.0", "11.8"), "utf-16")
    _write_version_file(root / "Unofficial_Patch", _UI, "utf-16")
    assert v.read_installed_version(root) == ("12.0", "Unofficial_Patch")


def test_no_version_when_the_file_is_missing_or_has_no_patch_line(tmp_path):
    root = _game_root(tmp_path)
    assert v.read_installed_version(root) is None
    _write_version_file(root / "Unofficial_Patch", '"lang" { }', "utf-16")
    assert v.read_installed_version(root) is None
    assert v.assess(root)["patch_version"] is None


def test_version_lookup_is_case_insensitive(tmp_path):
    root = _game_root(tmp_path)
    f = root / "unofficial_patch" / "Resource"
    f.mkdir(parents=True)
    (f / "GameUI_English.TXT").write_bytes(_UI.encode("utf-16"))
    assert v.read_installed_version(root) == ("12.0", "Unofficial_Patch")


@pytest.mark.parametrize("a,b,newer", [
    ("12.0", "12.0", False), ("11.9", "12.0", True), ("12.0", "12.1", True),
    ("12.1", "12.0", False), ("9.9", "12.0", True),   # numeric, not string, order
])
def test_version_tuple_orders_numerically(a, b, newer):
    assert (v.version_tuple(a) < v.version_tuple(b)) is newer


def test_parse_version_from_titles():
    assert v.parse_version('Listing "Vampire: The Masquerade - Bloodlines '
                           'Unofficial Patch 12.0" - setup data') == "12.0"
    assert v.parse_version("nothing here") is None


def test_installer_version_from_fake_tool_header(tmp_path):
    tool = tmp_path / "innoextract"
    tool.write_text('#!/bin/sh\necho \'Listing "Bloodlines Unofficial Patch 12.3"\'\n')
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    installer = tmp_path / "VTMBup.exe"
    installer.write_text("x")
    assert v.installer_version(installer, str(tool)) == "12.3"
    assert v.installer_version(tmp_path / "missing.exe", str(tool)) is None


@pytest.mark.skipif(not (_REAL.is_file() and v.find_innoextract()),
                    reason="real Unofficial Patch installer / innoextract not present")
def test_real_installer_reports_its_version():
    assert v.installer_version(_REAL) == "12.0"


@pytest.mark.parametrize("name,expected", [
    ("VTMBup115rc9.1.exe", "11.5"),                              # the real collection entry
    ("VTMBup120.6.exe", "12.0"),
    ("VTMBup120.6 80 12.0 2026-10-15T19-52Z sPCTRqnU.exe", "12.0"),   # Nexus-named download
    ("vtmbup105.exe", "10.5"),
    ("VTMBup-readme.txt", None),
    ("VTMBup95.exe", None),                                      # too short to mean anything
    ("SomeOtherPatch115.exe", None),
    ("", None),
])
def test_version_from_installer_name(name, expected):
    assert v.version_from_installer_name(name) == expected


@pytest.mark.parametrize("installed,wanted,met", [
    ("12.0", "11.5", True),      # newer installed than the collection asks for
    ("12.0", "12.0", True),      # equal
    ("11.5", "12.0", False),     # older installed: the collection's patch is newer
    ("12.1", "12.0", True),
    ("9.9", "12.0", False),      # numeric, not string, order
])
def test_installed_version_satisfies(tmp_path, installed, wanted, met):
    root = _game_root(tmp_path)
    _write_version_file(root / "Unofficial_Patch", _UI.replace("12.0", installed), "utf-16")
    assert v.installed_version_satisfies(root, wanted) is met


def test_nothing_installed_never_satisfies(tmp_path):
    root = _game_root(tmp_path)
    assert v.installed_version_satisfies(root, "11.5") is False
    assert v.installed_version_satisfies(root, None) is False
