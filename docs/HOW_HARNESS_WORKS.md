# How the harness works

A technical explanation of the two discovery passes the harness runs
back-to-back: the `Cachable.notifyUpdate` listener pass and the
`invoke_classes` UI-handler pass.

## The spine: `Cachable`

SIF1 caches every endpoint response under a per-endpoint **cache key**
like `$userInfo`, `$liveSchedule`, `$rewardList`. Anywhere the rest of
the client code wants a response, it calls
`Cachable.get(cache_key)` — and crucially, it can register a listener
via `Cachable.addListener(cache_key, callback)` that runs whenever the
response is updated.

The listeners are where the field-reads live. A listener body
typically looks like:

```lua
Cachable.addListener("$liveSchedule", function(cached)
  for _, ev in ipairs(cached.event_list) do
    if ev.event_id and ev.event_status_code == 2 then
      local title = ev.title  -- !!! reads `event_list[].title`
      ...
    end
  end
end)
```

Every dereference on `cached.event_list[].title` is signal: it tells us
the response shape has at least these fields. **No live wire capture
needed** — the listener bodies are static client code, and we can read
the field accesses by exercising them.

## The flow

```
┌──────────────────────────────────────────────────────────────────┐
│ PRELOAD (once per harness process)                               │
│ 1a. Load all common/svapi/*.lua dispatch files.                  │
│ 1b. Load m_boot/initialize.lua + ~30 model files + ~17           │
│     bulkSend-calling files. Run setupUpdaters() to register the  │
│     91 listeners inside its body. Each file is pcall-wrapped.    │
│     Result: ~120 listeners registered against ~100 cache_keys.   │
│ 1c. finalize_modules() — stamp every module table with a         │
│     sentinel-on-miss __index so chained access on undefined      │
│     helpers degrades gracefully.                                 │
│ 1d. Load 355 UI handler files AFTER finalize_modules (so they    │
│     benefit from sentinel-on-miss on engine imports). Retry any  │
│     first-pass file that crashed. Re-invoke setupUpdaters() in   │
│     case the second pass landed new dependencies. Result: ~360   │
│     classes registered via define("ClassName", M).               │
├──────────────────────────────────────────────────────────────────┤
│ PASS 1 — notifyUpdate (every endpoint)                           │
│ 2. For each of 358 endpoints:                                    │
│    a. Build a candidate response from the synth/scraper schema.  │
│    b. Wrap response in our recursive permissive sentinel, which  │
│       absorbs __index/__call/__pairs/arithmetic/comparison.      │
│    c. Try svapi.<module>.<fn>(candidate, ...) — and unconditionally│
│       fire Cachable.notifyUpdate(cache_key, candidate) which runs│
│       every registered listener for that cache_key.              │
│    d. The spy logs every getattr/getindex the listener does to   │
│       build/runtime/traces/<endpoint>.json.                      │
├──────────────────────────────────────────────────────────────────┤
│ PASS 2 — invoke_classes (ui-only bucket only)                    │
│ 3a. classify_coverage.py bins endpoints; pick the ui-only bucket │
│     (initially ~115 endpoints).                                  │
│ 3b. find_ui_handlers.py greps each ui-only endpoint's            │
│     cache_key/fn_name across m_*/, extracts the ClassName from   │
│     each candidate file's define(name, M) call, builds           │
│     build/ui_handler_map.json.                                   │
│ 3c. For each ui-only endpoint:                                   │
│      i. Cachable.set(cache_key, spied_candidate)                 │
│     ii. Fire notifyUpdate once with the populated cache.         │
│    iii. For each class in the endpoint's candidate_files (capped │
│         at 30), iterate the class's real-exported methods (next),│
│         skip likely-loop methods (update/tick/render/poll/loop), │
│         pcall each with sentinel args. debug.sethook caps each   │
│         method at 100k Lua instructions so infinite loops die.   │
│     iv. Write build/runtime/traces_classes/<endpoint>.json.      │
├──────────────────────────────────────────────────────────────────┤
│ MERGE & CLASSIFY                                                 │
│ 4a. merge_observations.py unions accessed_keys from traces/ AND  │
│     traces_classes/. Filters bulksend.[N].* artifacts and        │
│     sentinel-runaway chains (depth > 8) so phantom iterations    │
│     don't pollute the schema.                                    │
│ 4b. classify_coverage.py re-bins endpoints with the merged       │
│     discovery counts. ui-only bucket shrinks; harness-covered    │
│     grows.                                                       │
└──────────────────────────────────────────────────────────────────┘
```

