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

# Function declaration: `function ...(args)` -- captures the arg list.
# Matches `function foo(a)`, `function tbl.foo(a)`, `function(a)`,
# `local foo = function(a)`, etc.
FN_DECL_RE = re.compile(r"\bfunction\s*(?:[\w.:]+\s*)?\(([^)]*)\)")

# Lua block-opening / closing keywords. `function` opens, but we count
# it via FN_DECL_RE separately for arg capture; here we count all opens
# uniformly for balance tracking.
OPEN_RE = re.compile(r"\b(function|if|for|while|do|repeat)\b")
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


def strip_noise(line: str) -> str:
    line = STRING_RE.sub('""', line)
    line = COMMENT_RE.sub("", line)
    return line


def find_function_bodies(text: str):
    """Yield (first_arg, body_start_line, body_end_line, lines_slice).

    body_end_line is INCLUSIVE -- the line containing the matching `end`.
    Nested functions yield their own (start, end) ranges; the outer one
    still spans the whole block (nesting is via the balance counter).
    """
    lines = text.splitlines()
    n = len(lines)
    # Pre-strip noise once per line.
    stripped = [strip_noise(line) for line in lines]
    for i in range(n):
        m = FN_DECL_RE.search(stripped[i])
        if not m:
            continue
        args = m.group(1).strip()
        if not args:
            continue
        first_arg = args.split(",")[0].strip()
        if not first_arg or not re.match(r"^\w+$", first_arg):
            continue
        # Walk forward, balancing opens vs closes, until depth returns to 0.
        # The function decl itself counts as 1 open; the keyword is at
        # m.start() within stripped[i].
        # On line i, count opens/closes BOTH before and after m.end()?
        # No -- the function itself is already the open; any opens BEFORE
        # it on the same line belong to an enclosing scope, not us. Count
        # opens/closes only AFTER m.end() on the first line.
        depth = 1
        tail = stripped[i][m.end():]
        depth += len(OPEN_RE.findall(tail))
        depth -= len(CLOSE_RE.findall(tail))
        if depth <= 0:
            # Single-line function; not useful for our pattern (no body).
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
        yield first_arg, i, end_line, lines[i:end_line + 1]


def harvest_one_function(arg_name: str, body: list[str]) -> set[str]:
    """Within a function body, find response_data field reads chained
    off the function's first arg. Returns set of `response_data.<field>`
    paths (no leading prefix; the merger adds it).
    """
    fields: set[str] = set()

    # Stage 1: build the taint set. The arg itself is tainted (any
    # `<arg>.response_data` access propagates). Then walk for assignments
    # that capture .response_data into a local.
    #
    # taint[<local_name>] = path-of-cached-table (e.g. "" for the
    # response_data root, "unit_list" for L = response_data.unit_list).
    taint: dict[str, str] = {}

    arg_re = re.compile(rf"\b{re.escape(arg_name)}\.response_data\b")
    arg_field_re = re.compile(rf"\b{re.escape(arg_name)}\.response_data\.([a-z_][a-z0-9_]*)")

    for line in body:
        s = strip_noise(line)

        # Direct: <arg>.response_data.<field>
        for f in arg_field_re.findall(s):
            fields.add(f)

        # Capture: local X = <arg>.response_data  (root capture)
        for assign_re in (LOCAL_ASSIGN_RE, BARE_ASSIGN_RE):
            m = assign_re.match(s)
            if not m:
                continue
            lhs, rhs = m.group(1), m.group(2)
            # rhs matches `<arg>.response_data` exactly (no further chain
            # -- if it had a chain, we'd want to taint with that chain).
            if arg_re.search(rhs) and ".response_data." not in rhs:
                # Capture only if rhs is essentially "<arg>.response_data"
                # (allow trailing whitespace / comments which we stripped).
                if rhs.strip().endswith(".response_data"):
                    taint[lhs] = ""
            # Capture nested: local X = <arg>.response_data.<field>
            mf = arg_field_re.search(rhs)
            if mf and rhs.strip() == f"{arg_name}.response_data.{mf.group(1)}":
                taint[lhs] = mf.group(1)
                fields.add(mf.group(1))

    # Stage 2: another pass; now harvest `<tainted>.<field>` reads.
    # Also propagate the taint one more hop (local Y = X.<field>) so
    # `for k,v in pairs(X.unit_list)` and `Y = X.unit_list; Y.[1].id`
    # both register their parent field.
    if not taint:
        return fields

    for line in body:
        s = strip_noise(line)
        for local_name, prefix in list(taint.items()):
            tre = re.compile(rf"\b{re.escape(local_name)}\.([a-z_][a-z0-9_]*)")
            for f in tre.findall(s):
                path = f"{prefix}.{f}" if prefix else f
                fields.add(path)
                # Propagate taint one hop on bare/local assignment.
                for assign_re in (LOCAL_ASSIGN_RE, BARE_ASSIGN_RE):
                    am = assign_re.match(s)
                    if am and am.group(2).strip() == f"{local_name}.{f}":
                        lhs2 = am.group(1)
                        if lhs2 not in taint:
                            taint[lhs2] = path

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
    appears in > CORPUS_THRESHOLD of UI files.
    """
    total = corpus.get("__total_files__", 0)
    if total <= 0:
        return fields, set()
    threshold = total * CORPUS_THRESHOLD
    kept: set[str] = set()
    dropped: set[str] = set()
    for path in fields:
        leaf = path.rsplit(".", 1)[-1]
        if leaf in ENVELOPE_FIELDS:
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
        file_fields: set[str] = set()
        for arg_name, _start, _end, body in find_function_bodies(text):
            # Pre-check: skip the body unless it contains the response_data
            # anchor at all. Faster than running the full harvester.
            if "response_data" not in "".join(body):
                continue
            file_fields |= harvest_one_function(arg_name, body)
        if file_fields:
            per_file[str(fp.relative_to(SOURCE_ROOT))] = sorted(file_fields)
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
