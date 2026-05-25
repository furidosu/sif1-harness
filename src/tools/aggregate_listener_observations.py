#!/usr/bin/env python3
"""aggregate_listener_observations.py

Compares the V5 listener-driven traces under build/runtime/traces_v5/
against the corresponding static schema for each endpoint, producing:

  build/runtime_listener_observations.json
    Per-endpoint structured record:
      {
        "module": ..., "action": ...,
        "cache_key": ...,
        "declared_fields":  [paths declared by the static schema],
        "runtime_discovered": [paths the listener read that we did NOT declare],
        "runtime_declared_but_unread": [paths we declared the listener never read],
        "runtime_listener_errors": int,
        "notes": ""
      }

  build/runtime_listener_summary.md
    Human report: per-module + top-N by discovery count.

V5 vs V1 distinction: V5 traces include reads from real Cachable
listeners (preloaded from m_boot/initialize.lua + ~30 model files) on
top of the V1 schema_walk navigator. The `runtime_discovered` set is
exactly the field paths the listeners read that the static schema
doesn't declare -- ground truth for fields that future synth passes
should bake in as declared.

Schema cascade matches gen_models.py / run_lua_harness.py: scraper
truth wins unless its response_data is an empty object stub, in which
case synthesized wins.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TRACES_V5_DIR = ROOT / "build" / "runtime" / "traces"
TRACES_V1_DIR = ROOT / "build" / "runtime" / "traces_v1"  # legacy compat
PROMOTED_PATH = ROOT / "build" / "response_types_promoted.json"
SYNTHESIZED_PATH = ROOT / "build" / "synthesized_types.json"
EXTRACTED_PATH = ROOT / "build" / "extracted_apis.json"
OUT_JSON = ROOT / "build" / "runtime_listener_observations.json"
OUT_MD = ROOT / "build" / "runtime_listener_summary.md"


def load_json(path: Path):
    with path.open() as f:
        return json.load(f)


def _union_object(left: dict, right: dict, depth: int = 0) -> None:
    """Union `right`'s fields into `left` recursively. Left wins on conflict."""
    if depth > 6:
        return
    lf = left.setdefault("fields", {})
    rf = right.get("fields") or {}
    for k, rv in rf.items():
        if k in lf:
            lv = lf[k]
            if (isinstance(lv, dict) and lv.get("type") == "object"
                    and isinstance(rv, dict) and rv.get("type") == "object"):
                _union_object(lv, rv, depth + 1)
            continue
        lf[k] = rv


def get_shape(endpoint_key: str, promoted: dict, synthesized: dict) -> dict | None:
    """Mirror gen_models.py's cascade: scraper-truth wins, but runtime-grounded
    synth (source=='synth_v3') unions additional fields into scraper-derived
    shapes. Without this, listener traces see fewer "declared" fields than
    the actual emitted Pydantic model declares, and absorption stats undercount."""
    p_entry = promoted.get(endpoint_key) if isinstance(promoted.get(endpoint_key), dict) else None
    s_entry = synthesized.get(endpoint_key) if isinstance(synthesized.get(endpoint_key), dict) else None
    p = (p_entry or {}).get("shape")
    s = (s_entry or {}).get("shape")
    is_runtime_grounded = (s_entry or {}).get("source") == "synth_v3"
    if p:
        rd = p.get("fields", {}).get("response_data") if isinstance(p.get("fields"), dict) else None
        p_has_fields = (
            isinstance(rd, dict) and rd.get("type") == "object" and bool(rd.get("fields"))
        )
        if p_has_fields:
            if is_runtime_grounded and isinstance(s, dict):
                s_rd = s.get("fields", {}).get("response_data") if isinstance(s.get("fields"), dict) else None
                if isinstance(s_rd, dict) and s_rd.get("type") == "object" and s_rd.get("fields"):
                    # Deep-copy then union (don't mutate the loaded JSON).
                    merged_p = json.loads(json.dumps(p))
                    _union_object(merged_p["fields"]["response_data"], s_rd)
                    return merged_p
            return p
    if s:
        return s
    return p


