"""The self-update relaunch must not inherit the old AppImage mount's environment."""

from __future__ import annotations

from Utils import version_check


def test_relaunch_env_drops_every_bundle_var_incl_games_dir(monkeypatch, tmp_path):
    old = "/tmp/.mount_mosaicOLD123"
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "mosaic.appimage"))
    monkeypatch.setenv("APPDIR", old)
    monkeypatch.setenv("MOD_MANAGER_GAMES", f"{old}/share/mosaic-mod-manager/Games")
    monkeypatch.setenv("SSL_CERT_FILE", f"{old}/cacert.pem")
    monkeypatch.setenv("HOME_MARKER", "kept")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    seen = {}

    class _Popen:
        def __init__(self, *a, **kw):
            seen.update(kw)

    monkeypatch.setattr(version_check.subprocess, "Popen", _Popen)
    version_check.run_installer()

    env = seen["env"]
    assert "MOD_MANAGER_GAMES" not in env
    assert "APPDIR" not in env and "APPIMAGE" not in env and "SSL_CERT_FILE" not in env
    assert env["HOME_MARKER"] == "kept"
