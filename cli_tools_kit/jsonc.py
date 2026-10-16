"""Read JSON with comments and trailing commas, and delete one member in place.

opencode keeps its config as JSONC (``opencode.json`` or ``opencode.jsonc``):
``//`` and ``/* */`` comments and trailing commas are allowed. :func:`loads`
reads such a file; :func:`remove_member` deletes one member and leaves every
other byte as it was, comments included.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional, Sequence, Tuple

# (kind, start, end): kind is one of "{", "}", "[", "]", ":", ",", "string",
# "atom" (number, true, false, null). Comments and whitespace are skipped.
Token = Tuple[str, int, int]


def _tokens(text: str) -> List[Token]:
    out: List[Token] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n﻿":
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise ValueError("unterminated comment")
            i = end + 2
        elif c in "{}[]:,":
            out.append((c, i, i + 1))
            i += 1
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise ValueError("unterminated string")
            out.append(("string", i, j + 1))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in " \t\r\n,:{}[]/\"":
                j += 1
            if j == i:
                raise ValueError(f"unexpected character at {i}")
            out.append(("atom", i, j))
            i = j
    return out


def loads(text: str) -> Any:
    """Parse JSONC: the tokens are joined back without comments and trailing commas."""
    toks = _tokens(text)
    parts = []
    for k, (kind, start, end) in enumerate(toks):
        if kind == "," and k + 1 < len(toks) and toks[k + 1][0] in "}]":
            continue
        parts.append(text[start:end])
    return json.loads("".join(parts) or "null")


def _value_end(toks: List[Token], k: int) -> int:
    """Index of the last token of the value that starts at token *k*."""
    if toks[k][0] not in "{[":
        return k
    depth = 0
    for j in range(k, len(toks)):
        if toks[j][0] in "{[":
            depth += 1
        elif toks[j][0] in "}]":
            depth -= 1
            if depth == 0:
                return j
    raise ValueError("unbalanced brackets")


def _members(text: str, toks: List[Token], k: int) -> List[Tuple[str, int, int]]:
    """(key, first token, last token of the value) for the object at token *k*."""
    out = []
    j = k + 1
    while j < len(toks) and toks[j][0] != "}":
        if toks[j][0] == ",":
            j += 1
            continue
        if toks[j][0] != "string" or j + 2 >= len(toks) or toks[j + 1][0] != ":":
            raise ValueError("malformed object")
        key = json.loads(text[toks[j][1]:toks[j][2]])
        last = _value_end(toks, j + 2)
        out.append((key, j, last))
        j = last + 1
    return out


def remove_member(text: str, path: Sequence[str]) -> Optional[str]:
    """*text* without the member at *path* (e.g. ``["mcp", "name"]``).

    Returns ``None`` when there is no such member. The member goes with one
    adjoining comma; when it fills whole lines, those lines go too. Comments
    elsewhere stay.
    """
    toks = _tokens(text)
    if not toks or toks[0][0] != "{":
        return None
    obj = 0
    for depth, key in enumerate(path):
        hit = next((m for m in _members(text, toks, obj) if m[0] == key), None)
        if hit is None:
            return None
        _, first, last = hit
        if depth < len(path) - 1:
            if toks[first + 2][0] != "{":
                return None
            obj = first + 2
            continue
        if last + 1 < len(toks) and toks[last + 1][0] == ",":
            return _cut_lines(text, toks[first][1], toks[last + 1][2])
        if toks[first - 1][0] == ",":
            # The last member without a trailing comma: the comma before it goes.
            comma = toks[first - 1]
            rest = _cut_lines(text, toks[first][1], toks[last][2])
            return rest[:comma[1]] + rest[comma[2]:]
        return _cut_lines(text, toks[first][1], toks[last][2])
    return None


def _cut_lines(text: str, start: int, end: int) -> str:
    """Remove text[start:end], and its lines when nothing else is on them."""
    line_start = text.rfind("\n", 0, start) + 1
    newline = text.find("\n", end)
    line_end = len(text) if newline < 0 else newline + 1
    if not text[line_start:start].strip() and not text[end:line_end].strip():
        return text[:line_start] + text[line_end:]
    return text[:start] + text[end:]
