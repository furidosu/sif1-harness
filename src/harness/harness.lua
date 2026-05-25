-- harness.lua
-- V5 persistent worker. Boots the engine stubs + the real Cachable
-- port + runs preload.lua (loads m_boot/initialize.lua and the ~18
-- model files that register listeners) ONCE, then loops on stdin
-- reading one JSON job per line and writing one JSON trace per line.
--
-- Why persistent: preload involves loading ~2700 lines of decompiled
-- Lua (initialize.lua alone is 2444). Restarting the VM per endpoint
-- (the V1 model) would multiply that cost by 358. With persistence
-- the whole batch runs in O(seconds) instead of O(minutes).
--
-- Usage:
--   luajit harness.lua <source_root>
-- Sends a startup marker to stdout, then enters the loop:
--   <-- stdin:   one JSON job per line
--   --> stdout:  one JSON trace per line
-- Send EOF to terminate.

local script_dir = arg[0]:match("(.*/)") or "./"
package.path = script_dir .. "?.lua;" .. package.path

local stubs = require("stubs")
local json = require("json_min")
local preload = require("preload")
local sv = require("sentinel_variants")

local source_root = arg[1]
if not source_root or source_root == "" then
  io.stderr:write("harness.lua: missing source_root argument\n")
  os.exit(2)
end

-- ---- boot ------------------------------------------------------------------
-- One-shot setup: install globals, default modules (svapi/http/Cachable),
-- run preload to register listeners.
stubs.boot()
stubs.install_globals(_G)

