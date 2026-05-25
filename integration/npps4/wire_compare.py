"""NPPS4 wire-compare — three modes (static-diff, regression, live-probe).

Produces actionable per-endpoint diffs between:
  - what the SIF1 client listener-layer reads (harness output), and
  - what NPPS4 server emits (static: Pydantic types; live: actual wire).

PLAN Stage 3.

USAGE
-----
  # No infra required — uses build/npps4_priors.json + build/runtime_listener_observations.json
  uv run --no-project python integration/npps4/wire_compare.py --mode static-diff --out report.md

  # Regression — compare current harness output vs committed snapshot
  uv run --no-project python integration/npps4/wire_compare.py --mode regression \
      --baseline build/runtime_listener_observations.json \
      --current /tmp/fresh_observations.json --out regression.md

  # Live-probe — gated on NPPS4 Docker stack (see PLAN Stage 3 gating). NOT
  # currently implemented; PLAN Stage 3 ships static-diff + regression only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRIORS_PATH = ROOT / "build" / "npps4_priors.json"
OBS_PATH = ROOT / "build" / "runtime_listener_observations.json"
MERGED_PATH = ROOT / "build" / "merged_endpoints.json"


def load_json(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"missing: {path}")
    return json.load(path.open())


def _normalize_npps4_field(field: str) -> str:
    """Strip RootModel artifacts to compare against listener observations.

    NPPS4 priors carry synthetic '__root__' / '__root_item__.X' for
    RootModel[list[X]] declarations. Listener observations name fields
    under 'response_data.<field>'. We strip the synthetic prefixes so
    comparable.
    """
    if field == "__root__":
        return ""
    if field.startswith("__root_item__."):
        return field[len("__root_item__.") :]
    return field


def _normalize_listener_field(field: str) -> str:
    """Strip envelope + RootModel-wrapper artifacts so we compare just the
    per-endpoint shape against the NPPS4 prior.

    `__root__` is the harness candidate-builder's synthetic key for
    RootModel endpoints (it wraps `list[X]` in a `__root__: [...]` key
    so the listener has somewhere to traverse). The real wire is just
    the bare list, so the `__root__.` prefix in client reads is harness
    artifact, not a server-side field. Strip it symmetrically with
    NPPS4's `__root_item__.` normalization.
    """
    if field.startswith("response_data."):
        field = field[len("response_data.") :]
    elif field == "response_data":
        return ""
    if field.startswith("__root__."):
        return field[len("__root__.") :]
    if field == "__root__":
        return ""
    return field


# Envelope-level fields the harness whitelists into every endpoint's
# declared set (see aggregate_listener_observations.ENVELOPE_DECLARED /
# merge_observations.ENVELOPE_DECLARED). They're not per-endpoint shape
# fields and NPPS4 priors don't model them, so they must not be counted
# as "client reads, NPPS4 doesn't emit" disagreements.
_ENVELOPE_TOP_LEVEL = {
    "status_code",
    "server_timestamp",
    "server_timestamp_sync_flag",
    "present_cnt",
    "museum_info",
    "release_info",
}


def static_diff(
    priors: dict, observations: dict, merged: dict
) -> list[dict]:
    """For every endpoint covered by both sides, emit a diff record."""
    findings: list[dict] = []
    common = sorted(set(priors) & set(observations))
    for ep in common:
        prior = priors[ep] or {}
        obs = observations[ep] or {}

        npps4_fields = {
            _normalize_npps4_field(k): v
            for k, v in (prior.get("fields") or {}).items()
        }
        npps4_fields.pop("", None)

        # All paths the client read (declared + discovered), at any depth.
        # Envelope-level whitelist entries (status_code, ...) are filtered
        # so they don't masquerade as response-shape disagreements.
        client_paths: set[str] = set()
        for source_key in ("runtime_discovered_field_names", "declared_field_names"):
            for k in obs.get(source_key) or []:
                n = _normalize_listener_field(k)
                if not n:
                    continue
                top = n.split(".", 1)[0]
                if top in _ENVELOPE_TOP_LEVEL:
                    continue
                client_paths.add(n)

        # NPPS4 paths after recursive type expansion in the priors extractor
        # are dotted (e.g. `after_user_info.energy_full_time`). Compare at
        # full depth -- top-level-only collapse used to hide nested
        # disagreements (Q7).
        npps4_paths = set(npps4_fields.keys())

        client_only = sorted(client_paths - npps4_paths)
        npps4_only = sorted(npps4_paths - client_paths)
        overlap = sorted(client_paths & npps4_paths)

        if not client_only and not npps4_only:
            continue  # perfect agreement — no finding to surface

        findings.append({
            "endpoint": ep,
            "npps4_source": prior.get("source_file"),
            "npps4_return_type": prior.get("return_type"),
            "client_reads_but_npps4_missing": client_only,
            # Fields NPPS4 declares that the harness saw no client read
            # for. NOT "the client doesn't read these" -- the harness's
            # coverage is bounded (listener pass + invoke_classes pass),
            # so a real UI-handler closure we didn't exercise could
            # still read them. Report them as "unconfirmed by harness",
            # not "dead".
            "npps4_emits_no_client_read_observed": npps4_only,
            "agreement": overlap,
            "npps4_field_count": len(npps4_paths),
            "client_field_count": len(client_paths),
        })
    return findings


def static_diff_report(findings: list[dict]) -> str:
    lines: list[str] = []
    lines.append("# NPPS4 ↔ SIF1 client wire-compare (static-diff)\n")
    lines.append(
        "Compares NPPS4 Pydantic response model fields (at full path depth, "
        "with nested model references recursively expanded) against fields "
        "the SIF1 client listener layer reads. **Listener evidence is "
        "empirical**; NPPS4 type declarations are informative-only per PLAN "
        "Prior 10 (9 of 11 RootModel[list[X]] cases contradict listener "
        "evidence).\n"
    )
    n_total = len(findings)
    n_missing = sum(1 for f in findings if f["client_reads_but_npps4_missing"])
    n_extra = sum(
        1 for f in findings if f["npps4_emits_no_client_read_observed"]
    )
    lines.append(
        f"- Endpoints compared (in both NPPS4 + harness): **{n_total}**"
    )
    lines.append(
        f"- Endpoints where client reads field NPPS4 doesn't emit: "
        f"**{n_missing}** (server bug candidates)"
    )
    lines.append(
        f"- Endpoints where NPPS4 emits field with no observed client "
        f"read: **{n_extra}** (no harness evidence of a client read — "
        f"may still be read in UI-handler closures the harness didn't "
        f"exercise)\n"
    )
    lines.append("## Per-endpoint findings\n")

    # Surface the highest-signal findings first: client-missing >
    # npps4-emits-no-observed-read
    findings_sorted = sorted(
        findings,
        key=lambda f: (
            -len(f["client_reads_but_npps4_missing"]),
            -len(f["npps4_emits_no_client_read_observed"]),
            f["endpoint"],
        ),
    )

    for f in findings_sorted:
        if not (f["client_reads_but_npps4_missing"]
                or f["npps4_emits_no_client_read_observed"]):
            continue
        lines.append(f"### `{f['endpoint']}`")
        if f["npps4_source"]:
            lines.append(
                f"- NPPS4 source: `{f['npps4_source']}` "
                f"(return type: `{f.get('npps4_return_type') or '?'}`)"
            )
        if f["client_reads_but_npps4_missing"]:
            lines.append("- **Client reads, NPPS4 doesn't emit:**")
            for fld in f["client_reads_but_npps4_missing"]:
                lines.append(f"  - `{fld}`")
        if f["npps4_emits_no_client_read_observed"]:
            lines.append(
                "- **NPPS4 emits, no client read observed by harness:**"
            )
            for fld in f["npps4_emits_no_client_read_observed"]:
                lines.append(f"  - `{fld}`")
        if f["agreement"]:
            lines.append(
                f"- Agreement on {len(f['agreement'])} field(s): "
                + ", ".join(f"`{x}`" for x in f["agreement"][:6])
                + ("..." if len(f["agreement"]) > 6 else "")
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def regression(baseline_path: Path, current_path: Path) -> dict:
    """Diff a previous observation snapshot vs a fresh harness run."""
    base = load_json(baseline_path)
    curr = load_json(current_path)
    common = set(base) & set(curr)
    only_base = sorted(set(base) - set(curr))
    only_curr = sorted(set(curr) - set(base))
    changed: list[dict] = []
    for ep in sorted(common):
        b = set((base[ep] or {}).get("runtime_discovered_field_names") or [])
        c = set((curr[ep] or {}).get("runtime_discovered_field_names") or [])
        if b != c:
            changed.append({
                "endpoint": ep,
                "fields_lost": sorted(b - c),
                "fields_gained": sorted(c - b),
            })
    return {
        "endpoints_only_in_baseline": only_base,
        "endpoints_only_in_current": only_curr,
        "changed_endpoints": changed,
    }


def regression_report(diff: dict) -> str:
    lines: list[str] = []
    lines.append("# Harness regression report\n")
    n_lost = len(diff["endpoints_only_in_baseline"])
    n_new = len(diff["endpoints_only_in_current"])
    n_chg = len(diff["changed_endpoints"])
    lines.append(f"- Endpoints lost since baseline: **{n_lost}**")
    lines.append(f"- New endpoints since baseline: **{n_new}**")
    lines.append(f"- Endpoints with field-set change: **{n_chg}**\n")
    if diff["endpoints_only_in_baseline"]:
        lines.append("## Lost endpoints\n")
        for ep in diff["endpoints_only_in_baseline"]:
            lines.append(f"- `{ep}`")
    if diff["endpoints_only_in_current"]:
        lines.append("\n## New endpoints\n")
        for ep in diff["endpoints_only_in_current"]:
            lines.append(f"- `{ep}`")
    if diff["changed_endpoints"]:
        lines.append("\n## Endpoints with field-set change\n")
        for c in diff["changed_endpoints"]:
            lines.append(f"### `{c['endpoint']}`")
            if c["fields_lost"]:
                lines.append("- Lost: " + ", ".join(
                    f"`{x}`" for x in c["fields_lost"]))
            if c["fields_gained"]:
                lines.append("- Gained: " + ", ".join(
                    f"`{x}`" for x in c["fields_gained"]))
            lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--mode",
        choices=("static-diff", "regression", "live-probe"),
        required=True,
    )
    ap.add_argument("--out", type=Path, default=Path("report.md"))
    ap.add_argument(
        "--baseline", type=Path,
        help="(regression mode) committed snapshot path",
        default=OBS_PATH,
    )
    ap.add_argument(
        "--current", type=Path,
        help="(regression mode) fresh harness output path",
    )
    args = ap.parse_args(argv)

    if args.mode == "static-diff":
        priors = load_json(PRIORS_PATH)
        obs = load_json(OBS_PATH)
        merged = load_json(MERGED_PATH)
        findings = static_diff(priors, obs, merged)
        args.out.write_text(static_diff_report(findings))
        print(f"wrote {args.out} ({len(findings)} findings)")
        return 0

    if args.mode == "regression":
        if not args.current:
            ap.error("--current required for regression mode")
        diff = regression(args.baseline, args.current)
        args.out.write_text(regression_report(diff))
        n = (
            len(diff["endpoints_only_in_baseline"])
            + len(diff["endpoints_only_in_current"])
            + len(diff["changed_endpoints"])
        )
        print(f"wrote {args.out} ({n} differences)")
        return 0 if n == 0 else 1  # non-zero exit = regression detected

    if args.mode == "live-probe":
        print(
            "live-probe mode is gated on NPPS4 Docker stack (PLAN Stage 3 "
            "gating). NPPS4 needs Postgres + alembic + signed user session. "
            "See docs/NPPS4_INTEGRATION.md for the stand-up procedure when "
            "you're ready to enable this mode.",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
