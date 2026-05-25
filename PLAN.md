# SIF1 client-exercise harness — design plan

A third-attempt project, after a v1 full-stack RE attempt (~12
sessions, 48% schema agreement with an LLM-generated reference) and a
v2 clean-slate rerun (~1 day, 80% agreement with NPPS4 on overlapping
endpoints via listener-driven discovery).

v2 revealed the real artifact worth building. **The listener harness is
the differentiated thing.** Everything else in v1 and v2 was either
supporting tooling or schema-generation downstream of the harness's
output. This project extracts the harness, pushes it toward its
coverage ceiling, and packages it for collaboration with NPPS4.

## The problem this addresses

NPPS4 (`github.com/DarkEnergyProcessor/NPPS4`) has implemented ~113 of the
358 SIF1 wire endpoints over years of work, with production-traffic
validation behind every field. The remaining ~245 are unimplemented not
because they're hard to write, but because **exercising the client to
find out what each endpoint's response shape needs to look like is
labor-intensive**. Each new endpoint takes a human at an emulator,
navigating to the right screen, triggering the action, and capturing the
wire response — repeated for every variant of state that affects the
shape.

**This harness automates the listener-layer slice of that exercise.** It
preloads the client's listener-registering files into luajit, fires
`Cachable.notifyUpdate(cache_key, spied_response)` for every endpoint,
and logs every field path the listener bodies destructure. No device, no
server, no human. v2 surfaced field-level observations for 166 of 358
endpoints in ~5 seconds of luajit runtime, from a cold start with no
synth-populated candidates.

The ceiling we know about from v2:
- **166 endpoints** with non-empty `runtime_discovered_field_names`
- **137 unique field paths** total
- **295 of 358 endpoints** produce traces with >2 keys (i.e. the listener
  fired and read at least one undeclared field)
- Plateau after one iteration of synth-populated candidates (170, then 166)

The remaining ~190 endpoints fall into three buckets:
- ~110 envelope-only acks (`*.cancel`, `*.leave`, `*.skip`, `*.set*`) —
  no listener fields to discover
- ~50 UI-handler-only endpoints (read in `m_*/view/*.lua` setup, not in a
  Cachable listener body) — extension target
- ~30 state-dependent endpoints (`arena.matching`, `login.topInfo`,
  `live.liveStatus`) — irreducible Frida / PCAP territory

This project pushes the listener-layer ceiling as high as it can go, and
provides the integration tooling NPPS4 needs to consume the output.

## What this deliberately is NOT

- **Not a server implementation.** NPPS4 is the canonical reference.
  Every schema we surface goes to them; we don't compete.
- **Not a FastAPI scaffold.** v1 and v2 both emitted `fastapi_app/` with
  Pydantic models. That's NPPS4's job. We emit data, they emit code.
- **Not a synth pass.** LLM-driven schema synthesis is in v2 and stays
  there. NPPS4 won't trust LLM-generated fields; they're right not to.
  This project's output is empirical only.
- **Not a comparison against API.md / sif_schemas-2.** Same reason as v2:
  LLM-on-LLM circularity. NPPS4 is the only verified reference, and
  comparison-against-NPPS4 is itself the deliverable here.
- **Not a UI emulator.** We can invoke handler entry points programmatically;
  we don't simulate touch events, screen lifecycles, or animations.
- **Not a Frida hook.** That's the complement, not the substitute. We
  hand the ~30 irreducible-Frida endpoints to NPPS4 and the user.

## Priors encoded (paid in v1 and v2 — don't relearn)

These were discovered the hard way. Bake them in from day 1.

1. **`Cachable.notifyUpdate` is the spine of listener-driven discovery.**
   Real per-cache_key listeners registered via `Cachable.addListener` are
   the single biggest field-discovery surface. The harness MUST port the
   real `common/cachable.lua` — not stub it.

2. **`setupUpdaters()` registers ~91 of ~120 listeners inside a function
   body** (`m_boot/initialize.lua`). Loading the file isn't enough — must
   `import("boot").initialize.setupUpdaters()` after preload.

3. **Permissive recursive sentinel for crash-resistance.** Listeners
   access nested fields that don't exist; the sentinel must absorb
   `__index/__call/__len/__pairs/__ipairs` plus the arithmetic and
   comparison metamethods so listener bodies run to completion past
   missing keys. v2 confirmed: `__add/__sub/__mul/__div/__mod/__unm/`
   `__concat/__eq/__lt/__le/__tostring` are all required.

