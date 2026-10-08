"""A small reader for Steam's text KeyValues files (``.acf`` app manifests,
``.vdf``): quoted keys and values, nested ``{ }`` blocks, ``//`` comments.

``parse_vdf(text)`` returns nested dicts with keys as written. Steam's own
keys vary in case between files (``InstalledDepots`` vs ``installeddepots``),
so use :func:`get_ci` to look keys up case-insensitively.
"""
from __future__ import annotations

from pathlib import Path


class VdfError(ValueError):
    """The text isn't valid KeyValues."""


def _tokens(text: str):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == "/" and text.startswith("//", i):
            nl = text.find("\n", i)
            i = n if nl < 0 else nl + 1
        elif c in "{}":
            yield c
            i += 1
        elif c == '"':
            out, i = [], i + 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    out.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(nxt, "\\" + nxt))
                    i += 2
                else:
                    out.append(text[i])
                    i += 1
            if i >= n:
                raise VdfError("unterminated string")
            yield ("str", "".join(out))
            i += 1
        else:  # unquoted token (rare in Steam files)
            j = i
            while j < n and not text[j].isspace() and text[j] not in '{}"':
                j += 1
            yield ("str", text[i:j])
            i = j


def parse_vdf(text: str) -> dict:
    stack: "list[dict]" = [{}]
    key = None
    for tok in _tokens(text):
        if tok == "{":
            if key is None:
                raise VdfError("block without a key")
            child: dict = {}
            stack[-1][key] = child
            stack.append(child)
            key = None
        elif tok == "}":
            if key is not None or len(stack) == 1:
                raise VdfError("unexpected '}'")
            stack.pop()
        elif key is None:
            key = tok[1]
        else:
            stack[-1][key] = tok[1]
            key = None
    if len(stack) != 1 or key is not None:
        raise VdfError("unexpected end of file")
    return stack[0]


def read_vdf(path: "str | Path") -> dict:
    return parse_vdf(Path(path).read_text(encoding="utf-8", errors="replace"))


def get_ci(d, *keys, default=None):
    """``d[k1][k2]...`` with each key matched case-insensitively."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        hit = next((v for kk, v in cur.items() if kk.lower() == k.lower()), None)
        if hit is None:
            return default
        cur = hit
    return cur
