-- stubs.lua
-- Engine-side global stubs for KLab's PlaygroundOSS engine.
-- Installs minimal placeholders so decompiled svapi/* files can load
-- and execute their dispatch tables in a vanilla Lua 5.1 / LuaJIT VM.
--
-- The runtime answer the harness needs:
--   "what fields does the Lua handler actually read off a candidate
--    response table?"
--
-- V5 additions (listener-driven discovery):
--   - svapi.cacheResponse now stores the SPIED response_data into
--     GLOBAL.CACHE[cache_key] and fires Cachable.notifyUpdate, so
--     listener bodies registered in m_boot/initialize.lua + ~18
--     model files run against the spy and log every field they read.
--   - import("Cachable") returns the real port at cachable.lua,
--     not a permissive sentinel.
--   - Spy __index returns a recursive permissive sentinel when the
--     underlying field is missing -- so a listener that reads a
--     field absent from our candidate keeps going (instead of nil-
--     indexing and crashing), and all subsequent reads on that
--     sentinel keep getting logged.

local M = {}

local sentinel = require("sentinel")
local sv = require("sentinel_variants")
local Cachable = require("cachable")

-- ---- permissive sentinel (legacy, for non-spy globals) --------------------
-- A table that absorbs arbitrary key reads / calls without erroring.
-- Returned from import() for modules we did not anticipate, and used as a
-- default fallback in several stubs below. Differs from sentinel.lua in
-- that this version intentionally does NOT log -- it is for engine-side
-- globals that the listener should not be discovering fields against.
local function noop() return nil end

local _permissive_mt
_permissive_mt = {
  -- Rawget first so explicitly-stored values are retrievable. If the
  -- caller wrote `t.foo = bar`, `t.foo` should return bar -- NOT a
  -- fresh sentinel. Without this rawget, functions stored on a
  -- permissive table (via __newindex's rawset) become unreachable.
  __index = function(t, k)
    local v = rawget(t, k)
    if v ~= nil then return v end
    return setmetatable({}, _permissive_mt)
  end,
  -- __call returns ANOTHER permissive sentinel so chained patterns
  -- like `Engine.getInstance().update(x)` keep degrading instead of
  -- erroring on `nil.update`. Listeners written against the real
  -- engine helpers very commonly chain like this.
  __call = function() return setmetatable({}, _permissive_mt) end,
  __newindex = function(t, k, v) rawset(t, k, v) end,
  -- Without __tostring, `tostring(permissive)` produces `table: 0x<addr>`
  -- which then surfaces in log paths when a listener uses a permissive
  -- as a table key (e.g. `cached.unit_list[stub] = something`). The
  -- spy's __index/__pairs format such paths as `[table: 0x...]`,
  -- polluting wire-compare findings with run-specific heap addresses.
  __tostring = function() return "<permissive>" end,
}
local function permissive() return setmetatable({}, _permissive_mt) end
M.permissive = permissive

-- ---- spy -------------------------------------------------------------------
-- spy(t, log, prefix, variant) returns a proxy table whose __index records
-- every key access into `log` as a dotted path, and recurses on table values.
-- __newindex errors -- handlers must not mutate the response.
--
-- V5 change: when the underlying field is nil, return a permissive
-- sentinel (which keeps logging deeper reads). Pre-V5 we returned nil,
-- which silently halted discovery at the first undeclared field.
--
-- Tier 1 (Session 9) change: the spy carries a variant. The variant
-- propagates to every nil-field sentinel it constructs, so the entire
-- listener traversal sees consistent per-variant semantics. The spy
-- *also* applies the bool override at its top layer so declared-bool
-- fields can be flipped even when the candidate populated them with a
-- default (the default is harness-side, not wire-state). The list_len
-- override is reflected in __ipairs/__len for empty underlying lists
-- so list[any] schemas (which arrive as []) still produce phantom
-- iteration when fuzzing list_one / list_many.
local function spy(t, log, prefix, variant)
  if type(t) ~= "table" then return t end
  prefix = prefix or ""
  local proxy = {}
  local mt = {
    __index = function(_, k)
      local key_repr
      if type(k) == "string" then
        key_repr = k
      else
        key_repr = "[" .. tostring(k) .. "]"
      end
      local path
      if prefix == "" then
        path = key_repr
      else
        path = prefix .. "." .. key_repr
      end
      log[#log + 1] = path
      -- Variant bool override (BEFORE rawget). Declared bool fields default
      -- to `false` from the harness-side candidate builder; the wire could
      -- legitimately produce either polarity. The variant tier fires both
      -- per-key branches by overriding bool-shaped names regardless of
      -- whether the field was declared.
      if variant and variant.bool ~= nil and sv.is_bool_like_key(k) then
        return variant.bool
      end
      local v = rawget(t, k)
      if v == nil then
        return sentinel.new(log, path, variant)
      end
      if type(v) == "table" then
        return spy(v, log, path, variant)
      end
      return v
    end,
    __newindex = function(_, k, _v)
      -- Earlier versions raised here; that killed the listener mid-body
      -- and lost every field read past the offending assignment. The
      -- spy is informational, not enforcement -- absorb writes silently
      -- so the listener's REMAINING reads still log. Record the attempt
      -- in ctx.spy_writes for diagnostic purposes.
      if _G._HARNESS_CTX then
        local ctx = _G._HARNESS_CTX
        ctx.spy_writes = ctx.spy_writes or {}
        ctx.spy_writes[#ctx.spy_writes + 1] = {
          key = tostring(k),
          prefix = prefix == "" and "<root>" or prefix,
        }
      end
    end,
    __pairs = function(_)
      return function(_, last)
        local k, v = next(t, last)
        if k == nil then return nil end
        local key_repr = type(k) == "string" and k or ("[" .. tostring(k) .. "]")
        local path = prefix == "" and key_repr or (prefix .. "." .. key_repr)
        log[#log + 1] = path
        if type(v) == "table" then
          return k, spy(v, log, path, variant)
        end
        return k, v
      end, t, nil
    end,
    __ipairs = function(_)
      -- If the underlying list is empty AND a variant requests phantom
      -- iteration, yield variant.list_len sentinels with synthesized
      -- positional paths. This unblocks list[any] schemas (`[]` in the
      -- candidate) so the listener's per-element body still executes.
      if variant and variant.list_len and variant.list_len > 0 and #t == 0 then
        local i = 0
        local n = variant.list_len
        return function()
          i = i + 1
          if i > n then return nil end
          local path = prefix == "" and ("[" .. i .. "]") or (prefix .. ".[" .. i .. "]")
          log[#log + 1] = path
          return i, sentinel.new(log, path, variant)
        end, t, 0
      end
      return function(_, i)
        i = i + 1
        local v = rawget(t, i)
        if v == nil then return nil end
        local path = prefix == "" and ("[" .. i .. "]") or (prefix .. ".[" .. i .. "]")
        log[#log + 1] = path
        if type(v) == "table" then
          return i, spy(v, log, path, variant)
        end
        return i, v
      end, t, 0
    end,
    __len = function()
      -- Variant list_len overrides empty real lists; non-empty real lists
      -- keep their actual length so multi-element candidates iterate
      -- naturally.
      if variant and variant.list_len and variant.list_len > 0 and #t == 0 then
        return variant.list_len
      end
      return #t
    end,
  }
  return setmetatable(proxy, mt)
end
M.spy = spy

-- ---- import / module registry ---------------------------------------------
-- Modules registered via define(name, value) live here. Listed up-front
-- so `import("Cachable")` returns the real Cachable port before any model
-- file's body runs.
M.modules = {}
-- Tracks names explicitly registered via define(). Distinct from M.modules,
-- which also receives placeholder stubs from import() calls on names that
-- haven't been defined yet. is_defined consults this set (not M.modules)
-- so files guarded by `if is_defined("header") then return end` correctly
-- see "not defined" even after import("header") has auto-stubbed.
M.real_defines = {}

local function make_default_modules()
  -- svapi: shared module table. svapi/<name>.lua files register into
  -- this; client-side callers reference svapi.cacheResponse etc.
  -- V5: cacheResponse now stores the SPIED response_data and fires
  -- Cachable.notifyUpdate so listeners log their field reads against
  -- the spy.
  local svapi = {
    cacheResponse = function(cache_key, envelope, ...)
      if type(envelope) ~= "table" then return end
      -- Touch envelope to record envelope-level access in the log.
      local rd = envelope.response_data
      local _ = envelope.status_code
      -- Session 9 Tier 2: track every cache_key written so the
      -- aggregator can attribute prefix-"" reads (V5-style http.send
      -- spies that log `response_data.field`) back to whichever
      -- cache_key was most recently set during this handler_invoke.
      -- Not perfect (multi-endpoint handlers will mis-attribute) but
      -- gives a usable signal when only one cache_key is touched.
      if type(rd) == "table" then
        Cachable.set(cache_key, rd)
        local cur = M.current
        if cur then
          cur.cache_keys_set = cur.cache_keys_set or {}
          cur.cache_keys_set_seen = cur.cache_keys_set_seen or {}
          if not cur.cache_keys_set_seen[cache_key] then
            cur.cache_keys_set_seen[cache_key] = true
            cur.cache_keys_set[#cur.cache_keys_set + 1] = cache_key
          end
          cur.last_cache_key = cache_key
        end
      end
      Cachable.notifyUpdate(cache_key, ...)
    end,
    onlyCacheResponse = function(cache_key, payload, ...)
      if type(payload) == "table" then Cachable.set(cache_key, payload) end
      Cachable.notifyUpdate(cache_key, ...)
    end,
    -- Session 9 Tier 2 Approach B: bulkSend mirrors the real
    -- implementation (common/svapi/_util.lua L5_1 around line 18-99):
    -- for each batch entry, build a per-batch envelope
    -- `{response_data = <result>, status_code = 200}` and call
    -- entry.on_success(per_batch_envelope). The Stub's on_success
    -- typically does cacheResponse(cache_key, envelope, ...), which
    -- our cacheResponse stub spies the response_data into Cachable.
    -- Then user_success_cb gets the full wire envelope so any direct
    -- reads on it (or subsequent Cachable.get(cache_key) reads) hit
    -- spies and log fields.
    --
    -- Each result table is shared between the per-batch envelope and
    -- the wire envelope's response_data[i].result, so the spies are
    -- consistent regardless of which view the handler accesses.
    --
    -- M.current.bulksend_batch records (index, module, action) per
    -- batch entry so the aggregator can attribute discovered paths
    -- back to specific endpoints.
    bulkSend = function(batch, success_cb, ...)
      if type(batch) ~= "table" then return end
      if #batch == 0 then return end
      local cur = M.current
      if not cur then return end
      local mapping = {}
      local results = {}
      for i = 1, #batch do
        local entry = batch[i]
        local mod, act = nil, nil
        if type(entry) == "table" and type(entry.command_table) == "table" then
          mod, act = entry.command_table.module, entry.command_table.action
        end
        mapping[#mapping + 1] = {index = i, module = mod, action = act}
        results[i] = {}
      end
      cur.bulksend_batch = mapping
      cur.bulksend_call_count = (cur.bulksend_call_count or 0) + 1

      -- Fire each batch entry's on_success with a per-batch envelope.
      -- Each result table is spied with a per-batch prefix so paths
      -- log as `bulksend.[i].result.X` -- attributable to mapping[i].
      for i = 1, #batch do
        local entry = batch[i]
        if type(entry) == "table" and type(entry.on_success) == "function" then
          local per_batch = {response_data = results[i], status_code = 200}
          local prefix = "bulksend.[" .. i .. "]"
          local spied = spy(per_batch, cur.accessed_keys, prefix, cur.variant)
          local ok, err = pcall(entry.on_success, spied)
          if not ok then
            cur.errors[#cur.errors + 1] =
              "bulkSend on_success[" .. i .. "] error: " .. tostring(err)
          end
        end
      end

      -- Then fire the user's success_cb with the full wire envelope.
      -- Each response_data[i].result shares the result table from above
      -- so any reads here AND any Cachable.get(cache_key) chase the
      -- same spy chain.
      if type(success_cb) == "function" then
        local response_data = {}
        for i = 1, #batch do
          response_data[i] = {result = results[i], status = 200}
        end
        local envelope = {response_data = response_data, status_code = 200}
        local spied = spy(envelope, cur.accessed_keys, "wire", cur.variant)
        local ok, err = pcall(success_cb, spied)
        if not ok then
          cur.errors[#cur.errors + 1] = "bulkSend success_cb error: " .. tostring(err)
        end
      end
    end,
    -- bulkSendSysloadable carries the same envelope shape; just delegate.
    bulkSendSysloadable = function(batch, success_cb, ...)
      return M.modules.svapi.bulkSend(batch, success_cb, ...)
    end,
    bindParams = function(fn, ...)
      local args = {...}
      return function(env, cb_args)
        return fn(env, cb_args, unpack(args))
      end
    end,
  }
  M.modules.svapi = svapi

  -- http: send(request, success_cb, url, error_cb, ...) is the hook the
  -- harness drives. The current endpoint's pre-staged response and
  -- access log live in M.current; when an svapi file calls
  -- http.send(req, success_cb, url, ...), this stub spies the response
  -- and invokes success_cb synchronously.
  local http = {
    DEFAULT_TIMEOUT_MS = 30000,
    SESSION_TIMEOUT_S = 82800,
    createCommandNum = function()
      M.current = M.current or {}
      M.current.command_num = (M.current.command_num or 0) + 1
      return M.current.command_num
    end,
    getServerInfo = function()
      return {domain = "stub.invalid", port = 443, path = "/"}
    end,
    setSessionKey = function() end,
    getSessionKey = function() return "" end,
    is_disabled = false,
    getLoginStatus = function() return "SUCCEEDED" end,
    shouldLock = function() return false end,
    send = function(request, success_cb, url, error_cb, ...)
      local cur = M.current
      if not cur then
        error("http.send invoked with no current endpoint context")
      end
      cur.request = request
      cur.url = url
      cur.send_call_count = (cur.send_call_count or 0) + 1
      if not success_cb then
        cur.errors[#cur.errors + 1] = "http.send invoked with no success_cb"
        return
      end
      local spied = spy(cur.candidate_response, cur.accessed_keys, "", cur.variant)
      cur.spied_envelope = spied
      local ok, err = pcall(success_cb, spied)
      if not ok then
        cur.errors[#cur.errors + 1] = "success_cb error: " .. tostring(err)
      end
    end,
  }
  M.modules.http = http

  -- Real Cachable port -- listeners registered via initialize.lua /
  -- model/*.lua are stored on GLOBAL.CACHE_OBSERVER and fired by
  -- notifyUpdate.
  M.modules.Cachable = Cachable
end

-- M.reset(current_ctx) -- called per-endpoint. V5 caveat: it does NOT
-- clear M.modules or the listener registry. Those are populated by
-- preload.lua at boot and are reused for every endpoint in the batch.
--
-- It DOES wipe GLOBAL.CACHE -- spies from a previous endpoint's
-- response are tied to that endpoint's log, so leaving them in the
-- cache means a later endpoint's listener reading from
-- `Cachable.get($priorKey)` would log its accesses into the PRIOR
-- endpoint's trace, silently corrupting it. Resetting the cache
-- per endpoint keeps logs isolated; listeners that read from a now-
-- empty cache simply hit nil + the permissive __index fallback.
function M.reset(current_ctx)
  M.current = current_ctx
  if type(GLOBAL) == "table" then
    GLOBAL.CACHE = {}
    -- Re-alias the bare CACHE global so it tracks the new table.
    _G.CACHE = GLOBAL.CACHE
  end
end

-- M.boot() -- called ONCE at harness startup, before preload runs.
-- Wires up the default svapi / http / Cachable module table and resets
-- the global cache state.
function M.boot()
  M.modules = {}
  M.real_defines = {}
  make_default_modules()
  -- Reset cache + observer tables; preload.lua will repopulate the
  -- observer table by loading initialize.lua + the 18 model files.
  if type(GLOBAL) == "table" then
    GLOBAL.CACHE = {}
    GLOBAL.CACHE_OBSERVER = {}
  end
end

-- M.finalize_modules() -- called ONCE after preload completes.
-- Stamps every module table with a fall-through __index so that
-- listener-time chained access (e.g. Underscore.deepExtend(t),
-- ClassInfo.getInstance()) on never-defined helpers returns a
-- permissive sentinel rather than nil. Sentinels absorb further
-- calls/reads, so a listener can run past missing engine helpers
-- and reach its actual field reads.
--
-- Why this is safe to do only AFTER preload: source-file idempotency
-- guards (`if M.initialized then return end`) read module fields and
-- check truthiness. Returning a sentinel would make those guards
-- always fire and the body would never run, so we leave modules
-- nil-on-unset during preload and only switch to permissive after.
function M.finalize_modules()
  for _name, mod in pairs(M.modules) do
    if type(mod) == "table" then
      local mt = getmetatable(mod)
      if mt == nil then
        setmetatable(mod, {
          __index = function(t, k)
            local v = rawget(t, k)
            if v ~= nil then return v end
            return permissive()
          end,
          __newindex = function(t, k, v) rawset(t, k, v) end,
        })
      end
    end
  end
end

function M.import(name)
  if M.modules[name] then return M.modules[name] end
  -- Return a REAL empty table (not a permissive sentinel). Source-file
  -- idempotency guards rely on `if mod.initialized then return end`
  -- being falsy on first import, otherwise the body never runs and
  -- the addListener calls inside never fire. A sentinel here would
  -- read truthy on every access and silently skip every preload body.
  --
  -- Chained access on unset keys (e.g. `import("Underscore").extend(t)`
  -- when Underscore was never loaded) errors -- but those error sites
  -- are caught by per-listener pcalls and per-preload-file pcalls, so
  -- a single missing helper degrades gracefully instead of killing
  -- the whole preload.
  local stub = {}
  M.modules[name] = stub
  return stub
end

-- ---- engine globals --------------------------------------------------------
function M.install_globals(env)
  env = env or _G

  env.import = M.import

  -- define(name, value) is how decompiled module files register
  -- themselves so `import(name)` returns the right table. The
  -- pattern at the end of every module file is `define("Foo", L0_1)`.
  -- Records the name in M.real_defines so is_defined can distinguish
  -- a real define from an auto-stub created by an earlier import.
  --
  -- Merge semantics: if import("Foo") was called BEFORE Foo.lua's
  -- define("Foo", real), the import caller captured the stub table
  -- as an upvalue. A naive replace `M.modules["Foo"] = real` makes
  -- subsequent imports get `real`, but the prior upvalue still
  -- points at the empty stub — listeners registered using that
  -- upvalue crash on missing methods. Instead, copy real's keys
  -- into the existing stub so the captured reference sees the
  -- real definitions too. This is the listener pre-registration
  -- race that bit `initialize.lua: L41_1 = import("AccessoryModel")`
  -- followed by setupUpdaters() before AccessoryModel.lua ran.
  env.define = function(name, value)
    local existing = M.modules[name]
    if existing ~= nil and existing ~= value
        and type(existing) == "table" and type(value) == "table" then
      for k, v in pairs(value) do existing[k] = v end
      M.real_defines[name] = true
      return existing
    end
    M.modules[name] = value
    M.real_defines[name] = true
    return value
  end

  -- include_once / include are file-based source loaders the engine
  -- uses for "file://install/..." paths. We do not resolve them; the
  -- referenced files are .lua under source/all but their behavior
  -- is mostly UI bootstrapping irrelevant to listener registration.
  env.include_once = function(_path) end
  env.include = function(_path) end

  -- is_defined(name) -> bool. Returns true only if `name` was passed
  -- to define() at some point. Important: this is NOT the same as
  -- `M.modules[name] ~= nil`, because import() lazily inserts stubs
  -- for un-yet-defined names so chained access doesn't crash. If
  -- is_defined consulted M.modules, every file guarded by
  -- `if is_defined("foo") then return end` would early-out the
  -- moment ANY earlier file did `import("foo")`. Consulting
  -- real_defines preserves the guard's intent.
  env.is_defined = function(name)
    if type(name) ~= "string" then return false end
    return M.real_defines[name] == true
  end

  -- tofunction(x) -- engine helper that resolves a table-with-__call
  -- to a callable. For our purposes the identity is fine; missing
  -- callables degrade to sentinels.
  env.tofunction = function(x)
    if type(x) == "function" then return x end
    return function(...) end
  end

  -- Some decompiled files do `local fn = tonumber` and call it; the
  -- engine globals below cover only the ones we have seen actually
  -- referenced at top level. Anything else falls through to nil.

  -- tonumber / tostring don't trip metamethods on sentinels in Lua 5.1,
  -- so a listener doing `tonumber(x.exp)` where `.exp` came back as a
  -- sentinel (the field wasn't populated by the candidate) receives
  -- raw nil and crashes downstream. Coerce sentinel inputs to 0 / ""
  -- so subsequent arithmetic / concat keeps running.
  local function _is_sentinel(x)
    if type(x) ~= "table" then return false end
    local mt = getmetatable(x)
    return mt and mt.__sentinel == true
  end
  local _raw_tonumber = tonumber
  env.tonumber = function(x, base)
    if _is_sentinel(x) then return 0 end
    if base ~= nil then return _raw_tonumber(x, base) end
    return _raw_tonumber(x)
  end
  local _raw_tostring = tostring
  env.tostring = function(x)
    if _is_sentinel(x) then return "" end
    return _raw_tostring(x)
  end

  env.klb_assert = function(cond, msg)
    if not cond then
      error("klb_assert failed: " .. tostring(msg or "(no message)"), 2)
    end
  end
  env.klb_logger_debug = function(...) end
  env.klb_logger_info = function(...) end
  env.klb_logger_warn = function(...) end
  env.klb_logger_error = function(...) end
  env.klb_logger_fatal = function(...) end

  env.is_table_type = function(x) return type(x) == "table" end
  env.is_string_type = function(x) return type(x) == "string" end
  env.is_number_type = function(x) return type(x) == "number" end
  env.is_function_type = function(x) return type(x) == "function" end
  env.is_boolean_type = function(x) return type(x) == "boolean" end
  env.is_nil_type = function(x) return x == nil end

  env.custom_unpack = function(t, i, j)
    i = i or 1
    j = j or (type(t) == "table" and (t.n or #t)) or 0
    if unpack then return unpack(t, i, j) end
    return table.unpack(t, i, j)
  end
  if env.unpack == nil and table.unpack then env.unpack = table.unpack end

  env.getSysLoadCount = function() return 0 end
  env.IN_LUASPEC = false

  -- GLOBAL and CACHE need to be real tables so cachable.lua's
  -- ensure_cache() can populate them and listeners can read from them.
  -- (Pre-V5 these were permissive sentinels, which meant Cachable
  -- couldn't actually persist its state -- harmless then because we
  -- weren't calling Cachable for real.)
  if type(env.GLOBAL) ~= "table" then env.GLOBAL = {} end
  if type(env.GLOBAL.CACHE) ~= "table" then env.GLOBAL.CACHE = {} end
  if type(env.GLOBAL.CACHE_OBSERVER) ~= "table" then
    env.GLOBAL.CACHE_OBSERVER = {}
  end
  -- Some code reads bare CACHE; alias to GLOBAL.CACHE.
  env.CACHE = env.GLOBAL.CACHE

  env.handle_network_error = function(msg)
    if M.current then
      M.current.errors[#M.current.errors + 1] =
        "handle_network_error: " .. tostring(msg)
    end
  end
  env.NETAPIMSG_UNKNOWN = "NETAPIMSG_UNKNOWN"
  env.NETAPIMSG_TIMEOUT = "NETAPIMSG_TIMEOUT"
  env.NETAPIMSG_DISCONNECT = "NETAPIMSG_DISCONNECT"

  env.setTimeout = function(fn, _ms)
    if type(fn) == "function" then pcall(fn) end
    return 0
  end
  env.clearTimeout = function() end
  env.setInterval = function() return 0 end
  env.clearInterval = function() end

  local _t0 = 1700000000
  env.os = env.os or {}
  env.os.time = function() return _t0 end
  env.os.clock = function() return 0.0 end
  env.os.date = function(fmt, _t)
    fmt = fmt or "%c"
    return "1970-01-01T00:00:00Z"
  end

  -- Patch pairs/ipairs to honor our spy's __pairs/__ipairs metamethods
  -- under Lua 5.1 semantics (5.1 ignores those metamethods by default).
  local _raw_pairs = pairs
  env.pairs = function(t)
    local mt = getmetatable(t)
    if mt and mt.__pairs then return mt.__pairs(t) end
    return _raw_pairs(t)
  end
  local _raw_ipairs = ipairs
  env.ipairs = function(t)
    local mt = getmetatable(t)
    if mt and mt.__ipairs then return mt.__ipairs(t) end
    return _raw_ipairs(t)
  end
end

return M