4. **Variant-aware sentinel from day 1.** Plain sentinel gates listeners
   behind `if x.is_complete then ... read fields ... end`. Variants
   needed: `baseline`, `list_one` (phantom 1-element iteration),
   `true_bool`, `false_bool`. v2 confirmed `list_many` and `cmp_gt` add
   nothing after normalization — skip them.

5. **Synth-populated candidate iteration.** Cold start gave 86 endpoints
   in v2; one round of "synth shape → richer candidate → re-fire harness"
   pushed it to 170 (97% jump). Bake this iteration loop into the
   default pipeline, not as an optional retrofit.

6. **Naming bridge: wire-name vs file-name vs alt-fn-name.**
   `extract_apis.py` keys by the wire-level `module = "..."` literal
   (`lbonus`); `svapi.lbonus` is actually defined in
   `common/svapi/lBonus.lua` (file-cased); the public binding inside is
   `lBonusExecute` (file-cased fn). The harness dispatch MUST try all
   three names. v2 fix added `alt_fn_names` to extract_apis and a
   `svapi_file` fallback chain in `harness.lua`.

7. **Always-fire `notifyUpdate` post-dispatch.** Even when the svapi
   function dispatch fails (missing module, missing fn), explicitly call
   `Cachable.notifyUpdate(job.cache_key)` against a spied candidate.
   The listener still fires and surfaces field reads. v2 fix added this;
   gained ~10 endpoints of coverage.

8. **Decompiler quirks**: KLab uses `\x1bLuaR` magic for Lua 5.1
   bytecode; unluac 1.2.3.569 handles it natively. ~3 of 1987 files fail
   to decompile (one `start.old.lua`, two old-format) — accept the
   small loss.

9. **NPPS4 priors come from `RootModel[list[X]]` too.** Naive AST walking
   misses subscript bases. v2 fix: resolve `RootModel[list[ClassName]]`
   into synthetic `__root__` + `__root_item__.<field>` entries. Also
   fold in DLAPI's `n4dlapi/model.py` so `download.*` endpoints get
   their actual `DownloadInfo` element shapes.

10. **NPPS4's Python types lie about wire format more often than they
    don't.** 9 of 11 `RootModel[list[X]]` claims in NPPS4 are
    contradicted by listener evidence — the wire actually wraps the list
    under a named field. Use listener observations as the primary
    tiebreaker; treat NPPS4 type declarations as informative-only on
    container shape.

## Technical gotchas (master reference)

Inline in each stage too, but collected here so you can scan before
starting any stage and not get bitten by something already burned in
v1 or v2.

### Lua harness

- **`pcall` around every preload file body.** v2 had 14 of 50 preload
  files crash mid-body (`m_basicsettings/start.lua:152`,
  `m_class/model/competition_final.lua:2332 undefined label 'lbl_41'`,
  etc.). pcall-wrapping ensures listeners registered BEFORE the crash
  still count. Never let a single bad file abort the whole preload.
- **`m_class/model/competition_final.lua` is undecompilable.** unluac
  emits an `undefined label 'lbl_41'` error every time. Filter it out
  of the preload list, don't try to "fix" the decompiler.
- **Patch `_G.pairs` and `_G.ipairs` after preload, before dispatch.**
  LuaJIT's default `pairs`/`ipairs` don't honor `__pairs`/`__ipairs`
  metamethods on plain tables (only on a few stdlib types). The spy and
  sentinel rely on these metamethods firing. v2's `stubs.lua` patches
  both globals at the end of `boot()`.
- **`__variant__` must inherit through every child sentinel.** When a
  sentinel's `__index` returns a fresh child sentinel for a missing key,
  the child has to carry the same variant config. Otherwise `list_one`
  semantics evaporate two levels into the access chain. v2's
  `sentinel.lua` does this via `rawget(self, "__variant__")` + pass to
  `make_sentinel`.
- **`rawget(svapi, mod_name)` not `svapi[mod_name]`.** After
  `finalize_modules` stamps modules with a fall-through `__index`,
  `svapi[mod_name]` synthesizes a phantom sentinel and the dispatcher
  thinks it found a module. v2's `harness.lua` uses `rawget`
  everywhere on the svapi tree.
