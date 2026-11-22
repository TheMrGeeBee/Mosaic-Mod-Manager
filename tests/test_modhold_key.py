"""Modhold key storage: keyring path, encrypted-file fallback, clearing."""
from __future__ import annotations

import keyring.errors
import pytest

from Modhold import modhold_key
from Modhold.game_slugs import MODHOLD_GAME_SLUGS, modhold_slug_for


class _FakeKeyring:
    errors = keyring.errors

    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value

    def delete_password(self, service, user):
        if (service, user) not in self.store:
            raise keyring.errors.PasswordDeleteError("missing")
        del self.store[(service, user)]


@pytest.fixture
def kr(monkeypatch, tmp_path):
    fake = _FakeKeyring()
    monkeypatch.setattr(modhold_key, "keyring", fake)
    monkeypatch.setattr(modhold_key, "_keyring_ok", lambda: True)
    monkeypatch.setattr(modhold_key, "get_config_dir", lambda: tmp_path)
    return fake


@pytest.fixture
def no_kr(monkeypatch, tmp_path):
    monkeypatch.setattr(modhold_key, "_keyring_ok", lambda: False)
    monkeypatch.setattr(modhold_key, "get_config_dir", lambda: tmp_path)
    return tmp_path


def test_nothing_stored_reads_empty(kr):
    assert modhold_key.load_modhold_key() == ""
    assert modhold_key.load_modhold_user() == ""


def test_keyring_round_trip_strips_whitespace(kr):
    modhold_key.save_modhold_key("  mhm_abc \n", " Sunny ")
    assert modhold_key.load_modhold_key() == "mhm_abc"
    assert modhold_key.load_modhold_user() == "Sunny"


def test_keyring_clear_removes_both(kr):
    modhold_key.save_modhold_key("mhm_abc", "Sunny")
    modhold_key.clear_modhold_key()
    assert modhold_key.load_modhold_key() == ""
    assert modhold_key.load_modhold_user() == ""
    modhold_key.clear_modhold_key()   # clearing twice is fine


def test_file_fallback_round_trip_and_is_not_plaintext(no_kr):
    modhold_key.save_modhold_key("mhm_secret", "Sunny")
    path = no_kr / "modhold_key.bin"
    assert path.is_file()
    assert b"mhm_secret" not in path.read_bytes()
    assert (path.stat().st_mode & 0o777) == 0o600
    assert modhold_key.load_modhold_key() == "mhm_secret"
    assert modhold_key.load_modhold_user() == "Sunny"


def test_file_fallback_clear_deletes_the_file(no_kr):
    modhold_key.save_modhold_key("mhm_secret", "Sunny")
    modhold_key.clear_modhold_key()
    assert not (no_kr / "modhold_key.bin").exists()
    assert modhold_key.load_modhold_key() == ""


def test_corrupt_file_reads_as_empty(no_kr):
    (no_kr / "modhold_key.bin").write_bytes(b"not a fernet token")
    assert modhold_key.load_modhold_key() == ""


def test_keyring_failure_falls_back_to_the_file(monkeypatch, tmp_path):
    class _Broken(_FakeKeyring):
        def set_password(self, *a):
            raise keyring.errors.KeyringError("locked")

        def get_password(self, *a):
            raise keyring.errors.KeyringError("locked")

    monkeypatch.setattr(modhold_key, "keyring", _Broken())
    monkeypatch.setattr(modhold_key, "_keyring_ok", lambda: True)
    monkeypatch.setattr(modhold_key, "get_config_dir", lambda: tmp_path)
    modhold_key.save_modhold_key("mhm_k", "Sunny")
    assert modhold_key.load_modhold_key() == "mhm_k"


def test_slug_lookup():
    assert modhold_slug_for("skyrim_se") == "skyrim-se"
    assert modhold_slug_for("not_a_game") == ""
    assert modhold_slug_for("") == ""


@pytest.mark.parametrize("game_id,slug", [
    ("skyrim_se", "skyrim-se"), ("skyrim", "skyrim"), ("Fallout4", "fallout-4"),
    ("FalloutNV", "fallout-new-vegas"), ("baldurs_gate_3", "baldurs-gate-3"),
    ("kingdom_come_deliverance_2", "kcd2"), ("Mount___Blade_II__Bannerlord", "bannerlord"),
    ("Fallout3GOTY", "fallout-3"), ("morrowind_openmw", "morrowind"),
])
def test_the_games_modhold_lists_are_mapped(game_id, slug):
    assert modhold_slug_for(game_id) == slug


@pytest.mark.parametrize("game_id", [
    "Fallout4VR", "skyrimvr", "Subnautica_Below_Zero", "kingdom_come_deliverance",
    "enderal", "enderalse", "resident_evil_village", "resident_evil_2", "Fallout76",
    "witcher_ee", "Valheim", "the_sims_4",
])
def test_a_different_game_with_a_similar_name_is_not_mapped(game_id):
    """Their mods would not fit, so they stay without a Modhold tab."""
    assert modhold_slug_for(game_id) == ""


def test_every_mapped_slug_is_a_plain_modhold_slug():
    import re
    for game_id, slug in MODHOLD_GAME_SLUGS.items():
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug), (game_id, slug)


def test_every_mapped_slug_has_a_real_mosaic_game_id():
    from pathlib import Path
    games = Path(__file__).resolve().parents[1] / "src" / "Games"
    text = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                     for p in games.rglob("*.py"))
    for game_id in MODHOLD_GAME_SLUGS:
        assert f'"{game_id}"' in text or f"'{game_id}'" in text, game_id
