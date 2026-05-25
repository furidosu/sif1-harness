"""Shared Lua-parsing helpers for the scrape/extract pipeline.

`function_spans(src)` returns spans for every `function ...(...) ... end`
block (including nested), properly tracking `if/for/while/repeat/do` so we
don't terminate on the wrong `end`. Decompiled SIF1 Lua uses positional
locals `L<n>_<depth>` and params `A<n>_<depth>`; we capture the param
depth as `depth_marker` so callers can identify `A0_<d>` (the response in
a callback at depth d).
"""
from __future__ import annotations

import re

FUNC_DEF_RE = re.compile(r"\bfunction\s+(?P<name>[A-Za-z_][\w.]*|L\d+_\d+)\s*\(([^)]*)\)")
# Tokens that open a block requiring `end` (or `until` for repeat).
BLOCK_TOKEN_RE = re.compile(
    r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|--\[\[.*?\]\]|--[^\n]*|"
    r"\b(function|if|for|while|repeat|do|end|until|then)\b",
    re.DOTALL,
)


def _scan_block_tokens(src: str) -> list[tuple[int, int, str]]:
    toks: list[tuple[int, int, str]] = []
    for m in BLOCK_TOKEN_RE.finditer(src):
        kw = m.group(1)
        if not kw:
            continue
        toks.append((m.start(), m.end(), kw))
    return toks


def function_spans(src: str) -> list[dict]:
    tokens = _scan_block_tokens(src)
    spans: list[dict] = []
    for m in FUNC_DEF_RE.finditer(src):
        name = m.group("name")
        params_raw = m.group(2).strip()
        params = [p.strip() for p in params_raw.split(",") if p.strip()]
        dm = None
        if params:
            head = params[0]
            mm = re.match(r"A\d+_(\d+)", head)
            if mm:
                dm = int(mm.group(1))
        body_start = m.end()
        depth = 1
        end = None
        expecting_do = False
        for ts, te, kw in tokens:
            if ts < body_start:
                continue
            if kw == "function":
                depth += 1
                expecting_do = False
            elif kw == "if":
                depth += 1
                expecting_do = False
            elif kw == "for" or kw == "while":
                depth += 1
                expecting_do = True
            elif kw == "repeat":
                depth += 1
                expecting_do = False
            elif kw == "do":
                if expecting_do:
                    expecting_do = False
                else:
                    depth += 1
            elif kw == "then":
                expecting_do = False
            elif kw == "end":
                depth -= 1
                expecting_do = False
                if depth == 0:
                    end = te
                    break
            elif kw == "until":
                depth -= 1
                expecting_do = False
                if depth == 0:
                    end = te
                    break
        if end is None:
            end = len(src)
        spans.append({
            "name": name,
            "params": params,
            "start": m.start(),
            "body_start": body_start,
            "end": end,
            "depth_marker": dm,
        })
    return spans


def nested_function_starts(span: dict, all_spans: list[dict]) -> list[tuple[int, int]]:
    out = []
    for s in all_spans:
        if s["start"] > span["body_start"] and s["end"] < span["end"]:
            out.append((s["start"], s["end"]))
    out.sort()
    return out


def carve_body(src: str, span: dict, all_spans: list[dict]) -> str:
    nested = nested_function_starts(span, all_spans)
    if not nested:
        return src[span["body_start"]:span["end"]]
    parts: list[str] = []
    cursor = span["body_start"]
    for s, e in nested:
        parts.append(src[cursor:s])
        parts.append(" " * (e - s))
        cursor = e
    parts.append(src[cursor:span["end"]])
    return "".join(parts)
