#!/usr/bin/env -S uv run --no-project python
"""Build build/merged_endpoints.json from build/extracted_apis.json.

In v1 this file was a merge with an external reference; here it's a
straight projection of extracted_apis, since we don't import API.md /
sif_schemas as priors. The harness only needs (module, action, fn_name,
request.ours) per endpoint.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IN = ROOT / "build" / "extracted_apis.json"
OUT = ROOT / "build" / "merged_endpoints.json"


def main() -> int:
    data = json.loads(IN.read_text())
    out: dict[str, dict] = {}
    for mod, info in data.items():
        for api in info["apis"]:
            key = f"{api['module']}.{api['action']}"
            ours = [f["name"] for f in api.get("fields", [])]
            svapi_file = api.get("svapi_file") or ""
            svapi_file = svapi_file.removesuffix(".lua") if svapi_file else None
            out[key] = {
                "module": api["module"],
                "action": api["action"],
                "fn_name": api["fn_name"],
                "cache_key": api.get("cache_key"),
                "request": {"ours": ours, "theirs": []},
                "url": api.get("url"),
                "svapi_file": svapi_file,
                "alt_fn_names": api.get("alt_fn_names", []),
            }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"wrote {OUT.relative_to(ROOT)} - {len(out)} endpoints")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
