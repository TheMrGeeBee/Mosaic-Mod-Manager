"""When a new file shares its Nexus mod_id with an already-installed mod
(MAIN/UPDATE category), prepare_archive used to silently rename it to match
the existing folder — correct for a genuine hotfix/update, but wrong for an
unrelated alternate file on the same mod page (real report: "Dragonborn Walk
Fix Male" and "...Female" are two different files sharing one Nexus mod
page; installing the second silently merged into the first's folder).

There is no reliable local signal that tells these two cases apart, so
prepare_archive now always asks via an on_variant_choice(existing_name,
new_name) callback when one is supplied — "replace" merges as before,
"rename:<name>" (or anything not "replace"/"cancel") keeps the new file
under its own name, "cancel" aborts the install. None (the default, every
non-interactive caller) preserves the old always-merge behaviour untouched.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from Nexus.nexus_meta import NexusModMeta, write_meta
from Utils.mods.mod_install import prepare_archive


class _FakeGame:
    def __init__(self, staging_dir: Path):
        self._staging_dir = staging_dir

    def get_effective_mod_staging_path(self):
        return self._staging_dir


def _seed_existing_mod(staging_root: Path, folder_name: str, mod_id: int) -> None:
    mod_dir = staging_root / folder_name
    mod_dir.mkdir(parents=True)
    write_meta(mod_dir / "meta.ini",
              NexusModMeta(mod_name=folder_name, mod_id=mod_id,
                          file_category="MAIN"))


def _make_zip(tmp_path: Path, name: str) -> Path:
    archive = tmp_path / name
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("dummy.txt", "content")
    return archive


def _new_file_meta(mod_id: int, nexus_file_name: str) -> NexusModMeta:
    return NexusModMeta(mod_id=mod_id, file_category="MAIN",
                       nexus_file_name=nexus_file_name)


def test_no_callback_preserves_old_silent_merge_behavior(tmp_path):
    staging = tmp_path / "mods"
    _seed_existing_mod(staging, "Dragonborn Walk Fix Female", mod_id=19663)
    archive = _make_zip(tmp_path, "male.zip")
    game = _FakeGame(staging)

    prepared = prepare_archive(
        str(archive), game, tmp_path / "profile", log_fn=lambda _m: None,
        prebuilt_meta=_new_file_meta(19663, "Dragonborn Walk Fix Male"))

    assert prepared is not None
    assert prepared.mod_name == "Dragonborn Walk Fix Female"
    prepared.cleanup()


def test_replace_choice_merges_into_existing_folder(tmp_path):
    staging = tmp_path / "mods"
    _seed_existing_mod(staging, "Dragonborn Walk Fix Female", mod_id=19663)
    archive = _make_zip(tmp_path, "male.zip")
    game = _FakeGame(staging)

    prepared = prepare_archive(
        str(archive), game, tmp_path / "profile", log_fn=lambda _m: None,
        prebuilt_meta=_new_file_meta(19663, "Dragonborn Walk Fix Male"),
        on_variant_choice=lambda existing, new: "replace")

    assert prepared is not None
    assert prepared.mod_name == "Dragonborn Walk Fix Female"
    prepared.cleanup()


def test_keep_separate_choice_uses_its_own_name(tmp_path):
    staging = tmp_path / "mods"
    _seed_existing_mod(staging, "Dragonborn Walk Fix Female", mod_id=19663)
    archive = _make_zip(tmp_path, "male.zip")
    game = _FakeGame(staging)

    prepared = prepare_archive(
        str(archive), game, tmp_path / "profile", log_fn=lambda _m: None,
        prebuilt_meta=_new_file_meta(19663, "Dragonborn Walk Fix Male"),
        on_variant_choice=lambda existing, new: f"rename:{new}")

    assert prepared is not None
    assert prepared.mod_name == "Dragonborn Walk Fix Male"
    prepared.cleanup()


def test_custom_rename_choice_is_honored(tmp_path):
    staging = tmp_path / "mods"
    _seed_existing_mod(staging, "Dragonborn Walk Fix Female", mod_id=19663)
    archive = _make_zip(tmp_path, "male.zip")
    game = _FakeGame(staging)

    prepared = prepare_archive(
        str(archive), game, tmp_path / "profile", log_fn=lambda _m: None,
        prebuilt_meta=_new_file_meta(19663, "Dragonborn Walk Fix Male"),
        on_variant_choice=lambda existing, new: "rename:My Custom Name")

    assert prepared is not None
    assert prepared.mod_name == "My Custom Name"
    prepared.cleanup()


def test_cancel_choice_aborts_the_install(tmp_path):
    staging = tmp_path / "mods"
    _seed_existing_mod(staging, "Dragonborn Walk Fix Female", mod_id=19663)
    archive = _make_zip(tmp_path, "male.zip")
    game = _FakeGame(staging)

    prepared = prepare_archive(
        str(archive), game, tmp_path / "profile", log_fn=lambda _m: None,
        prebuilt_meta=_new_file_meta(19663, "Dragonborn Walk Fix Male"),
        on_variant_choice=lambda existing, new: "cancel")

    assert prepared is None


def test_no_existing_mod_never_consults_the_callback(tmp_path):
    staging = tmp_path / "mods"
    staging.mkdir()
    archive = _make_zip(tmp_path, "male.zip")
    game = _FakeGame(staging)
    calls = []

    prepared = prepare_archive(
        str(archive), game, tmp_path / "profile", log_fn=lambda _m: None,
        prebuilt_meta=_new_file_meta(19663, "Dragonborn Walk Fix Male"),
        on_variant_choice=lambda existing, new: calls.append((existing, new)))

    assert prepared is not None
    assert prepared.mod_name == "Dragonborn Walk Fix Male"
    assert calls == []
    prepared.cleanup()
