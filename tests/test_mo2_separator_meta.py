"""Separator meta.ini round-trip against real Mod Organizer 2 output.

The fixtures below are copied verbatim from a Wabbajack-installed MO2 2.5.2
tree (Vampire: The Masquerade - Bloodlines, 405 mods / 76 separators), not
hand-constructed — the point of the test is that Mosaic agrees with what MO2
actually writes, including the escaping quirks.
"""
from __future__ import annotations

from pathlib import Path

from Utils.mods.mo2_separator_meta import (
    SeparatorMeta,
    apply_separator_state,
    encode_color_variant,
    is_separator_dir,
    parse_color_variant,
    read_separator_meta,
    scan_separator_colors,
    scan_separator_nesting,
    write_separator_color,
    write_separator_meta,
)

# From mods/###Antitribu - Optional Mods_separator/meta.ini
REAL_SEPARATOR_COLOR = r"@Variant(\0\0\0\x43\x1\xff\xff\xff\xffUU\xff\xff\0\0)"
# From mods/Clan_Quest_UPgraded/meta.ini — a plain mod, colour spec 0
REAL_PLAIN_MOD_COLOR = r"@Variant(\0\0\0\x43\0\xff\xff\0\0\0\0\0\0\0\0)"


def test_parses_real_separator_colour():
    assert parse_color_variant(REAL_SEPARATOR_COLOR) == "#FF55FF"


def test_plain_mod_invalid_spec_is_not_a_colour():
    # Spec 0 means "unset". Decoding it as black would paint every ordinary
    # mod, which is the bug this guards against.
    assert parse_color_variant(REAL_PLAIN_MOD_COLOR) is None


def test_encode_reproduces_mo2_byte_for_byte():
    # Exercises the escaping rule: 0x43 is printable 'C' but must still be
    # escaped because it follows an escape and is a hex digit.
    assert encode_color_variant("#FF55FF") == REAL_SEPARATOR_COLOR


def test_encode_none_matches_mo2_unset_form():
    assert encode_color_variant(None) == REAL_PLAIN_MOD_COLOR


def test_colour_round_trips():
    for colour in ("#FF55FF", "#000000", "#FFFFFF", "#1A2B3C"):
        assert parse_color_variant(encode_color_variant(colour)) == colour


def test_bad_colour_degrades_to_unset_not_exception():
    for bad in ("", "nonsense", "#12", "@Variant(zz)", "#GGGGGG"):
        assert parse_color_variant(bad) is None
    assert parse_color_variant(encode_color_variant("not a colour")) is None


def test_reads_nesting_from_real_meta_ini(tmp_path: Path):
    sep = tmp_path / "###Antitribu - Optional Mods_separator"
    sep.mkdir()
    (sep / "meta.ini").write_text(
        "[General]\n"
        "modid=0\n"
        "gameName=vampirebloodlines\n"
        f"color={REAL_SEPARATOR_COLOR}\n"
        "\n"
        "[installedFiles]\n"
        "size=0\n"
        "\n"
        "[Plugins]\n"
        "Sub-Separator\\maxSize=3\n"
        "Sub-Separator\\nest=2\n",
        encoding="utf-8",
    )
    meta = read_separator_meta(sep)
    assert meta.color == "#FF55FF"
    assert meta.nest == 2
    assert meta.max_size == 3


def test_missing_meta_ini_is_empty_not_an_error(tmp_path: Path):
    sep = tmp_path / "Nothing_separator"
    sep.mkdir()
    meta = read_separator_meta(sep)
    assert meta == SeparatorMeta()


def test_write_preserves_unrelated_mo2_keys(tmp_path: Path):
    sep = tmp_path / "Keep_separator"
    sep.mkdir()
    (sep / "meta.ini").write_text(
        "[General]\n"
        "modid=0\n"
        "nexusLastModified=2026-03-18T18:14:19Z\n"
        "tracked=0\n"
        f"color={REAL_PLAIN_MOD_COLOR}\n",
        encoding="utf-8",
    )
    write_separator_meta(sep, SeparatorMeta(color="#FF55FF", nest=2, max_size=3))
    text = (sep / "meta.ini").read_text(encoding="utf-8")
    # MO2 bookkeeping survives a Mosaic edit
    assert "nexusLastModified=2026-03-18T18:14:19Z" in text
    assert "tracked=0" in text
    # and the new state is readable back
    again = read_separator_meta(sep)
    assert (again.color, again.nest, again.max_size) == ("#FF55FF", 2, 3)


