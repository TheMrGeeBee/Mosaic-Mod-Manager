"""
mo2_separator_meta.py
Read and write the ``meta.ini`` that Mod Organizer 2 keeps inside a separator's
mod folder.

Mosaic models a separator as a modlist.txt entry plus colour/lock state held in
profile_state.json, and ``separator_copy.py`` states that separators "have no
on-disk folder".  A real MO2 install disagrees: every separator *is* a real
directory under ``mods/`` named ``<Name>_separator`` containing nothing but a
``meta.ini``, and that file is where MO2 keeps the colour and the grouping
depth::

    [General]
    color=@Variant(\\0\\0\\0\\x43\\x1\\xff\\xff\\xff\\xffUU\\xff\\xff\\0\\0)

    [Plugins]
    Sub-Separator\\nest=2
    Sub-Separator\\maxSize=3

Importing an MO2/Wabbajack modlist therefore loses every separator colour and
all nesting unless that file is read, and separators Mosaic writes back cannot
be coloured by MO2.  This module converts between the two representations; it
deliberately does no I/O beyond the single meta.ini so callers decide where it
is wired in.

Colour encoding
---------------
``color=`` holds a QSettings-escaped ``QVariant`` byte string:

* 4 bytes       big-endian QMetaType id — ``0x43`` (67) is ``QColor``
* 1 byte        colour spec: 0 invalid, 1 RGB, 2 HSV, 3 CMYK, 4 HSL
* 5 x 2 bytes   big-endian alpha, red, green, blue, pad, each 0..65535

Channels are 16-bit, so the 8-bit value is ``v >> 8``.  Spec 0 means "no colour
set", which is what ordinary (non-separator) mods carry — those decode to None
rather than to black.

QSettings escaping writes ``\\0`` for NUL, ``\\xN`` with *minimal* hex digits
otherwise, and leaves printable ASCII literal — except that a hex digit
immediately following an escape must itself be escaped, or it would be read as
a continuation.  That is why ``0x43`` appears as ``\\x43`` and not as ``C``.
Reproducing that rule keeps the output byte-identical to MO2's own.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path

from Utils.app_log import app_log
from Utils.atomic_write import write_atomic_text

_SEPARATOR_SUFFIX = "_separator"

_QMETATYPE_QCOLOR = 0x43
_SPEC_INVALID = 0
_SPEC_RGB = 1

_HEX_DIGITS = set("0123456789abcdefABCDEF")

# MO2 writes both keys under [Plugins], with a literal backslash in the key.
_NEST_KEY = "Sub-Separator\\nest"
_MAXSIZE_KEY = "Sub-Separator\\maxSize"


@dataclass
class SeparatorMeta:
    """Separator presentation state as MO2 stores it on disk."""

    color: str | None = None      # "#RRGGBB", or None when unset/invalid
    nest: int | None = None       # grouping depth; mirrors the #/##/### prefix
    max_size: int | None = None   # MO2's Sub-Separator maxSize hint


# --------------------------------------------------------------------------
# QSettings byte-string escaping
# --------------------------------------------------------------------------

def _unescape(text: str) -> bytes:
    """Decode a QSettings-escaped byte string.

    Hex escapes are read greedily up to two digits, which covers every byte
    value. A longer run cannot occur in a byte-oriented blob.
    """
    out = bytearray()
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != "\\":
            out.append(ord(ch) & 0xFF)
            i += 1
            continue
        i += 1
        if i >= n:
            break
        esc = text[i]
        if esc == "0":
            out.append(0)
            i += 1
        elif esc in ("x", "X"):
            i += 1
            digits = ""
            while i < n and len(digits) < 2 and text[i] in _HEX_DIGITS:
                digits += text[i]
                i += 1
            if digits:
                out.append(int(digits, 16) & 0xFF)
        elif esc == "\\":
            out.append(0x5C)
            i += 1
        else:
            out.append(ord(esc) & 0xFF)
            i += 1
    return bytes(out)


def _escape(data: bytes) -> str:
    """Encode bytes the way QSettings does, matching MO2 byte-for-byte."""
    parts: list[str] = []
    prev_was_escape = False
    for b in data:
        if b == 0:
            parts.append("\\0")
            prev_was_escape = True
            continue
        printable = 0x20 <= b < 0x7F and b != 0x5C
        # A hex digit straight after an escape would be swallowed as a
        # continuation of it, so escape that too.
        if printable and not (prev_was_escape and chr(b) in _HEX_DIGITS):
            parts.append(chr(b))
            prev_was_escape = False
        else:
            parts.append(f"\\x{b:x}")
            prev_was_escape = True
    return "".join(parts)


# --------------------------------------------------------------------------
# QColor variant <-> "#RRGGBB"
# --------------------------------------------------------------------------

def parse_color_variant(value: str) -> str | None:
    """Return ``"#RRGGBB"`` for an MO2 ``color=`` value, or None.

    None is returned for an absent value, a non-QColor variant, an invalid
    colour spec (what plain mods carry), or anything malformed — callers treat
    "no colour" and "unreadable colour" the same way.
    """
    if not value:
        return None
    text = value.strip()
    if text.startswith("@Variant(") and text.endswith(")"):
        text = text[len("@Variant("):-1]
    raw = _unescape(text)
    if len(raw) < 5:
        return None
    if int.from_bytes(raw[0:4], "big") != _QMETATYPE_QCOLOR:
        return None
    if raw[4] != _SPEC_RGB:
        # 0 = invalid (unset). Other specs are valid QColors but MO2 only ever
        # writes RGB for separators; converting them is not worth guessing at.
        return None
    if len(raw) < 13:
        return None
    _alpha = int.from_bytes(raw[5:7], "big")
    red = int.from_bytes(raw[7:9], "big")
    green = int.from_bytes(raw[9:11], "big")
    blue = int.from_bytes(raw[11:13], "big")
    return f"#{red >> 8:02X}{green >> 8:02X}{blue >> 8:02X}"


def encode_color_variant(color: str | None) -> str:
    """Build an MO2 ``color=`` value from ``"#RRGGBB"``.

    None (or an unparseable string) produces the invalid-spec variant that MO2
    writes for an uncoloured entry, so a round trip through Mosaic never
    invents a colour.
    """
    rgb: tuple[int, int, int] | None = None
    if color:
        text = color.strip().lstrip("#")
        if len(text) == 6:
            try:
                rgb = (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
            except ValueError:
                rgb = None

    out = bytearray(_QMETATYPE_QCOLOR.to_bytes(4, "big"))
    if rgb is None:
        out.append(_SPEC_INVALID)
        out += (0xFFFF).to_bytes(2, "big")      # alpha
        out += bytes(8)                          # r, g, b, pad
    else:
        out.append(_SPEC_RGB)
        out += (0xFFFF).to_bytes(2, "big")      # alpha, always opaque
        for channel in rgb:
            # 8-bit -> 16-bit the way Qt does it, so 0xFF maps to 0xFFFF.
            out += ((channel << 8) | channel).to_bytes(2, "big")
        out += (0).to_bytes(2, "big")            # pad
    return f"@Variant({_escape(bytes(out))})"


# --------------------------------------------------------------------------
# meta.ini
# --------------------------------------------------------------------------

def is_separator_dir(path: Path) -> bool:
    """True if *path* looks like an MO2 separator folder."""
    return path.is_dir() and path.name.endswith(_SEPARATOR_SUFFIX)


def _new_parser() -> configparser.RawConfigParser:
    # Raw: meta.ini values contain '%' and Qt escapes that interpolation would
    # mangle. optionxform=str: MO2's keys are case-sensitive ("maxSize").
    parser = configparser.RawConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    return parser


def read_separator_meta(mod_dir: Path) -> SeparatorMeta:
    """Read colour and nesting from ``<mod_dir>/meta.ini``.

    A missing or unreadable file yields an empty SeparatorMeta rather than
    raising — an MO2 tree in mid-write should not break an import.
    """
    meta = SeparatorMeta()
    path = mod_dir / "meta.ini"
    if not path.is_file():
        return meta
    parser = _new_parser()
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error) as exc:
        app_log(f"mo2_separator_meta: cannot read {path}: {exc}")
        return meta

    meta.color = parse_color_variant(parser.get("General", "color", fallback=""))
    for key, attr in ((_NEST_KEY, "nest"), (_MAXSIZE_KEY, "max_size")):
        raw = parser.get("Plugins", key, fallback="").strip()
        if raw:
            try:
                setattr(meta, attr, int(raw))
            except ValueError:
                pass
    return meta


def write_separator_meta(mod_dir: Path, meta: SeparatorMeta) -> None:
    """Write *meta* into ``<mod_dir>/meta.ini``, preserving unrelated keys.

    Existing content is read back first so MO2-only fields (modid, version,
    nexus bookkeeping) survive a Mosaic edit untouched.
    """
    path = mod_dir / "meta.ini"
    parser = _new_parser()
    if path.is_file():
        try:
            parser.read(path, encoding="utf-8")
        except (OSError, configparser.Error) as exc:
            app_log(f"mo2_separator_meta: cannot read {path} before write: {exc}")

    if not parser.has_section("General"):
        parser.add_section("General")
    parser.set("General", "color", encode_color_variant(meta.color))

    if meta.nest is not None or meta.max_size is not None:
        if not parser.has_section("Plugins"):
            parser.add_section("Plugins")
        if meta.max_size is not None:
            parser.set("Plugins", _MAXSIZE_KEY, str(meta.max_size))
        if meta.nest is not None:
            parser.set("Plugins", _NEST_KEY, str(meta.nest))

    buf: list[str] = []
    for section in parser.sections():
        buf.append(f"[{section}]")
        for key, value in parser.items(section):
            buf.append(f"{key}={value}")
        buf.append("")
    mod_dir.mkdir(parents=True, exist_ok=True)
    write_atomic_text(path, "\n".join(buf).rstrip("\n") + "\n")


def scan_separator_colors(mods_dir: Path) -> dict[str, str]:
    """Map ``<Name>_separator`` -> ``"#RRGGBB"`` for every coloured separator
    folder in *mods_dir*.

    Shaped to drop straight into profile_state's ``separator_colors``, which is
    keyed by the internal name (suffix included). Separators with no colour are
    omitted rather than recorded as None.
    """
    colors: dict[str, str] = {}
    if not mods_dir.is_dir():
        return colors
    for child in sorted(mods_dir.iterdir()):
        if not is_separator_dir(child):
            continue
        color = read_separator_meta(child).color
        if color:
            colors[child.name] = color
    return colors