- **`m_boot/initialize.lua` is 2444 lines.** `setupUpdaters()` is a
  function defined late in the body and must be invoked via
  `import("boot").initialize.setupUpdaters()` AFTER preload completes.
  If `m_boot/initialize.lua` itself crashes mid-body, `setupUpdaters`
  may be undefined; check before calling.
- **`luajit` not `lua`.** SIF1's bytecode is Lua 5.1; LuaJIT is
  compatible. Stock `lua` (5.4 in most distros) won't load it. v2's
  `run_lua_harness.py` uses `pick_lua_runtime()` to detect and bail
  with a clear error.
- **Decompiled Lua strips ALL locals/params** to `L<n>_<depth>` /
  `A<n>_<depth>`. emmylua / lua-language-server cannot infer types
  from it. Don't waste time wiring up LSP tooling — only string
  constants and global references survive decompilation.

### Naming gaps (three distinct namespaces)

The svapi module name appears in three forms that don't always agree:

1. **Wire name**: the literal in `<table>.module = "lbonus"` inside
   the function body. This is what extract_apis keys by.
2. **File name**: `common/svapi/lBonus.lua`. The svapi module
   table is registered under this name (`svapi.lBonus`, not
   `svapi.lbonus`).
3. **Fn name on the module**: `L2_1.lBonusExecute = L5_1`. The public
   binding uses file-cased camelCase.

The harness must try all three. v2's fix:
- `extract_apis.py` emits `alt_fn_names` per endpoint
- `merged_endpoints.json` carries `svapi_file` (filename minus .lua)
- `harness.lua` dispatch falls back through `module → svapi_file → action`
  AND `fn_name → alt_fn_names[i] → action`

Worst offenders: `lbonus ↔ lBonus`, `klab ↔ klab_id`, `free ↔ freeLive`,
`download ↔ luadownload`, `effort ↔ effortPoint`, `platform ↔ platformAccount`,
`precise ↔ preciseScore`, `top ↔ login`. Eight modules total.

### NPPS4 priors

- **AST walker must handle `RootModel[list[X]]` subscript bases.**
  `pydantic.RootModel[list[X]]` is a Subscript, not a Name/Attribute.
  Naive `ast.unparse` on bases skips it. v2 fix: detect `Subscript` with
  value `RootModel` (Name or Attribute), unparse the slice, optionally
  resolve `list[ClassName]` into per-element fields.
- **DLAPI is a separate Python project.** A clone of
  `github.com/DarkEnergyProcessor/NPPS4-DLAPI` defines `DownloadInfo`,
  `DownloadUpdateInfo`, `ChecksumModel`. NPPS4 game/download.py
  references these by name. Must merge DLAPI's `n4dlapi/` into the
  global class index, or 11 download/album/profile endpoints resolve
  to empty `fields: {}`.
- **`@idol.register("mod", "act", batchable=False, check_version=False)`**:
  the decorator can carry kwargs that affect runtime behavior. We don't
  reason about them; just extract `(mod, act)` from positional args.
- **NPPS4's `XMCVerifyMode.CROSS` and friends** affect auth flow, not
  schema shape. Safe to ignore.

### Wire-compare (Stage 3) integration

- **NPPS4 uses Postgres + alembic.** Docker stack needs DB seeding;
  bare server doesn't accept most requests. Bootstrap a fresh user
  account via `/login/login` (start-up flow) before probing anything
  user-state-dependent.
- **KLab signs requests.** A header (`Auth-Header` or similar, computed
  in `libGame.so`) is required by NPPS4's auth middleware. For
  wire-compare, either run NPPS4 with auth disabled (`config.toml`:
  `disable_auth = true` if it has one) or compute a valid signature.
  Static analysis of `common/svapi/_util.lua` showed signing is
  Lua-side, not native — extractable in principle.
- **HTTP envelope is batched.** Outer body is
  `{"status_code": 200, "response_data": [{"result": <ep_shape>,
  "status": 200}, ...]}` for multi-command POSTs. Single-command POSTs
  unwrap to `{"status_code": 200, "response_data": <ep_shape>}`. Both
  forms appear in the wire; wire-compare must handle both.
