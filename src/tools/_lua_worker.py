"""Shared subprocess machinery for Lua-harness worker drivers.

Three Python tools (run_lua_harness, run_invoke_classes, run_invoke_stub)
each spawn long-lived luajit subprocesses and dispatch JSON jobs over
stdin/stdout. This module factors out the common pieces:

  - _StderrDrainer: background thread that continuously drains stderr
    so the Lua process never blocks on `io.stderr:write` when the
    ~64KB kernel pipe buffer fills.
  - LuaWorkerBase: subprocess lifecycle (_boot with select-gated
    startup timeout, _restart with dead-state bookkeeping, kill, close)
    plus a default `run()` that returns an error dict on subprocess
    problems instead of raising. Subclasses with a different contract
    (e.g. raise-on-timeout) override `run()`.

The module name is underscore-prefixed because it's an internal helper,
not a CLI entry point.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path


class _StderrDrainer:
    """Background-thread stderr drain.

    Without an active reader, the Lua harness deadlocks once the
    ~64KB kernel pipe buffer fills: listener_errors + preload
    warnings + per-invoke errors stream to stderr and `io.stderr:write`
    blocks. Python's select() on stdout then times out forever
    because the Lua process is stuck on a stderr write. The drainer
    runs as a daemon thread; it exits cleanly on stream EOF when the
    subprocess closes its stderr.
    """

    def __init__(self, stream):
        self._stream = stream
        self._buf: list[str] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(4096)
                if not chunk:
                    break
                with self._lock:
                    self._buf.append(chunk)
        except (OSError, ValueError):
            pass

    def snapshot(self) -> str:
        with self._lock:
            return "".join(self._buf)

    def consume(self) -> str:
        with self._lock:
            out = "".join(self._buf)
            self._buf.clear()
            return out


class LuaWorkerBase:
    """Long-lived luajit subprocess + stdin/stdout JSON dispatcher.

    Handles spawning, the select-gated startup handshake, stderr
    draining, and restart/death bookkeeping. Subclasses customize
    `run()` if they need a different contract (e.g. raise-on-timeout
    rather than the default return-error-dict).
    """

    # Cap how long we wait for the harness's __startup line. Preload
    # is bounded in practice (~1-2s for the ~30 model files); 30s
    # leaves generous headroom but stops cold if setupUpdaters
    # infinite-loops or a preload-file pcall enters a tight loop.
    _STARTUP_TIMEOUT_S = 30.0
    # Default per-job timeout for the inherited run() implementation.
    # Subclasses override or callers pass timeout_s explicitly.
    _DEFAULT_RUN_TIMEOUT_S = 8.0

    def __init__(self, lua_bin: str, source_root: Path, harness_dir: Path):
        self.lua_bin = lua_bin
        self.source_root = source_root
        self.harness_dir = harness_dir
        self.proc: subprocess.Popen | None = None
        self._stderr_drainer: _StderrDrainer | None = None
        self.startup: dict | None = None
        self._dead = False
        self._boot()

    def _boot(self) -> None:
        cmd = [self.lua_bin, str(self.harness_dir / "harness.lua"),
               str(self.source_root)]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.harness_dir),
            text=True,
            bufsize=1,
        )
        # Daemon thread continuously drains stderr so the Lua process
        # never blocks on `io.stderr:write` when the pipe buffer fills.
        self._stderr_drainer = _StderrDrainer(self.proc.stderr)
        import select
        assert self.proc.stdout
        fd = self.proc.stdout.fileno()
        rdy, _, _ = select.select([fd], [], [], self._STARTUP_TIMEOUT_S)
        if not rdy:
            self.kill()
            raise RuntimeError(
                f"worker startup timed out after {self._STARTUP_TIMEOUT_S}s. "
                f"stderr:\n{self._stderr_drainer.snapshot()[:800]}"
            )
        line = self.proc.stdout.readline()
        if not line:
            time.sleep(0.05)  # let drainer catch death-throes stderr
            raise RuntimeError(
                f"worker died at startup. "
                f"stderr:\n{self._stderr_drainer.snapshot()[:400]}"
            )
        try:
            marker = json.loads(line)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"worker startup line was not JSON: {line!r} ({e}). "
                f"stderr:\n{self._stderr_drainer.snapshot()[:400]}"
            )
        if not marker.get("__startup"):
            raise RuntimeError(f"unexpected worker startup line: {marker}")
        self.startup = marker

    def _restart(self) -> None:
        # Kill the existing subprocess and try to boot a fresh one. If
        # _boot() raises, leave the worker in a clean "dead" state so
        # subsequent run() calls short-circuit rather than write to a
        # dangling/partially-initialised proc (which would leak
        # processes on consecutive boot failures or hang on writes to
        # a stale pipe).
        self.kill()
        self.proc = None
        self._stderr_drainer = None
        try:
            self._boot()
        except Exception as e:
            print(
                f"worker boot failed; subsequent jobs will short-circuit: {e}",
                file=sys.stderr,
            )
            self.proc = None
            self._stderr_drainer = None
            self._dead = True

    def run(self, job: dict, timeout_s: float | None = None) -> dict:
        """Send a job, wait up to `timeout_s` for one JSON response line.

        Returns the parsed response dict, or an error dict (with an
        `errors` list and empty `accessed_keys`) on timeout, broken
        pipe, or bad JSON. Never raises — the workers run in batch
        loops where one bad endpoint should not abort the rest.
        """
        ts = timeout_s if timeout_s is not None else self._DEFAULT_RUN_TIMEOUT_S
        if self._dead or self.proc is None:
            return {"errors": ["worker is dead (boot failed)"],
                    "accessed_keys": []}
        try:
            self.proc.stdin.write(json.dumps(job) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            self._restart()
            return {"errors": ["worker restart (broken pipe)"],
                    "accessed_keys": []}
        import select
        rdy, _, _ = select.select([self.proc.stdout.fileno()], [], [], ts)
        if not rdy:
            self._restart()
            return {"errors": [f"timeout {ts}s — worker restarted"],
                    "accessed_keys": []}
        line = self.proc.stdout.readline()
        if not line:
            self._restart()
            return {"errors": ["worker died (empty output)"],
                    "accessed_keys": []}
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {"errors": [f"bad JSON: {line[:200]}"],
                    "accessed_keys": []}

    def drain_stderr(self) -> str:
        """Consume and clear all buffered stderr (returns the snapshot)."""
        if not self._stderr_drainer:
            return ""
        return self._stderr_drainer.consume()

    def stderr_snapshot(self) -> str:
        """Return buffered stderr without clearing it."""
        if not self._stderr_drainer:
            return ""
        return self._stderr_drainer.snapshot()

    def close(self) -> None:
        try:
            if self.proc and self.proc.stdin:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
        except Exception:
            self.kill()

    def kill(self) -> None:
        try:
            if self.proc:
                self.proc.kill()
        except Exception:
            pass
