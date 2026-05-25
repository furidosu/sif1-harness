#!/usr/bin/env -S uv run --no-project python
"""Extract per-endpoint Pydantic response models from NPPS4 server source.

NPPS4 (github.com/DarkEnergyProcessor/NPPS4) registers endpoints via
`@idol.register("<module>", "<action>", ...)` decorators. The decorated
function's return type annotation is a Pydantic BaseModel that NPPS4
serializes back to the SIF1 client.

We walk NPPS4's game/*.py and system/*.py files, identify
@idol.register decorations, resolve the return type to a Pydantic class
definition (searched across all files), and emit a flat
{module.action: {fields: {fname: pytype, ...}, source_file}} JSON.

This is the ONLY verified external prior (per plan): every documented
field corresponds to one a working server returned. Even so, NPPS4
fields MUST NOT be merged into canonical schemas without listener/
scraper corroboration; they're tagged `prior_source: npps4` so the
consumer can filter.

Output: build/npps4_priors.json
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Set NPPS4_SRC to a checkout of github.com/DarkEnergyProcessor/NPPS4
# and DLAPI_SRC to a checkout of NPPS4-DLAPI (without DLAPI, the
# ~11 download/album/profile endpoints whose response shapes live in
# n4dlapi/model.py resolve to empty fields).
import os
NPPS4_ROOT = Path(os.environ.get("NPPS4_SRC", "./npps4"))
DLAPI_ROOT = Path(os.environ.get("DLAPI_SRC", "./npps4-dlapi"))
OUT = ROOT / "build" / "npps4_priors.json"


def _class_fields(node: ast.ClassDef, klass_index: dict[str, ast.ClassDef]) -> dict[str, str]:
    """Extract field names + (best-effort) type strings from a Pydantic BaseModel.

    Walks AnnAssign nodes in the class body and any base classes that
    are themselves found in `klass_index` (mixins like AchievementMixin
    are inlined). Returns {field_name: type_repr} where type_repr is
    the unparsed annotation text.

    Special case: when a class is `pydantic.RootModel[list[X]]` (or just
    `RootModel[list[X]]`), the response root IS a list of X — no field
    wrapper. We surface this as a single synthetic `__root_list_of__`
    entry so the bundle template can render it differently.
    """
    out: dict[str, str] = {}
    for base in node.bases:
        # RootModel[list[X]] -> the entire response IS list[X]
        if isinstance(base, ast.Subscript):
            value = base.value
            is_root = (
                (isinstance(value, ast.Name) and value.id == "RootModel")
                or (isinstance(value, ast.Attribute) and value.attr == "RootModel")
            )
            if is_root:
                try:
                    inner = ast.unparse(base.slice)
                except Exception:
                    inner = "Any"
                out["__root__"] = inner
                # Resolve element class fields if it's list[ClassName]
                m = re.match(r"list\[(\w+)\]", inner)
                if m and m.group(1) in klass_index:
                    elem_fields = _class_fields(klass_index[m.group(1)], klass_index)
                    for k, v in elem_fields.items():
                        out[f"__root_item__.{k}"] = v
                continue
        if isinstance(base, ast.Name) and base.id in klass_index:
            out.update(_class_fields(klass_index[base.id], klass_index))
        elif isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
            base_name = base.attr
            if base_name in klass_index:
                out.update(_class_fields(klass_index[base_name], klass_index))
    for body_node in node.body:
        if isinstance(body_node, ast.AnnAssign) and isinstance(body_node.target, ast.Name):
            fname = body_node.target.id
            if fname.startswith("_"):
                continue
            try:
                tystr = ast.unparse(body_node.annotation)
            except Exception:
                tystr = "Any"
            out[fname] = tystr
    return out


def _resolve_return_annotation(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    if fn.returns is None:
        return None
    try:
        return ast.unparse(fn.returns)
    except Exception:
        return None


def _find_class_in_index(name: str, klass_index: dict[str, ast.ClassDef]) -> ast.ClassDef | None:
    # Strip module prefix (e.g. `lbonus.LoginBonusResponse` -> `LoginBonusResponse`).
    bare = name.split(".")[-1].split("[")[0].strip()
    return klass_index.get(bare)


def _decorator_args(dec: ast.expr) -> tuple[str, str] | None:
    """If dec is `@idol.register("mod", "act", ...)`, return ("mod", "act")."""
    if not isinstance(dec, ast.Call):
        return None
    callee = dec.func
    is_idol_register = (
        (isinstance(callee, ast.Attribute) and callee.attr == "register"
         and isinstance(callee.value, ast.Name) and callee.value.id == "idol")
        or (isinstance(callee, ast.Name) and callee.id == "register")
    )
    if not is_idol_register:
        return None
    if len(dec.args) < 2:
        return None
    a0, a1 = dec.args[0], dec.args[1]
    if isinstance(a0, ast.Constant) and isinstance(a1, ast.Constant):
        if isinstance(a0.value, str) and isinstance(a1.value, str):
            return a0.value, a1.value
    return None


def main() -> int:
    if not NPPS4_ROOT.exists():
        sys.exit(f"NPPS4 source not found at {NPPS4_ROOT}")
    # Build global class index across all NPPS4 .py files so cross-file
    # references resolve (LoginBonusResponse defined in game/lbonus.py,
    # AchievementMixin in system/achievement.py). Also fold DLAPI models
    # in — the game-side download.* endpoints return DLAPI shapes.
    py_files = list((NPPS4_ROOT / "npps4").rglob("*.py"))
    if DLAPI_ROOT.exists():
        py_files.extend((DLAPI_ROOT / "n4dlapi").rglob("*.py"))
    klass_index: dict[str, ast.ClassDef] = {}
    for f in py_files:
        try:
            src = f.read_text()
            tree = ast.parse(src)
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                klass_index.setdefault(node.name, node)

    out: dict[str, dict] = {}
    for f in py_files:
        if "/test" in str(f):
            continue
        try:
            src = f.read_text()
            tree = ast.parse(src)
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                pair = _decorator_args(dec)
                if not pair:
                    continue
                module, action = pair
                key = f"{module}.{action}"
                if key in out:
                    continue  # first wins
                ret_ann = _resolve_return_annotation(node)
                if not ret_ann:
                    out[key] = {
                        "module": module, "action": action,
                        "source_file": str(f.relative_to(NPPS4_ROOT)),
                        "return_type": None,
                        "fields": {},
                    }
                    break
                bare = ret_ann.split("[")[0].strip()
                cls = _find_class_in_index(bare, klass_index)
                fields: dict[str, str] = {}
                if cls is not None:
                    fields = _class_fields(cls, klass_index)
                out[key] = {
                    "module": module, "action": action,
                    "source_file": str(f.relative_to(NPPS4_ROOT)),
                    "return_type": ret_ann,
                    "fields": fields,
                }
                break  # don't process additional decorators on same fn

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False, sort_keys=True))
    n_with_fields = sum(1 for v in out.values() if v["fields"])
    print(f"wrote {OUT.relative_to(ROOT)} - {len(out)} endpoints "
          f"({n_with_fields} with parsed fields)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
