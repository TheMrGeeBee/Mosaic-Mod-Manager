"""Steam Cloud defeats per-profile saves: each launch it copies the game's cloud
saves into the profile's save folder. Real case, 2026-11-25: Fallout New Vegas,
17 cloud saves landed in a new profile six seconds before the game started."""
from __future__ import annotations

from Utils.steam import steam_cloud as sc

_RC = '''"22380"
{
	"ChangeNumber"		"0"
	"My Games/FalloutNV/Saves/autosave.fos"
	{
		"root"		"2"
		"size"		"4262829"
	}
	"My Games/FalloutNV/Saves/Save 1   Gordon  Vault 101  00 55 20.fos"
	{
		"root"		"2"
	}
	"My Games/FalloutNV/FalloutPrefs.ini"
	{
		"root"		"2"
	}
}
'''


def _shared(cloud: "str | None", app_cloud: "str | None" = None) -> str:
    inner = f'\t\t\t\t"CloudEnabled"\t\t"{cloud}"\n' if cloud is not None else ""
    if app_cloud is not None:
        inner += ('\t\t\t\t"apps"\n\t\t\t\t{\n\t\t\t\t\t"22380"\n\t\t\t\t\t{\n'
                  f'\t\t\t\t\t\t"CloudEnabled"\t\t"{app_cloud}"\n\t\t\t\t\t}}\n\t\t\t\t}}\n')
    return ('"UserRoamingConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n'
            f'\t\t\t"Steam"\n\t\t\t{{\n{inner}\t\t\t}}\n\t\t}}\n\t}}\n}}\n')


def _steam(tmp_path, *, rc=_RC, shared: "str | None" = None):
    user = tmp_path / "Steam" / "userdata" / "6430221"
    (user / "22380").mkdir(parents=True)
    if rc is not None:
        (user / "22380" / "remotecache.vdf").write_text(rc)
    if shared is not None:
        (user / "7" / "remote").mkdir(parents=True)
        (user / "7" / "remote" / "sharedconfig.vdf").write_text(shared)
    return [tmp_path / "Steam"]


def test_cloud_saves_are_counted(tmp_path):
    roots = _steam(tmp_path, shared=_shared(None))
    assert sc.cloud_save_count("22380", roots) == 2          # the INI isn't a save


def test_cloud_turned_off_for_every_game(tmp_path):
    assert sc.cloud_save_count("22380", _steam(tmp_path, shared=_shared("0"))) == 0


def test_cloud_turned_off_for_the_game(tmp_path):
    roots = _steam(tmp_path, shared=_shared("1", app_cloud="0"))
    assert sc.cloud_save_count("22380", roots) == 0


def test_no_cloud_record_no_warning(tmp_path):
    assert sc.cloud_save_count("22380", _steam(tmp_path, rc=None)) == 0
    assert sc.cloud_save_count("", _steam(tmp_path / "b")) == 0


def test_the_warning_names_the_count_and_the_switch(monkeypatch):
    monkeypatch.setattr(sc, "cloud_save_count", lambda app, roots=None: 17)

    class G:
        steam_id = "22380"
    msg = sc.cloud_saves_warning(G())
    assert "17 in the cloud" in msg and "Keep game saves in the Steam Cloud" in msg
    monkeypatch.setattr(sc, "cloud_save_count", lambda app, roots=None: 0)
    assert sc.cloud_saves_warning(G()) is None


def test_deploy_logs_the_warning(tmp_path, monkeypatch):
    from Games.Bethesda.fallout_nv import Fallout_NV
    g = Fallout_NV()
    g._profile_saves = True
    mygames = tmp_path / "My Games" / "FalloutNV"
    monkeypatch.setattr(g, "_saves_link_targets", lambda: [mygames])
    monkeypatch.setattr(g, "_profile_saves_dir", lambda p: tmp_path / "profile" / "Saves")
    monkeypatch.setattr(sc, "cloud_saves_warning", lambda game: "Steam Cloud syncs this game's saves")
    logs = []
    g._symlink_profile_saves("A", logs.append)
    assert (mygames / "Saves").is_symlink()
    assert any(m.startswith("  WARN: Steam Cloud") for m in logs)
