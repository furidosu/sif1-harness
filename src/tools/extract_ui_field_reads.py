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
# Use the pre-merge bucket assignment — static extraction runs before
# merge, so reading the canonical post-merge coverage file would shrink
# the probe set spuriously. See classify_coverage.py --initial.
COVERAGE_PATH = ROOT / "build" / "coverage_classification_initial.json"
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

# Cache-consumer (Anchor 3) module-level patterns. Decompiled bytecode
# spreads the binding chain across multiple lines:
#
#   L0_1 = import
#   L1_1 = "Cachable"
#   L0_1 = L0_1(L1_1)              -- L0_1 now == Cachable module
#   ...
#   L6_1.cache_key = "$exchangeOwningPoint"
#   ...
#   L2_2 = L0_1.get                -- bind .get method off Cachable
#   L3_2 = L6_1.cache_key           -- arg via field-of-object, OR a
#   L2_2 = L2_2(L3_2)              --   "$key" string literal inline
#
# We pre-detect the cachable_locals (L0_1 here) and cache_key_objs (L6_1
# here) per-file so the harvest pass can recognize the call site without
# re-parsing the whole module.
CACHABLE_IMPORT_LINE_RE = re.compile(r'^(L\d+_\d+)\s*=\s*import\s*$')
STR_ASSIGN_LINE_RE = re.compile(r'^(L\d+_\d+)\s*=\s*"([^"]+)"\s*$')
CALL_REASSIGN_LINE_RE = re.compile(
    r'^(L\d+_\d+)\s*=\s*(L\d+_\d+)\((L\d+_\d+)\)\s*$'
)
TABLE_FIELD_LITERAL_RE = re.compile(
    r'^([A-Z]\d+_\d+)\.([a-z_][a-z0-9_]*)\s*=\s*"([^"]+)"\s*$'
)
# `L = X.get` -- binds .get method off an arbitrary local. The harvest
# uses this together with `cachable_locals` (which X must be in) to
# recognize a Cachable.get binding.
GET_BIND_LINE_RE = re.compile(r'^(L\d+_\d+)\s*=\s*(L\d+_\d+)\.get\s*$')
# Same-line return capture: `return <local>` or just `return`. The
# stripped-noise body lets us match on this loosely.
RETURN_LOCAL_RE = re.compile(r'^\s*return\s+(\w+)\s*$')


def find_cachable_locals(text: str) -> set[str]:
    """Return locals (e.g. {"L0_1"}) bound to the Cachable module in this file.

    Detects the three-line pattern:
        L<X> = import
        L<Y> = "Cachable"
        L<X> = L<X>(L<Y>)
    where the first and third lines bind the same local X. Multiple
    Cachable bindings per file are rare but supported.
    """
    cachable: set[str] = set()
    import_bound: set[str] = set()
    string_assigns: dict[str, str] = {}
    for raw in text.splitlines():
        s = raw.strip()
        m_imp = CACHABLE_IMPORT_LINE_RE.match(s)
        if m_imp:
            import_bound.add(m_imp.group(1))
            continue
        m_str = STR_ASSIGN_LINE_RE.match(s)
        if m_str:
            string_assigns[m_str.group(1)] = m_str.group(2)
            continue
        m_call = CALL_REASSIGN_LINE_RE.match(s)
        if m_call:
            lhs, callee, arg = m_call.group(1), m_call.group(2), m_call.group(3)
            if (lhs == callee and callee in import_bound
                    and string_assigns.get(arg) == "Cachable"):
                cachable.add(lhs)
    return cachable


def find_cache_key_objs(text: str, cache_key: str) -> set[str]:
    """Return locals (e.g. {"L6_1"}) whose `.cache_key` field equals cache_key.

    Pattern at module scope:
        L6_1 = {}
        L6_1.cache_key = "$exchangeOwningPoint"

    Returns an empty set if cache_key is empty/None.
    """
    if not cache_key:
        return set()
    objs: set[str] = set()
    for raw in text.splitlines():
        m = TABLE_FIELD_LITERAL_RE.match(raw.strip())
        if m and m.group(2) == "cache_key" and m.group(3) == cache_key:
            objs.add(m.group(1))
    return objs