def declared_paths(shape: dict | None, prefix: str = "", depth: int = 0) -> set[str]:
    """Walk the schema, emitting every leaf+intermediate path as a
    dotted string -- the same shape the spy's __index log uses."""
    out: set[str] = set()
    if not isinstance(shape, dict) or depth > 32:
        return out
    fields = shape.get("fields")
    if isinstance(fields, dict):
        for k, sub in fields.items():
            path = f"{prefix}.{k}" if prefix else k
            out.add(path)
            if isinstance(sub, dict):
                if sub.get("type") == "list" and sub.get("element"):
                    out.update(declared_paths(sub["element"], f"{path}.[1]", depth + 1))
                    # Lua-side ipairs starts at 1 but the spy's __ipairs
                    # logs [1], [2], ... and our schema_walk logs [1]
                    # explicitly. Listener traces sometimes show [0]
                    # (LuaJIT's __ipairs increment quirk); accept both.
                    out.add(f"{path}.[0]")
                out.update(declared_paths(sub, path, depth + 1))
    return out


def cache_key_table(extracted: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for mod, val in extracted.items():
        for api in val.get("apis", []):
            ck = api.get("cache_key")
            if ck:
                out[f"{api['module']}.{api['action']}"] = ck
    return out


def main() -> int:
    if not TRACES_V5_DIR.exists():
        sys.exit(f"missing traces dir: {TRACES_V5_DIR}")

    promoted = load_json(PROMOTED_PATH)
    synthesized = load_json(SYNTHESIZED_PATH)
    extracted = load_json(EXTRACTED_PATH)
    cache_keys = cache_key_table(extracted)

    records: dict[str, dict] = {}
    total_v5 = 0
    total_discovered = 0
    listener_err_endpoints = 0
    listener_err_count = 0
    no_schema_endpoints = 0

    for trace_path in sorted(TRACES_V5_DIR.glob("*.json")):
        ep = trace_path.stem
        trace = load_json(trace_path)
        accessed = trace.get("accessed_keys") or []
        accessed_set = set(accessed)
        total_v5 += len(accessed_set)

        shape = get_shape(ep, promoted, synthesized)
        declared = declared_paths(shape, "") if shape else set()
        # The envelope-level keys come from cacheResponse's envelope
        # access in the stubs -- treat them as declared so they don't
        # show up as discovery.
        declared |= {"response_data", "status_code",
                     "response_data.server_timestamp",
                     "response_data.server_timestamp_sync_flag",
                     "response_data.present_cnt",
                     "response_data.museum_info"}

        discovered = sorted(accessed_set - declared)
        unread = sorted(declared - accessed_set - {"response_data", "status_code"})

        # Normalize discovered paths for downstream V6 re-synth use:
        # strip every `.[N]` index segment so list-element reads collapse
        # to their parent field names. `.[0]` specifically is an artifact
        # of listeners doing `arr[#arr]` on a sentinel (where `#sentinel`
        # returns 0, so `[0]` is the resolved index). `.[1]` is real list
        # iteration but the integer index conveys no additional info -- the
        # field name + "this is list-shaped" is all V6 needs.
        def normalize(p: str) -> str:
            parts = [seg for seg in p.split(".") if not (seg.startswith("[") and seg.endswith("]"))]
            return ".".join(parts)
        discovered_field_names: list[str] = []
        seen_fn: set[str] = set()
        list_shaped: set[str] = set()
        for p in discovered:
            n = normalize(p)
            if not n or n in seen_fn:
                # Even if duplicated, still mark list-shaped when applicable.
                if ".[" in p:
                    list_shaped.add(n)
                continue
            seen_fn.add(n)
            discovered_field_names.append(n)
            if ".[" in p:
                list_shaped.add(n)
        errs = trace.get("errors") or []
        listener_err = sum(1 for e in errs if "listener error" in e)
        if listener_err:
            listener_err_endpoints += 1
            listener_err_count += listener_err
        if not shape:
            no_schema_endpoints += 1

        records[ep] = {
            "module": trace.get("module"),
            "action": trace.get("action"),
            "cache_key": cache_keys.get(ep),
            "declared_count": len(declared),
            # Emit the declared field-name list too -- wire_compare static-diff
            # consumes this key, and previously only the merge step wrote it,
            # so a single-pass `make aggregate` run produced an observations
            # file that wire_compare couldn't compare against. Listing them
            # here makes the static-diff mode work directly off aggregate
            # output too.
            "declared_field_names": sorted(declared),
            "runtime_accessed_count": len(accessed_set),
            "runtime_discovered": discovered,
            "runtime_discovered_field_names": discovered_field_names,
            "runtime_list_shaped_fields": sorted(list_shaped),
            "runtime_declared_but_unread": unread,
            "runtime_listener_errors": listener_err,
        }
        total_discovered += len(discovered_field_names)

    OUT_JSON.write_text(json.dumps(records, indent=2, sort_keys=False))
    print(f"wrote {OUT_JSON.relative_to(ROOT)} ({len(records)} endpoints)")

    # -------- human report ---------------------------------------------------
    # Rank by NORMALIZED field name count (discovered_field_names), not raw
    # paths -- raw paths double-count the same field via `.[1]` / `.[0]` suffix.
    top = sorted(
        ((len(r["runtime_discovered_field_names"]), ep, r) for ep, r in records.items()
         if r["runtime_discovered_field_names"]),
        reverse=True,
    )

    per_module: dict[str, int] = {}
    for ep, r in records.items():
        per_module[r["module"]] = per_module.get(r["module"], 0) + len(r["runtime_discovered_field_names"])
    per_module_sorted = sorted(per_module.items(), key=lambda kv: -kv[1])

    lines = []
    lines.append("# V5 listener-driven discovery summary")
    lines.append("")
    lines.append(f"- Endpoints analyzed: **{len(records)}**")
    lines.append(f"- Total runtime accessed keys (union): **{total_v5}**")
    lines.append(f"- Total **discovered field names** (normalized; listener read but schema didn't declare): **{total_discovered}**")
    lines.append(f"- Endpoints with at least 1 discovered field: **{sum(1 for _, _, r in top if r['runtime_discovered_field_names'])}**")
    lines.append(f"- Endpoints with listener errors (helper crash mid-way): **{listener_err_endpoints}** ({listener_err_count} total errors)")
    lines.append(f"- Endpoints with no static schema (envelope-only): **{no_schema_endpoints}**")
    lines.append("")
    lines.append("## Top endpoints by discovered field count")
    lines.append("")
    lines.append("| count | endpoint | cache_key | sample field names |")
    lines.append("|---:|---|---|---|")
    for n, ep, r in top[:40]:
        sample = ", ".join(f"`{p}`" for p in r["runtime_discovered_field_names"][:4])
        lines.append(f"| {n} | `{ep}` | `{r['cache_key'] or '-'}` | {sample} |")
    lines.append("")
    lines.append("## Per-module discovery totals")
    lines.append("")
    lines.append("| module | total discovered |")
    lines.append("|---|---:|")
    for mod, count in per_module_sorted:
        if count == 0:
            continue
        lines.append(f"| {mod} | {count} |")
    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    lines.append("- `runtime_discovered` is the strongest signal for a Push V6 re-synth: these")
    lines.append("  field names came from a REAL Cachable listener body destructuring the")
    lines.append("  response, not an LLM guess. They should be promoted to declared fields")
    lines.append("  in the next gen_models.py run.")
    lines.append("- `runtime_listener_errors` is expected. Many listener bodies call into")
    lines.append("  engine helpers (e.g. `ClassInfo.getInstance().update(...)`) that our")
    lines.append("  permissive stubs cannot fully simulate. Listeners that crash partway")
    lines.append("  still log every field they touched before the crash.")
    lines.append("- `runtime_declared_but_unread` does NOT mean we should drop those fields")
    lines.append("  -- only this batch's specific listeners didn't read them. The server")
    lines.append("  must still send them. Surfaced here for spot-checks only.")
    OUT_MD.write_text("\n".join(lines))
    print(f"wrote {OUT_MD.relative_to(ROOT)} ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
