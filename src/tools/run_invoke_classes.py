"""Drive `invoke_classes` job for ui-only endpoints.

For each endpoint, pre-populates Cachable with the spied candidate and
invokes every exported method on every class registered by the UI files
that reference the endpoint. Method bodies that destructure the cache
or read the response_data via Cachable.get fire the spy and log paths.

Writes traces to build/runtime/traces_classes/<endpoint>.json.

Usage:
  uv run --no-project python src/tools/run_invoke_classes.py --bucket ui-only
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from _lua_worker import LuaWorkerBase

ROOT = Path(__file__).resolve().parents[2]
HARNESS_DIR = ROOT / "src" / "harness"
SOURCE_ROOT = ROOT / "assets" / "decompiled" / "all"
MERGED_PATH = ROOT / "build" / "merged_endpoints.json"
EXTRACTED_PATH = ROOT / "build" / "extracted_apis.json"
COVERAGE_PATH = ROOT / "build" / "coverage_classification.json"
PROMOTED_PATH = ROOT / "build" / "response_types_promoted.json"
SYNTHESIZED_PATH = ROOT / "build" / "synthesized_types.json"
UI_HANDLER_MAP = ROOT / "build" / "ui_handler_map.json"
DEFAULT_TRACES_DIR = ROOT / "build" / "runtime" / "traces_classes"


def pick_lua_runtime() -> str:
    for cand in ("luajit", "lua5.1", "lua-5.1"):
        if shutil.which(cand):
            return cand
    sys.exit("no Lua runtime found (need luajit or lua5.1)")


def load_json(path: Path) -> dict:
    return json.load(path.open())


# Borrowed from run_lua_harness.py — keep the candidate builder consistent.
DEFAULTS_BY_TYPE = {
    "int": 0, "integer": 0, "float": 0.0, "number": 0.0,
    "str": "", "string": "", "bool": False, "boolean": False,
    "list": [], "array": [], "object": {}, "dict": {}, "any": None,
}


def default_for(field_shape: dict | None):
    if not isinstance(field_shape, dict):
        return None
    t = (field_shape.get("type") or "").lower()
    if t == "object":
        nested = field_shape.get("fields") or {}
        return {fk: default_for(fv) for fk, fv in nested.items()}
    if t in ("list", "array"):
        elem = field_shape.get("element") or {}
        if elem and (elem.get("type") == "object" or elem.get("fields")):
            return [default_for(elem)]
        return []
    return DEFAULTS_BY_TYPE.get(t)


def build_candidate(shape: dict | None) -> dict:
    candidate = {"response_data": {}, "status_code": 200, "release_info": []}
    if not isinstance(shape, dict):
        return candidate
    fields = (shape.get("fields") or {}).get("response_data")
    if isinstance(fields, dict) and fields.get("fields"):
        candidate["response_data"] = default_for(fields)
    elif isinstance(shape.get("fields"), dict):
        candidate["response_data"] = {
            k: default_for(v) for k, v in shape["fields"].items()
        }
    return candidate


def get_shape_for(ep: str, promoted: dict, synthesized: dict) -> dict | None:
    p = promoted.get(ep) or {}
    s = synthesized.get(ep) or {}
    p_shape = p.get("shape") if isinstance(p, dict) else None
    rd = (p_shape or {}).get("fields", {}).get("response_data") if p_shape else None
    p_has_fields = (
        isinstance(rd, dict) and rd.get("type") == "object" and bool(rd.get("fields"))
    )
    if p_shape and p_has_fields:
        return p_shape
    if isinstance(s, dict) and s.get("shape"):
        return s["shape"]
    return p_shape


class Worker(LuaWorkerBase):
    def __init__(self, lua_bin: str, source_root: Path):
        super().__init__(lua_bin, source_root, HARNESS_DIR)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", action="append", default=[])
    ap.add_argument("--bucket", default="ui-only")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default=str(DEFAULT_TRACES_DIR))
    args = ap.parse_args(argv)

    merged = load_json(MERGED_PATH)
    extracted = load_json(EXTRACTED_PATH)
    promoted = load_json(PROMOTED_PATH)
    synthesized = load_json(SYNTHESIZED_PATH)
    ui_handlers = load_json(UI_HANDLER_MAP) if UI_HANDLER_MAP.exists() else {}

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
        coverage = load_json(COVERAGE_PATH)
        keys = sorted([
            ep for ep, c in coverage["endpoints"].items()
            if c["bucket"] == args.bucket
        ])

    traces_dir = Path(args.out)
    if not traces_dir.is_absolute():
        traces_dir = ROOT / args.out
    traces_dir.mkdir(parents=True, exist_ok=True)

    lua_bin = pick_lua_runtime()
    print(f"using lua: {lua_bin}, {len(keys)} endpoints", file=sys.stderr)
    worker = Worker(lua_bin, SOURCE_ROOT)

    ok = 0
    fail = 0
    total_keys_added = 0
    t0 = time.time()
    try:
        for ep in keys:
            entry = merged.get(ep)
            if not entry:
                fail += 1
                continue
            module = entry["module"]
            action = entry["action"]
            shape = get_shape_for(ep, promoted, synthesized)
            candidate = build_candidate(shape)
            # Pull class names from ui_handler_map.json
            classes_info = (ui_handlers.get(ep) or {}).get("candidate_files") or []
            class_names = sorted({
                c["class"] for c in classes_info if c.get("class")
            })
            # Cap to avoid runaway method invocations per endpoint. The Lua
            # harness applies its own MAX_TOTAL_METHODS=60 cap so total work
            # is bounded regardless; this guards against degenerate cases
            # (e.g. shared base class referenced by hundreds of UI files).
            # Bumped from 30 -> 100 since a sorted-alphabetical cap was
            # silently dropping later-sorted classes for endpoints with
            # broad UI coverage; warn when truncating so it's visible.
            CLASS_CAP = 100
            if len(class_names) > CLASS_CAP:
                print(
                    f"  {ep}: {len(class_names)} candidate classes, capping at {CLASS_CAP}",
                    file=sys.stderr,
                )
                class_names = class_names[:CLASS_CAP]

            job = {
                "kind": "invoke_classes",
                "module": module,
                "action": action,
                "cache_key": cache_keys.get(ep, ""),
                "candidate_response": candidate,
                "classes": class_names,
            }
            trace = worker.run(job, timeout_s=8.0)
            trace["endpoint"] = ep
            seen = set()
            deduped = []
            for k in trace.get("accessed_keys") or []:
                if k not in seen:
                    seen.add(k)
                    deduped.append(k)
            trace["accessed_keys"] = deduped

            (traces_dir / f"{ep}.json").write_text(json.dumps(trace, indent=2))

            n_keys = len(trace["accessed_keys"])
            total_keys_added += n_keys
            methods = trace.get("methods_invoked", 0)
            classes = len(trace.get("classes_seen") or [])
            errs = len(trace.get("errors") or [])
            if n_keys > 0:
                ok += 1
                status = "OK"
            else:
                fail += 1
                status = "--"
            print(
                f"{status:3} {ep:50} cls={classes:<3} mtd={methods:<4} "
                f"keys={n_keys:<4} errs={errs}",
                file=sys.stderr,
            )
    finally:
        worker.close()

    dt = time.time() - t0
    print(
        f"done: {ok} ok / {fail} dud in {dt:.1f}s; total keys logged: {total_keys_added}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