def strip_noise(line: str) -> str:
    line = STRING_RE.sub('""', line)
    line = COMMENT_RE.sub("", line)
    return line


def find_function_bodies(text: str, require_arg: bool = True):
    """Yield (name_or_none, first_arg, body_start_line, body_end_line, lines_slice).

    body_end_line is INCLUSIVE -- the line containing the matching `end`.
    Nested functions yield their own (start, end) ranges; the outer one
    still spans the whole block (nesting is via the balance counter).

    `require_arg`: when True (default, kept for backward compat with the
    closure/listener anchors), skip argless functions like `function foo()`
    — they have no taint anchor. When False (used by Pass 3 cache-consumer),
    yield them too with first_arg="" so the harvest can seed taint
    internally via Cachable.get / cache-returning-callee patterns.
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
        first_arg = args.split(",")[0].strip() if args else ""
        if require_arg:
            if not first_arg or not re.match(r"^\w+$", first_arg):
                continue
        else:
            # Pass 3 mode: accept argless bodies; validate first_arg shape
            # only if non-empty (an argless function gets first_arg="").
            if first_arg and not re.match(r"^\w+$", first_arg):
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


def build_named_function_index(text: str) -> dict[str, list[tuple[str, list[str], int]]]:
    """Identifier -> list of (first_arg, body_lines, start_line) for every
    function declaration or local-assignment-of-function in the file.

    Returns a LIST per name (decompiled bytecode reuses local names
    heavily; a single name like `L10_1` is often bound to many distinct
    function bodies throughout a file). `start_line` lets the caller
    pick the definition that was current at a particular point in source
    order -- e.g. `M.initialize = L10_1 @ line 284` captured whichever
    L10_1 body was most-recently defined BEFORE line 284.

    Indexes BOTH argful and argless bodies (require_arg=False): the
    cache-returning fixpoint needs to see wrapper functions like
    `function getCache() return Cachable.get(key) end`, which have no
    args. For argless bodies first_arg is "".
    """
    out: dict[str, list[tuple[str, list[str], int]]] = {}
    for name, first_arg, start, _end, body in find_function_bodies(
        text, require_arg=False
    ):
        if name:
            out.setdefault(name, []).append((first_arg, body, start))
    return out


def pick_body_at_line(
    bodies: list[tuple[str, list[str], int]],
    binding_line: int,
) -> tuple[str, list[str]] | None:
    """From a list of (arg, body, start_line) tuples, return the one
    whose start_line is the greatest value <= binding_line. That's
    the definition the name held when `binding_line` was processed.
    Returns None if no body precedes binding_line.
    """
    best: tuple[str, list[str], int] | None = None
    for entry in bodies:
        if entry[2] <= binding_line and (best is None or entry[2] > best[2]):
            best = entry
    if best is None:
        return None
    return (best[0], best[1])


# Decompiled-bytecode pattern matchers for cache-listener detection.
# `<X> = "$cacheKey"` -- captures (local, key).
CACHE_KEY_ASSIGN_RE = re.compile(r'^\s*(?:local\s+)?(\w+)\s*=\s*"(\$\w+)"\s*$')
# `<class>.<field> = <ident>` -- tracks `M.initialize = L10_1`-style
# class-field bindings so we can resolve `M.initialize` to L10_1.
CLASS_FIELD_BIND_RE = re.compile(r'^\s*(\w+)\.(\w+)\s*=\s*(\w+)\s*$')


def find_cache_listener_fn_idents(
    text: str,
    cache_key: str,
    file_fn_index: dict[str, list[tuple[str, list[str], int]]],
) -> list[tuple[str, list[str]]]:
    """For `cache_key`, return identifiers in `file_fn_index` that are
    registered as listeners via `Cachable.addListener(<cache_key>, fn)`.

    Decompiled pattern:
        L10_1 = L3_1.addListener      -- method-ref into local
        L11_1 = "$arenaMatching"      -- cache_key literal into local
        L12_1 = L8_1.initialize       -- fn-ref into local
        L10_1(L11_1, L12_1)           -- the call

    We resolve by:
      1. Finding lines that contain the cache_key string literal.
      2. Looking in a window around each occurrence for an `addListener`
         token.
      3. Collecting function-ident candidates from the window: identifiers
         that appear directly in `file_fn_index`, AND class-field bindings
         like `M.initialize = LX_Y` where LX_Y is in `file_fn_index`.

    Caller harvests EACH candidate body in subtree mode; the corpus
    filter catches noise from over-collection.
    """
    if not cache_key or not cache_key.startswith("$"):
        return []
    lines = text.splitlines()
    stripped = [strip_noise(line) for line in lines]
    cache_str_lit = f'"{cache_key}"'
    results: list[tuple[str, list[str]]] = []
    seen_starts: set[int] = set()

    # Build a list of `<Class>.<field> = <ident>` bindings WITH their
    # source line numbers. Decompiled bytecode reuses names, so the same
    # binding can recur with different idents/lines; keep all.
    field_bindings: list[tuple[str, str, str, int]] = []  # (cls, field, ident, line)
    for line_i, s in enumerate(stripped):
        cb = CLASS_FIELD_BIND_RE.match(s)
        if cb and cb.group(3) in file_fn_index:
            field_bindings.append(
                (cb.group(1), cb.group(2), cb.group(3), line_i)
            )

    def resolve_body(ident: str, binding_line: int) -> tuple[str, list[str]] | None:
        bodies = file_fn_index.get(ident)
        if not bodies:
            return None
        return pick_body_at_line(bodies, binding_line)

    # Locate the cache_key string literal on RAW lines (strip_noise blanks
    # string contents, so the literal wouldn't survive in stripped view).
    # Use a TIGHT window (the addListener call typically appears within
    # 2-3 lines of the cache_key string literal in decompiled output).
    for i, raw in enumerate(lines):
        if cache_str_lit not in raw:
            continue
        lo = max(0, i - 4)
        hi = min(len(lines), i + 6)
        window = stripped[lo:hi]
        window_text = "\n".join(window)
        if "addListener" not in window_text:
            continue

        # The addListener call is the anchor for "which definition was
        # current". Find its line within the window.
        call_line = i
        for j in range(lo, hi):
            if "addListener" in stripped[j]:
                call_line = j
                break

        # Candidate idents in the window. Pick the definition that was
        # most recently defined before `call_line`.
        cand_idents: set[str] = set()
        for ident in re.findall(r"\b(\w+)\b", window_text):
            if ident in file_fn_index:
                cand_idents.add(ident)
        for cw in re.finditer(r"\b(\w+)\.(\w+)\b", window_text):
            # Look up via field_bindings: the field binding's IDENT is
            # the listener body. Use the binding line to pick the
            # ident's correct definition.
            cls, field = cw.group(1), cw.group(2)
            for bcls, bfield, bident, bline in field_bindings:
                if (bcls == cls and bfield == field) or bfield == field:
                    body = resolve_body(bident, bline)
                    if body is not None and id(body[1]) not in seen_starts:
                        seen_starts.add(id(body[1]))
                        results.append(body)
                    break

        for ident in cand_idents:
            body = resolve_body(ident, call_line)
            if body is not None and id(body[1]) not in seen_starts:
                seen_starts.add(id(body[1]))
                results.append(body)

    return results


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

# Cache-consumer (Anchor 3) per-line patterns. Together they let
# harvest_one_function recognize a `Cachable.get(cache_key)` call site
# even though the decompiler hoists every step onto its own line:
#
#   L<lhs> = L<cachable>.get        -- bind .get method, GET_BIND_INLINE_RE
#   L<arg> = "$cacheKey"            -- arg via inline literal
#   ... OR ...
#   L<arg> = L<obj>.cache_key       -- arg via .cache_key field
#   L<lhs> = L<get_fn>(L<arg>)     -- the call, CALL_SINGLE_INLINE_RE
#
# Plus the multi-LHS form the decompiler emits for multi-return calls:
#   L<lhs>, L<lhs2>, ... = L<callee>(<args>)
GET_BIND_INLINE_RE = re.compile(
    r"^\s*(?:local\s+)?(\w+)\s*=\s*(\w+)\.get\s*$"
)
CALL_SINGLE_INLINE_RE = re.compile(
    r"^\s*(?:local\s+)?(\w+)\s*=\s*(\w+)\(([^)]*)\)\s*$"
)
MULTI_LHS_CALL_RE = re.compile(
    r"^\s*(?:local\s+)?(\w+)(?:\s*,\s*\w+)+\s*=\s*(\w+)\(([^)]*)\)\s*$"
)


def harvest_one_function(
    arg_name: str | None,
    body: list[str],
    *,
    file_fn_index: dict[str, tuple[str, list[str]]] | None = None,
    base_prefix: str | None = None,
    visited: set[str] | None = None,
    max_depth: int = 2,
    cache_key: str | None = None,
    cachable_locals: set[str] | None = None,
    cache_key_objs: set[str] | None = None,
    cache_returning_idents: dict[str, str] | None = None,
    track_return: bool = False,
) -> tuple[set[str], str | None]:
    """Harvest response_data-relative field paths from a function body.

    THREE INVOCATION MODES, distinguished by base_prefix and arg_name:

    1. ENVELOPE (base_prefix=None, arg_name=str): the closure receives
       the wire envelope; the entry point is `<arg>.response_data.<field>`.
    2. SUBTREE (base_prefix=str, arg_name=str): helper invocation. The
       caller passed a tainted local; this body's `<arg>` represents
       the subtree at `response_data.<base_prefix>`.
    3. CACHE-CONSUMER (arg_name=None or unused): no anchor arg. Taint
       seeds INTERNALLY when the body has `lhs = Cachable.get(cache_key)`
       or `lhs = <cache_returning_callee>(...)`. The cache value is
       semantically equivalent to `response_data`, so seeds taint at
       prefix "" (cache root).

    Single-pass walk over the body in source order, maintaining a
    `taint` map (local_name -> response_data subpath). At each line we
    (a) harvest field reads against the CURRENT taint, then (b) update
    taint based on assignments. Decompiled bytecode reuses local names
    heavily, so reassignment to an untainted RHS must DE-TAINT the lhs.

    cache_key + cachable_locals + cache_key_objs enable inline Cachable.get
    seeding (mode 3). cache_returning_idents enables "this callee returns
    a tainted value" propagation — call sites taint their lhs.

    If track_return is True, also returns the return-taint prefix iff the
    body's `return <local>` references a tainted local. Used by the
    fixpoint that classifies cache-returning functions.

    Returns (fields, return_taint_or_None).
    """
    if visited is None:
        visited = set()
    if cachable_locals is None:
        cachable_locals = set()
    if cache_key_objs is None:
        cache_key_objs = set()
    if cache_returning_idents is None:
        cache_returning_idents = {}
    fields: set[str] = set()

    # Initial taint. In subtree mode, arg itself is tainted at base_prefix.
    # In envelope mode, no taint until we see `<arg>.response_data` capture.
    # In cache-consumer mode, no initial taint — seeded by internal call sites.
    #
    # arg_name="" is special: it can mean (a) Pass 3 cache-consumer mode
    # (intentional — no anchor arg) or (b) Pass 2 picked an argless
    # listener-body (the decompiler bound the same name to multiple bodies,
    # and pick_body_at_line returned an argless wrapper that's actually
    # for a DIFFERENT cache_key, not this endpoint's). Case (b) is
    # over-collection but historically catches adjacent fields in shared
    # listener-registration files like m_boot/initialize.lua, so we
    # preserve it: `taint[""] = ""` makes the harvest regex match every
    # `.<field>` access in the body. The corpus filter downstream catches
    # the worst noise. To disable this leniency entirely (e.g. for a
    # strict Pass 3 invocation), pass base_prefix=None alongside arg_name="".
    taint: dict[str, str] = {}
    if base_prefix is not None and arg_name is not None:
        taint[arg_name] = base_prefix

    # Locals currently bound to the Cachable.get method ref (mode 3).
    get_fns: set[str] = set()
    # Return-taint tracker (mode 3 fixpoint).
    return_taint: str | None = None

    # Envelope-mode anchors -- only used when base_prefix is None AND we
    # have a concrete arg_name to anchor on. Pass 3 (cache-consumer) calls
    # us with arg_name="" because the function has no params; that's
    # fine — taint seeds via internal Cachable.get patterns instead.
    if base_prefix is None and arg_name:
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

    # Recurse into queued helper calls. Each helper-name in file_fn_index
    # may bind to MULTIPLE definitions (decompiler reuses names); harvest
    # the first one (earliest def, typically the one captured by a
    # class-field assignment right after).

        # (4b) RETURN TRACKING. `return <local>` — if the local is
        # tainted, surface that prefix to the caller so it can mark
        # this function as cache-returning.
        if track_return:
            mr = RETURN_LOCAL_RE.match(s)
            if mr and mr.group(1) in taint and return_taint is None:
                return_taint = taint[mr.group(1)]

        # (4c) CACHE-CONSUMER TAINT ORIGINS (mode 3 — Anchor 3).
        # These run BEFORE the envelope-mode captures so a Cachable.get
        # call site beats the generic field-access copy detector
        # below (which would otherwise de-taint the lhs).
        #
        # Pattern A: bind .get method off a Cachable module local.
        m_bind = GET_BIND_INLINE_RE.match(s)
        if m_bind and m_bind.group(2) in cachable_locals:
            get_fns.add(m_bind.group(1))
            continue

        # Pattern B: call to a known get-fn local OR a known
        # cache-returning ident. Match both the single-LHS form
        # `lhs = X(args)` and the multi-LHS form `lhs, _, _ = X(args)`.
        m_call_s = CALL_SINGLE_INLINE_RE.match(s)
        m_call_m = MULTI_LHS_CALL_RE.match(s)
        m_call = m_call_s or m_call_m
        if m_call:
            lhs, callee, args_str = (
                m_call.group(1), m_call.group(2), m_call.group(3)
            )
            args_split = [a.strip() for a in args_str.split(",") if a.strip()]
            first_arg = args_split[0] if args_split else ""
            handled = False
            # B1: Cachable.get(cache_key) — taint at cache root.
            if (callee in get_fns
                    and cache_key
                    and _arg_resolves_to_cache_key(
                        first_arg, body, line, cache_key, cache_key_objs
                    )):
                taint[lhs] = ""
                handled = True
            # B2: call to a cache-returning ident (wrapper).
            elif callee in cache_returning_idents:
                taint[lhs] = cache_returning_idents[callee]
                handled = True
            # B3: call to an alias_map ident that resolves to a
            # cache-returning ident.
            elif (callee in alias_map
                    and alias_map[callee] in cache_returning_idents):
                taint[lhs] = cache_returning_idents[alias_map[callee]]
                handled = True
            if handled:
                continue

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

    # Recurse into queued helper calls. file_fn_index maps each name to
    # a LIST of (arg, body, start_line) (decompiler reuses local names);
    # harvest the FIRST definition -- earliest in file, typically the
    # one captured by a class-field assignment right after.
    for resolved, taint_snapshot, info in pending_recurse:
        first_arg = info["first_arg"]
        if first_arg not in taint_snapshot:
            continue
        bodies = file_fn_index.get(resolved) or []
        if not bodies:
            continue
        helper_arg, helper_body = bodies[0][0], bodies[0][1]
        new_visited = visited | {resolved}
        sub_fields, _ret = harvest_one_function(
            helper_arg,
            helper_body,
            file_fn_index=file_fn_index,
            base_prefix=taint_snapshot[first_arg],
            visited=new_visited,
            max_depth=max_depth - 1,
            cache_key=cache_key,
            cachable_locals=cachable_locals,
            cache_key_objs=cache_key_objs,
            cache_returning_idents=cache_returning_idents,
        )
        fields |= sub_fields

    return fields, return_taint


def compute_cache_returning_idents(
    fn_index: dict[str, list[tuple[str, list[str], int]]],
    cache_key: str | None,
    cachable_locals: set[str],
    cache_key_objs: set[str],
    max_passes: int = 4,
) -> dict[str, str]:
    """Classify which functions in the file return a cache-rooted value.

    Returns ident -> return-prefix. Empty string "" means "the cache
    value itself"; a non-empty prefix would mean a sub-field (rare; the
    decompiler usually `return getCache()` rather than
    `return getCache().some_field`, but the harvest tracks both).

    Method: iterate fixpoint. Each pass runs harvest_one_function on
    every fn body with `track_return=True`, accumulating the
    cache_returning_idents map. Stop when a pass adds nothing — usually
    converges in 2-3 passes (direct Cachable.get → first wrappers →
    wrappers-of-wrappers).

    No-cache-key shortcut: if there's no cache_key for this endpoint,
    there's nothing to seed taint from, so return empty.
    """
    if not cache_key or (not cachable_locals and not fn_index):
        return {}
    result: dict[str, str] = {}
    for _pass in range(max_passes):
        grew = False
        for ident, bodies in fn_index.items():
            if ident in result:
                continue
            for arg, body, _start in bodies:
                _fields, ret = harvest_one_function(
                    arg, body,
                    file_fn_index=fn_index,
                    cache_key=cache_key,
                    cachable_locals=cachable_locals,
                    cache_key_objs=cache_key_objs,
                    cache_returning_idents=result,
                    track_return=True,
                    max_depth=0,  # no helper-following during classification
                )
                if ret is not None and ident not in result:
                    result[ident] = ret
                    grew = True
                    break
        if not grew:
            break
    return result


def _arg_resolves_to_cache_key(
    arg: str,
    body: list[str],
    current_line: str,
    cache_key: str,
    cache_key_objs: set[str],
) -> bool:
    """Walk backward from current_line to see if `arg` resolves to cache_key.

    Two recognized forms:
      1. Inline literal: `arg = "$cacheKey"` somewhere earlier in body.
      2. Module-table field: `arg = L<obj>.cache_key` where L<obj> was
         set up with `L<obj>.cache_key = "$cacheKey"` at module scope
         (passed in via cache_key_objs).
    Stops the walk at any unrecognized reassignment of `arg` to avoid
    a stale earlier binding being mistaken for the current value.
    """
    try:
        idx = body.index(current_line)
    except ValueError:
        return False
    for prev in body[:idx][::-1]:
        ps = prev.strip()
        m_str = STR_ASSIGN_LINE_RE.match(ps)
        if m_str and m_str.group(1) == arg:
            return m_str.group(2) == cache_key
        # `arg = L<obj>.cache_key`
        m_field = LOCAL_FIELD_ASSIGN_RE.match(ps)
        if (m_field and m_field.group(1) == arg
                and m_field.group(3) == "cache_key"
                and m_field.group(2) in cache_key_objs):
            return True
        # Any other reassignment of arg invalidates the walk-back.
        if re.match(rf"^\s*(?:local\s+)?{re.escape(arg)}\s*=", ps):
            return False
    return False


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
    cache_consumer_only: bool = False,
) -> dict:
    """Run static field-extraction for one endpoint.

    cache_consumer_only:
      False (default): run all three anchors (closure / listener / cache-
        consumer). Should be restricted to endpoints in the ui-only
        bucket — Pass 1 (closure-anchored) over-collects when run on
        ack-style endpoints because find_files_for_endpoint matches
        cache_key in shared files like m_boot/initialize.lua, where
        unrelated listener closures' arg.response_data reads would leak
        in.
      True: run ONLY Pass 3 (cache-consumer). Safe for any endpoint with
        a cache_key — taint seeds only via Cachable.get(target_cache_key)
        so cross-listener leaks can't happen.
    """
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
        # File-wide function index for helper-following + listener detection.
        fn_index = build_named_function_index(text)
        # File-wide cache-consumer context (Anchor 3 enabler).
        cachable_locals = find_cachable_locals(text)
        cache_key_objs = find_cache_key_objs(text, cache_key or "")
        # Fixpoint: classify functions whose body returns a tainted value.
        # Wrappers like `function getCache() return Cachable.get(key) end`
        # become known "cache-returning" idents so call sites taint their
        # lhs without us having to inline the wrapper.
        cache_returning_idents = compute_cache_returning_idents(
            fn_index, cache_key, cachable_locals, cache_key_objs
        ) if cache_key else {}

        file_fields: set[str] = set()

        # Common kwargs passed to every harvest invocation in this file
        # so all three anchors see the same file context.
        harvest_ctx = {
            "file_fn_index": fn_index,
            "cache_key": cache_key,
            "cachable_locals": cachable_locals,
            "cache_key_objs": cache_key_objs,
            "cache_returning_idents": cache_returning_idents,
        }

        if not cache_consumer_only:
            # Pass 1: closure-anchored harvest. For each inner
            # `function(arg) ... arg.response_data` site, harvest fields.
            for _name, arg_name, _start, _end, body in find_function_bodies(text):
                if "response_data" not in "".join(body):
                    continue
                sub, _ret = harvest_one_function(
                    arg_name, body, **harvest_ctx,
                )
                file_fields |= sub

            # Pass 2: cache-listener harvest. Functions registered via
            # `Cachable.addListener("$cacheKey", fn)` receive response_data
            # DIRECTLY (not the envelope) as their first arg, so they don't
            # match the closure anchor's `arg.response_data` pattern. The
            # detector returns concrete (arg, body) tuples already resolved
            # via line-aware lookup so we pick the right body for names the
            # decompiler reused across the file.
            for arg_name, body in find_cache_listener_fn_idents(
                text, cache_key or "", fn_index
            ):
                sub, _ret = harvest_one_function(
                    arg_name, body, base_prefix="", **harvest_ctx,
                )
                file_fields |= sub

        # Pass 3: cache-consumer harvest. Any function in the file whose
        # body calls Cachable.get(cache_key) directly OR calls a known
        # cache-returning ident (the wrapper case) seeds taint internally.
        # The harvest engine handles it via the same machinery; we iterate
        # over EVERY function body, including argless ones — the closure
        # pattern requires an arg but the cache-consumer pattern doesn't.
        #
        # IMPORTANT: pass arg_name="" so envelope-mode is DISABLED. Pass 3
        # must NOT use the arg.response_data anchor because Pass 3 visits
        # every body regardless of which cache_key the body is tied to.
        # If envelope mode were on, a closure registered for a different
        # cache_key (e.g. m_boot/initialize.lua has 50+ closures, one per
        # listener) would leak its arg.response_data reads into our
        # endpoint's harvest. Pass 1 (closure-anchored) handles those
        # reads correctly, scoped to its file selection.
        if cache_key and (cachable_locals or cache_returning_idents):
            for _name, _arg_name, _start, _end, body in find_function_bodies(
                text, require_arg=False
            ):
                sub, _ret = harvest_one_function(
                    "", body, **harvest_ctx,
                )
                file_fields |= sub

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

    # Endpoint dispatch. Each endpoint gets either the full 3-anchor
    # harvest (closure + listener + cache-consumer) or just Pass 3
    # (cache-consumer only):
    #
    #   envelope-only / ack-style: cache-consumer only. Their candidate
    #     file set tends to include shared listener-registration files
    #     (e.g. m_boot/initialize.lua has 50+ listeners side-by-side).
    #     Pass 1 (closure-anchored) on those files leaks unrelated reads:
    #     ANY `<arg>.response_data.<field>` closure body is harvested
    #     and attributed to whichever target endpoint we're processing,
    #     regardless of which listener the closure actually belongs to.
    #     For ack-style endpoints (cancel/leave/expel/etc.) the wire
    #     response is empty anyway, so the closure-anchored fields are
    #     all noise. Pass 3 is safe because it only seeds taint from
    #     `Cachable.get(target_cache_key)` calls — no leak possible.
    #
    #   everything else (ui-only, harness-covered, needs-Frida): full
    #     harvest. Their candidate files don't tend to be the shared
    #     listener-registration sinks, so Pass 1 stays scoped.
    cache_only_keys_set: set[str] = set()
    keys: list[str]
    if args.endpoint:
        keys = list(args.endpoint)
    elif args.bucket:
        coverage = json.load(COVERAGE_PATH.open())
        keys = sorted([
            ep for ep, c in coverage["endpoints"].items()
            if c["bucket"] == args.bucket
        ])
    else:
        # All endpoints with a cache_key; tag the ack-style ones to
        # downgrade them to cache-consumer-only harvest.
        from classify_coverage import ACK_PREFIXES, ACK_SUFFIXES  # noqa: PLC0415
        keys = []
        for ep in sorted(merged.keys()):
            if not (merged.get(ep) or {}).get("cache_key"):
                continue
            keys.append(ep)
            action = ep.split(".", 1)[1] if "." in ep else ep
            if ACK_PREFIXES.match(action) or ACK_SUFFIXES.search(action):
                cache_only_keys_set.add(ep)
    cache_only_set = cache_only_keys_set

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
            trace = extract_for_endpoint(
                ep, entry, cache_keys.get(ep), corpus,
                cache_consumer_only=(ep in cache_only_set),
            )
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
