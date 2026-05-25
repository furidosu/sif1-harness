#!/usr/bin/env -S uv run --no-project python
"""Re-derive build/extracted_apis.json from source/all/common/svapi/*.lua.

Each svapi file declares 1+ endpoints via a strictly templated `function L_n_1`
body that writes `<table>.module = "..."`, `<table>.action = "..."`, and a
set of `<table>.<field> = A<i>_2` request-field assignments, then calls
`L<x>_2 = L0_1.send; L<x>_2(<table>, ...)`.

Output: {module: {apis: [{action, fn_name, cache_key, arg_names, url, ...}]}}
keyed by the wire-level module string (NOT the filename — that bridge is in
scrape_responses.py).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from _lua_spans import function_spans  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SVAPI = ROOT / "source" / "all" / "common" / "svapi"
OUT = ROOT / "build" / "extracted_apis.json"

MODULE_RE = re.compile(r'^\s*L\d+_\d+\.module\s*=\s*"([^"]+)"', re.MULTILINE)
ACTION_RE = re.compile(r'^\s*L\d+_\d+\.action\s*=\s*"([^"]+)"', re.MULTILINE)
FIELD_FROM_ARG_RE = re.compile(r"^\s*(L\d+_\d+)\.(\w+)\s*=\s*(A\d+)_\d+\s*$", re.MULTILINE)
CACHE_KEY_RE = re.compile(r'L\d+_\d+\.cacheResponse\b.*?"(\$[A-Za-z0-9_]+)"', re.DOTALL)
URL_RE = re.compile(r'^\s*L\d+_\d+\s*=\s*"(/[\w/]+)"\s*$', re.MULTILINE)
SEND_RE = re.compile(r"^\s*L\d+_\d+\s*=\s*L\d+_\d+\.send\s*$", re.MULTILINE)
BIND_RE = re.compile(r"^\s*L\d+_\d+\.(?P<api>\w+)\s*=\s*(?P<rhs>L\d+_\d+)\s*$", re.MULTILINE)


def attach_public_bindings(src: str, spans: list[dict]) -> dict[int, list[str]]:
    by_name: dict[str, list[tuple[int, int, int]]] = {}
    for i, s in enumerate(spans):
        if s["name"] and s["name"].startswith("L"):
            by_name.setdefault(s["name"], []).append((i, s["start"], s["end"]))
    out: dict[int, list[str]] = {s["start"]: [] for s in spans}
    for m in BIND_RE.finditer(src):
        owners = by_name.get(m.group("rhs"), [])
        bind_pos = m.start()
        owner_start = None
        for _, s, e in owners:
            if e <= bind_pos:
                owner_start = s
            else:
                break
        if owner_start is not None:
            out[owner_start].append(m.group("api"))

    # Wrapper-chain propagation: many svapi files wrap the inner http.send
    # function in a thin wrapper that's the one actually bound on the
    # module (e.g. `function L7_1(...) L3_2 = L6_1; L3_2(...); end` and
    # `L2_1.profileUser = L7_1` — the inner endpoint span L6_1 has no
    # direct binding). For each span without a binding, look at every
    # other span that calls it (mentions its name as a function value or
    # invokes it directly) and inherit those bindings. Limited to one
    # hop; deeper nesting hasn't been observed in svapi files.
    name_to_span_idx: dict[str, int] = {}
    for i, s in enumerate(spans):
        if s["name"] and s["name"].startswith("L"):
            name_to_span_idx[s["name"]] = i
    for i, s in enumerate(spans):
        if out[s["start"]]:
            continue
        target = s["name"]
        if not target:
            continue
        ref_re = re.compile(rf"\b{re.escape(target)}\b")
        for j, other in enumerate(spans):
            if j == i or out.get(other["start"]) is None or not out[other["start"]]:
                continue
            body = src[other["body_start"]:other["end"]]
            if ref_re.search(body):
                for api in out[other["start"]]:
                    if api not in out[s["start"]]:
                        out[s["start"]].append(api)
    return out


def extract_endpoints_in_file(path: Path) -> list[dict]:
    src = path.read_text()
    spans = function_spans(src)
    binds = attach_public_bindings(src, spans)
    out: list[dict] = []
    by_fn_name: dict[str, dict] = {}
    for span in spans:
        body = src[span["body_start"]:span["end"]]
        if not SEND_RE.search(body):
            continue
        m_mod = MODULE_RE.search(body)
        m_act = ACTION_RE.search(body)
        if not (m_mod and m_act):
            continue
        module = m_mod.group(1)
        action = m_act.group(1)
        table_var = MODULE_RE.search(body).group(0).split(".module")[0].strip().split()[-1]
        fields: list[dict] = []
        seen: set[str] = set()
        for fm in FIELD_FROM_ARG_RE.finditer(body):
            tbl, fname, arg = fm.group(1), fm.group(2), fm.group(3)
            if tbl != table_var:
                continue
            if fname in ("module", "action", "timeStamp", "commandNum"):
                continue
            if fname in seen:
                continue
            seen.add(fname)
            arg_idx = int(arg[1:])
            fields.append({"name": fname, "source": "arg", "arg_index": arg_idx})
        url = None
        for um in URL_RE.finditer(body):
            cand = um.group(1)
            if cand.startswith(f"/{module}/{action}"):
                url = cand
                break
        if url is None:
            url = f"/{module}/{action}"
        cm = CACHE_KEY_RE.search(body)
        cache_key = cm.group(1) if cm else None
        pub_names = binds.get(span["start"], [])
        fn_name = pub_names[0] if pub_names else None
        # When the public binding isn't on the inner-endpoint function (e.g.
        # lBonus.lua wraps the execute body in L4_1 but binds L5_1 as
        # lBonusExecute), the default fn_name uses the WIRE module name and
        # may not match the actual binding's file-cased name. Compute both
        # candidates so the harness can try each in turn.
        file_module = path.stem
        wire_default = f"{module}{action[0].upper()}{action[1:]}"
        file_default = f"{file_module}{action[0].upper()}{action[1:]}"
        if not fn_name:
            fn_name = wire_default
        alt_fn_names: list[str] = []
        for cand in (wire_default, file_default, action):
            if cand != fn_name and cand not in alt_fn_names:
                alt_fn_names.append(cand)
        params = span["params"]
        record = {
            "module": module,
            "action": action,
            "url": url,
            "cache_key": cache_key,
            "fields": fields,
            "inner_fn": span["name"],
            "args_count": len(params),
            "arg_names": params,
            "variants": 1,
            "fn_name": fn_name,
            "alt_fn_names": alt_fn_names,
            "svapi_file": path.name,
        }
        key = f"{module}.{action}"
        if key in by_fn_name:
            by_fn_name[key]["variants"] += 1
        else:
            by_fn_name[key] = record
            out.append(record)
    return out


def main() -> int:
    files = sorted(SVAPI.glob("*.lua"))
    files = [f for f in files if f.name not in ("include.lua", "_util.lua")]
    by_module: dict[str, list[dict]] = {}
    total = 0
    for f in files:
        eps = extract_endpoints_in_file(f)
        for ep in eps:
            by_module.setdefault(ep["module"], []).append(ep)
            total += 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out = {mod: {"apis": apis} for mod, apis in sorted(by_module.items())}
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(
        f"wrote {OUT.relative_to(ROOT)} - {total} endpoints across "
        f"{len(by_module)} modules from {len(files)} svapi files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
