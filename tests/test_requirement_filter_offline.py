"""updatefilter.txt: the bundled copy counts, and either/or requirements work
without a network call (used at deploy time).

UT - KAVT - Universal Automatic Patcher (Nexus 20629) lists Unique Tav Custom
Appearance (2754) and KAVT (16325) as requirements but needs only one of them.
KAVT is the modern replacement for Unique Tav ("compatible with any Unique Tav
enabled mod"), so only the Unique Tav requirement is satisfied by KAVT.
"""
from __future__ import annotations

from Nexus import nexus_requirements as nr


def test_bundled_filter_has_the_unique_tav_kavt_alternative(tmp_path, monkeypatch):
    monkeypatch.setattr(nr, "get_requirement_external_tool_mod_ids_path",
                        lambda: tmp_path / "missing-cache.txt")
    _external, alternatives = nr.load_requirement_filter_offline()
    # KAVT replaces Unique Tav, so a Unique Tav requirement is met by KAVT...
    assert nr._alternative_satisfied_for_game("baldursgate3", 2754, {16325}, alternatives)
    # ...but not the reverse: mods requiring KAVT need its extra features.
    assert not nr._alternative_satisfied_for_game("baldursgate3", 16325, {2754}, alternatives)
    assert not nr._alternative_satisfied_for_game("baldursgate3", 2754, set(), alternatives)


def test_user_cache_entries_are_merged(tmp_path, monkeypatch):
    cache = tmp_path / "cache.txt"
    cache.write_text("baldursgate3:111#222\n", encoding="utf-8")
    monkeypatch.setattr(nr, "get_requirement_external_tool_mod_ids_path", lambda: cache)
    _external, alternatives = nr.load_requirement_filter_offline()
    assert alternatives[("baldursgate3", 111)] == {222}
    assert ("baldursgate3", 2754) in alternatives