-- All svapi/*.lua modules are eagerly loaded in preload.run before
-- finalize_modules stamps modules with sentinel-on-miss semantics.
-- Per-endpoint dispatch just looks up svapi[module][fn]; no lazy load.

-- Preload listener-registering files. Warnings go to stderr but never
-- abort the harness (a single listener file failing is degraded but
-- recoverable -- the other 17 still register).
local preload_report = preload.run(source_root, function(msg)
  io.stderr:write(msg .. "\n")
end)
-- Module tables stop being nil-on-unset at this point: listeners now
-- get permissive sentinels for un-defined helpers so they can run
-- past missing engine modules and reach actual field reads.
stubs.finalize_modules()

-- Session 10 Approach B: load UI handler files AFTER finalize_modules
-- so engine imports they dereference (`const`, `dbapi`, ...) get
-- sentinel-on-miss semantics. Each file's body crashes are pcall-absorbed;
-- the goal is to get as many define("Foo", M) calls to fire as possible
-- so that invoke_classes can find the registered classes.
local ui_report = preload.run_ui_handlers(source_root, function(_msg) end, preload_report)

io.stderr:write(string.format(
  "preload: svapi=%d/%d models=%d/%d ui=%d/%d listeners=%d->%d\n",
  preload_report.svapi_loaded,
  preload_report.svapi_loaded + preload_report.svapi_failed,
  preload_report.loaded,
  preload_report.loaded + preload_report.failed,
  ui_report.loaded,
  ui_report.loaded + ui_report.failed,
  preload_report.listeners_before, preload_report.listeners_after))

-- Emit startup marker so the Python driver can detect a healthy boot.
io.stdout:write(json.encode({
  __startup = true,
  preload_loaded = preload_report.loaded,
  preload_failed = preload_report.failed,
  preload_files = preload_report.files,
  svapi_loaded = preload_report.svapi_loaded,
  svapi_failed = preload_report.svapi_failed,
  svapi_files = preload_report.svapi_files,
  ui_handlers_loaded = ui_report.loaded,
  ui_handlers_failed = ui_report.failed,
  listeners_registered = preload_report.listeners_after,
}) .. "\n")
io.stdout:flush()

-- ---- per-job dispatch ------------------------------------------------------

local function schema_walk(spied, schema, prefix, depth)
  if type(spied) ~= "table" then return end
  if type(schema) ~= "table" then return end
  depth = depth or 0
  if depth > 64 then return end
  if schema.fields then
    for k, sub in pairs(schema.fields) do
      local nested = spied[k]
      if type(sub) == "table" and sub.fields then
        if type(nested) == "table" then
          schema_walk(nested, sub, (prefix or "") .. "." .. k, depth + 1)
        end
      elseif type(sub) == "table" and sub.type == "list" and sub.element then
        if type(nested) == "table" then
          local first = nested[1]
          if type(first) == "table" then
            schema_walk(first, sub.element,
              (prefix or "") .. "." .. k .. ".[1]", depth + 1)
          end
        end
      end
    end
  end
end

-- Session 9 Tier 2 Approach B: dispatch a handler_invoke job. Calls
-- M.modules[<module_table>][<fn_name>] with phantom args; any
-- svapi.bulkSend / bulkSendSysloadable encountered inside fires the
-- success_cb against a spied envelope (see stubs.lua). Returns the ctx
-- with the per-bulkSend batch -> endpoint mapping so the aggregator
-- can attribute discovered field paths back to specific endpoints.
local function dispatch_handler_invoke(job)
  local variant_cfg = sv.get(job.variant)
  -- Empty candidate so http.send-style fallbacks (svapi handlers that
  -- bypass bulkSend and instead call http.send for a single endpoint)
  -- still spy a real-but-empty envelope, where all reads degrade to
  -- sentinels.
  local ctx = {
    module_table = job.module_table,
    fn_name = job.fn_name,
    candidate_response = {response_data = {}, status_code = 200},
    accessed_keys = {},
    errors = {},
    send_call_count = 0,
    variant = variant_cfg,
    variant_name = variant_cfg.name,
    bulksend_batch = nil,
    bulksend_call_count = 0,
  }
  stubs.reset(ctx)
  _G._HARNESS_CTX = ctx

  local mod_tbl = stubs.modules[job.module_table]
  if type(mod_tbl) ~= "table" then
    ctx.errors[#ctx.errors + 1] =
      "module table not found: " .. tostring(job.module_table)
    _G._HARNESS_CTX = nil
    return ctx, "handler_invoke"
  end
  local fn = rawget(mod_tbl, job.fn_name)
  if type(fn) ~= "function" then
    ctx.errors[#ctx.errors + 1] =
      "fn not found on " .. job.module_table .. ": " .. tostring(job.fn_name)
    _G._HARNESS_CTX = nil
    return ctx, "handler_invoke"
  end

  -- Phantom args. Handler functions in this codebase take 0-3 args
  -- (usually a context table or nil). Pass a permissive sentinel that
  -- absorbs every read so the handler can run past undeclared args.
  local args = {}
  local arity = tonumber(job.arity) or 1
  for i = 1, arity do
    args[i] = stubs.permissive()
  end
  local ok, err = pcall(function() return fn(unpack(args)) end)
  if not ok then
    ctx.errors[#ctx.errors + 1] =
      "handler error in " .. job.module_table .. "." .. job.fn_name ..
      ": " .. tostring(err)
  end

  _G._HARNESS_CTX = nil
  return ctx, "handler_invoke"
end

-- Reflects the post-preload module registry back to the Python driver:
-- {module_name: [function_member_names...]}. Used by the handler-invoke
-- driver to know what to call without re-scanning Lua source.
local function dispatch_list_modules(job)
  local out = {}
  for name, mod in pairs(stubs.modules) do
    if type(mod) == "table" then
      local fns = {}
      -- Use rawpairs equivalent via next() so finalize_modules' sentinel
      -- fallthrough metatable doesn't fabricate phantom entries.
      local k = next(mod)
      while k ~= nil do
        local v = rawget(mod, k)
        if type(v) == "function" and type(k) == "string" then
          fns[#fns + 1] = k
        end
        k = next(mod, k)
      end
      if #fns > 0 then
        table.sort(fns)
        out[name] = fns
      end
    end
  end
  return out
end

-- Session 9 Tier 2 Approach B-full: invoke an `svapi.<module>.<action>Stub`
-- builder directly, then call the returned descriptor's `on_success` with a
-- sentinel-backed envelope. This exposes the per-endpoint on_success closure
-- without needing to discover and invoke an outer module function -- the
-- coverage cap that bounded Approach B-cheap. Path attribution is via a
-- per-job "ep:<module>.<action>" prefix on the envelope spy, so every read
-- traceable to this Stub is unambiguously attributed.
local function dispatch_invoke_stub(job)
  local variant_cfg = sv.get(job.variant)
  local ctx = {
    module = job.module,
    action = job.action,
    accessed_keys = {},
    errors = {},
    variant = variant_cfg,
    variant_name = variant_cfg.name,
    cache_keys_set = {},
    cache_keys_set_seen = {},
    candidate_response = {response_data = {}, status_code = 200},
    send_call_count = 0,
  }
  stubs.reset(ctx)
  _G._HARNESS_CTX = ctx

  local svapi = stubs.modules.svapi
  -- The wire module name (job.module = "login") may not match the svapi-file
  -- module name (svapi.top hosts login endpoints). Build a fallback list of
  -- candidate svapi tables and pick the first one that exposes <action>Stub.
  local stub_fn_name = job.action .. "Stub"
  local function find_stub()
    local candidates = {job.module}
    if job.svapi_file and job.svapi_file ~= job.module then
      candidates[#candidates + 1] = job.svapi_file
    end
    for _, name in ipairs(candidates) do
      local mod_tbl = rawget(svapi, name)
      if type(mod_tbl) == "table" then
        local fn = rawget(mod_tbl, stub_fn_name)
        if type(fn) == "function" then return fn, name end
      end
    end
    -- Last-resort scan: walk every svapi.* table for the Stub name.
    for k, v in pairs(svapi) do
      if type(v) == "table" then
        local fn = rawget(v, stub_fn_name)
        if type(fn) == "function" then return fn, k end
      end
    end
    return nil, nil
  end

  local stub_fn, svapi_file = find_stub()
  if not stub_fn then
    ctx.errors[#ctx.errors + 1] =
      "Stub not found: svapi." .. tostring(job.module) .. "." .. stub_fn_name
    _G._HARNESS_CTX = nil
    return ctx, stub_fn_name
  end

  -- Build phantom args. The Stub builder's signature is
  -- (user_cb, error_cb, ...request_params). Pass permissive sentinels --
  -- sentinels absorb calls + field reads so any guard like
  -- `if user_cb then user_cb(env) end` enters the branch, and the user_cb
  -- call itself degrades gracefully.
  local args = {}
  local arity = tonumber(job.stub_arity) or 6
  for i = 1, arity do
    args[i] = stubs.permissive()
  end

  local ok, descriptor = pcall(function() return stub_fn(unpack(args)) end)
  if not ok then
    ctx.errors[#ctx.errors + 1] = "Stub call error: " .. tostring(descriptor)
    _G._HARNESS_CTX = nil
    return ctx, stub_fn_name
  end
  if type(descriptor) ~= "table" or type(descriptor.on_success) ~= "function" then
    ctx.errors[#ctx.errors + 1] =
      "Stub returned no on_success: type=" .. type(descriptor)
    _G._HARNESS_CTX = nil
    return ctx, stub_fn_name
  end

  -- Build an envelope and spy with the endpoint-tagged prefix. The spy
  -- propagates the prefix into all chained field reads, so every key
  -- the listener or user_cb touches logs with `ep:mod.action.<rest>`.
  local envelope = {response_data = {}, status_code = 200}
  local prefix = "ep:" .. job.module .. "." .. job.action
  local spied = stubs.spy(envelope, ctx.accessed_keys, prefix, variant_cfg)

  local ok2, err = pcall(descriptor.on_success, spied)
  if not ok2 then
    ctx.errors[#ctx.errors + 1] = "on_success error: " .. tostring(err)
  end

  _G._HARNESS_CTX = nil
  ctx.svapi_file = svapi_file
  return ctx, stub_fn_name
end

-- Session 10 Approach B-broad: invoke every exported method on a
-- list of classes after pre-populating the Cachable cache for this
-- endpoint's cache_key with a spied candidate. Designed for the
-- 108 ui-only endpoints that don't have a `<action>Stub` builder.
--
-- Many methods crash on missing UI / scene context. pcall absorbs.
-- The signal we want: methods that call Cachable.get(cache_key) or
-- destructure response fields through registered listeners.
local function dispatch_invoke_classes(job)
  local variant_cfg = sv.get(job.variant)
  local ctx = {
    module = job.module,
    action = job.action,
    cache_key = job.cache_key,
    accessed_keys = {},
    errors = {},
    methods_invoked = 0,
    methods_succeeded = 0,
    methods_failed = 0,
    classes_seen = {},
    variant = variant_cfg,
    variant_name = variant_cfg.name,
  }
  stubs.reset(ctx)
  _G._HARNESS_CTX = ctx

  -- Use debug.sethook to bound execution by instruction count.
  -- 100k instructions is well beyond any normal handler; defeats infinite
  -- loops in the registered listeners (and in invoked methods).
  local function timed_pcall(fn, ...)
    local args = {...}
    local count = 0
    local function hook()
      count = count + 1
      if count > 100000 then error("instruction_limit", 0) end
    end
    debug.sethook(hook, "", 5000)
    local ok, err = pcall(function() return fn(unpack(args)) end)
    debug.sethook()
    return ok, err
  end

  local Cachable = stubs.modules.Cachable
  if job.cache_key and job.cache_key ~= "" then
    local rd = (job.candidate_response or {}).response_data or {}
    local spied = stubs.spy(rd, ctx.accessed_keys, "response_data", variant_cfg)
    Cachable.set(job.cache_key, spied)
    local okN, errN = timed_pcall(Cachable.notifyUpdate, job.cache_key)
    if not okN then
      ctx.errors[#ctx.errors + 1] =
        "notifyUpdate error: " .. tostring(errN)
    end
  end

  -- Method names matching these patterns are likely tight loops or
  -- infinite-poll handlers — skip them to keep total runtime bounded.
  -- `^start$` and `^main$` are typically one-shot setup methods that
  -- DO read response data; `^start$` was removed from this list since
  -- it's our common target. `^main$` stays because m_main top files
  -- frequently have a `main()` that's the actual frame-rate driver.
  local SKIP_PATTERNS = {
    "^update$", "^tick$", "^render$", "^draw$", "^step$",
    "^onUpdate", "^loop", "^poll", "^_update", "^advance",
    "^play$", "^run$", "^main$", "^enterFrame",
  }
  local function should_skip(name)
    for _, p in ipairs(SKIP_PATTERNS) do
      if name:match(p) then return true end
    end
    return false
  end

  -- Cap per invocation to bound runtime + memory. Each method is
  -- separately bounded by debug.sethook (100k * 5000 = 5e8 instructions,
  -- ~few seconds worst case), so these caps mostly bound total endpoint
  -- runtime, not catastrophic-method runaway. Bumped 8/60 -> 16/200
  -- because next()-order method iteration was missing relevant methods
  -- past index 8, and broad-class endpoints (those referencing 10+
  -- candidate UI files) were hitting the total cap before exploring
  -- the latter classes' setup methods.
  local MAX_METHODS_PER_CLASS = 16
  local MAX_TOTAL_METHODS = 200

  local classes = job.classes or {}
  for _, class_name in ipairs(classes) do
    if ctx.methods_invoked >= MAX_TOTAL_METHODS then break end
    local cls = rawget(stubs.modules, class_name)
    if type(cls) == "table" then
      ctx.classes_seen[#ctx.classes_seen + 1] = class_name
      local per_class = 0
      local k = next(cls)
      while k ~= nil and per_class < MAX_METHODS_PER_CLASS
          and ctx.methods_invoked < MAX_TOTAL_METHODS do
        local v = rawget(cls, k)
        if type(v) == "function" and type(k) == "string"
            and not should_skip(k) then
          ctx.methods_invoked = ctx.methods_invoked + 1
          per_class = per_class + 1
          -- Per-method-invocation instance. Writes to `self.x` need to
          -- land on a disposable table, NOT the class table itself --
          -- mutating stubs.modules[class_name] would persist across
          -- subsequent endpoints' invoke_classes jobs and cross-
          -- contaminate their accessed_keys logs. The instance's
          -- __index falls through to the class so method-self method
          -- lookups (`self:helper()`) still resolve.
          local instance = setmetatable({}, {__index = cls})
          local args = {instance, stubs.permissive(), stubs.permissive(),
                        stubs.permissive(), stubs.permissive()}
          local ok, err = timed_pcall(v, unpack(args))
          if ok then
            ctx.methods_succeeded = ctx.methods_succeeded + 1
          else
            ctx.methods_failed = ctx.methods_failed + 1
          end
        end
        k = next(cls, k)
      end
    end
  end

  _G._HARNESS_CTX = nil
  return ctx, "invoke_classes"
end

local function dispatch(job)
  if job.kind == "handler_invoke" then
    return dispatch_handler_invoke(job)
  end
  if job.kind == "invoke_stub" then
    return dispatch_invoke_stub(job)
  end
  if job.kind == "invoke_classes" then
    return dispatch_invoke_classes(job)
  end
  local variant_cfg = sv.get(job.variant)
  local ctx = {
    module = job.module,
    action = job.action,
    fn_name = job.fn_name,
    candidate_response = job.candidate_response or {},
    accessed_keys = {},
    errors = {},
    listener_errors = {},
    send_call_count = 0,
    spied_envelope = nil,
    variant = variant_cfg,
    variant_name = variant_cfg.name,
  }
  stubs.reset(ctx)
  -- The Cachable listeners need a way to log to the CURRENT ctx when
  -- they error. cachable.lua looks at _G._HARNESS_CTX.
  _G._HARNESS_CTX = ctx

  local svapi = stubs.modules.svapi
  -- Try wire-level module name first, then svapi_file (filename without
  -- .lua). The two differ for ~8 endpoints (lbonus <-> lBonus,
  -- freeLive <-> free, etc.) — see naming bridge in plan note 5.
  -- ALSO: when the wire module exists as a svapi.<mod> table but the
  -- function is in a different file (e.g. wire login.topInfo lives in
  -- common/svapi/top.lua, not login.lua), build a list of candidate
  -- mod_tbls and search them all, case-insensitively for the fn name.
  local function _ci_rawget(tbl, name)
    if type(tbl) ~= "table" or type(name) ~= "string" then return nil end
    local v = rawget(tbl, name)
    if v ~= nil then return v end
    -- case-insensitive fallback: common after action/module renames
    -- where e.g. "multiunitscenarioStatus" should match "multiunitScenarioStatus".
    local lower = name:lower()
    local k = next(tbl)
    while k ~= nil do
      if type(k) == "string" and k:lower() == lower then
        return rawget(tbl, k)
      end
      k = next(tbl, k)
    end
    return nil
  end

  local mod_candidates = {}
  if type(rawget(svapi, job.module)) == "table" then
    mod_candidates[#mod_candidates + 1] = rawget(svapi, job.module)
  end
  if job.svapi_file and job.svapi_file ~= job.module
      and type(rawget(svapi, job.svapi_file)) == "table" then
    mod_candidates[#mod_candidates + 1] = rawget(svapi, job.svapi_file)
  end
  if #mod_candidates == 0 then
    ctx.errors[#ctx.errors + 1] =
      "svapi module table missing: svapi." .. tostring(job.module)
    -- Even without the dispatch fn, fire the listener so we still surface
    -- whatever the cache_key-registered listener reads on a sentinel envelope.
    if job.cache_key and job.cache_key ~= "" then
      local Cachable = stubs.modules.Cachable
      local rd = (job.candidate_response or {}).response_data or {}
      local spied = stubs.spy(rd, ctx.accessed_keys, "response_data", variant_cfg)
      Cachable.set(job.cache_key, spied)
      local okN, errN = pcall(Cachable.notifyUpdate, job.cache_key)
      if not okN then
        ctx.errors[#ctx.errors + 1] =
          "notifyUpdate error for " .. job.cache_key .. ": " .. tostring(errN)
      end
    end
    return ctx, nil, nil
  end

  -- Try each (mod_tbl, name) combination. fn_name first (qualified
  -- like loginTopInfo), then bare action, then alt_fn_names. rawget so
  -- finalize_modules' sentinel-on-miss doesn't synthesize a callable
  -- sentinel and silently drop the dispatch-missing case.
  local fn, fn_key, mod_tbl
  local names_to_try = {job.fn_name, job.action}
  if type(job.alt_fn_names) == "table" then
    for _, alt in ipairs(job.alt_fn_names) do
      names_to_try[#names_to_try + 1] = alt
    end
  end
  for _, candidate in ipairs(mod_candidates) do
    for _, name in ipairs(names_to_try) do
      local cand = _ci_rawget(candidate, name)
      if type(cand) == "function" then
        fn = cand
        fn_key = name
        mod_tbl = candidate
        break
      end
    end
    if fn then break end
  end
  if type(fn) ~= "function" then
    ctx.errors[#ctx.errors + 1] =
      "dispatch fn missing: svapi." .. job.module .. "." .. job.fn_name ..
      " (also tried ." .. job.action .. ")"
    return ctx, nil, nil
  end

  local user_cb_invoked = false

  local user_success_cb = function(envelope, ...)
    user_cb_invoked = true
    -- (a) Walk the declared schema: surfaces every path our static
    --     analysis already knows about. Backward-compatible with V1.
    if job.schema then
      local ok2, err2 = pcall(schema_walk, envelope, job.schema, "", 0)
      if not ok2 then
        ctx.errors[#ctx.errors + 1] = "schema_walk error: " .. tostring(err2)
      end
    end
    -- (b) V5 listener fire: if the endpoint has a cache_key, call
    --     Cachable.notifyUpdate against the SPIED response_data so
    --     every registered listener runs against the spy and logs
    --     its undeclared reads. svapi.cacheResponse normally does
    --     this for the endpoint's own cache_key, but many svapi
    --     handlers do NOT call cacheResponse (they forward to a
    --     user callback directly). Firing notifyUpdate here covers
    --     that path.
    if job.cache_key and job.cache_key ~= "" and type(envelope) == "table" then
      local rd = envelope.response_data
      if type(rd) == "table" then
        local Cachable = stubs.modules.Cachable
        Cachable.set(job.cache_key, rd)
        local ok3, err3 = pcall(Cachable.notifyUpdate, job.cache_key)
        if not ok3 then
          ctx.errors[#ctx.errors + 1] =
            "notifyUpdate error for " .. job.cache_key .. ": " .. tostring(err3)
        end
      end
    end
  end

  local user_error_cb = function(...)
    ctx.errors[#ctx.errors + 1] = "user_error_cb invoked"
  end

  local function default_arg() return stubs.permissive() end
  local arity = tonumber(job.request_arity) or 0
  local call_args = {user_success_cb, user_error_cb}
  for i = 1, arity do
    call_args[#call_args + 1] = default_arg()
  end

  local ok2, ret_or_err = pcall(function()
    return fn(unpack(call_args))
  end)
  if not ok2 then
    ctx.errors[#ctx.errors + 1] =
      "svapi." .. job.module .. "." .. fn_key .. " error: " ..
      tostring(ret_or_err)
  end

  if not user_cb_invoked then
    ctx.errors[#ctx.errors + 1] =
      "user success_cb was never invoked (http.send never reached?)"
  end

  -- Always fire the cache_key listener explicitly with a spied candidate,
  -- even if dispatch failed or success_cb wasn't invoked. The dispatch
  -- path (http.send -> svapi.cacheResponse -> notifyUpdate) handles the
  -- common case; this catches handlers that bypass it (svapi handlers
  -- that build their own envelope and never call cacheResponse).
  if job.cache_key and job.cache_key ~= "" then
    local Cachable = stubs.modules.Cachable
    local rd = (job.candidate_response or {}).response_data or {}
    local spied = stubs.spy(rd, ctx.accessed_keys, "response_data", variant_cfg)
    Cachable.set(job.cache_key, spied)
    local okN, errN = pcall(Cachable.notifyUpdate, job.cache_key)
    if not okN then
      ctx.errors[#ctx.errors + 1] =
        "post-dispatch notifyUpdate error for " .. job.cache_key ..
        ": " .. tostring(errN)
    end
  end

  _G._HARNESS_CTX = nil
  return ctx, fn_key, ok2 and ret_or_err or nil
end

-- ---- loop ------------------------------------------------------------------

while true do
  local line = io.stdin:read("*l")
  if line == nil then break end
  if line ~= "" then
    local ok, job = pcall(json.decode, line)
    if not ok then
      io.stdout:write(json.encode({
        __error = true,
        errors = {"failed to parse job JSON: " .. tostring(job)},
      }) .. "\n")
      io.stdout:flush()
    else
      local out
      if job.kind == "list_modules" then
        out = {kind = "list_modules", modules = dispatch_list_modules(job)}
        io.stdout:write(json.encode(out) .. "\n")
        io.stdout:flush()
      else
      local ctx, fn_key, lua_returned = dispatch(job)
      if job.kind == "handler_invoke" then
        out = {
          kind = "handler_invoke",
          module_table = ctx.module_table,
          fn_name = ctx.fn_name,
          variant = ctx.variant_name,
          accessed_keys = ctx.accessed_keys,
          errors = ctx.errors,
          bulksend_batch = ctx.bulksend_batch,
          bulksend_call_count = ctx.bulksend_call_count,
          cache_keys_set = ctx.cache_keys_set,
          last_cache_key = ctx.last_cache_key,
        }
      elseif job.kind == "invoke_stub" then
        out = {
          kind = "invoke_stub",
          module = ctx.module,
          action = ctx.action,
          svapi_file = ctx.svapi_file,
          variant = ctx.variant_name,
          accessed_keys = ctx.accessed_keys,
          errors = ctx.errors,
          cache_keys_set = ctx.cache_keys_set,
        }
      elseif job.kind == "invoke_classes" then
        out = {
          kind = "invoke_classes",
          module = ctx.module,
          action = ctx.action,
          cache_key = ctx.cache_key,
          variant = ctx.variant_name,
          accessed_keys = ctx.accessed_keys,
          errors = ctx.errors,
          methods_invoked = ctx.methods_invoked,
          methods_succeeded = ctx.methods_succeeded,
          methods_failed = ctx.methods_failed,
          classes_seen = ctx.classes_seen,
        }
      else
        out = {
          module = ctx.module,
          action = ctx.action,
          variant = ctx.variant_name,
          accessed_keys = ctx.accessed_keys,
          errors = ctx.errors,
          listener_errors = ctx.listener_errors,
          send_call_count = ctx.send_call_count,
          fn_key = fn_key,
          lua_returned = lua_returned,
        }
      end
      io.stdout:write(json.encode(out) .. "\n")
      io.stdout:flush()
      end
    end
  end
end
