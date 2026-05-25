"""Per-endpoint coverage classification.

Sort all 358 endpoints into one of four buckets and emit
build/coverage_classification.json + a markdown summary.

Buckets (from PLAN.md Stage 2):
  - harness-covered: listener-discovered field paths exist
  - envelope-only:   no listener fires; URL/method/cache_key extracted only
  - ui-only:         listener body present but reads no fields; UI handler
                     reads them (candidate for follow-on Approach B)
  - needs-Frida:     no listener body / all listeners are envelope-acks
                     AND no UI handler reads the response

The classification is the deliverable for the NPPS4 conversation: a finite
prioritized list, not "everything we haven't done".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRACES_DIR = ROOT / "build" / "runtime" / "traces"
CLASSES_DIR = ROOT / "build" / "runtime" / "traces_classes"
OBS_PATH = ROOT / "build" / "runtime_listener_observations.json"
MERGED_PATH = ROOT / "build" / "merged_endpoints.json"
EXTRACTED_PATH = ROOT / "build" / "extracted_apis.json"
SOURCE_ROOT = ROOT / "assets" / "decompiled" / "all"
OUT_JSON = ROOT / "build" / "coverage_classification.json"
OUT_MD = ROOT / "build" / "coverage_classification.md"

# Heuristic: action names matching these prefixes are typically fire-and-forget
# acks that legitimately have no response body of interest. PLAN Priors §1
# names this bucket "~110 envelope-only acks".
ACK_PREFIXES = re.compile(
    r"^(cancel|leave|skip|set|reserve|abort|read|wait|init|use|expel|response|"
    r"agree|clearCache|disconnect|kidRegister|syncDeactivate|removeAccount|"
    r"abortDelete|reserveDelete|setBirth|setNotification)"
)
ACK_SUFFIXES = re.compile(r"(Set|Cancel|Skip|Leave|Read|Ack|Init)$")


def grep_uihandler_callers(
    action: str, fn_name: str, cache_key: str | None = None
) -> list[str]:
    """Return decompiled m_*/ files that mention this endpoint.

    Decompiled bytecode strips local var names, so we look for the string
    constant references that survive: cache_key strings like "$rewardList"
    and svapi-table dereferences like `.rewardList` / `.rewardListStub`.
    The cache_key string and fn_name are usually DIFFERENT (e.g.
    fn_name="rewardOpen" but cache_key="$rewardList"), so we accept both.
    """
    if not SOURCE_ROOT.exists():
        return []
    candidates: list[str] = []
    # Search via filesystem grep for cache key / stub references.
    patterns = [f"\\.{fn_name}\\b", f"\\.{fn_name}Stub\\b"]
    if cache_key and cache_key.startswith("$"):
        # Escape the $ for the regex engine. rg uses Rust regex which treats
        # $ as end-of-line; in a quoted literal we want the literal char.
        patterns.append(f'"\\{cache_key}"')
    import subprocess

    pat = "|".join(patterns)
    try:
        out = subprocess.run(
            [
                "rg",
                "-l",
                "--type",
                "lua",
                "--no-messages",
                pat,
                str(SOURCE_ROOT),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    for line in out.stdout.splitlines():
        rel = line.replace(str(SOURCE_ROOT) + "/", "")
        # We only care about non-svapi (i.e. UI / handler) files
        if "/svapi/" in rel:
            continue
        candidates.append(rel)
    return candidates


def classify_one(
    ep: str,
    observations: dict,
    merged_entry: dict | None,
    trace: dict,
    cache_key: str | None = None,
) -> dict:
    accessed = trace.get("accessed_keys") or []
    obs = observations.get(ep) or {}
    discovered = set(obs.get("runtime_discovered_field_names") or [])

    action = ep.split(".", 1)[1] if "." in ep else ep
    fn_name = (merged_entry or {}).get("fn_name") or action

    is_ack = bool(ACK_PREFIXES.match(action) or ACK_SUFFIXES.search(action))

    bucket: str
    rationale: str
    ui_handler_files: list[str] = []

    if discovered:
        bucket = "harness-covered"
        rationale = (
            f"listener discovered {len(discovered)} field path(s) "
            f"beyond declared schema"
        )
    elif len(accessed) >= 5:
        bucket = "harness-covered"
        rationale = (
            f"{len(accessed)} accessed keys via listener (no synth-grounded "
            "schema to compare against, but listener clearly read response)"
        )
    elif is_ack:
        bucket = "envelope-only"
        rationale = (
            f"action name matches ack pattern; {len(accessed)} keys "
            "(envelope only, no body of interest)"
        )
    else:
        # Borderline. Check if any UI handler file references it -- via
        # svapi-table dereference OR via the cache_key string (the two
        # are decoupled: fn_name="rewardOpen" / cache_key="$rewardList").
        ui_handler_files = grep_uihandler_callers(action, fn_name, cache_key)
        if ui_handler_files:
            bucket = "ui-only"
            rationale = (
                f"{len(accessed)} keys via listener; "
                f"{len(ui_handler_files)} UI file(s) reference fn_name — "
                "response likely unpacked in UI handler, not Cachable listener"
            )
        else:
            bucket = "needs-Frida"
            rationale = (
                f"{len(accessed)} keys via listener; no UI file references "
                "the fn_name or cache_key. Likely state-dependent (matching, "
                "polling, login) or response shape only visible via wire capture."
            )

    return {
        "endpoint": ep,
        "bucket": bucket,
        "rationale": rationale,
        "accessed_keys_count": len(accessed),
        "discovered_field_count": len(discovered),
        "fn_name": fn_name,
        "ui_handler_files": ui_handler_files,
    }


def main() -> int:
    if not TRACES_DIR.exists():
        raise SystemExit(f"missing traces dir: {TRACES_DIR}")
    with OBS_PATH.open() as f:
        observations = json.load(f)
    with MERGED_PATH.open() as f:
        merged = json.load(f)

    # Build cache_key lookup so the UI-handler grep can search for cache_key
    # string constants (e.g. `"$rewardList"`), not just fn_name dereferences.
    cache_keys: dict[str, str] = {}
    if EXTRACTED_PATH.exists():
        with EXTRACTED_PATH.open() as f:
            extracted = json.load(f)
        for _mod, val in extracted.items():
            for api in val.get("apis", []):
                ck = api.get("cache_key")
                if ck:
                    cache_keys[f"{api['module']}.{api['action']}"] = ck

    # Build a per-endpoint accessed_keys union across the V5 listener pass
    # (TRACES_DIR) and the invoke_classes pass (CLASSES_DIR). Without this,
    # the `len(accessed) >= 5` bucket heuristic only saw V5 reads and
    # ignored invoke_classes evidence -- so an endpoint whose listener
    # logged 0 keys but whose UI-handler methods (Approach B) read >=5
    # response fields fell into ui-only/needs-Frida instead of harness-
    # covered. CLASSES_DIR is absent on the initial classify call (before
    # ui-classes runs); we handle that gracefully.
    accessed_by_ep: dict[str, set[str]] = {}
    traces_by_ep: dict[str, dict] = {}
    for trace_path in sorted(TRACES_DIR.glob("*.json")):
        ep = trace_path.stem
        with trace_path.open() as f:
            trace = json.load(f)
        traces_by_ep[ep] = trace
        accessed_by_ep.setdefault(ep, set()).update(trace.get("accessed_keys") or [])
    if CLASSES_DIR.exists():
        for trace_path in sorted(CLASSES_DIR.glob("*.json")):
            ep = trace_path.stem
            with trace_path.open() as f:
                trace = json.load(f)
            accessed_by_ep.setdefault(ep, set()).update(
                trace.get("accessed_keys") or []
            )

    classifications: dict[str, dict] = {}
    for ep, trace in traces_by_ep.items():
        merged_trace = dict(trace)
        merged_trace["accessed_keys"] = sorted(accessed_by_ep.get(ep, set()))
        classifications[ep] = classify_one(
            ep, observations, merged.get(ep), merged_trace, cache_keys.get(ep)
        )

    by_bucket: dict[str, list[str]] = {}
    for ep, c in classifications.items():
        by_bucket.setdefault(c["bucket"], []).append(ep)

    summary = {
        "total": len(classifications),
        "by_bucket": {b: len(v) for b, v in sorted(by_bucket.items())},
        "endpoints": classifications,
    }
    OUT_JSON.write_text(json.dumps(summary, indent=2, sort_keys=False))
    print(f"wrote {OUT_JSON.relative_to(ROOT)}")

    md: list[str] = []
    md.append("# Per-endpoint coverage classification\n")
    md.append(f"Total endpoints: **{len(classifications)}**\n")
    md.append("| Bucket | Count | What NPPS4 does with this |")
    md.append("|---|---:|---|")
    bucket_action = {
        "harness-covered": "Schema-correctable via harness output",
        "envelope-only": "extra='allow' stub is correct as-is",
        "ui-only": "Tier 2 follow-on candidate (handler invocation)",
        "needs-Frida": "Hand back: needs wire capture / state injection",
    }
    for b in ("harness-covered", "envelope-only", "ui-only", "needs-Frida"):
        count = len(by_bucket.get(b, []))
        md.append(f"| `{b}` | {count} | {bucket_action.get(b, '')} |")
    md.append("")
    for b in ("harness-covered", "ui-only", "needs-Frida", "envelope-only"):
        eps = by_bucket.get(b, [])
        if not eps:
            continue
        md.append(f"\n## `{b}` ({len(eps)} endpoints)\n")
        for ep in sorted(eps):
            c = classifications[ep]
            md.append(f"- `{ep}` — {c['rationale']}")
    OUT_MD.write_text("\n".join(md) + "\n")
    print(f"wrote {OUT_MD.relative_to(ROOT)}")

    print("\nBucket totals:")
    for b, count in summary["by_bucket"].items():
        print(f"  {b:20} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
