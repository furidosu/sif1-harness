#!/usr/bin/env python3
"""run_lua_harness.py

Drives scripts/lua_harness/harness.lua over the project's 358 SIF1
endpoints, recording per-endpoint runtime field-read traces.

V5 (listener-driven): one persistent luajit subprocess runs the entire
batch. At boot the worker loads m_boot/initialize.lua + the ~18 model
files that register Cachable listeners. Each endpoint then runs through
the worker as a single JSON request/response over stdin/stdout. After
the svapi callback chain runs, the harness invokes
Cachable.notifyUpdate(endpoint.cache_key) so the listener bodies fire
against the spied response and their field-reads land in accessed_keys.

V1 traces remain untouched under build/runtime/traces/. V5 writes to
build/runtime/traces_v5/ so the V1 regression suite stays stable.

Inputs consumed (read-only):
  build/merged_endpoints.json        -- canonical endpoint -> fn_name table
  build/extracted_apis.json          -- canonical cache_key per endpoint
  build/response_types_promoted.json -- scraper-derived shape (preferred)
  build/synthesized_types.json       -- LLM-synthesized shape (fallback)
  source/all/                        -- decompiled client tree

Outputs written:
  build/runtime/traces_v5/<mod>.<act>.json

CLI:
  uv run --no-project python scripts/run_lua_harness.py --endpoint unit.unitAll
  uv run --no-project python scripts/run_lua_harness.py --endpoint live.reward
  uv run --no-project python scripts/run_lua_harness.py --all
  uv run --no-project python scripts/run_lua_harness.py --all --out build/runtime/traces_v5

Idempotent; re-running overwrites existing trace files.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HARNESS_DIR = ROOT / "src" / "harness"
SOURCE_ROOT = ROOT / "assets" / "decompiled" / "all"
SVAPI_DIR = SOURCE_ROOT / "common" / "svapi"
DEFAULT_TRACES_DIR = ROOT / "build" / "runtime" / "traces"

MERGED_PATH = ROOT / "build" / "merged_endpoints.json"
EXTRACTED_PATH = ROOT / "build" / "extracted_apis.json"
PROMOTED_PATH = ROOT / "build" / "response_types_promoted.json"
SYNTHESIZED_PATH = ROOT / "build" / "synthesized_types.json"


def pick_lua_runtime() -> str:
    """Prefer luajit (Lua 5.1 semantics), then lua5.1, then lua."""
    for cand in ("luajit", "lua5.1", "lua-5.1"):
        if shutil.which(cand):
            return cand
    lua = shutil.which("lua")
    if lua is None:
        sys.exit("no Lua runtime found (need luajit or lua5.1)")
    try:
        ver = subprocess.run(
            [lua, "-v"], capture_output=True, text=True, timeout=5
        )
    except Exception as e:
        sys.exit(f"could not probe {lua}: {e}")
    blob = (ver.stdout + ver.stderr).lower()
    if "lua 5.1" in blob or "luajit" in blob:
        return lua
    sys.exit(
        f"only `lua` available and it reports {blob.strip()!r}; "
        "harness needs Lua 5.1 / LuaJIT semantics. "
        "Install luajit (brew install luajit) and re-run."
    )


# ---- candidate response synthesis -----------------------------------------

DEFAULTS_BY_TYPE = {
    "int": 0,
    "integer": 0,
    "float": 0.0,
    "number": 0.0,
    "str": "",
    "string": "",
    "bool": False,
    "boolean": False,
    "list": [],
    "array": [],
    "object": {},
    "dict": {},
    "any": None,
}


def default_for(field_shape: dict | None):
    if not isinstance(field_shape, dict):
        return None
    t = (field_shape.get("type") or "").lower()
    if t == "object":
        nested = field_shape.get("fields") or {}
        out: dict = {}
        for fk, fv in nested.items():
            out[fk] = default_for(fv)
        return out
    if t == "list" or t == "array":
        elem = field_shape.get("element") or {}
        if elem and elem.get("type") == "object":
            return [default_for(elem)]
        if elem and elem.get("fields"):
            return [default_for(elem)]
        return [default_for(elem)] if elem else []
    return DEFAULTS_BY_TYPE.get(t, None)


def build_candidate_response(shape: dict | None) -> dict:
    candidate = {
        "response_data": {},
        "status_code": 200,
        "release_info": [],
    }
    if not isinstance(shape, dict):
        return candidate
    fields = (shape.get("fields") or {}).get("response_data")
    if isinstance(fields, dict) and fields.get("fields"):
        candidate["response_data"] = default_for(fields)
    elif isinstance(shape.get("fields"), dict):
        candidate["response_data"] = {
            k: default_for(v) for k, v in shape["fields"].items()
        }
    rd = candidate["response_data"]
    if isinstance(rd, dict):
        rd.setdefault("server_timestamp", 1700000000)
        rd.setdefault("server_timestamp_sync_flag", 0)
        rd.setdefault("present_cnt", 0)
    return candidate


def get_shape_for(endpoint_key: str, promoted: dict, synthesized: dict) -> dict | None:
    """Mirror gen_models.py's cascade: scraper truth wins unless its
    response_data is an empty object (Push C / Push X promoted Any -> object
    from sibling observation but never populated inner fields)."""
    p = promoted.get(endpoint_key) or {}
    s = synthesized.get(endpoint_key) or {}
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


# ---- cache_key lookup ------------------------------------------------------

def build_cache_key_table(extracted: dict) -> dict[str, str]:
    """extracted_apis.json -> {module.action: cache_key}.

    merged_endpoints.json drops cache_key during its merge with the
    fellow-dev reference; we read it straight from extracted_apis where
    every endpoint has a populated cache_key.
    """
    out: dict[str, str] = {}
    for mod, val in extracted.items():
        for api in val.get("apis", []):
            ck = api.get("cache_key")
            if ck:
                out[f"{api['module']}.{api['action']}"] = ck
    return out


# ---- request arity inference ----------------------------------------------

def infer_request_arity(merged_entry: dict) -> int:
    req = merged_entry.get("request") or {}
    ours = req.get("ours") or []
    theirs = req.get("theirs") or []
    if ours:
        return len(ours)
    return len(theirs)


# ---- persistent worker -----------------------------------------------------

class HarnessWorker:
    """Wraps a long-lived luajit subprocess.

    Stdin: one JSON job per line. Stdout: one JSON trace per line.
    On startup the worker emits a single line marker we consume here
    before submitting any jobs.
    """

    def __init__(self, lua_bin: str, source_root: Path):
        cmd = [lua_bin, str(HARNESS_DIR / "harness.lua"), str(source_root)]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(HARNESS_DIR),
            text=True,
            bufsize=1,
        )
        self._read_startup()

    def _read_startup(self) -> None:
        line = self.proc.stdout.readline()  # type: ignore[union-attr]
        if not line:
            stderr = (self.proc.stderr.read() if self.proc.stderr else "")
            raise RuntimeError(
                f"harness died at startup. stderr:\n{stderr}"
            )
        try:
            marker = json.loads(line)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"harness startup line was not JSON: {line!r} ({e})"
            )
        if not marker.get("__startup"):
            raise RuntimeError(f"unexpected harness startup line: {marker}")
        self.startup = marker
        # Drain a snapshot of stderr that came before the startup line
        # so we can surface preload warnings to the caller.
        self.startup_stderr_snippet = ""
        if self.proc.stderr:
            os.set_blocking(self.proc.stderr.fileno(), False)

    def run(self, job: dict, timeout_s: float = 15.0) -> dict:
        if self.proc.poll() is not None:
            raise RuntimeError(
                f"harness exited unexpectedly (rc={self.proc.returncode})"
            )
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(job) + "\n")
        self.proc.stdin.flush()

        line = self._readline_with_timeout(timeout_s)
        if not line:
            raise RuntimeError("harness produced no output for job " + repr(job))
        try:
            return json.loads(line)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"harness output was not JSON ({e}): {line[:500]!r}"
            )

    def _readline_with_timeout(self, timeout_s: float) -> str:
        import select
        assert self.proc.stdout
        fd = self.proc.stdout.fileno()
        rdy, _, _ = select.select([fd], [], [], timeout_s)
        if not rdy:
            self.kill()
            raise RuntimeError(
                f"harness timed out after {timeout_s}s waiting for output"
            )
        return self.proc.stdout.readline()

    def drain_stderr(self) -> str:
        if not self.proc.stderr:
            return ""
        buf: list[str] = []
        while True:
            try:
                chunk = self.proc.stderr.read(65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            buf.append(chunk)
        return "".join(buf)

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.kill()

    def kill(self) -> None:
        try:
            self.proc.kill()
        except Exception:
            pass


# ---- per-endpoint driver ---------------------------------------------------

def run_one(
    worker: HarnessWorker,
    endpoint_key: str,
    merged_entry: dict,
    shape: dict | None,
    cache_key: str | None,
) -> dict:
    module = merged_entry["module"]
    action = merged_entry["action"]
    fn_name = merged_entry.get("fn_name") or action
    candidate = build_candidate_response(shape)
    arity = infer_request_arity(merged_entry)
    walk_schema = shape if isinstance(shape, dict) else None

    job = {
        "module": module,
        "action": action,
        "fn_name": fn_name,
        "candidate_response": candidate,
        "request_arity": arity,
        "schema": walk_schema,
        "cache_key": cache_key or "",
        "svapi_file": merged_entry.get("svapi_file"),
        "alt_fn_names": merged_entry.get("alt_fn_names", []),
    }

    try:
        parsed = worker.run(job, timeout_s=20.0)
    except RuntimeError as e:
        parsed = {
            "accessed_keys": [],
            "errors": [f"worker error: {e}"],
        }

    parsed["module"] = module
    parsed["action"] = action
    parsed.setdefault("accessed_keys", [])
    parsed.setdefault("errors", [])
    # Dedupe accessed_keys (first-seen order).
    seen: set[str] = set()
    deduped: list[str] = []
    for k in parsed["accessed_keys"]:
        if k not in seen:
            seen.add(k)
            deduped.append(k)
    parsed["accessed_keys"] = deduped
    parsed["candidate_response"] = candidate
    parsed.setdefault("lua_returned", None)
    parsed["cache_key"] = cache_key
    return parsed


def write_trace(traces_dir: Path, endpoint_key: str, trace: dict) -> Path:
    traces_dir.mkdir(parents=True, exist_ok=True)
    out = traces_dir / f"{endpoint_key}.json"
    out.write_text(json.dumps(trace, indent=2, sort_keys=False))
    return out


# ---- main -----------------------------------------------------------------

def load_json(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"missing input: {path}")
    with path.open() as f:
        return json.load(f)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", action="append", default=[],
                    help="endpoint key like 'unit.unitAll' (repeatable)")
    ap.add_argument("--all", action="store_true",
                    help="run every endpoint in merged_endpoints.json")
    ap.add_argument("--lua", default=None, help="path to lua binary")
    ap.add_argument("--out", default=str(DEFAULT_TRACES_DIR),
                    help="output traces dir (default: build/runtime/traces)")
    ap.add_argument("--list", action="store_true",
                    help="just list endpoint keys and exit")
    args = ap.parse_args(argv)

    merged = load_json(MERGED_PATH)
    extracted = load_json(EXTRACTED_PATH)
    promoted = load_json(PROMOTED_PATH)
    synthesized = load_json(SYNTHESIZED_PATH)
    cache_keys = build_cache_key_table(extracted)

    keys: list[str]
    if args.list:
        for k in sorted(merged.keys()):
            print(k)
        return 0
    if args.all:
        keys = sorted(merged.keys())
    elif args.endpoint:
        keys = list(args.endpoint)
    else:
        ap.print_help()
        return 2

    lua_bin = args.lua or pick_lua_runtime()
    print(f"using lua: {lua_bin}", file=sys.stderr)

    traces_dir = Path(args.out)
    if not traces_dir.is_absolute():
        traces_dir = ROOT / args.out

    worker = HarnessWorker(lua_bin, SOURCE_ROOT)
    print(
        f"harness booted. preload: {worker.startup.get('preload_loaded')} ok, "
        f"{worker.startup.get('preload_failed')} err, "
        f"listeners registered: {worker.startup.get('listeners_registered')}",
        file=sys.stderr,
    )
    # Surface preload errors verbatim.
    for f in worker.startup.get("preload_files") or []:
        if not f.get("ok"):
            print(f"  preload-fail {f['file']}: {f.get('err')!s}", file=sys.stderr)

    ok = 0
    fail = 0
    t0 = time.time()
    try:
        for k in keys:
            entry = merged.get(k)
            if not entry:
                print(f"skip {k}: not in merged_endpoints.json", file=sys.stderr)
                fail += 1
                continue
            shape = get_shape_for(k, promoted, synthesized)
            ck = cache_keys.get(k)
            trace = run_one(worker, k, entry, shape, ck)
            out = write_trace(traces_dir, k, trace)
            had_errors = bool(trace.get("errors"))
            n_keys = len(trace.get("accessed_keys") or [])
            status = "OK " if not had_errors else "ERR"
            if had_errors:
                fail += 1
            else:
                ok += 1
            print(
                f"{status} {k:<55} keys={n_keys:<4} -> {out.relative_to(ROOT)}",
                file=sys.stderr,
            )
    finally:
        worker.close()

    dt = time.time() - t0
    print(
        f"done: {ok} ok / {fail} err / {len(keys)} total in {dt:.1f}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
