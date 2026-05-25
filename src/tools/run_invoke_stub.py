"""Run the `invoke_stub` job kind against ui-only-bucket endpoints.

invoke_stub directly drives `svapi.<module>.<action>Stub(...)` to build
the Stub descriptor and then fires `descriptor.on_success(spied_envelope)`.
This exposes the per-call success closure -- where many ui-only-bucket
endpoints actually destructure response fields -- without needing to
discover and invoke an outer module function.

Writes traces under build/runtime/traces_stub/<endpoint>.json. The
aggregator merges these into the main listener observations file.

Usage:
  uv run --no-project python src/tools/run_invoke_stub.py --all
  uv run --no-project python src/tools/run_invoke_stub.py --bucket ui-only
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HARNESS_DIR = ROOT / "src" / "harness"
SOURCE_ROOT = ROOT / "assets" / "decompiled" / "all"
MERGED_PATH = ROOT / "build" / "merged_endpoints.json"
EXTRACTED_PATH = ROOT / "build" / "extracted_apis.json"
COVERAGE_PATH = ROOT / "build" / "coverage_classification.json"
DEFAULT_TRACES_DIR = ROOT / "build" / "runtime" / "traces_stub"


def pick_lua_runtime() -> str:
    for cand in ("luajit", "lua5.1", "lua-5.1"):
        if shutil.which(cand):
            return cand
    sys.exit("no Lua runtime found (need luajit or lua5.1)")


def load_json(path: Path) -> dict:
    return json.load(path.open())


class Worker:
    def __init__(self, lua_bin: str, source_root: Path):
        cmd = [lua_bin, str(HARNESS_DIR / "harness.lua"), str(source_root)]
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=str(HARNESS_DIR),
            text=True, bufsize=1,
        )
        line = self.proc.stdout.readline()
        marker = json.loads(line)
        if not marker.get("__startup"):
            raise RuntimeError(f"bad startup: {marker}")
        self.startup = marker

    def run(self, job: dict, timeout_s: float = 15.0) -> dict:
        self.proc.stdin.write(json.dumps(job) + "\n")
        self.proc.stdin.flush()
        import select
        rdy, _, _ = select.select([self.proc.stdout.fileno()], [], [], timeout_s)
        if not rdy:
            self.kill()
            raise RuntimeError(f"timeout: {job}")
        line = self.proc.stdout.readline()
        return json.loads(line)

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.kill()

    def kill(self) -> None:
        try:
            self.proc.kill()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", action="append", default=[])
    ap.add_argument("--all", action="store_true",
                    help="run every endpoint in merged_endpoints.json")
    ap.add_argument("--bucket",
                    help="restrict to endpoints in this coverage bucket "
                    "(harness-covered/envelope-only/ui-only/needs-Frida)")
    ap.add_argument("--out", default=str(DEFAULT_TRACES_DIR))
    args = ap.parse_args(argv)

    merged = load_json(MERGED_PATH)
    extracted = load_json(EXTRACTED_PATH)

    cache_keys: dict[str, str] = {}
    for _mod, val in extracted.items():
        for api in val.get("apis", []):
            ck = api.get("cache_key")
            if ck:
                cache_keys[f"{api['module']}.{api['action']}"] = ck

    keys: list[str]
    if args.endpoint:
        keys = list(args.endpoint)
    elif args.bucket:
        coverage = load_json(COVERAGE_PATH)
        keys = sorted([
            ep for ep, c in coverage["endpoints"].items()
            if c["bucket"] == args.bucket
        ])
    elif args.all:
        keys = sorted(merged.keys())
    else:
        ap.print_help()
        return 2

    traces_dir = Path(args.out)
    if not traces_dir.is_absolute():
        traces_dir = ROOT / args.out
    traces_dir.mkdir(parents=True, exist_ok=True)

    lua_bin = pick_lua_runtime()
    print(f"using lua: {lua_bin}, {len(keys)} endpoints to probe",
          file=sys.stderr)
    worker = Worker(lua_bin, SOURCE_ROOT)
    print(
        f"harness booted. preload: {worker.startup.get('preload_loaded')} ok, "
        f"{worker.startup.get('preload_failed')} err, "
        f"listeners: {worker.startup.get('listeners_registered')}",
        file=sys.stderr,
    )

    ok = 0
    fail = 0
    no_stub = 0
    t0 = time.time()
    try:
        for ep in keys:
            entry = merged.get(ep)
            if not entry:
                fail += 1
                continue
            module = entry["module"]
            action = entry["action"]
            job = {
                "kind": "invoke_stub",
                "module": module,
                "action": action,
                "svapi_file": entry.get("svapi_file"),
                "cache_key": cache_keys.get(ep, ""),
                "stub_arity": 6,
            }
            try:
                trace = worker.run(job, timeout_s=20.0)
            except RuntimeError as e:
                trace = {"errors": [str(e)], "accessed_keys": []}
            trace["endpoint"] = ep
            # Dedupe accessed_keys
            seen = set()
            deduped = []
            for k in trace.get("accessed_keys") or []:
                if k not in seen:
                    seen.add(k)
                    deduped.append(k)
            trace["accessed_keys"] = deduped

            out_path = traces_dir / f"{ep}.json"
            out_path.write_text(json.dumps(trace, indent=2))

            errs = trace.get("errors") or []
            keys_n = len(trace["accessed_keys"])
            if any("Stub not found" in e for e in errs):
                no_stub += 1
                status = "NoStub"
            elif errs and keys_n == 0:
                fail += 1
                status = "ERR"
            else:
                ok += 1
                status = "OK"
            print(
                f"{status:6} {ep:55} keys={keys_n:<4} errs={len(errs)}",
                file=sys.stderr,
            )
    finally:
        worker.close()

    dt = time.time() - t0
    print(f"done: {ok} ok / {fail} err / {no_stub} no-stub in {dt:.1f}s",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
