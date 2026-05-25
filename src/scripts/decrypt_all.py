#!/usr/bin/env -S uv run --no-project python
"""Decrypt the entire encrypted asset tree under `assets/`.

Reads:   assets/**/*.lua  +  assets/**/*.db_
Writes:  decrypted/**/*.lua  +  decrypted_db/**/*.db

Idempotent: skips a target whose mtime is newer than the source.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# honkypy is the SIF1 asset decryption library. It is not on PyPI;
# obtain it from a SIF1 RE community fork and place it on the
# Python path via HONKYPY_PATH (a directory containing the `honkypy`
# package) or pre-install it however you prefer.
_honkypy_path = os.environ.get("HONKYPY_PATH")
if _honkypy_path:
    sys.path.insert(0, _honkypy_path)
try:
    import honkypy  # noqa: E402
except ImportError as exc:
    sys.exit(
        "honkypy not importable — set HONKYPY_PATH to a directory "
        "containing the honkypy package, or install it directly. "
        f"(error: {exc})"
    )

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets"
LUA_OUT = ROOT / "decrypted"
DB_OUT = ROOT / "decrypted_db"


def needs_rebuild(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return True
    return src.stat().st_mtime > dst.stat().st_mtime


def decrypt_file(src: Path, dst: Path, want_sqlite: bool) -> str:
    data = src.read_bytes()
    if len(data) < 16:
        return "SHORT"
    dctx, _gt = honkypy.decrypt_setup_probe(src.name, data[:16])
    plain = dctx.decrypt_block(data[16:])
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(plain)
    head = plain[:5]
    if want_sqlite and not plain.startswith(b"SQLite format 3"):
        return "NOT_SQLITE"
    if not want_sqlite and head not in (b"\x1bLuaR", b"\x1bLuaQ", b"\x1bLua\x51", b"\x1bLua\x52"):
        return "NOT_LUA_BC"
    return "OK"


def main() -> int:
    if not SRC.exists():
        print(f"no assets dir at {SRC}", file=sys.stderr)
        return 1
    lua_srcs = sorted(SRC.rglob("*.lua"))
    db_srcs = sorted(SRC.rglob("*.db_"))
    print(f"lua: {len(lua_srcs)} files; db: {len(db_srcs)} files")
    summary: dict[str, int] = {"lua_ok": 0, "lua_skip": 0, "db_ok": 0, "db_skip": 0, "err": 0}
    for src in lua_srcs:
        rel = src.relative_to(SRC)
        dst = LUA_OUT / rel
        if not needs_rebuild(src, dst):
            summary["lua_skip"] += 1
            continue
        try:
            st = decrypt_file(src, dst, want_sqlite=False)
        except Exception as e:  # noqa: BLE001
            summary["err"] += 1
            print(f"  ! {rel}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if st == "OK":
            summary["lua_ok"] += 1
        else:
            summary[st] = summary.get(st, 0) + 1
            print(f"  ? {rel} -> {st}")
    for src in db_srcs:
        rel = src.relative_to(SRC).with_suffix(".db")
        dst = DB_OUT / rel
        if not needs_rebuild(src, dst):
            summary["db_skip"] += 1
            continue
        try:
            st = decrypt_file(src, dst, want_sqlite=True)
        except Exception as e:  # noqa: BLE001
            summary["err"] += 1
            print(f"  ! {rel}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if st == "OK":
            summary["db_ok"] += 1
        else:
            summary[st] = summary.get(st, 0) + 1
            print(f"  ? {rel} -> {st}")
    print(f"done: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
