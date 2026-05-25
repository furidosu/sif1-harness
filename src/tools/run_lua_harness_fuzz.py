#!/usr/bin/env python3
"""Tier 1 fuzz driver -- runs the V5 listener-driven harness once per variant
per endpoint, then aggregates the union of accessed field paths across variants.

Per the Session 9 plan (`build/session9_plan.md`): the baseline V5 sentinel
returns one fixed shape for every undeclared field, so listener code gated
on `for _, v in ipairs(x.list) do ...`, `if #x.list > 0 then ...`,
`if x.is_complete then ...`, or `if x.score > 0 then ...` can't fire its
field-discovering branch. The variants in `scripts/lua_harness/sentinel_variants.lua`
perturb the sentinel's __ipairs/__len/__lt/__le/__index so each gate gets
flipped once, surfacing the nested reads gated behind it.

Outputs:
  build/runtime/traces_fuzz/<variant>/<module>.<action>.json
  build/runtime_fuzz_observations.json
  build/runtime_fuzz_summary.md

Invocation:
  uv run --no-project python scripts/run_lua_harness_fuzz.py --all
  uv run --no-project python scripts/run_lua_harness_fuzz.py --endpoint arena.top
  uv run --no-project python scripts/run_lua_harness_fuzz.py --variant list_many --all
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_lua_harness import (  # noqa: E402
    HarnessWorker,
    SOURCE_ROOT,
    MERGED_PATH,
    EXTRACTED_PATH,
    PROMOTED_PATH,
    SYNTHESIZED_PATH,
    build_cache_key_table,
    build_candidate_response,
    get_shape_for,
    infer_request_arity,
    load_json,
    pick_lua_runtime,
)


TRACES_FUZZ_DIR = ROOT / "build" / "runtime" / "traces_fuzz"

# Mirrored from scripts/lua_harness/sentinel_variants.lua. Single source of
# truth (the Lua file) is consulted by the harness; this list controls the
# Python-side dispatch order so output filenames stay sorted.
VARIANT_ORDER = ["baseline", "list_one", "list_many", "true_bool", "false_bool", "cmp_gt"]


def run_endpoint(
    worker: HarnessWorker,
    endpoint_key: str,
    merged_entry: dict,
    shape: dict | None,
    cache_key: str | None,
    variants: list[str],
    out_root: Path,
) -> dict[str, dict]:
    module = merged_entry["module"]
    action = merged_entry["action"]
    fn_name = merged_entry.get("fn_name") or action
    candidate = build_candidate_response(shape)
    arity = infer_request_arity(merged_entry)
    walk_schema = shape if isinstance(shape, dict) else None

    traces: dict[str, dict] = {}
    for variant in variants:
        job = {
            "module": module,
            "action": action,
            "fn_name": fn_name,
            "candidate_response": candidate,
            "request_arity": arity,
            "schema": walk_schema,
            "cache_key": cache_key or "",
            "variant": variant,
        }
        try:
            parsed = worker.run(job, timeout_s=20.0)
        except RuntimeError as e:
            parsed = {
                "accessed_keys": [],
                "errors": [f"worker error: {e}"],
                "variant": variant,
            }
        parsed.setdefault("module", module)
        parsed.setdefault("action", action)
        parsed.setdefault("accessed_keys", [])
        parsed.setdefault("errors", [])
        parsed["variant"] = variant
        # Dedupe first-seen.
        seen: set[str] = set()
        deduped: list[str] = []
        for k in parsed["accessed_keys"]:
            if k not in seen:
                seen.add(k)
                deduped.append(k)
        parsed["accessed_keys"] = deduped

        variant_dir = out_root / variant
        variant_dir.mkdir(parents=True, exist_ok=True)
        (variant_dir / f"{endpoint_key}.json").write_text(
            json.dumps(parsed, indent=2, sort_keys=False)
        )
        traces[variant] = parsed
    return traces


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", action="append", default=[],
                    help="endpoint key like 'arena.top' (repeatable)")
    ap.add_argument("--all", action="store_true",
                    help="run every endpoint in merged_endpoints.json")
    ap.add_argument("--variant", action="append", default=[],
                    help="restrict to these variants (default: all 6)")
    ap.add_argument("--lua", default=None, help="path to lua binary")
    ap.add_argument("--out", default=str(TRACES_FUZZ_DIR),
                    help="output traces dir root (default: build/runtime/traces_fuzz)")
    args = ap.parse_args(argv)

    variants = args.variant or VARIANT_ORDER
    for v in variants:
        if v not in VARIANT_ORDER:
            sys.exit(f"unknown variant {v!r}; choices: {VARIANT_ORDER}")

    merged = load_json(MERGED_PATH)
    extracted = load_json(EXTRACTED_PATH)
    promoted = load_json(PROMOTED_PATH)
    synthesized = load_json(SYNTHESIZED_PATH)
    cache_keys = build_cache_key_table(extracted)

    if args.all:
        keys = sorted(merged.keys())
    elif args.endpoint:
        keys = list(args.endpoint)
    else:
        ap.print_help()
        return 2

    lua_bin = args.lua or pick_lua_runtime()
    print(f"using lua: {lua_bin}", file=sys.stderr)
    out_root = Path(args.out)
    if not out_root.is_absolute():
        out_root = ROOT / args.out

    worker = HarnessWorker(lua_bin, SOURCE_ROOT)
    print(
        f"harness booted. preload: {worker.startup.get('preload_loaded')} ok, "
        f"{worker.startup.get('preload_failed')} err, "
        f"listeners: {worker.startup.get('listeners_registered')}",
        file=sys.stderr,
    )

    t0 = time.time()
    total_runs = 0
    errored_runs = 0
    try:
        for k in keys:
            entry = merged.get(k)
            if not entry:
                print(f"skip {k}: not in merged", file=sys.stderr)
                continue
            shape = get_shape_for(k, promoted, synthesized)
            ck = cache_keys.get(k)
            traces = run_endpoint(worker, k, entry, shape, ck, variants, out_root)
            counts = []
            for variant in variants:
                tr = traces[variant]
                n = len(tr.get("accessed_keys") or [])
                e = len(tr.get("errors") or [])
                if e:
                    errored_runs += 1
                total_runs += 1
                counts.append(f"{variant}={n}")
            print(f"{k:<55} {' '.join(counts)}", file=sys.stderr)
    finally:
        worker.close()
    dt = time.time() - t0
    print(
        f"done: {total_runs} runs ({errored_runs} with errors) in {dt:.1f}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
