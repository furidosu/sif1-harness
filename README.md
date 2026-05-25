# sif1-harness

**Automated listener-layer client exercise for SIF1 schema discovery. Complements [NPPS4](https://github.com/DarkEnergyProcessor/NPPS4).**

NPPS4 has reverse-engineered ~113 of the 358 SIF1 wire endpoints by hand
over years of work, with production-traffic validation behind every
field. The remaining ~245 are unimplemented mostly because **exercising
the client to find out what each endpoint's response shape needs to look
like is labor-intensive** — someone has to navigate an emulator to the
right screen, trigger the action, and capture the wire response.

This harness automates the listener-layer slice of that exercise via
two complementary passes against the same LuaJIT-loaded client:

1. **`notifyUpdate` pass.** Preloads the client's listener-registering
   Lua files, fires `Cachable.notifyUpdate(cache_key, spied_response)`
   for every endpoint, and logs every field path the listener bodies
   read from the response object.
2. **`invoke_classes` pass (Approach B).** For endpoints whose responses
   are unpacked in UI-handler closures rather than Cachable listeners,
   pre-populates the Cachable cache with a spied candidate and then
   invokes every exported method on every UI class that references the
   endpoint. Reads on the populated cache fire the spy.

No device, no server, no human in the loop. Full pipeline (both passes,
plus aggregation, classification, and NPPS4 wire-compare) runs in
**~12 seconds** end-to-end on a laptop.

## Current results

| Metric | Count |
|---|---:|
| Endpoints exercised | **358 / 358** |
| Endpoints with field-path discoveries | **166** |
| Unique field paths discovered | **356** |
| Endpoints classified `harness-covered` | **261** |
| Endpoints classified `envelope-only` | **31** |
| Endpoints classified `ui-only` | **49** |
| Endpoints classified `needs-Frida` (state-dependent) | **17** |
| `wire-compare --mode static-diff` endpoints with disagreement | **86** |
| ↳ client-reads-NPPS4-doesn't-emit (server bug candidates) | **35** |
| ↳ NPPS4-emits-with-no-observed-client-read (harness saw no read; may still be read in UI closures the harness didn't exercise) | **75** |

These reflect both the baseline notifyUpdate-driven discovery and the
Approach B `invoke_classes` pass that targets ui-only endpoints.

See [`docs/COVERAGE_CEILING.md`](docs/COVERAGE_CEILING.md) for why the
ceiling is what it is and [`docs/FINDINGS_AGAINST_NPPS4.md`](docs/FINDINGS_AGAINST_NPPS4.md)
for the top findings. Two of those findings explain
[NPPS4 issue #22](https://github.com/DarkEnergyProcessor/NPPS4/issues/22),
an open user-reported crash — see
[`docs/CORROBORATION_WITH_NPPS4_ISSUES.md`](docs/CORROBORATION_WITH_NPPS4_ISSUES.md).

## What this deliberately is NOT

- **Not a server implementation.** NPPS4 is the canonical reference.
- **Not a FastAPI scaffold.** That's NPPS4's job — we emit data, they emit code.
- **Not a synth pass.** LLM-driven schema synthesis stays in the predecessor
  project; NPPS4 won't trust LLM-generated fields and they're right.
- **Not a UI emulator.** We invoke handler entry points programmatically.
- **Not a Frida hook.** That's the complement, not the substitute.

## Repository layout

```
src/
  harness/             # The Lua harness itself (LuaJIT — 8 files)
  tools/               # Python drivers:
                       #   run_lua_harness.py            (notifyUpdate pass)
                       #   aggregate_listener_observations.py
                       #   find_ui_handlers.py           (build ui_handler_map.json)
                       #   run_invoke_classes.py         (Approach B invoke_classes pass)
                       #   run_invoke_stub.py            (single-endpoint Stub probe)
                       #   merge_observations.py         (union both passes)
                       #   classify_coverage.py          (bucket into 4 categories)
  scripts/             # Asset pipeline: decrypt_all.py, decompile_all.py, extract_apis.py
priors/                # NPPS4 prior-extraction (AST walker)
integration/npps4/     # wire_compare.py (--mode static-diff / regression / live-probe)
build/                 # All outputs (runtime/ subdir gitignored; observations snapshot committed)
assets/decompiled/all  # symlink to decompiled client tree (gitignored — KLab proprietary)
docs/                  # HOW_HARNESS_WORKS / NPPS4_INTEGRATION / COVERAGE_CEILING / FINDINGS_AGAINST_NPPS4
```

## Quick start

Prerequisites:
- LuaJIT 2.1 (`brew install luajit` on macOS)
- Python 3.10+ with `uv`
- A decompiled SIF1 client tree (not provided — KLab proprietary)

```bash
# 1. Point at your decompiled client tree
ln -sfn /path/to/your/decompiled-client/all assets/decompiled/all

# 2. Clone NPPS4 next to this repo (or set NPPS4_SRC=<path>)
git clone --depth 1 https://github.com/DarkEnergyProcessor/NPPS4 ./npps4

# 3. Run the full pipeline (notifyUpdate + invoke_classes + aggregate + classify + compare)
make compare-npps4
# wrote build/wire_compare_static.md (86 findings, 35 client-reads-NPPS4-missing)
```

Step 3 is equivalent to running these in order:

```bash
uv run --no-project python src/tools/run_lua_harness.py --all --out build/runtime/traces
uv run --no-project python src/tools/aggregate_listener_observations.py
uv run --no-project python src/tools/classify_coverage.py         # initial classify -> coverage_classification.json
uv run --no-project python src/tools/find_ui_handlers.py          # builds ui_handler_map.json
uv run --no-project python src/tools/run_invoke_classes.py --bucket ui-only
uv run --no-project python src/tools/merge_observations.py
uv run --no-project python src/tools/classify_coverage.py         # re-classify with merged data
uv run --no-project python priors/extract_npps4_priors.py
uv run --no-project python integration/npps4/wire_compare.py --mode static-diff \
    --out build/wire_compare_static.md
```

## How this complements NPPS4

| Layer | Best tool |
|---|---|
| Top-level + nested response field paths (harness-covered: 261 endpoints) | This harness |
| Nested field shapes when listeners or UI handlers destructure them | This harness |
| Per-endpoint behavior, state machines, validation | NPPS4 |
| State-dependent endpoint shapes (17 needs-Frida) | Frida companion + NPPS4 |
| Residual UI-handler endpoints (49 ui-only) where invoke_classes didn't land | Per-endpoint hand-wiring |

The harness produces empirical evidence per field; NPPS4 absorbs that
evidence into typed Pydantic models. The output of `make compare-npps4`
is the input to a productive PR conversation.

## License

MIT. The harness contains no KLab-derived content — all bytecode /
decompiled trees are user-supplied via the symlinked
`assets/decompiled/all` directory and are not redistributed by this
repo.
