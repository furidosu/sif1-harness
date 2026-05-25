"""Static field-extraction over `Cachable.get(cache_key)` consumer files.

The third anchor in the static-extraction stack (after Approach D's
per-call success closures and Cachable.addListener bodies):

  model.lua, somewhere:
    L0_1 = import("Cachable")
    ...
    L6_1.cache_key = "$exchangeOwningPoint"
    ...
    function L7_1()
      L2_2 = L0_1.get               -- bind .get method
      L3_2 = L6_1.cache_key          -- OR an inline "$key" literal
      L2_2 = L2_2(L3_2)             -- L2_2 = Cachable.get(...) = cache value
      L2_2 = L2_2.exchange_point_list  -- field read! → "exchange_point_list"
      for L4_2, L5_2 in ipairs(L2_2) do
        L6_2 = L5_2.rarity          -- → "exchange_point_list.rarity"

These model files own the cache value's shape: a Cachable.set(key, ...)
upstream populates it from the wire response, and consumers read off the
populated structure. Field reads here reveal what the response shape
must contain.

We scan every endpoint that has a cache_key (essentially every endpoint
that goes through Cachable), and only descend into files that contain
that endpoint's cache_key string literal. That keeps noise low without
needing a corpus filter — a file referencing the cache_key is almost
always either the producer or a model consumer of that exact cache.

We do NOT filter by ui-only bucket: an endpoint can be harness-covered
in the pre-merge classification (because the listener already reads >=5
keys) while still having no `runtime_discovered_field_names` *novel*
beyond the declared schema. Cache-consumer extraction can surface those
novel reads even when the bucket already says "covered".

Output: build/runtime/traces_cache_consumer/<ep>.json, picked up by
merge_observations.py alongside traces_classes and traces_static.

Usage:
    uv run --no-project python src/tools/extract_cache_consumer_reads.py \\
        [--out build/runtime/traces_cache_consumer]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "assets" / "decompiled" / "all"
MERGED_PATH = ROOT / "build" / "merged_endpoints.json"
DEFAULT_OUT = ROOT / "build" / "runtime" / "traces_cache_consumer"

# Decompiled-bytecode patterns. The decompiler hoists every local
# assignment onto its own line, which makes the patterns simpler than
# they'd be against source-level Lua.
CACHABLE_IMPORT_RE = re.compile(r'^(L\d+_\d+)\s*=\s*import\s*$')
STRING_ASSIGN_RE = re.compile(r'^(L\d+_\d+)\s*=\s*"([^"]+)"\s*$')
CALL_REASSIGN_RE = re.compile(
    r'^(L\d+_\d+)\s*=\s*(L\d+_\d+)\((L\d+_\d+)\)\s*$'
)
GET_BIND_RE = re.compile(r'^(L\d+_\d+)\s*=\s*(L\d+_\d+)\.get\s*$')
ASSIGN_RE = re.compile(r'^(?:local\s+)?(L\d+_\d+)\s*=\s*(.*)$')
FIELD_CHAIN_RE = re.compile(
    r'^(L\d+_\d+)\s*=\s*(L\d+_\d+)\.([a-z_][a-z0-9_]*)\s*$'
)
TABLE_FIELD_ASSIGN_RE = re.compile(
    r'^([A-Z]\d+_\d+)\.([a-z_][a-z0-9_]*)\s*=\s*"([^"]+)"\s*$'
)
FOR_LOOP_RE = re.compile(
    r'^\s*for\s+(\w+)(?:\s*,\s*(\w+))?(?:\s*,\s*\w+)?\s+in\s+(.+?)\s+do\s*$'
)
LOCAL_IDENT_RE = re.compile(r'\b(L\d+_\d+)\b')
FIELD_READ_RE = re.compile(r'\b(L\d+_\d+)\.([a-z_][a-z0-9_]*)')


def find_cachable_locals(text: str) -> set[str]:
    """Locals (e.g. {"L0_1"}) bound to the Cachable module in this file."""
    cachable: set[str] = set()
    import_bound: set[str] = set()
    string_assigns: dict[str, str] = {}
    for ln in text.splitlines():
        s = ln.strip()
        m_imp = CACHABLE_IMPORT_RE.match(s)
        if m_imp:
            import_bound.add(m_imp.group(1))
            continue
        m_str = STRING_ASSIGN_RE.match(s)
        if m_str:
            string_assigns[m_str.group(1)] = m_str.group(2)
            continue
        m_call = CALL_REASSIGN_RE.match(s)
        if m_call:
            lhs, callee, arg = m_call.group(1), m_call.group(2), m_call.group(3)
            if (lhs == callee and callee in import_bound
                    and string_assigns.get(arg) == "Cachable"):
                cachable.add(lhs)
    return cachable


def find_cache_key_objs(text: str, cache_key: str) -> set[str]:
    """Locals whose `.cache_key` field equals the target cache_key."""
    objs: set[str] = set()
    for ln in text.splitlines():
        m = TABLE_FIELD_ASSIGN_RE.match(ln.strip())
        if m and m.group(2) == "cache_key" and m.group(3) == cache_key:
            objs.add(m.group(1))
    return objs


def find_function_bodies(text: str):
    """Yield (start_line, end_line, body_lines) for each function block.

    Lua's `function ... end` is brace-less; we depth-track on the
    function/end keyword stream. Inner functions are emitted as separate
    bodies, but they're contained within their outer body too, so the
    harvest visits both via the same call site (outer body sees the
    inner-function definition; inner body sees its own reads).
    """
    lines = text.splitlines()
    starts: list[int] = []
    for i, ln in enumerate(lines):
        if re.search(r'\bfunction\b', ln):
            starts.append(i)
    for s in starts:
        depth = 0
        end = None
        for j in range(s, len(lines)):
            for tok in re.findall(r'\b(function|end)\b', lines[j]):
                if tok == "function":
                    depth += 1
                else:
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
            if end is not None:
                break
        if end is not None:
            yield s, end, lines[s:end + 1]


def harvest_cache_consumer(
    body: list[str],
    cachable_locals: set[str],
    cache_key_objs: set[str],
    cache_key: str,
) -> set[str]:
    """Harvest field paths relative to the cache root.

    Returns paths like "exchange_point_list", "exchange_point_list.rarity",
    "user_info.elapsed_time_from_login". Caller prefixes with "response_data."
    to make them merge-compatible.

    Returns the empty set if no Cachable.get(cache_key) call was found
    in this function body — i.e. this body isn't a consumer of the
    target cache_key.
    """
    fields: set[str] = set()
    # Locals currently holding the Cachable.get method ref.
    get_fns: set[str] = set()
    # local -> path prefix relative to cache root ("" = root).
    taint: dict[str, str] = {}
    found_get_call = False

    # Pre-walk to build a line-indexed view of recent assignments. We
    # need to look BACK from a Cachable.get call site to see what the
    # arg was bound to — STRING_ASSIGN_RE for inline literals, or
    # FIELD_CHAIN_RE for `arg = <obj>.cache_key`.
    body_idx = {id(ln): i for i, ln in enumerate(body)}

    for ln in body:
        s = ln.strip()

        # (1) HARVEST against currently-tainted locals BEFORE we mutate
        # taint on this line. Critical for reassignment patterns like
        # `L2_2 = L2_2.exchange_point_list` — the read should attribute
        # to the OLD prefix of L2_2 (== "" cache root), not the new one.
        for local, prefix in list(taint.items()):
            for f in re.findall(
                rf'\b{re.escape(local)}\.([a-z_][a-z0-9_]*)', s
            ):
                path = f"{prefix}.{f}" if prefix else f
                fields.add(path)

        # (2) BIND .get method off the Cachable module local.
        m = GET_BIND_RE.match(s)
        if m and m.group(2) in cachable_locals:
            get_fns.add(m.group(1))
            continue

        # (3) CALL get(cache_key) — taint lhs at cache root.
        m = CALL_REASSIGN_RE.match(s)
        if m:
            lhs, callee, arg = m.group(1), m.group(2), m.group(3)
            if callee in get_fns and _arg_is_cache_key(
                arg, body, body_idx[id(ln)], cache_key, cache_key_objs
            ):
                taint[lhs] = ""
                found_get_call = True
                continue

        # (4) FIELD CHAIN: `lhs = src.field`. If src is tainted, lhs
        # inherits taint at the new subpath.
        m = FIELD_CHAIN_RE.match(s)
        if m:
            lhs, src, fld = m.group(1), m.group(2), m.group(3)
            if src in taint:
                prefix = taint[src]
                taint[lhs] = f"{prefix}.{fld}" if prefix else fld
            elif lhs in taint:
                # lhs reassigned to a non-tainted RHS — detaint.
                del taint[lhs]
            continue

        # (5) GENERIC LOCAL-TO-LOCAL COPY: `lhs = src` preserves taint.
        m = ASSIGN_RE.match(s)
        if m:
            lhs = m.group(1)
            rhs = m.group(2).strip()
            m_copy = re.match(r'^(L\d+_\d+)\s*$', rhs)
            if m_copy and m_copy.group(1) in taint:
                taint[lhs] = taint[m_copy.group(1)]
            elif lhs in taint:
                # Reassigned to a non-tainted RHS.
                del taint[lhs]
            # Fall through — we still want for-loop detection.

        # (6) FOR-LOOP ITERATION: `for K, V in iter do` — if any local
        # in iter is tainted, V (or K if no V) inherits the prefix.
        m = FOR_LOOP_RE.match(s)
        if m:
            k_var, v_var, iter_expr = m.group(1), m.group(2), m.group(3)
            target = v_var or k_var
            if target:
                for ident in LOCAL_IDENT_RE.findall(iter_expr):
                    if ident in taint:
                        taint[target] = taint[ident]
                        break

    return fields if found_get_call else set()


def _arg_is_cache_key(
    arg: str,
    body: list[str],
    call_idx: int,
    cache_key: str,
    cache_key_objs: set[str],
) -> bool:
    """Walk backward from a call site to see if arg resolves to cache_key.

    Two cases:
      1. Inline literal: `arg = "$cacheKey"` somewhere earlier.
      2. Module-table field: `arg = L6_1.cache_key` where L6_1 was
         set up with `L6_1.cache_key = "$cacheKey"` at module scope.
    """
    for prev in body[:call_idx][::-1]:
        ps = prev.strip()
        m_str = STRING_ASSIGN_RE.match(ps)
        if m_str and m_str.group(1) == arg:
            return m_str.group(2) == cache_key
        m_chain = FIELD_CHAIN_RE.match(ps)
        if (m_chain and m_chain.group(1) == arg
                and m_chain.group(3) == "cache_key"
                and m_chain.group(2) in cache_key_objs):
            return True
        # If arg got reassigned to something we DIDN'T recognize, the
        # walk-back is inconclusive — bail rather than risk a false hit
        # against an earlier-but-shadowed binding.
        if re.match(rf'^{re.escape(arg)}\s*=', ps):
            return False
    return False


def process_endpoint(
    ep: str,
    cache_key: str,
    ui_files: list[str],
) -> dict | None:
    """Run the cache-consumer harvest for a single endpoint.

    Returns a trace dict (merge-compatible) or None if no UI file in the
    list contained a Cachable.get(cache_key) consumer.
    """
    all_fields: set[str] = set()
    source_files: list[str] = []

    for f in ui_files:
        p = SOURCE_ROOT / f
        if not p.exists():
            continue
        text = p.read_text(errors="replace")
        if f'"{cache_key}"' not in text:
            continue
        cachable_locals = find_cachable_locals(text)
        if not cachable_locals:
            continue
        cache_key_objs = find_cache_key_objs(text, cache_key)
        file_fields: set[str] = set()
        for _start, _end, body in find_function_bodies(text):
            file_fields |= harvest_cache_consumer(
                body, cachable_locals, cache_key_objs, cache_key
            )
        if file_fields:
            all_fields |= file_fields
            source_files.append(f)

    if not all_fields:
        return None

    accessed_keys = sorted({
        f"response_data.{p}" if p else "response_data"
        for p in all_fields
    })
    return {
        "endpoint": ep,
        "kind": "cache_consumer_extraction",
        "accessed_keys": accessed_keys,
        "raw_fields": sorted(all_fields),
        "source_files": sorted(source_files),
    }


def files_referencing_cache_key(cache_key: str) -> list[str]:
    """rg-grep for the cache_key string literal across the decompiled tree.

    Restricted to UI/model files (m_*/, common/) — svapi/ is the producer
    side, not a consumer. The cache_key starts with `$` which we escape
    in the regex.
    """
    if not cache_key.startswith("$"):
        return []
    pat = f'"\\{cache_key}"'
    try:
        out = subprocess.run(
            ["rg", "-l", "--type", "lua", "--no-messages", pat,
             str(SOURCE_ROOT)],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    files: list[str] = []
    for line in out.stdout.splitlines():
        rel = line.replace(str(SOURCE_ROOT) + "/", "")
        if "/svapi/" in rel:
            continue
        if not (rel.startswith("m_") or rel.startswith("common/")):
            continue
        files.append(rel)
    files.sort()
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out", type=Path, default=DEFAULT_OUT,
        help=f"Output dir (default: {DEFAULT_OUT.relative_to(ROOT)})",
    )
    args = ap.parse_args()

    if not MERGED_PATH.exists():
        sys.exit(
            f"missing {MERGED_PATH.relative_to(ROOT)} — "
            "run extract_apis first."
        )

    merged = json.load(MERGED_PATH.open())

    # Scan every endpoint with a cache_key. The grep is cheap (~1ms per
    # cache_key) and lets us catch endpoints that the pre-merge bucketer
    # already lifted to harness-covered but where novel response fields
    # still hide in a Cachable.get consumer.
    targets: list[tuple[str, str, list[str]]] = []
    for ep, val in merged.items():
        cache_key = (val or {}).get("cache_key") or ""
        if not cache_key:
            continue
        ui_files = files_referencing_cache_key(cache_key)
        if not ui_files:
            continue
        targets.append((ep, cache_key, ui_files))

    args.out.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    n_with = 0
    n_no_hit = 0
    total_fields = 0
    for ep, cache_key, ui_files in sorted(targets):
        trace = process_endpoint(ep, cache_key, ui_files)
        if not trace:
            n_no_hit += 1
            continue
        n_with += 1
        total_fields += len(trace["accessed_keys"])
        (args.out / f"{ep}.json").write_text(
            json.dumps(trace, indent=2, sort_keys=False)
        )

    elapsed = time.monotonic() - start
    print(
        f"done: {n_with} endpoints with >=1 harvested field, "
        f"{n_no_hit} scanned but no Cachable.get consumer; "
        f"{total_fields} field paths total; {elapsed:.1f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
