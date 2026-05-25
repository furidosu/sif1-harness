# NPPS4 integration walkthrough

How to consume the sif1-harness output against NPPS4 — both today
(static-diff and regression modes) and tomorrow (live-probe mode,
deferred per PLAN Stage 3 gating).

## Today: static-diff

Requires nothing but a local NPPS4 checkout.

```bash
# Place an NPPS4 checkout next to this repo (or set NPPS4_SRC=<path>).
git clone --depth 1 https://github.com/DarkEnergyProcessor/NPPS4 ./npps4

# Optionally clone NPPS4-DLAPI too (set DLAPI_SRC=<path>); without it,
# the ~11 download/album/profile endpoints whose response shapes live
# in n4dlapi/model.py resolve to empty fields.
git clone --depth 1 https://github.com/DarkEnergyProcessor/NPPS4-DLAPI ./npps4-dlapi

# Run the full pipeline (notifyUpdate + invoke_classes + aggregate +
# classify + priors + wire-compare). ~20s end-to-end.
make compare-npps4
# wrote build/wire_compare_static.md (86 findings, 35 client-reads-NPPS4-missing)
```

The output is a per-endpoint markdown report with two sections per
finding:
- **Client reads, NPPS4 doesn't emit:** server-bug candidates. Most
  signal — the client expects this field at runtime. **35 endpoints**
  in the current report.
- **NPPS4 emits, no client read observed by harness:** the harness's
  listener + invoke_classes coverage saw no read of these fields. NOT
  proof the client doesn't read them — a UI-handler closure the
  harness didn't exercise could still read them. Treat as "unconfirmed
  by harness" rather than "dead". **75 endpoints.**

The comparison runs at **full path depth** (nested-field aware): the
priors extractor recursively expands referenced Pydantic models, so
disagreements like `event_list.[1].subtitle` (client reads, NPPS4's
`EventV1` doesn't declare) surface correctly rather than getting
hidden behind top-level `event_list` agreement.

The 12 highest-signal findings are summarized in
[`FINDINGS_AGAINST_NPPS4.md`](FINDINGS_AGAINST_NPPS4.md).

## Tomorrow: regression mode

When the client APK updates (new schemas land), re-run the harness and
compare against the committed observations:

```bash
# Run both passes against the new client
make harness aggregate ui-handlers ui-classes merge
mv build/runtime_listener_observations.json /tmp/new_obs.json

# Restore the committed baseline (git checkout, or use a tag)
git checkout HEAD -- build/runtime_listener_observations.json

# Diff
uv run --no-project python integration/npps4/wire_compare.py --mode regression \
    --baseline build/runtime_listener_observations.json \
    --current /tmp/new_obs.json \
    --out /tmp/regression.md
```

Non-zero exit if any endpoint gained/lost fields. Wireable into CI as
a "schemas changed" tripwire.

## Eventually: live-probe (deferred)

PLAN Stage 3 gating decided not to ship live-probe in this iteration.
The reasons:

1. NPPS4 Docker stack needs Postgres + alembic migrations + data
   directory + signed user session bootstrap (`/login/login` first).
2. XMC signature bypass exists (`NPPS4_CONFIG_ADVANCED_VERIFY_XMC=false`)
   but a *valid* user session is still required for most endpoints,
   which means the harness needs to handle the multi-step signup +
   login flow before it can probe anything.
3. Per-state shape variation: NPPS4 returns different fields based on
   event window, user level, deck state. A single probe captures one
   branch only — the report would need state-tagging that makes its
   findings less actionable than the static-diff above.

If/when this is built, the design is sketched in `wire_compare.py`'s
`--mode live-probe` branch:
- POST request envelopes to `/main.php` for each `harness-covered`
  endpoint
- Capture wire response, diff response field names vs the listener-
  observed reads
- Handle both batched (`response_data: [{result, status}, ...]`) and
  single-command (`response_data: <ep_shape>`) envelope forms
- Tag every diff with "single-probe; field absence may be conditional
  on state we didn't enter"

Open a GH Discussion thread on NPPS4 when ready — the maintainers know
the auth/session machinery best and the conversation will be more
productive after the static-diff findings have landed.

## Verifying the integration locally

```bash
# Run full pipeline + static-diff. Verifies all step outputs exist
# and that endpoint discovery hasn't regressed below the floor.
make test
```

(See `Makefile` for the canonical pipeline. The full
`make compare-npps4` run — both harness passes + aggregation +
classification + NPPS4 priors + wire-compare — finishes in
**~12 seconds** on a laptop.)