- **Per-state shape variation.** NPPS4 returns different fields for the
  same endpoint depending on user state. A single live-probe captures
  one branch only — flag the limitation in the report; don't claim a
  field is "missing" if our probe just didn't enter the branch that
  emits it.

### Iteration / fixpoint loops

- **`notifyUpdate` fixpoint can loop.** Listeners can `Cachable.set`
  other keys, which we'd want to also notify. Bound iteration depth
  (v2 didn't need this, but Stage 1 Approach C will).
- **Cross-endpoint state seeding has circular deps.** `liveSchedule` and
  `unitAll` may both reference each other transitively. Topo-sort with
  cycle detection; fall back to "fire each twice" on cycles.

### Pydantic v2 / FastAPI emission (only if we backslide into emitting models)

- **`__root__` is reserved by Pydantic v2.** Can't be a field name.
  v2 fix: gen_models.py skips it at emission. We're explicitly NOT
  emitting models in this project, but if a script does, remember this.

### Decryption

- **`honkypy` is not on PyPI.** Obtain it from a SIF1 RE community
  fork and expose it via `HONKYPY_PATH` so `decrypt_all.py` can
  `sys.path.insert(0, ...)` to load it. Don't try `pip install
  honkypy` — it isn't there.
- **`*.db_` suffix** (note the underscore) on encrypted SQLite DBs.
  Decrypted form has `.db` suffix and `SQLite format 3` magic.

## Stage layout (~3-4 days of focused work)

### Stage 0 — Bootstrap & harness extraction (0.5 day)

```
mkdir sif1-harness && cd sif1-harness
git init  # GitHub-public from day 1
mkdir -p assets/{decrypted,decompiled} src/{harness,tools,scripts} \
         tests build/runtime priors integration/npps4
```

Lift from the v2 source tree:
- `scripts/lua_harness/*` → `src/harness/*` (all 8 files)
- `scripts/decrypt_all.py` + `decompile_all.py` → `src/scripts/`
- `scripts/extract_apis.py` + `_lua_spans.py` → `src/scripts/`
- `scripts/run_lua_harness.py` + `run_lua_harness_fuzz.py` →
  `src/tools/`
- `scripts/aggregate_listener_observations.py` +
  `aggregate_fuzz_observations.py` → `src/tools/`

Don't copy v2's `fastapi_app/`, `fix_pattern_c.py`, `gen_models.py`,
`prepare_synth_bundles*.py`, `promote_types.py`, `infer_types.py` —
those are the schema-emission layer, out of scope here.

**Stop**: `python src/tools/run_lua_harness.py --all` produces 358
trace files in <1s. `python src/tools/aggregate_listener_observations.py`
emits the baseline observations file. Numbers match v2's iter-1 cold
start (~86 endpoints with discoveries).

### Stage 1 — Harness coverage push (1-1.5 days)

The goal: get from v2's 166/358 plateau to as close to 250/358 as
static analysis allows. Three parallel approaches, each independent.

| Approach | Target | Mechanism | Expected gain |
|---|---|---|---|
| **A. UI handler invocation** | endpoints whose response is unpacked in `m_*/start.lua` or `m_*/view/*.lua`, not Cachable | Build a per-endpoint candidate-handler list from grep of `<fn_name>(` callsites in `m_*/`. Invoke each with sentinel args; success_cb path runs against spied envelope | +30-50 endpoints |
| **B. Cross-endpoint state seeding** | endpoints that read another endpoint's cache (`cached.live_schedule = Cachable.get('$liveSchedule'); for _,v in ipairs(cached.live_schedule.list) do ...`) | Topologically order endpoints; pre-populate each cache_key from prior endpoint's spied response before firing the next | +10-20 endpoints |
| **C. Multi-step listener chains** | endpoints whose listener triggers another listener (`notifyUpdate(A)` → listener calls `Cachable.set(B)`) | Iterate notifyUpdate firings until fixpoint, not just one pass | +5-10 endpoints |

v1's "Tier 2 Approach B" was the precursor to (A); it added 14
endpoints but stalled. Pick up that thread.

**Risk budget for Approach A.** v1's prior attempt stalled at +14. This
plan asserts +30-50, which is a bet, not a measurement. **Pilot first,
re-estimate before committing the full stage**:

- Pick 5 endpoints known to be UI-handler-only (good candidates:
  `reward.rewardHistory`, `stamp.stampInfo`, `album.albumAll`,
  `friend.requestList`, `notice.index`). Find their UI handler entry
  points by grep, manually wire a one-off invocation, see how many
  field-reads land.
- **If pilot yields ≥3 of 5 endpoints**: proceed with the full
  approach; estimate holds.
- **If pilot yields ≤2 of 5**: drop Approach A to a follow-on; ship
  Stage 2+3 with current coverage. The harness still has value at
  166-180 endpoints — just don't burn the day chasing diminishing
  returns. Document the failure in `docs/COVERAGE_CEILING.md` so the
  next attempt knows.

The gotcha that probably bit v1: UI handlers usually expect a
`UIController` or `Scene` argument that the harness has no stub for.
Calls like `screen:addChild(view)` will crash unless `screen` is a
permissive sentinel — which means stubs.lua needs a few more module
stubs. Specifically, sniff for the top globals these handlers expect
(`Scene`, `UI`, `klb_*`, `game.*`) and add minimal sentinel-backed
modules to the stub set.

**Stop**: ≥220 endpoints with `runtime_discovered_field_names`.
≥200 unique field paths. 0 sentinel-coverage crashes. **OR** pilot
verdict says ship at ~180 — both are valid Stage 1 outcomes.

### Stage 2 — Per-endpoint coverage classification (0.5 day)

Sort all 358 endpoints into four buckets, output as
`build/coverage_classification.json`:

| Bucket | Definition | Action |
|---|---|---|
| **harness-covered** | Has ≥1 listener-discovered field path | Schema-correctable via harness output |
| **envelope-only** | No listener fires; URL/method/cache_key extracted only | `extra="allow"` stub is correct |
| **ui-only** | Listener body present but reads no fields; UI handler reads them | Tier 2 Approach A candidate |
| **needs-Frida** | No listener body OR all listeners are envelope-acks AND no UI handler reads the response | Hand to NPPS4 with a "this needs wire capture" tag |

This classification is the deliverable for the NPPS4 conversation:
"here are the 248 endpoints the harness can fully cover, here are the 80
that need your manual play." Gives them a finite, prioritized list
instead of "everything we haven't done".

### Stage 3 — NPPS4 wire-compare integration (1 day)

The differentiator. Tools that consume NPPS4's actual code, not just
its Pydantic types, and produce concrete actionable diffs.

**Half-day-zero gating step: verify NPPS4 Docker actually stands up.**
`live-probe` is the centerpiece of this stage and the demo. But NPPS4
needs Postgres + alembic migrations + auth keypair + a `server_data.json`
fixture, and the auth middleware checks a KLab-signed header that
`libGame.so` computes natively. **Before committing to `--mode live-probe`
as the primary deliverable**, spend ≤2 hours trying to:

1. `git clone` NPPS4 and follow their `docker-compose.example.yml`
2. Verify the container comes up and `/api/publicinfo` (or any DLAPI
   endpoint) responds
3. Find an auth bypass (config flag, dev mode) or compute a valid
   signature from the Lua-side signing code in `common/svapi/_util.lua`
4. Successfully POST a known-good request envelope to `/main.php` and
   get a 200 back

**Decision point at the end of those 2 hours**:
- **Docker works + auth solvable**: full Stage 3 as written, all three
  modes ship.
- **Docker works but auth is intractable in this timebox**: ship
  `--mode static-diff` + `--mode regression`. Mention live-probe as
  a future companion in the README; let NPPS4 maintainers help with
  auth-bypass as part of the conversation. Static-diff alone is still
  a concrete demo — v2 already produced 33 actionable findings that
  way.
- **Docker setup is itself broken on the current NPPS4 main**: open
  an issue on NPPS4 noting the breakage (becomes a low-stakes first
  contact); ship Stage 3 with `--mode static-diff` only.

Never let live-probe yak-shaving consume more than half a day. Static
diff is good enough for the demo, and getting the rest of the project
shipped is more important than the perfect comparison.

#### `priors/extract_npps4_priors.py` (port from v2 with fixes)
- AST-walk `npps4/` + `n4dlapi/` for `@idol.register(mod, action)`
- Resolve `RootModel[list[X]]` subscripts (Subscript bases — see
  Technical Gotchas)
- Fold in mixin base-class fields
- Output: `priors/npps4_endpoints.json` keyed by `mod.action`

#### `integration/npps4/wire_compare.py` (NEW)
Three modes, runnable independently. **Implementation order: static-diff
first (no infra deps), then regression (depends only on static-diff +
prior harness run), then live-probe LAST (depends on NPPS4 Docker).**

1. **`--mode static-diff`** ← always ships: compares NPPS4 Pydantic field
   names against our harness-discovered field names. Output:
   per-endpoint markdown "client reads {a, b, c} that NPPS4 doesn't
   emit / NPPS4 emits {d, e} that client never reads". This is the cheap
   demo for the GH Discussion. v2 already validated this mode produces
   useful output.

2. **`--mode regression`** ← always ships: re-runs the harness,
   re-aggregates, compares against the previously committed
   `runtime_listener_observations.json`. Emits a "schemas changed since
   last check" report. This is what NPPS4 uses on every client APK
   update. No external infra needed beyond the harness itself.

3. **`--mode live-probe`** ← ships IFF Docker gating succeeded: stands up
   NPPS4 locally, POSTs request envelopes to each endpoint's `/main.php`
   route, captures the actual wire response, diffs the response field
   names against the harness's observed reads. **Stronger evidence
   than static-diff** because it shows what NPPS4 *actually* serves,
   not just what its types claim — but it's the most fragile mode.

   Gotchas if you do ship it:
   - **Per-state shape variation**: NPPS4 emits different fields based
     on user state. A single probe captures one branch only. Tag every
     diff finding with "single-probe; field absence may be conditional
     on state we didn't enter".
   - **Batched vs single command**: the outer envelope is `{"status_code",
     "response_data": [{"result", "status"}, ...]}` for multi-command
     POSTs, `{"status_code", "response_data": <ep_shape>}` for singles.
     wire-compare must handle both forms.
   - **User bootstrap**: most endpoints require a logged-in user.
     Run `/login/login` start-up flow once to mint an account, persist
     the session token, reuse across probes.

#### `integration/npps4/report.py`
Renders the live-probe diff as a single markdown report with:
- One section per affected endpoint
- Source citations: `source/all/<file>.lua:line` for client reads,
  `npps4/<module>.py:class` for server emits
- Verdict per discrepancy: "client expects field, NPPS4 missing" /
  "NPPS4 emits field, client ignores" / "type mismatch with tiebreaker
  evidence"

**Stop**: `python integration/npps4/wire_compare.py --mode live-probe
--out report.md` produces a clean markdown diff against a running NPPS4
docker container. Includes ≥5 concrete actionable findings.

### Stage 4 — Packaging & outreach prep (0.5 day)

- `README.md` lead-in: "Automated listener-layer client exercise for
  SIF1 schema discovery. Complements NPPS4."
- `docs/HOW_HARNESS_WORKS.md`: one-page technical explanation, with the
  `Cachable.notifyUpdate` flow diagram
- `docs/NPPS4_INTEGRATION.md`: walkthrough of running wire-compare
  against a local NPPS4 instance, sample output
- `docs/COVERAGE_CEILING.md`: empirical analysis of why the harness
  can't reach the ~80 needs-Frida endpoints, with citations
- `docs/FINDINGS_AGAINST_NPPS4.md`: the demo. 5-10 concrete schema
  findings, each with: endpoint, our evidence (listener trace +
  decompiled Lua), NPPS4's current schema, recommended change
- GitHub Actions: run harness on every push, fail if coverage drops
- Tag `v0.1.0` when ready

**Stop**: A non-author can clone the repo, run `make test`, see the
harness pass, and run `make compare-npps4` to reproduce the demo
findings. This is the outreach moment.

### Stage 5 — Outreach (0.5 day, gated on Stage 4)

Open a GitHub Discussion on NPPS4:
- Title: "Automated listener-harness for JP v9.11 client + ~N concrete
  schema findings — interested in upstream collaboration?"
- Lead with the 5-10 findings from `docs/FINDINGS_AGAINST_NPPS4.md`
- Frame as "harness handles the cheap 80%, your manual play handles the
  irreducible 20% — want to use it together?"
- Offer the regression mode as the long-term value
- Acknowledge limits: static + sentinel-driven, no UI simulation, no
  wire capture (yet)

Wait for response. Do not open PRs unsolicited. Iterate on the
integration mode based on their feedback.

## What success looks like

| Metric | Target | v2 baseline |
|---|---:|---:|
| Harness-covered endpoints | ≥220 / 358 | 166 / 358 |
| Unique field paths | ≥200 | 137 |
| Coverage classification | 358 / 358 (every endpoint in one bucket) | not done |
| NPPS4 wire-compare findings | ≥10 actionable | 33 (but only static, not wire-verified) |
| Regression-mode runtime | ≤30s end-to-end | n/a |
| NPPS4 maintainer engagement | 1 response to GH Discussion | n/a |

## What we're NOT solving

These remain genuinely hard, out of scope, and explicitly handed back
to either NPPS4 or a future Frida companion:

- **The ~30 irreducible-Frida endpoints**: login.topInfo, live.liveStatus,
  arena.matching, duel.privateMakeMatch, online.play, reward.rewardHistory,
  stamp.stampInfo, album.albumAll, friend.requestList, etc. Listeners
  don't read fields on these; harness can't surface them.
- **First-time-account state**: `/login/login` defaults, `/user/userInfo`
  freshly-created shape. Only available via real signup capture.
- **Per-state shape variation**: many endpoints return different fields
  based on event window, user level, deck state. Single harness probe
  shows one branch; manual play shows all of them.
- **NPPS4 server logic gaps**: we tell them schemas are wrong, not how to
  implement the right behavior. State machines (matchmaking, ranking,
  event timer) stay theirs.
- **Wire format vs Pydantic type disagreements** that the harness can't
  resolve definitively. Some come down to "we saw the listener read X,
  NPPS4 says X is type Y, but Y wouldn't normally be readable as the
  listener does it". Flagged as "needs PCAP".

## Where to look when stuck

All of the v1 / v2 hard-won implementations now live in this repo —
the table below points at the in-tree files.

| What you need | Look at |
|---|---|
| Full engine stub list | `src/harness/stubs.lua` |
| Real Cachable port | `src/harness/cachable.lua` |
| Permissive sentinel + variants | `src/harness/sentinel.lua`, `src/harness/sentinel_variants.lua` |
| `alt_fn_names` + `svapi_file` naming bridge | `src/scripts/extract_apis.py` + `src/harness/harness.lua` |
| Always-fire-notifyUpdate fix | `src/harness/harness.lua` (post-dispatch block) |
| Synth-iteration loop | folded into `build/synthesized_types.json`; gain in v2 was 86→170 endpoints |
| RootModel resolution | `priors/extract_npps4_priors.py` |
| Coverage analysis methodology | `build/coverage_classification.{json,md}` + `src/tools/classify_coverage.py` |
| Approach B handler invocation (Session 10 ship) | `src/harness/harness.lua::dispatch_invoke_classes`, `src/tools/run_invoke_classes.py`, `src/tools/find_ui_handlers.py` |

## Stop criteria for the whole project

- ≥220 endpoints with non-empty `runtime_discovered_field_names`
- 358 / 358 endpoints classified into one of {harness-covered,
  envelope-only, ui-only, needs-Frida}
- `wire-compare --mode live-probe` produces a clean markdown report
  against a running NPPS4 instance
- `docs/FINDINGS_AGAINST_NPPS4.md` has ≥10 concrete findings with
  source citations
- One GitHub Discussion opened on NPPS4 with the findings
- README/docs let a stranger reproduce the demo in <15 minutes

## Then what?

Two follow-ons, gated on NPPS4 response:

1. **If they engage**: build the PR-friendly integration. Per-endpoint
   schema-correction PRs in their style, harness-as-CI for their repo,
   joint roadmap for the ui-only bucket extension.

2. **Tier 3 Frida companion** (parallel project, not this one):
   Android emulator + Frida hook on the HTTP layer + a play script that
   exercises the ~30 irreducible endpoints. Output: wire-capture JSONs
   that close the harness's blind spots. Worth doing only after this
   harness ships and we know which endpoints actually need it.

---

## Open decision: project name

Current dir is `sif1-harness`. Alternative names:
- `sif1-listener-probe` (most accurate, jargony)
- `sif1-client-exercise` (captures bottleneck angle)
- `sif1-schema-discovery` (broad, audience-friendly)
- `npps4-harness` (signals intent to integrate; may overstep)

`sif1-harness` is the working name. Rename before push if a better one
lands.
