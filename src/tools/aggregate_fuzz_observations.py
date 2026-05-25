#!/usr/bin/env python3
"""aggregate_fuzz_observations.py

Reads build/runtime/traces_fuzz/<variant>/<endpoint>.json (Tier 1 fuzz
output) and produces:

  build/runtime_fuzz_observations.json
    Per-endpoint structured record:
      {
        "module": ..., "action": ...,
        "cache_key": ...,
        "declared_count": int,
        "baseline_discovered_field_names": [...],   # what V5 baseline already finds
        "fuzz_new_field_names": [...],              # paths only visible under variants
        "fuzz_new_list_shaped_fields": [...],       # subset whose raw path had `.[N]`
        "variant_attribution": {
            "list_many": ["response_data.foo", ...],   # first variant that surfaced each path
            ...
        },
        "variant_listener_errors": {variant: int},
      }

  build/runtime_fuzz_summary.md
    Human-readable summary: total new paths, per-variant attribution
    counts, top endpoints by yield.

Strategy:
- For each variant trace, normalize each accessed_keys entry the same way
  aggregate_listener_observations does (strip `.[N]` index AND `.[table: 0x...]`
  sentinel-as-key artifacts).
- baseline_discovered_field_names = normalized accessed paths in baseline
  variant that are NOT in the declared schema (i.e. the runtime_discovered
  set as the V5 aggregator computes it).
- fuzz_new_field_names = normalized paths in any non-baseline variant that
  are NOT in baseline_discovered_field_names AND NOT in the declared schema.
- variant_attribution: for each fuzz_new path, list the variants whose
  trace contains it. The first-listed wins as the "primary attribution"
  in the summary.

The provenance discipline matters: paths only surfaced by fuzzing are a
weaker signal than V5 listener-driven (which is a weaker signal than
direct caller field-reads). Downstream re-synth should label them as
`discovered_via: fuzz_<variant>`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from aggregate_listener_observations import (  # noqa: E402
    declared_paths,
    get_shape,
    cache_key_table,
)

TRACES_FUZZ_DIR = ROOT / "build" / "runtime" / "traces_fuzz"
PROMOTED_PATH = ROOT / "build" / "response_types_promoted.json"
SYNTHESIZED_PATH = ROOT / "build" / "synthesized_types.json"
EXTRACTED_PATH = ROOT / "build" / "extracted_apis.json"
LISTENER_OBS_PATH = ROOT / "build" / "runtime_listener_observations.json"
OUT_JSON = ROOT / "build" / "runtime_fuzz_observations.json"
OUT_MD = ROOT / "build" / "runtime_fuzz_summary.md"

# Matches the order in scripts/lua_harness/sentinel_variants.lua and
# scripts/run_lua_harness_fuzz.py. Used to ensure deterministic iteration
# when attributing first-discovery to a variant.
VARIANT_ORDER = ["baseline", "list_one", "list_many", "true_bool", "false_bool", "cmp_gt"]


def normalize_path(p: str) -> str:
    """Strip every `.[...]` segment so `response_data.foo.[1].bar` and
    `response_data.foo.[table: 0x123].bar` both collapse to
    `response_data.foo.bar`. The sentinel-as-key artifact is a
    LuaJIT-side accident (`unit_list[some_sentinel]` reads); the
    field-name suffix is the part downstream synth needs."""
    parts = [seg for seg in p.split(".") if not (seg.startswith("[") and seg.endswith("]"))]
    return ".".join(parts)


def load_trace(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def main() -> int:
    if not TRACES_FUZZ_DIR.exists():
        sys.exit(f"missing fuzz traces dir: {TRACES_FUZZ_DIR}")

    promoted = json.loads(PROMOTED_PATH.read_text())
    synthesized = json.loads(SYNTHESIZED_PATH.read_text())
    extracted = json.loads(EXTRACTED_PATH.read_text())
    cks = cache_key_table(extracted)
    # Cross-reference against the V5 listener-driven observations so we
    # only count paths that are NEW vs the V5 baseline (the existing
    # aggregator already considers them discoveries; fuzz should add
    # incremental value on top).
    listener_obs = json.loads(LISTENER_OBS_PATH.read_text()) if LISTENER_OBS_PATH.exists() else {}

    variant_dirs = {v: TRACES_FUZZ_DIR / v for v in VARIANT_ORDER}
    available = [v for v, p in variant_dirs.items() if p.exists()]
    if not available:
        sys.exit(f"no variant subdirs under {TRACES_FUZZ_DIR}")

    # Use baseline endpoint listing if available, else union of all variants.
    base_dir = variant_dirs.get("baseline")
    if base_dir and base_dir.exists():
        endpoints = sorted(p.stem for p in base_dir.glob("*.json"))
    else:
        ep_set: set[str] = set()
        for v in available:
            ep_set |= {p.stem for p in variant_dirs[v].glob("*.json")}
        endpoints = sorted(ep_set)

    # Envelope keys that the harness touches structurally; not discoveries.
    envelope_declared = {
        "response_data", "status_code",
        "response_data.server_timestamp",
        "response_data.server_timestamp_sync_flag",
        "response_data.present_cnt",
        "response_data.museum_info",
    }

    records: dict[str, dict] = {}
    total_baseline = 0
    total_fuzz_new = 0
    per_variant_first_count: dict[str, int] = {v: 0 for v in VARIANT_ORDER if v != "baseline"}

    for ep in endpoints:
        shape = get_shape(ep, promoted, synthesized)
        declared = declared_paths(shape, "") if shape else set()
        declared |= envelope_declared

        # Read every variant's trace for this endpoint. Missing files mean
        # the run did not cover that variant (e.g. --variant filter).
        variant_normalized: dict[str, list[tuple[str, bool]]] = {}
        variant_listener_errors: dict[str, int] = {}
        for v in available:
            tp = variant_dirs[v] / f"{ep}.json"
            if not tp.exists():
                variant_normalized[v] = []
                variant_listener_errors[v] = 0
                continue
            tr = load_trace(tp)
            # Compute discovery candidates (accessed minus declared) and normalize.
            accessed = tr.get("accessed_keys") or []
            errs = tr.get("errors") or []
            variant_listener_errors[v] = sum(1 for e in errs if "listener error" in e)
            seen_fn: dict[str, bool] = {}
            for raw in accessed:
                if raw in declared:
                    continue
                norm = normalize_path(raw)
                if not norm:
                    continue
                # is_list = raw had an `.[N]` (real list index) -- skip
                # `.[table: ...]` since that's a sentinel-as-key artifact.
                is_list = any(
                    seg.startswith("[") and seg[1:-1].isdigit() and seg.endswith("]")
                    for seg in raw.split(".")
                )
                if norm not in seen_fn:
                    seen_fn[norm] = is_list
                elif is_list:
                    seen_fn[norm] = True
            variant_normalized[v] = list(seen_fn.items())

        baseline_set = {n for n, _ in variant_normalized.get("baseline", [])}
        baseline_list_shaped = {n for n, ls in variant_normalized.get("baseline", []) if ls}

        # Also subtract paths the V5 aggregator already flagged as discovered.
        # That includes baseline_set transitively, but listener_obs may have
        # been generated against a different traces dir -- guard with .get.
        v5_obs_fields = set(
            (listener_obs.get(ep, {}).get("runtime_discovered_field_names") or [])
        )
        already_known = baseline_set | v5_obs_fields

        # For each non-baseline variant, identify paths NEW vs already_known.
        attribution: dict[str, list[str]] = {v: [] for v in VARIANT_ORDER if v != "baseline" and v in variant_normalized}
        list_shape_attribution: set[str] = set()
        seen_first: dict[str, str] = {}
        for v in VARIANT_ORDER:
            if v == "baseline" or v not in variant_normalized:
                continue
            for norm, is_list in variant_normalized[v]:
                if norm in already_known:
                    continue
                if norm in seen_first:
                    continue
                seen_first[norm] = v
                attribution[v].append(norm)
                if is_list:
                    list_shape_attribution.add(norm)

        baseline_discovered_field_names = sorted(baseline_set - envelope_declared)
        fuzz_new_field_names = sorted(seen_first.keys())
        fuzz_new_list_shaped = sorted(list_shape_attribution)

        records[ep] = {
            "module": ep.split(".", 1)[0],
            "action": ep.split(".", 1)[-1],
            "cache_key": cks.get(ep),
            "declared_count": len(declared - envelope_declared),
            "baseline_discovered_field_names": baseline_discovered_field_names,
            "baseline_list_shaped_fields": sorted(baseline_list_shaped),
            "fuzz_new_field_names": fuzz_new_field_names,
            "fuzz_new_list_shaped_fields": fuzz_new_list_shaped,
            "variant_attribution": attribution,
            "variant_listener_errors": variant_listener_errors,
        }
        total_baseline += len(baseline_discovered_field_names)
        total_fuzz_new += len(fuzz_new_field_names)
        for v, paths in attribution.items():
            per_variant_first_count[v] = per_variant_first_count.get(v, 0) + len(paths)

    OUT_JSON.write_text(json.dumps(records, indent=2, sort_keys=False))
    print(f"wrote {OUT_JSON.relative_to(ROOT)} ({len(records)} endpoints)")

    # ---- summary md ----------------------------------------------------------
    top_yield = sorted(
        ((len(r["fuzz_new_field_names"]), ep, r) for ep, r in records.items()
         if r["fuzz_new_field_names"]),
        reverse=True,
    )

    lines: list[str] = []
    lines.append("# Tier 1 fuzz observations summary")
    lines.append("")
    lines.append(f"- Endpoints analyzed: **{len(records)}**")
    lines.append(f"- Baseline V5 discovered (sanity-check, totals match aggregate_listener_observations): **{total_baseline}**")
    lines.append(f"- **NEW field names surfaced only under variant fuzz** (vs baseline + V5 listener obs): **{total_fuzz_new}**")
    lines.append("")
    lines.append("## Per-variant first-discovery attribution")
    lines.append("(Each new path is attributed to the FIRST variant -- in VARIANT_ORDER -- that surfaced it.)")
    lines.append("")
    lines.append("| variant | first-discovered count |")
    lines.append("|---|---:|")
    for v in VARIANT_ORDER:
        if v == "baseline":
            continue
        lines.append(f"| `{v}` | {per_variant_first_count.get(v, 0)} |")
    lines.append("")
    lines.append("## Top endpoints by fuzz yield")
    lines.append("")
    lines.append("| count | endpoint | cache_key | first paths |")
    lines.append("|---:|---|---|---|")
    for n, ep, r in top_yield[:30]:
        sample = ", ".join(f"`{p}`" for p in r["fuzz_new_field_names"][:3])
        lines.append(f"| {n} | `{ep}` | `{r['cache_key'] or '-'}` | {sample} |")
    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    lines.append("- `fuzz_new_field_names` is the Tier 1 yield -- paths the baseline V5")
    lines.append("  listener-driven trace could not reach because the listener gated")
    lines.append("  on `ipairs(...)`, `#x > 0`, `x.is_complete`, or `score > N`.")
    lines.append("- Confidence: WEAKER than V5 listener observations because a single")
    lines.append("  variant could plausibly trigger spurious paths (e.g. the bool")
    lines.append("  variants force-flip branches even when the field is genuinely")
    lines.append("  always-false in the wire). Recommend tagging downstream as")
    lines.append("  `discovered_via: fuzz_<variant>`; do NOT promote to a scraper-")
    lines.append("  populated shape; merge as Optional fields with `extra='allow'`.")
    lines.append("- list_shaped attribution: when the raw path had `.[N]`, the listener")
    lines.append("  iterated the field -- a strong signal for `type: list` declarations.")
    OUT_MD.write_text("\n".join(lines))
    print(f"wrote {OUT_MD.relative_to(ROOT)} ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
