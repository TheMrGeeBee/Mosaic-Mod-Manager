"""Newer Nexus website filenames + md5-hit selection (TweakXL got identified
as mod 32315 because results[0] was taken blindly)."""
from Nexus.nexus_meta import _pick_md5_hit, parse_nexus_filename


def test_spaced_names():
    i = parse_nexus_filename("TweakXL 4197 1.11.4 2026-07-30T19-32Z FKtYKrJzt")
    assert (i.mod_id, i.version) == (4197, "1.11.4")
    i = parse_nexus_filename("Codeware 7780 1.20.5 2026-09-20T08-20Z FKtYKrtYr")
    assert (i.mod_id, i.version) == (7780, "1.20.5")
    i = parse_nexus_filename("Audioware 12001 v1.9.9 2026-08-05T01-33Z vD6JDHRv6")
    assert i.mod_id == 12001


def test_old_names_unchanged():
    i = parse_nexus_filename("redscript-1511-0-5-31-1757113888")
    assert i.mod_id == 1511 and i.version_parts == [0, 5, 31, 1757113888]
    assert parse_nexus_filename("random archive") is None


def test_pick_md5_hit():
    wrong = {"mod": {"mod_id": 32315, "name": ""}}
    other = {"mod": {"mod_id": 99, "name": "Other"}}
    right = {"mod": {"mod_id": 4197, "name": "TweakXL"}}
    assert _pick_md5_hit([wrong, other, right], 4197) is right
    assert _pick_md5_hit([wrong, other, right], 0) is other     # named beats empty
    assert _pick_md5_hit([wrong], 0) is wrong