def test_write_then_read_round_trips_on_a_fresh_dir(tmp_path: Path):
    sep = tmp_path / "Fresh_separator"
    write_separator_meta(sep, SeparatorMeta(color="#102030", nest=1))
    meta = read_separator_meta(sep)
    assert meta.color == "#102030"
    assert meta.nest == 1
    assert meta.max_size is None


def test_is_separator_dir(tmp_path: Path):
    sep = tmp_path / "Group_separator"
    sep.mkdir()
    mod = tmp_path / "SomeMod"
    mod.mkdir()
    loose = tmp_path / "Not_separator"
    loose.write_text("", encoding="utf-8")  # a file, not a directory
    assert is_separator_dir(sep)
    assert not is_separator_dir(mod)
    assert not is_separator_dir(loose)


def test_scan_separator_colors_shape(tmp_path: Path):
    mods = tmp_path / "mods"
    (mods / "A_separator").mkdir(parents=True)
    (mods / "B_separator").mkdir()
    (mods / "RegularMod").mkdir()
    write_separator_meta(mods / "A_separator", SeparatorMeta(color="#FF55FF"))
    write_separator_meta(mods / "B_separator", SeparatorMeta())  # no colour
    write_separator_meta(mods / "RegularMod", SeparatorMeta(color="#123456"))

    colors = scan_separator_colors(mods)
    # keyed by internal name (suffix kept), so it drops into separator_colors
    assert colors == {"A_separator": "#FF55FF"}


def test_scan_separator_colors_missing_dir_is_empty(tmp_path: Path):
    assert scan_separator_colors(tmp_path / "nope") == {}


# --------------------------------------------------------------------------
# wiring: the Wabbajack install carries separator colours into profile_state
# --------------------------------------------------------------------------

def test_wabbajack_install_imports_separator_colours(tmp_path: Path):
    """A separator's meta.ini reaches staging as an ordinary mod directive
    (``mods/<Name>_separator/meta.ini`` is 3 path parts), so _write_profile
    should lift its colour into the profile's separator_colors."""
    from Utils.profile.profile_state import read_separator_colors
    from Utils.wabbajack.wabbajack_install import (WabbajackInstallReport,
                                                   _write_profile)

    staging = tmp_path / "staging"
    write_separator_meta(staging / "Textures_separator",
                         SeparatorMeta(color="#FF55FF", nest=2))
    write_separator_meta(staging / "Plain_separator", SeparatorMeta())
    (staging / "RealMod").mkdir(parents=True, exist_ok=True)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "modlist.txt").write_text(
        "-Textures_separator\n-Plain_separator\n+RealMod\n", encoding="utf-8")

    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()

    _write_profile(profile_dir, scratch, {"RealMod"},
                   WabbajackInstallReport(), lambda *_a, **_k: None,
                   staging=staging)

    colors = read_separator_colors(profile_dir)
    assert colors.get("Textures_separator") == "#FF55FF"
    # a separator with no colour set must not be invented as black
    assert "Plain_separator" not in colors


def test_wabbajack_install_without_staging_is_unchanged(tmp_path: Path):
    """staging=None keeps the previous behaviour — no colour work at all."""
    from Utils.profile.profile_state import read_separator_colors
    from Utils.wabbajack.wabbajack_install import (WabbajackInstallReport,
                                                   _write_profile)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "modlist.txt").write_text("-A_separator\n+Mod\n", encoding="utf-8")
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()

    _write_profile(profile_dir, scratch, {"Mod"},
                   WabbajackInstallReport(), lambda *_a, **_k: None)

    assert read_separator_colors(profile_dir) == {}
    assert (profile_dir / "modlist.txt").is_file()


# --------------------------------------------------------------------------
# write-back: colours changed in Mosaic must reach MO2's meta.ini
# --------------------------------------------------------------------------

def test_write_back_updates_an_existing_separator(tmp_path: Path):
    mods = tmp_path / "mods"
    write_separator_meta(mods / "A_separator", SeparatorMeta(color="#FF55FF"))
    assert write_separator_color(mods, "A_separator", "#102030") is True
    assert read_separator_meta(mods / "A_separator").color == "#102030"


def test_write_back_preserves_nesting_and_foreign_keys(tmp_path: Path):
    """A colour edit must not drop MO2's own bookkeeping."""
    mods = tmp_path / "mods"
    sep = mods / "A_separator"
    write_separator_meta(sep, SeparatorMeta(color="#FF55FF", nest=2, max_size=3))
    (sep / "meta.ini").write_text(
        (sep / "meta.ini").read_text(encoding="utf-8") + "modid=1234\n",
        encoding="utf-8")

    write_separator_color(mods, "A_separator", "#010203")

    again = read_separator_meta(sep)
    assert (again.color, again.nest, again.max_size) == ("#010203", 2, 3)
    assert "modid=1234" in (sep / "meta.ini").read_text(encoding="utf-8")