## The hard parts (paid in v1, v2, and v3)

1. **The permissive recursive sentinel.** Listeners traverse nested
   fields that don't exist in our candidate response; the sentinel has
   to absorb `__index/__call/__len/__pairs/__ipairs` plus the
   arithmetic and comparison metamethods so listener bodies run to
   completion past missing keys. See `src/harness/sentinel.lua`.

2. **`setupUpdaters()` defines listeners inside a function body.**
   Loading `m_boot/initialize.lua` is not enough — must invoke
   `import("boot").initialize.setupUpdaters()` after preload to get the
   91 most important listeners actually registered. See `preload.lua`
   line 211.

3. **`pcall` around every preload file body.** 14 of 50 preload files
   crash mid-body in v2 (undefined labels, missing locals, decompiler
   artifacts). Without pcall the whole preload aborts; with pcall the
   listeners registered *before* the crash still count.

4. **Three distinct namespaces for the svapi module.** `extract_apis.py`
   keys by the wire-level `module = "..."` string ("lbonus"), but the
   svapi module table is registered under the file name ("lBonus"), and
   the public function inside is also file-cased ("lBonusExecute"). The
   harness dispatcher tries all three. Worst offenders: `lbonus ↔ lBonus`,
   `klab ↔ klab_id`, `free ↔ freeLive`, `download ↔ luadownload`,
   `effort ↔ effortPoint`, `platform ↔ platformAccount`,
   `precise ↔ preciseScore`, `top ↔ login`.

5. **Always fire `notifyUpdate` post-dispatch.** Even when the svapi
   fn call fails (missing module/fn or crashes in body), the harness
   explicitly calls `Cachable.notifyUpdate(cache_key, candidate)`
   afterwards. The listener still fires and surfaces field reads.
   Gained ~10 endpoints in v2.

6. **`LuaJIT`, not `lua`.** SIF1's bytecode is Lua 5.1; LuaJIT is
   compatible. Stock `lua` (5.4 in most distros) won't load it.
   `run_lua_harness.py::pick_lua_runtime` detects and bails.

7. **Patch `_G.pairs` and `_G.ipairs` after preload, before dispatch.**
   LuaJIT's stock pairs/ipairs don't honor `__pairs`/`__ipairs`
   metamethods on plain tables. The sentinel relies on them firing.
   `stubs.lua` patches both globals at the end of boot.

8. **Load UI handler files AFTER `finalize_modules()`.** UI files
   routinely do `const.SOME_FIELD` / `dbapi.foo` at module body.
   During the first preload pass, `import("const")` returns an empty
   `{}`, so `const.SOME_FIELD` is nil and crashes the body before
   `define()` fires. The fix in v3: load UI files in a SECOND pass
   after `finalize_modules` stamps sentinel-on-miss semantics — engine
   imports now degrade to sentinels and the body completes. UI file
   load success: 24/355 → 331/355.

9. **Bound `invoke_classes` runtime with `debug.sethook`.** Many UI
   handler methods enter loops on sentinel args (e.g. iterate a
   sentinel that pretends to be a list). Wrap each method invocation
   in a hook that errors after 100k Lua instructions. Without this,
   one bad endpoint hangs the worker indefinitely.

10. **Filter bulksend.[N].* attribution.** invoke_classes may invoke a
    method that triggers svapi.bulkSend; the spy logs paths like
    `bulksend.[1].response_data.[8174].is_last` (sentinel-iterated
    8000+ phantom entries). These are dispatching-endpoint
    cross-contamination and would falsely classify other endpoints as
    harness-covered. `merge_observations.py` drops them.

## What still doesn't get covered

See [`COVERAGE_CEILING.md`](COVERAGE_CEILING.md) for empirical
analysis of why 5 endpoints remain in the `needs-Frida` bucket: their
shape only exists in wire capture against a live server with specific
user state (KLab ID sync / handover / platform-account state probes).

The residual 28 `ui-only` endpoints didn't lift because their
candidate class either didn't register (file crashed even on second
pass) or the class had no method that reached the populated cache.
These are tractable with more per-endpoint hand-wiring — open question
for the NPPS4 collaboration whether that's worth the additional time
investment.
