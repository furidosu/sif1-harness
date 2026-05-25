#!/usr/bin/env -S uv run --no-project python
"""Run unluac on every decrypted Lua bytecode file -> source/all/.

Reads:  decrypted/**/*.lua  (KLab \x1bLuaR magic, handled by unluac.jar)
Writes: source/all/**/*.lua

Idempotent: skips a target whose mtime is newer than the source. unluac
spins up a fresh JVM per file; v1 found subprocess pools don't help much
under the cold-JVM cost, but the run completes in ~6min sequentially.
"""
from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNLUAC = ROOT / "tools" / "unluac.jar"
SRC = ROOT / "decrypted"
DST = ROOT / "source" / "all"


def needs_rebuild(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return True
    return src.stat().st_mtime > dst.stat().st_mtime


def _decompile_one(args: tuple[str, str]) -> tuple[str, str, str]:
    src, dst = args
    try:
        out = subprocess.run(
            ["java", "-jar", str(UNLUAC), src],
            check=True, capture_output=True, text=True, timeout=30,
        )
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_text(out.stdout)
        return src, "ok", ""
    except subprocess.CalledProcessError as e:
        return src, "err", (e.stderr or "").strip()[:200]
    except subprocess.TimeoutExpired:
        return src, "timeout", ""


def main() -> int:
    if not UNLUAC.exists():
        print(f"missing {UNLUAC}", file=sys.stderr)
        return 1
    jobs: list[tuple[str, str]] = []
    skip = 0
    for src in sorted(SRC.rglob("*.lua")):
        rel = src.relative_to(SRC)
        dst = DST / rel
        if not needs_rebuild(src, dst):
            skip += 1
            continue
        jobs.append((str(src), str(dst)))
    print(f"decompiling {len(jobs)} files (skip={skip})")
    ok = err = timeout = 0
    # Parallel JVMs help when the host has many cores; cap to avoid OOM.
    with ProcessPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_decompile_one, j) for j in jobs]
        for i, fut in enumerate(as_completed(futures), 1):
            src, st, msg = fut.result()
            if st == "ok":
                ok += 1
            elif st == "timeout":
                timeout += 1
                print(f"  ! {src}: timeout", file=sys.stderr)
            else:
                err += 1
                print(f"  ! {src}: {msg}", file=sys.stderr)
            if i % 200 == 0:
                print(f"  ... {i}/{len(jobs)}")
    print(f"done: ok={ok} skip={skip} err={err} timeout={timeout}")
    return 0 if (err + timeout) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