def test_write_back_clearing_a_colour(tmp_path: Path):
    mods = tmp_path / "mods"
    write_separator_meta(mods / "A_separator", SeparatorMeta(color="#FF55FF"))
    write_separator_color(mods, "A_separator", None)
    assert read_separator_meta(mods / "A_separator").color is None


def test_write_back_skips_a_separator_with_no_folder(tmp_path: Path):
    """Mosaic's own separators have nothing on disk; don't invent MO2 folders."""
    mods = tmp_path / "mods"
    mods.mkdir()
    assert write_separator_color(mods, "MosaicOnly_separator", "#FF0000") is False
    assert not (mods / "MosaicOnly_separator").exists()


def test_write_back_can_create_when_asked(tmp_path: Path):
    mods = tmp_path / "mods"
    mods.mkdir()
    assert write_separator_color(mods, "New_separator", "#FF0000", create=True) is True
    assert read_separator_meta(mods / "New_separator").color == "#FF0000"


def test_round_trip_through_mosaic_keeps_mo2_happy(tmp_path: Path):
    """scan -> edit in Mosaic -> write back: what MO2 reads is what Mosaic shows."""
    mods = tmp_path / "mods"
    write_separator_meta(mods / "A_separator", SeparatorMeta(color="#FF55FF", nest=2))
    write_separator_meta(mods / "B_separator", SeparatorMeta(color="#00FF00"))

    colors = scan_separator_colors(mods)
    nesting = scan_separator_nesting(mods)
    colors["A_separator"] = "#123456"            # user recolours one in Mosaic

    assert apply_separator_state(mods, colors, nesting) == 2
    assert scan_separator_colors(mods) == {"A_separator": "#123456",
                                           "B_separator": "#00FF00"}
    assert scan_separator_nesting(mods) == {"A_separator": {"nest": 2}}


# --------------------------------------------------------------------------
# nesting: parsed, stored, and no longer silently dropped
# --------------------------------------------------------------------------

def test_scan_nesting_shape(tmp_path: Path):
    mods = tmp_path / "mods"
    write_separator_meta(mods / "A_separator", SeparatorMeta(color="#FF55FF", nest=2, max_size=3))
    write_separator_meta(mods / "B_separator", SeparatorMeta(nest=1))
    write_separator_meta(mods / "C_separator", SeparatorMeta(color="#00FF00"))  # no nesting
    write_separator_meta(mods / "RegularMod", SeparatorMeta(nest=9))            # not a separator

    assert scan_separator_nesting(mods) == {
        "A_separator": {"nest": 2, "max_size": 3},
        "B_separator": {"nest": 1},
    }


def test_scan_nesting_missing_dir_is_empty(tmp_path: Path):
    assert scan_separator_nesting(tmp_path / "nope") == {}


def test_wabbajack_install_keeps_separator_nesting(tmp_path: Path):
    """Mosaic has no nesting of its own, so the import must park MO2's in
    profile_state or exporting the profile silently flattens the list."""
    from Utils.profile.profile_state import read_separator_nesting
    from Utils.wabbajack.wabbajack_install import (WabbajackInstallReport,
                                                   _write_profile)

    staging = tmp_path / "staging"
    write_separator_meta(staging / "Textures_separator",
                         SeparatorMeta(color="#FF55FF", nest=2, max_size=3))
    write_separator_meta(staging / "Plain_separator", SeparatorMeta())

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "modlist.txt").write_text(
        "-Textures_separator\n-Plain_separator\n", encoding="utf-8")
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()

    _write_profile(profile_dir, scratch, set(), WabbajackInstallReport(),
                   lambda *_a, **_k: None, staging=staging)

    nesting = read_separator_nesting(profile_dir)
    assert nesting == {"Textures_separator": {"nest": 2, "max_size": 3}}


def test_profile_state_nesting_round_trip(tmp_path: Path):
    from Utils.profile.profile_state import (read_separator_nesting,
                                             write_separator_nesting)
    write_separator_nesting(tmp_path, {"A_separator": {"nest": 2, "max_size": 3}})
    assert read_separator_nesting(tmp_path) == {"A_separator": {"nest": 2, "max_size": 3}}


def test_profile_state_nesting_rejects_junk(tmp_path: Path):
    from Utils.profile.profile_state import (read_separator_nesting,
                                             write_separator_nesting)
    write_separator_nesting(tmp_path, {"A_separator": {"nest": "two", "bogus": 1},
                                       "B_separator": "nope",
                                       "C_separator": {"nest": True}})
    assert read_separator_nesting(tmp_path) == {}
