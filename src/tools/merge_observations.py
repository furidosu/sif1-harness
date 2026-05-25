"""Merge invoke_classes traces into runtime_listener_observations.json.

The main aggregator only walks build/runtime/traces (V5 listener traces).
The invoke_classes driver writes to build/runtime/traces_classes/. This
script unions the per-endpoint accessed_keys from BOTH dirs, recomputes
runtime_discovered_field_names (the schema-undeclared subset), and
writes a refreshed observations file.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRACES_DIR = ROOT / "build" / "runtime" / "traces"
CLASSES_DIR = ROOT / "build" / "runtime" / "traces_classes"
PROMOTED_PATH = ROOT / "build" / "response_types_promoted.json"
SYNTHESIZED_PATH = ROOT / "build" / "synthesized_types.json"
OBS_PATH = ROOT / "build" / "runtime_listener_observations.json"
OUT_OBS = ROOT / "build" / "runtime_listener_observations.json"
OUT_MD = ROOT / "build" / "runtime_listener_summary.md"


def load_json(p: Path) -> dict:
    if not p.exists():
        return {}
    return json.load(p.open())


def get_shape(ep: str, promoted: dict, synthesized: dict) -> dict | None:
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


def declared_paths(shape: dict | None, prefix: str = "") -> set[str]:
    out: set[str] = set()
    if not isinstance(shape, dict):
        return out
    fields = shape.get("fields")
    if isinstance(fields, dict):
        for k, sub in fields.items():
            p = (prefix + "." + k) if prefix else k
            out.add(p)
            if isinstance(sub, dict):
                if sub.get("type") == "object" and sub.get("fields"):
                    out |= declared_paths(sub, p)
                elif sub.get("type") == "list" and sub.get("element"):
                    elem = sub["element"]
                    if isinstance(elem, dict) and elem.get("fields"):
                        out |= declared_paths({"fields": elem["fields"]}, p)
    return out


def normalize_path(p: str) -> str:
    if p.startswith("response_data."):
        return p[len("response_data."):]
    return p


def main() -> int:
    promoted = load_json(PROMOTED_PATH)
    synthesized = load_json(SYNTHESIZED_PATH)
    existing = load_json(OBS_PATH)

    # Collect accessed_keys from both trace dirs.
    accessed_by_ep: dict[str, set[str]] = {}
    for ep, rec in existing.items():
        rk = rec.get("runtime_accessed_keys") or rec.get("accessed_keys") or []
        accessed_by_ep[ep] = set(rk)

    for d in (TRACES_DIR, CLASSES_DIR):
        if not d.exists():
            continue
        for fp in sorted(d.glob("*.json")):
            ep = fp.stem
            try:
                trace = json.load(fp.open())
            except Exception:
                continue
            keys = trace.get("accessed_keys") or []
            accessed_by_ep.setdefault(ep, set()).update(keys)

    # Recompute discovered fields per endpoint.
    out: dict[str, dict] = {}
    n_with_discoveries = 0
    union_paths: set[str] = set()
    for ep in sorted(accessed_by_ep):
        accessed = accessed_by_ep[ep]
        # Keep ONLY response_data.<...> paths. Bulksend batch reads
        # (`bulksend.[N].*`) get attributed to the dispatching endpoint
        # but are not part of its own response shape -- they belong to
        # the per-batch endpoint of index N. They can also runaway-iterate
        # through sentinel phantoms (8000+ array elements observed for
        # personalnotice.get's bulkSend success_cb).
        accessed = {
            k for k in accessed
            if k.startswith("response_data.") and "." in k
        }
        shape = get_shape(ep, promoted, synthesized)
        declared = declared_paths(shape, "")
        # Normalize: strip the response_data. prefix from accessed; declared
        # paths come without it.
        accessed_norm = {normalize_path(k) for k in accessed}
        # Also drop paths that contain sentinel-iteration artifacts
        # ([N] indices with no field follow, or extremely long chains).
        accessed_norm = {
            k for k in accessed_norm
            if not k.endswith(".[1]") and k.count(".") < 8
        }
        discovered = sorted(accessed_norm - declared)
        union_paths |= set(discovered)
        rec = existing.get(ep, {}).copy() if isinstance(existing.get(ep), dict) else {}
        rec["runtime_accessed_keys"] = sorted(accessed)
        rec["runtime_discovered_field_names"] = discovered
        rec["declared_field_names"] = sorted(declared)
        if discovered:
            n_with_discoveries += 1
        out[ep] = rec

    OUT_OBS.write_text(json.dumps(out, indent=2, sort_keys=False))

    md = [
        "# Listener-driven discovery summary (merged: notifyUpdate + invoke_classes)",
        "",
        f"- Endpoints analyzed: **{len(out)}**",
        f"- Endpoints with >=1 discovered field: **{n_with_discoveries}**",
        f"- Unique discovered field paths across all endpoints: **{len(union_paths)}**",
        "",
    ]
    OUT_MD.write_text("\n".join(md) + "\n")
    print(f"wrote {OUT_OBS.relative_to(ROOT)}")
    print(f"  endpoints with discoveries: {n_with_discoveries}")
    print(f"  unique field paths: {len(union_paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
