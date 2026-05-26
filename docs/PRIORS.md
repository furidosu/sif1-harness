# Priors and technical gotchas

Hard-won knowledge from v1 and v2 rebuilds, plus the per-area gotchas
that bit those attempts. Read before touching any part of the harness
or the wire-compare pipeline — most of these took a session or more to
discover the first time.

## Priors encoded (paid in v1 and v2 — don't relearn)

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

Scan before starting any non-trivial change so you don't get bitten by
something already burned in v1 or v2.

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

### Wire-compare integration

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
  other keys, which we'd want to also notify. Bound iteration depth.
- **Cross-endpoint state seeding has circular deps.** `liveSchedule` and
  `unitAll` may both reference each other transitively. Topo-sort with
  cycle detection; fall back to "fire each twice" on cycles.

### Pydantic v2 / FastAPI emission (only if a script backslides into emitting models)

- **`__root__` is reserved by Pydantic v2.** Can't be a field name.
  Any model-emission script must skip it. This project explicitly does
  NOT emit Pydantic models — that's NPPS4's job — but the constraint is
  here so the rule survives any future regression.

### Decryption

- **`honkypy` is not on PyPI.** Obtain it from a SIF1 RE community
  fork and expose it via `HONKYPY_PATH` so `decrypt_all.py` can
  `sys.path.insert(0, ...)` to load it. Don't try `pip install
  honkypy` — it isn't there.
- **`*.db_` suffix** (note the underscore) on encrypted SQLite DBs.
  Decrypted form has `.db` suffix and `SQLite format 3` magic.

## Where to look when stuck

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
| Approach B handler invocation | `src/harness/harness.lua::dispatch_invoke_classes`, `src/tools/run_invoke_classes.py`, `src/tools/find_ui_handlers.py` |
| Static field-extraction (closure + listener + Cachable.get anchors, with return-value taint and fixpoint wrapper classification) | `src/tools/extract_ui_field_reads.py` |
