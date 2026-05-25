"""Map each ui-only endpoint to candidate UI handler files + class names.

For each endpoint in the ui-only bucket, scan the decompiled tree for files
that reference either the svapi fn name (`.<fn>(`, `.<fn>Stub(`) or the
cache_key string (`"$<cacheKey>"`). For each candidate file, extract the
`define("ClassName", ...)` registration so the harness can invoke its
exported methods.

Output: build/ui_handler_map.json keyed by endpoint.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "assets" / "decompiled" / "all"
EXTRACTED_PATH = ROOT / "build" / "extracted_apis.json"
MERGED_PATH = ROOT / "build" / "merged_endpoints.json"
COVERAGE_PATH = ROOT / "build" / "coverage_classification.json"
OUT = ROOT / "build" / "ui_handler_map.json"

DEFINE_RE = re.compile(r'define\s*\(\s*"([^"]+)"', re.MULTILINE)
# Old-style: L40_1 = define; L41_1 = "RewardList"; L42_1 = L34_1; L40_1(L41_1, L42_1)
# We pick up the constant-string form which works on most decompiled files.
# Backup: any line that does `L<n> = "ClassName"` directly preceded by
# `L<n> = define` and followed by an invocation.
DEFINE_STRING_RE = re.compile(r'L\d+_\d+\s*=\s*"([A-Z][A-Za-z0-9_]+)"')


def candidate_files_for(action: str, fn_name: str,
                        cache_key: str | None) -> list[str]:
    """Return decompiled m_*/ files that reference this endpoint."""
    if not SOURCE_ROOT.exists():
        return []
    pats: list[str] = []
    # Direct svapi calls
    pats.append(rf"\.{re.escape(fn_name)}\b")
    pats.append(rf"\.{re.escape(fn_name)}Stub\b")
    if action and action != fn_name:
        pats.append(rf'"\.\s*{re.escape(action)}\b')
        pats.append(rf"\.{re.escape(action)}\b")
    # Cache-key string references — only if present
    if cache_key and cache_key.startswith("$"):
        pats.append(rf'"\{cache_key}"')
    pat = "|".join(pats)

    try:
        out = subprocess.run(
            ["rg", "-l", "--type", "lua", "--no-messages", pat,
             str(SOURCE_ROOT)],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    files: list[str] = []
    for line in out.stdout.splitlines():
        rel = line.replace(str(SOURCE_ROOT) + "/", "")
        # Skip svapi files (those ARE the endpoint, not consumers)
        if "/svapi/" in rel:
            continue
        # Only m_* / common (UI files) are relevant
        if not (rel.startswith("m_") or rel.startswith("common/")):
            continue
        files.append(rel)
    return files


def extract_define_name(file_path: Path) -> str | None:
    """Return the ClassName registered via define() at end of file."""
    if not file_path.exists():
        return None
    try:
        src = file_path.read_text(errors="ignore")
    except Exception:
        return None
    # Look for the "ClassName" string AFTER `<varN> = define`, scanning the
    # last few hundred lines of the file (decompiled m_*/ files almost
    # always do define() in the last 5-10 lines).
    tail = src.split("\n")[-50:]
    tail_str = "\n".join(tail)
    # First try: `<L> = define` then `<L> = "Name"` then the call
    if "= define" in tail_str:
        names = DEFINE_STRING_RE.findall(tail_str)
        # Pick a name that looks plausibly like a class (CamelCase, >= 4 chars)
        for n in names:
            if len(n) >= 4 and n[0].isupper():
                return n
    return None


def main() -> int:
    extracted = json.load(EXTRACTED_PATH.open())
    merged = json.load(MERGED_PATH.open())
    coverage = json.load(COVERAGE_PATH.open())

    # cache_key per endpoint
    cache_keys: dict[str, str] = {}
    for _mod, val in extracted.items():
        for api in val.get("apis", []):
            ck = api.get("cache_key")
            if ck:
                cache_keys[f"{api['module']}.{api['action']}"] = ck

    ui_only = [ep for ep, c in coverage["endpoints"].items()
               if c["bucket"] == "ui-only"]
    print(f"ui-only endpoints: {len(ui_only)}", file=sys.stderr)

    result: dict[str, dict] = {}
    for ep in sorted(ui_only):
        entry = merged.get(ep) or {}
        action = ep.split(".", 1)[1] if "." in ep else ep
        fn_name = entry.get("fn_name") or action
        ck = cache_keys.get(ep)

        files = candidate_files_for(action, fn_name, ck)
        files_with_class: list[dict] = []
        for f in files:
            cls = extract_define_name(SOURCE_ROOT / f)
            files_with_class.append({"file": f, "class": cls})

        result[ep] = {
            "cache_key": ck,
            "fn_name": fn_name,
            "action": action,
            "candidate_files": files_with_class,
        }

    OUT.write_text(json.dumps(result, indent=2, sort_keys=False))

    n_with = sum(1 for v in result.values() if v["candidate_files"])
    n_classed = sum(
        1 for v in result.values()
        for f in v["candidate_files"] if f.get("class")
    )
    n_files = sum(len(v["candidate_files"]) for v in result.values())
    print(f"wrote {OUT.relative_to(ROOT)}", file=sys.stderr)
    print(f"  ui-only with >=1 candidate file: {n_with}/{len(result)}",
          file=sys.stderr)
    print(f"  total candidate files: {n_files}, with class: {n_classed}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
