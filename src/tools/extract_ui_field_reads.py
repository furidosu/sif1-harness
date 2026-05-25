"""Static field-extraction over decompiled UI source for the ui-only bucket.

The runtime listener pass (run_lua_harness) only reaches what registered
Cachable listeners destructure. For many endpoints the destructure lives
INSIDE the per-call success closure that the application passes to
svapi.<module>.<action>(success_cb, ...) -- code the harness doesn't run
because it doesn't issue the request, only fires notifyUpdate. Those
success closures DO survive in the decompiled bytecode: as inner
`function(A0_*) ... end` definitions whose first arg is destructured
via `A0_*.response_data.<field>` or `local L = A0_*.response_data;
L.<field>`.

This tool walks every UI file referenced by an endpoint's cache_key or
fn_name, finds inner-function bodies matching that anchor pattern, and
harvests the field names they read. Output is per-endpoint JSON in
build/runtime/traces_static/, structured to be unioned in by
merge_observations.py exactly like the runtime traces.

Two layers reduce noise:

  - Anchor specificity: only collect fields off a local that is
    provably bound (in source) to the function's first arg's
    response_data. Co-mingled UI state on unrelated variables is
    ignored.
  - Corpus filter: a field name appearing on > CORPUS_THRESHOLD of all
    UI files is flagged as a UI-wide token (e.g. `appear`, `middle`,
    `ok`, `is_open`) and dropped. Computed once across the full
    m_*/ tree.

Usage:
  uv run --no-project python src/tools/extract_ui_field_reads.py
  uv run --no-project python src/tools/extract_ui_field_reads.py --bucket ui-only
  uv run --no-project python src/tools/extract_ui_field_reads.py --endpoint duty.startWait
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from _lua_worker import LuaWorkerBase

ROOT = Path(__file__).resolve().parents[2]
HARNESS_DIR = ROOT / "src" / "harness"
SOURCE_ROOT = ROOT / "assets" / "decompiled" / "all"
MERGED_PATH = ROOT / "build" / "merged_endpoints.json"
EXTRACTED_PATH = ROOT / "build" / "extracted_apis.json"
COVERAGE_PATH = ROOT / "build" / "coverage_classification.json"
DEFAULT_TRACES_DIR = ROOT / "build" / "runtime" / "traces_static"

# Field name appearing on > this fraction of UI files is treated as a
# UI-wide token, not a per-endpoint response field. 25% is the empirical
# elbow on the corpus -- tighter drops real fields (e.g. `unit_list`
# appears legitimately on many endpoints), looser keeps `appear`/`ok`.
CORPUS_THRESHOLD = 0.25

# Envelope fields the aggregator already knows about; drop them so they
# don't double-count as discoveries.
ENVELOPE_FIELDS = {
    "response_data", "status_code", "release_info",
    "server_timestamp", "server_timestamp_sync_flag",
    "present_cnt", "museum_info",
}

# Function declaration: `function ...(args)` -- captures (optional name, args).
# Matches `function foo(a)`, `function tbl.foo(a)`, `function(a)`,
# `local foo = function(a)`, etc.
FN_DECL_RE = re.compile(r"\bfunction\s*([\w.:]*)\s*\(([^)]*)\)")

# Local assignment of an anonymous function: `local foo = function(a)` or
# `foo = function(a)`. Used by helper-following to map identifier -> body
# when the source uses the local-assignment form instead of a named
# function declaration.
LOCAL_FN_ASSIGN_RE = re.compile(
    r"^\s*(?:local\s+)?(\w+)\s*=\s*function\s*\(([^)]*)\)"
)

# Builtins / keywords / language ops we should NEVER try to follow as a
# helper call. Decompiled bytecode rarely has these as callees of
# tainted args, but a guard keeps recursion deterministic.
BUILTIN_CALLEES = frozenset({
    "pcall", "xpcall", "assert", "print", "tostring", "tonumber", "select",
    "ipairs", "pairs", "type", "setmetatable", "getmetatable",
    "rawget", "rawset", "rawequal", "next", "unpack", "error", "require",
    "if", "while", "for", "return", "function", "and", "or", "not",
    "string", "table", "math", "io", "os", "debug", "package", "coroutine",
})

# Lua block-opening / closing keywords. Note `do` is NOT in this list:
# `for ... do ... end` and `while ... do ... end` are a single block per
# the language grammar; `for`/`while` opens and `end` closes (paired with
# `do` only syntactically). Counting `do` would double-open and the
# balance counter would never reach 0. Standalone `do ... end` blocks are
# essentially nonexistent in decompiled bytecode; if we hit one, we'll
# accumulate one stray close — recoverable in practice but flagged here
# as a known limitation.
OPEN_RE = re.compile(r"\b(function|if|for|while|repeat)\b")
CLOSE_RE = re.compile(r"\b(end|until)\b")

# Lua single-line comments and strings should be stripped before block-
# balance counting, otherwise a keyword inside a comment or string
# corrupts the depth. Decompiled bytecode rarely has either, but the
# guard is cheap.
COMMENT_RE = re.compile(r"--[^\n]*")
STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')

# `local foo = bar` -- captures (lhs, rhs_chain) for taint propagation.
# Decompiled bytecode emits one local per line.
LOCAL_ASSIGN_RE = re.compile(r"^\s*local\s+(\w+)\s*=\s*(.+?)\s*$")

# Also handle bare `lhs = rhs` (no `local`); the decompiler sometimes
# reuses locals.
BARE_ASSIGN_RE = re.compile(r"^\s*(\w+)\s*=\s*(.+?)\s*$")

# `for K[, V] in <iter_expr> do` -- captures (K, V_or_None, iter_expr).
# Used to propagate taint: when iter_expr references a tainted local,
# the value variable V (or K if no V) inherits the same prefix because
# list-element indices `.[N]` collapse during merger normalization, so
# `cached.unit_list.[1].id` and `cached.unit_list.id` end up at the
# same path.
FOR_ITER_RE = re.compile(r"\bfor\s+(\w+)\s*(?:,\s*(\w+))?\s+in\s+(.+?)\s+do\b")


def strip_noise(line: str) -> str:
    line = STRING_RE.sub('""', line)
    line = COMMENT_RE.sub("", line)
    return line


def find_function_bodies(text: str):
    """Yield (name_or_none, first_arg, body_start_line, body_end_line, lines_slice).

    body_end_line is INCLUSIVE -- the line containing the matching `end`.
    Nested functions yield their own (start, end) ranges; the outer one
    still spans the whole block (nesting is via the balance counter).
    """
    lines = text.splitlines()
    n = len(lines)
    stripped = [strip_noise(line) for line in lines]
    for i in range(n):
        m = FN_DECL_RE.search(stripped[i])
        local_assign_m = None
        # `local foo = function(a)` -- the FN_DECL_RE captures `function(a)`
        # without the assigned name. Check separately so we can still index
        # the body by identifier for helper-following.
        if m and not m.group(1):
            local_assign_m = LOCAL_FN_ASSIGN_RE.search(stripped[i])
        if not m:
            continue
        name = m.group(1) or None
        if not name and local_assign_m:
            name = local_assign_m.group(1)
        args = m.group(2).strip()
        if not args:
            continue
        first_arg = args.split(",")[0].strip()
        if not first_arg or not re.match(r"^\w+$", first_arg):
            continue
        depth = 1
        tail = stripped[i][m.end():]
        depth += len(OPEN_RE.findall(tail))
        depth -= len(CLOSE_RE.findall(tail))
        if depth <= 0:
            continue
        end_line = -1
        for j in range(i + 1, n):
            sj = stripped[j]
            depth += len(OPEN_RE.findall(sj))
            depth -= len(CLOSE_RE.findall(sj))
            if depth <= 0:
                end_line = j
                break
        if end_line < 0:
            continue
        yield name, first_arg, i, end_line, lines[i:end_line + 1]


def build_named_function_index(text: str) -> dict[str, tuple[str, list[str]]]:
    """Identifier -> (first_arg, body_lines) for every function declaration
    or local-assignment-of-function in the file. Used by helper-following:
    when a closure passes a tainted local to `<helper>(<L>)`, we look up
    <helper> in this index to recurse into its body.

    Decompiled bytecode tends to reuse the same local names across many
    function definitions, so later defs OVERWRITE earlier ones. That's
    fine for our purposes -- we want the most recently defined body for a
    given identifier in file order, which is usually the right one when
    the call site appears further down the file.
    """
    out: dict[str, tuple[str, list[str]]] = {}
    for name, first_arg, _start, _end, body in find_function_bodies(text):
        if name:
            out[name] = (first_arg, body)
    return out


# Generic `<callee>(<arg_list>)` matcher used for helper-following. Captures
# the callee identifier and the raw arg list; arg parsing is done after.
HELPER_CALL_RE = re.compile(r"\b(\w+)\s*\(([^)]*)\)")

# `local? <lhs> = <ident>.<field_chain>` -- captures the immediate parent
# ident so we can decide whether the assignment is to a tainted local's
# field (taint-propagating) or to something else (taint-clearing).
LOCAL_FIELD_ASSIGN_RE = re.compile(
    r"^\s*(?:local\s+)?(\w+)\s*=\s*(\w+)\.([a-z_][a-z0-9_]*)\s*$"
)

# `local? <lhs> = <ident>` exact -- copies the value (and taint) wholesale.
LOCAL_COPY_ASSIGN_RE = re.compile(r"^\s*(?:local\s+)?(\w+)\s*=\s*(\w+)\s*$")

# `local? <lhs> = <expr>` general -- if neither of the above match but
# this does, the assignment overwrote the lhs with an unrelated value
# and the lhs should be DE-TAINTED. We use this to invalidate stale
# taint after locals are reused (very common in decompiled bytecode).
ANY_ASSIGN_RE = re.compile(r"^\s*(?:local\s+)?(\w+)\s*=")


def harvest_one_function(
    arg_name: str,
    body: list[str],
    *,
    file_fn_index: dict[str, tuple[str, list[str]]] | None = None,
    base_prefix: str | None = None,
    visited: set[str] | None = None,
    max_depth: int = 2,
) -> set[str]:
    """Harvest response_data-relative field paths from a function body.

    Top-level invocation: base_prefix=None (the closure receives the
    envelope; the entry point is `<arg>.response_data[.<field>]`).

    Recursive invocation (helper-following): base_prefix=str. The
    closure passed a tainted local to a helper; that helper's first arg
    represents the subtree at `response_data.<base_prefix>`. Reads of
    `<arg>.<field>` map to `response_data.<base_prefix>.<field>`.

    Single-pass design: walk the body in source order, maintaining a
    `taint` map (local_name -> response_data subpath). At each line we
    (a) harvest field reads against the CURRENT taint, then (b) update
    the taint based on assignments. Decompiled bytecode reuses local
    names heavily, so a reassignment to an untainted RHS must
    DE-TAINT the lhs -- otherwise every subsequent `<lhs>.<field>` is
    wrongly attributed to the response subtree the local previously
    held.
    """
    if visited is None:
        visited = set()
    fields: set[str] = set()

    # Initial taint. In subtree mode, arg itself is tainted at base_prefix.
    # In envelope mode, no taint until we see `<arg>.response_data` capture.
    taint: dict[str, str] = {}
    if base_prefix is not None:
        taint[arg_name] = base_prefix

    # Envelope-mode anchors -- only used when base_prefix is None.
    if base_prefix is None:
        rd_field_re = re.compile(
            rf"\b{re.escape(arg_name)}\.response_data\.([a-z_][a-z0-9_]*)"
        )
        rd_root_re = re.compile(rf"^{re.escape(arg_name)}\.response_data$")
        rd_root_field_re = re.compile(
            rf"^{re.escape(arg_name)}\.response_data\.([a-z_][a-z0-9_]*)$"
        )
    else:
        rd_field_re = rd_root_re = rd_root_field_re = None

    # Per-line alias map for helper-following: <local> -> <function_ident>.
    alias_map: dict[str, str] = {}
    # Per-line helper-call candidates: list of (line_text, taint_snapshot,
    # alias_snapshot). We collect during the single pass and recurse at
    # the end so a helper's harvest doesn't pollute the outer taint state.
    pending_recurse: list[tuple[str, dict[str, str], dict[str, str]]] = []

    ident_re = re.compile(r"\b(\w+)\b")

    for line in body:
        s = strip_noise(line)

        # (1) ENVELOPE-MODE HARVEST: direct `<arg>.response_data.<field>`
        # reads. Only matters when base_prefix is None.
        if rd_field_re is not None:
            for f in rd_field_re.findall(s):
                fields.add(f)

        # (2) GENERAL HARVEST: for every currently-tainted local, harvest
        # `<local>.<field>` reads on this line. Done BEFORE taint updates
        # so reassignments take effect only on subsequent lines.
        for local_name, local_prefix in list(taint.items()):
            tre = re.compile(rf"\b{re.escape(local_name)}\.([a-z_][a-z0-9_]*)")
            for f in tre.findall(s):
                path = f"{local_prefix}.{f}" if local_prefix else f
                fields.add(path)

        # (3) FOR-LOOP ITERATION TAINT. `for K, V in <iter> do` -- if any
        # ident in <iter> is currently tainted, V (or K if no V) inherits
        # that prefix (list-element index collapses in merger).
        fm = FOR_ITER_RE.search(s)
        if fm:
            k_var, v_var, iter_expr = fm.group(1), fm.group(2), fm.group(3)
            target = v_var or k_var
            if target:
                for ident in ident_re.findall(iter_expr):
                    if ident in taint:
                        taint[target] = taint[ident]
                        break

        # (4) HELPER-CALL CANDIDATES: queue for post-pass recursion. We
        # snapshot taint + alias so the helper's body sees the state at
        # the call site, not a stale-or-future one.
        if file_fn_index is not None and max_depth > 0:
            for m in HELPER_CALL_RE.finditer(s):
                callee = m.group(1)
                if callee in BUILTIN_CALLEES or callee in visited:
                    continue
                args_str = m.group(2)
                first_arg = args_str.split(",")[0].strip()
                if not first_arg or first_arg not in taint:
                    continue
                resolved = callee if callee in file_fn_index else alias_map.get(callee)
                if not resolved or resolved not in file_fn_index or resolved in visited:
                    continue
                pending_recurse.append(
                    (resolved, dict(taint), {"first_arg": first_arg})
                )

        # (5) ASSIGNMENT-BASED TAINT UPDATES. Order matters: check the
        # most specific patterns first.
        #
        # Envelope-mode captures: `lhs = <arg>.response_data[.<f>]?`
        if rd_root_re is not None:
            cap_done = False
            mfa = ANY_ASSIGN_RE.match(s)
            if mfa:
                lhs = mfa.group(1)
                # Get just the RHS for shape testing.
                rhs_match = re.match(r"^\s*(?:local\s+)?\w+\s*=\s*(.*?)\s*$", s)
                if rhs_match:
                    rhs = rhs_match.group(1)
                    if rd_root_re.match(rhs):
                        taint[lhs] = ""
                        cap_done = True
                    else:
                        mfx = rd_root_field_re.match(rhs)
                        if mfx:
                            taint[lhs] = mfx.group(1)
                            fields.add(mfx.group(1))
                            cap_done = True
            if cap_done:
                continue  # don't fall through to generic ident/field handling

        # Identifier copy: `lhs = rhs_ident` -- alias for helper-following
        # AND propagates taint if rhs_ident is tainted.
        ma = LOCAL_COPY_ASSIGN_RE.match(s)
        if ma:
            lhs, rhs = ma.group(1), ma.group(2)
            alias_map = dict(alias_map)
            # Transitive resolution through alias chain.
            target = rhs
            seen_chain = {lhs}
            while target in alias_map and target not in seen_chain:
                seen_chain.add(target)
                target = alias_map[target]
            alias_map[lhs] = target
            # Taint propagation: if rhs is tainted, copy. Otherwise
            # DE-TAINT lhs.
            if rhs in taint:
                taint[lhs] = taint[rhs]
            elif lhs in taint:
                del taint[lhs]
            continue

        # Field-access copy: `lhs = ident.field` -- propagates taint if
        # ident is tainted (prefix.field).
        mf = LOCAL_FIELD_ASSIGN_RE.match(s)
        if mf:
            lhs, rhs_ident, rhs_field = mf.group(1), mf.group(2), mf.group(3)
            if rhs_ident in taint:
                parent_prefix = taint[rhs_ident]
                new_prefix = f"{parent_prefix}.{rhs_field}" if parent_prefix else rhs_field
                taint[lhs] = new_prefix
                fields.add(new_prefix)
            elif lhs in taint:
                del taint[lhs]
            # Also note this isn't an identifier alias for helper-following
            # purposes (alias_map tracks only ident-to-ident).
            continue

        # Generic assignment to ANY rhs -- if the RHS isn't one of the
        # taint-propagating forms above, DE-TAINT the lhs (it now holds
        # an unrelated value). This is critical because decompiled
        # bytecode aggressively reuses locals.
        ag = ANY_ASSIGN_RE.match(s)
        if ag:
            lhs = ag.group(1)
            if lhs in taint:
                del taint[lhs]
            # Drop alias too -- the local no longer points at the
            # previously-aliased function ident.
            if lhs in alias_map:
                alias_map = dict(alias_map)
                del alias_map[lhs]

    # Recurse into queued helper calls. Outer taint is already finalized;
    # each helper gets its own subtree-mode harvest.
    for resolved, taint_snapshot, info in pending_recurse:
        first_arg = info["first_arg"]
        if first_arg not in taint_snapshot:
            continue
        helper_arg, helper_body = file_fn_index[resolved]
        new_visited = visited | {resolved}
        sub_fields = harvest_one_function(
            helper_arg,
            helper_body,
            file_fn_index=file_fn_index,
            base_prefix=taint_snapshot[first_arg],
            visited=new_visited,
            max_depth=max_depth - 1,
        )
        fields |= sub_fields

    return fields


def find_files_for_endpoint(action: str, fn_name: str, cache_key: str | None) -> list[Path]:
    if not SOURCE_ROOT.exists():
        return []
    patterns = [rf"\.{fn_name}\b", rf"\.{fn_name}Stub\b"]
    if cache_key and cache_key.startswith("$"):
        patterns.append(f'"\\{cache_key}"')
    pat = "|".join(patterns)
    try:
        out = subprocess.run(
            ["rg", "-l", "--type", "lua", "--no-messages", pat, str(SOURCE_ROOT)],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    files: list[Path] = []
    for line in out.stdout.splitlines():
        rel = line.replace(str(SOURCE_ROOT) + "/", "")
        # Skip svapi / svext (the binding files themselves, not consumers).
        if rel.startswith("common/svapi/") or rel.startswith("common/svext/"):
            continue
        files.append(Path(line))
    return files


def build_corpus_frequency() -> dict[str, int]:
    """For each candidate field name, count how many UI files contain it
    as a `\\.field` token. Used to drop UI-wide tokens that aren't
    response fields.
    """
    if not SOURCE_ROOT.exists():
        return {}
    # Walk every m_* file once; counting per-file presence (not per-line).
    counts: Counter[str] = Counter()
    files = list(SOURCE_ROOT.glob("m_*/**/*.lua"))
    tok_re = re.compile(r"\.([a-z_][a-z0-9_]*)")
    for fp in files:
        try:
            text = fp.read_text(errors="replace")
        except OSError:
            continue
        seen_in_file: set[str] = set()
        for m in tok_re.finditer(text):
            seen_in_file.add(m.group(1))
        for tok in seen_in_file:
            counts[tok] += 1
    counts["__total_files__"] = len(files)
    return dict(counts)


def filter_by_corpus(fields: set[str], corpus: dict[str, int]) -> tuple[set[str], set[str]]:
    """Returns (kept, dropped). Drops field paths whose LEAF token
    appears in > CORPUS_THRESHOLD of UI files, OR whose path contains
    "response_data" as a non-leading segment (a leak from recursion into
    a helper that destructures arg.response_data despite being passed
    a subtree, not an envelope -- the path is structurally wrong).
    """
    total = corpus.get("__total_files__", 0)
    if total <= 0:
        return fields, set()
    threshold = total * CORPUS_THRESHOLD
    kept: set[str] = set()
    dropped: set[str] = set()
    for path in fields:
        segments = path.split(".")
        leaf = segments[-1]
        if leaf in ENVELOPE_FIELDS:
            dropped.add(path)
            continue
        # "response_data" should never appear as an internal path segment;
        # if it does, the recursion mismatched an envelope-shaped helper
        # against a subtree-shaped arg.
        if "response_data" in segments:
            dropped.add(path)
            continue
        if corpus.get(leaf, 0) > threshold:
            dropped.add(path)
        else:
            kept.add(path)
    return kept, dropped


def extract_for_endpoint(
    ep: str,
    merged_entry: dict,
    cache_key: str | None,
    corpus: dict[str, int],
) -> dict:
    action = ep.split(".", 1)[1] if "." in ep else ep
    fn_name = merged_entry.get("fn_name") or action
    files = find_files_for_endpoint(action, fn_name, cache_key)

    raw: set[str] = set()
    per_file: dict[str, list[str]] = {}
    for fp in files:
        try:
            text = fp.read_text(errors="replace")
        except OSError:
            continue
        # File-wide function index for helper-following.
        fn_index = build_named_function_index(text)
        file_fields: set[str] = set()
        for _name, arg_name, _start, _end, body in find_function_bodies(text):
            # Pre-check: skip the body unless it contains the response_data
            # anchor at all. Faster than running the full harvester.
            if "response_data" not in "".join(body):
                continue
            file_fields |= harvest_one_function(
                arg_name, body, file_fn_index=fn_index
            )
        rel = str(fp.relative_to(SOURCE_ROOT))
        if file_fields:
            per_file[rel] = sorted(file_fields)
            raw |= file_fields

    kept, dropped = filter_by_corpus(raw, corpus)
    accessed = sorted(f"response_data.{p}" for p in kept)
    return {
        "endpoint": ep,
        "kind": "static_extraction",
        "accessed_keys": accessed,
        "raw_fields": sorted(raw),
        "dropped_by_corpus_filter": sorted(dropped),
        "source_files": sorted(per_file.keys()),
    }


def pick_lua_runtime() -> str:
    for cand in ("luajit", "lua5.1", "lua-5.1"):
        if shutil.which(cand):
            return cand
    sys.exit("no Lua runtime found (need luajit or lua5.1)")


class Worker(LuaWorkerBase):
    def __init__(self, lua_bin: str, source_root: Path):
        super().__init__(lua_bin, source_root, HARNESS_DIR)


def build_augmented_candidate(extracted_fields: list[str]) -> dict:
    """Build a response_data dict that surfaces every extracted field as
    a populated key. Two-level paths (e.g. `event_team_duty.item_bonus_list`)
    are nested. Empty dicts and lists give listeners something to iterate
    without crashing on nil.
    """
    rd: dict = {}
    for path in extracted_fields:
        # Strip leading "response_data." if present.
        path = path[len("response_data."):] if path.startswith("response_data.") else path
        parts = path.split(".")
        cur = rd
        for i, seg in enumerate(parts):
            last = i == len(parts) - 1
            if last:
                # Heuristic: name ends in _list / _info -> list/object. Otherwise
                # empty object so any deeper access lands on a sentinel.
                if cur.get(seg) is None:
                    if seg.endswith("_list") or seg.endswith("_lists"):
                        cur[seg] = [{}]
                    else:
                        cur[seg] = {}
            else:
                if not isinstance(cur.get(seg), dict):
                    cur[seg] = {}
                cur = cur[seg]
    return {"response_data": rd, "status_code": 200, "release_info": []}


def verify_via_listener(
    worker: Worker,
    ep: str,
    merged_entry: dict,
    cache_key: str | None,
    extracted_fields: list[str],
) -> dict:
    """Re-run the listener pass with an augmented candidate that populates
    every statically-extracted field. Uses the DEFAULT dispatch (the same
    path run_lua_harness uses) so the svapi function gets called with
    phantom args -- that registers and fires the per-call success closures
    that are the actual destructure sites. Any extracted field the closure
    or a registered listener reads off the augmented envelope shows up in
    accessed_keys -> verified.

    Fields not in `verified` aren't necessarily wrong: they may be read
    purely in UI code (post-listener, outside any cache observer), in which
    case no listener will ever touch them. They stay in `static_only` as
    lower-confidence candidates.
    """
    if not extracted_fields:
        return {"listener_accessed": [], "verified": [], "skipped": "no extracted fields"}
    candidate = build_augmented_candidate(extracted_fields)
    # Build the same job shape run_lua_harness uses (no `kind` -> default
    # dispatch). fn_name and alt_fn_names come from merged_endpoints so
    # we hit the right svapi function (often qualified like `dutyStartWait`).
    action = merged_entry.get("action") or ep.split(".", 1)[1]
    req = merged_entry.get("request") or {}
    arity = len(req.get("ours") or req.get("theirs") or [])
    job = {
        "module": merged_entry["module"],
        "action": action,
        "fn_name": merged_entry.get("fn_name") or action,
        "candidate_response": candidate,
        "request_arity": arity or 1,
        "cache_key": cache_key or "",
        "svapi_file": merged_entry.get("svapi_file"),
        "alt_fn_names": merged_entry.get("alt_fn_names") or [],
    }
    try:
        trace = worker.run(job, timeout_s=10.0)
    except RuntimeError as e:
        return {"listener_accessed": [], "verified": [], "skipped": f"worker error: {e}"}
    accessed = trace.get("accessed_keys") or []

    def norm(p: str) -> str:
        return ".".join(
            s for s in p.split(".") if not (s.startswith("[") and s.endswith("]"))
        )

    accessed_norm = sorted({norm(k) for k in accessed if k and "[table:" not in k})
    extracted_set = set(extracted_fields)
    verified = sorted(set(accessed_norm) & extracted_set)
    return {
        "listener_accessed": accessed_norm,
        "verified": verified,
        "errors": trace.get("errors") or [],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", action="append", default=[])
    ap.add_argument("--bucket", default=None,
                    help="restrict to this coverage bucket (default: ui-only)")
    ap.add_argument("--all", action="store_true",
                    help="run every endpoint in merged_endpoints.json")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the listener-verification pass")
    ap.add_argument("--out", default=str(DEFAULT_TRACES_DIR))
    args = ap.parse_args(argv)

    merged = json.load(MERGED_PATH.open())
    extracted = json.load(EXTRACTED_PATH.open())
    cache_keys: dict[str, str] = {}
    for _mod, val in extracted.items():
        for api in val.get("apis", []):
            ck = api.get("cache_key")
            if ck:
                cache_keys[f"{api['module']}.{api['action']}"] = ck

    keys: list[str]
    if args.endpoint:
        keys = list(args.endpoint)
    elif args.all:
        keys = sorted(merged.keys())
    else:
        # Default: ui-only (the bucket this tool exists to lift).
        bucket = args.bucket or "ui-only"
        coverage = json.load(COVERAGE_PATH.open())
        keys = sorted([
            ep for ep, c in coverage["endpoints"].items()
            if c["bucket"] == bucket
        ])

    traces_dir = Path(args.out)
    if not traces_dir.is_absolute():
        traces_dir = ROOT / args.out
    traces_dir.mkdir(parents=True, exist_ok=True)

    print(f"Building UI corpus frequency table...", file=sys.stderr)
    t0 = time.time()
    corpus = build_corpus_frequency()
    print(
        f"  scanned {corpus.get('__total_files__', 0)} files, "
        f"{len(corpus) - 1} unique field tokens, "
        f"threshold @ >{int(corpus.get('__total_files__', 0) * CORPUS_THRESHOLD)} files ({int(CORPUS_THRESHOLD*100)}%)",
        file=sys.stderr,
    )

    worker: Worker | None = None
    if not args.no_verify:
        lua_bin = pick_lua_runtime()
        print(f"Booting lua worker for verification ({lua_bin})...", file=sys.stderr)
        worker = Worker(lua_bin, SOURCE_ROOT)

    # Pre-load existing listener observations (production trace from
    # run_lua_harness) for the cross-check verification path. Cheap and
    # complementary: re-running with augmented candidates only fires
    # listeners whose dispatch fn exists; the production trace already
    # ran the dispatch for every endpoint.
    obs_path = ROOT / "build" / "runtime_listener_observations.json"
    listener_obs: dict[str, set[str]] = {}
    if obs_path.exists():
        raw = json.load(obs_path.open())
        for ep_k, rec in raw.items():
            if not isinstance(rec, dict):
                continue
            ep_keys = rec.get("runtime_accessed_keys") or []
            norm = set()
            for k in ep_keys:
                if not k.startswith("response_data."):
                    continue
                parts = [s for s in k.split(".") if not (s.startswith("[") and s.endswith("]"))]
                p = ".".join(parts)
                if p:
                    norm.add(p)
            listener_obs[ep_k] = norm

    n_with = 0
    n_zero = 0
    total_kept = 0
    total_dropped = 0
    total_verified = 0
    try:
        for ep in keys:
            entry = merged.get(ep)
            if not entry:
                continue
            trace = extract_for_endpoint(ep, entry, cache_keys.get(ep), corpus)
            kept_n = len(trace["accessed_keys"])
            dropped_n = len(trace["dropped_by_corpus_filter"])
            extracted_set = set(trace["accessed_keys"])
            verified_active: set[str] = set()
            verified_obs: set[str] = set()

            # Cross-check 1: production listener trace already captured by
            # run_lua_harness. Free (no re-run); only catches reads the
            # listener actually does in the production pipeline.
            verified_obs = extracted_set & listener_obs.get(ep, set())

            # Cross-check 2: re-run listener with augmented candidate. The
            # augmented candidate populates extracted fields, so listener
            # iterators (`for k,v in pairs(cached.unit_list)`) that were
            # no-ops on the empty production candidate may now iterate
            # and log per-element reads. Catches strictly more than
            # cross-check 1 when the listener has data-dependent paths.
            if worker is not None and kept_n > 0:
                vres = verify_via_listener(
                    worker, ep, entry, cache_keys.get(ep), trace["accessed_keys"]
                )
                trace["listener_accessed"] = vres.get("listener_accessed", [])
                if "skipped" in vres:
                    trace["verification_skipped"] = vres["skipped"]
                verified_active = set(vres.get("verified", []))

            verified = sorted(verified_active | verified_obs)
            trace["verified_by_listener"] = verified
            trace["verified_by_active_listener"] = sorted(verified_active)
            trace["verified_by_production_obs"] = sorted(verified_obs)
            trace["static_only"] = sorted(extracted_set - set(verified))
            verified_n = len(verified)
            (traces_dir / f"{ep}.json").write_text(json.dumps(trace, indent=2))
            total_kept += kept_n
            total_dropped += dropped_n
            total_verified += verified_n
            if kept_n > 0:
                n_with += 1
            else:
                n_zero += 1
            status = "OK " if kept_n > 0 else "-- "
            print(
                f"{status} {ep:50} files={len(trace['source_files']):2} "
                f"kept={kept_n:<3} dropped={dropped_n:<2} verified={verified_n}",
                file=sys.stderr,
            )
    finally:
        if worker is not None:
            worker.close()

    dt = time.time() - t0
    print(
        f"\ndone: {n_with} endpoints with >=1 kept field, "
        f"{n_zero} with 0; {total_kept} fields kept, "
        f"{total_dropped} dropped by corpus filter, "
        f"{total_verified} verified by listener; {dt:.1f}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
