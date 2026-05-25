-- cachable.lua
-- Lua-port of source/all/common/cachable.lua (the decompiled
-- registers-only output is hard to follow; this is the obvious
-- intent of the original). Used by the V5 harness to fire real
-- listeners against the spied response.
--
-- Persists state through two well-known global tables:
--   GLOBAL.CACHE          : cache_key -> cached_payload
--   GLOBAL.CACHE_OBSERVER : cache_key -> ordered list of listener fns
-- Both tables are created lazily.

local M = {}

local sentinel = require("sentinel")

local function ensure_cache()
  if type(GLOBAL) ~= "table" then GLOBAL = {} end
  if type(GLOBAL.CACHE) ~= "table" then GLOBAL.CACHE = {} end
  if type(GLOBAL.CACHE_OBSERVER) ~= "table" then GLOBAL.CACHE_OBSERVER = {} end
end

-- get() returns a sentinel when no value has been cached for cache_key.
-- The real client populates other cache keys ($userInfo, $userUnitAll, ...)
-- as side-effects of earlier responses; in the harness, listeners fired
-- for endpoint A may try to navigate $userInfo etc. that endpoint B
-- would have populated. Returning a sentinel lets the navigation chain
-- pass through without crashing instead of hitting nil at depth 2. The
-- sentinel has no log context, so these reads don't contaminate the
-- accessed_keys log of the endpoint currently under test.
function M.get(cache_key)
  ensure_cache()
  local v = GLOBAL.CACHE[cache_key]
  if v == nil then
    return sentinel.new(nil, "$cache." .. tostring(cache_key), nil)
  end
  return v
end

function M.set(cache_key, value)
  ensure_cache()
  GLOBAL.CACHE[cache_key] = value
end

function M.addListener(cache_key, fn)
  ensure_cache()
  assert(type(cache_key) == "string",
    "Cachable.addListener: cache_key must be string, got " .. type(cache_key))
  assert(type(fn) == "function",
    "Cachable.addListener: fn must be function, got " .. type(fn))
  local list = GLOBAL.CACHE_OBSERVER[cache_key]
  if not list then list = {}; GLOBAL.CACHE_OBSERVER[cache_key] = list end
  table.insert(list, fn)
end

function M.addListenerHead(cache_key, fn)
  ensure_cache()
  assert(type(cache_key) == "string",
    "Cachable.addListenerHead: cache_key must be string, got " .. type(cache_key))
  assert(type(fn) == "function",
    "Cachable.addListenerHead: fn must be function, got " .. type(fn))
  local list = GLOBAL.CACHE_OBSERVER[cache_key]
  if not list then list = {}; GLOBAL.CACHE_OBSERVER[cache_key] = list end
  table.insert(list, 1, fn)
end

function M.removeListener(cache_key, fn)
  ensure_cache()
  local list = GLOBAL.CACHE_OBSERVER[cache_key]
  if not list then return end
  local idx
  for i, v in pairs(list) do
    if v == fn then idx = i; break end
  end
  if idx then list[idx] = nil end
end

-- notifyUpdate fires every registered listener for cache_key against
-- the current GLOBAL.CACHE[cache_key]. In real client code,
-- _util.lua's cacheResponse populates the cache from the wire
-- envelope first, then calls notifyUpdate. The harness's modified
-- svapi.cacheResponse stub does the equivalent: it stores the
-- spied response_data directly (no extend, so the spy chain stays
-- intact) and then calls notifyUpdate.
function M.notifyUpdate(cache_key, ...)
  ensure_cache()
  local list = GLOBAL.CACHE_OBSERVER[cache_key]
  if not list then return end
  local cached = GLOBAL.CACHE[cache_key]
  for _, fn in pairs(list) do
    if type(fn) == "function" then
      local ok, err = pcall(fn, cached, ...)
      -- Listener crashes are logged separately from ctx.errors. A
      -- listener typically reads a real module singleton (e.g.
      -- `MuseumModel.getInstance().add(...)`) that needs an init flow
      -- the harness can't replicate. The listener's pre-crash field
      -- reads are still in ctx.accessed_keys -- that's the value we
      -- extract. The crash itself isn't a harness regression, just an
      -- inherent partial-execution artifact, so it goes in
      -- listener_errors (informational) rather than errors (verdict).
      if not ok and _G._HARNESS_CTX then
        local ctx = _G._HARNESS_CTX
        ctx.listener_errors = ctx.listener_errors or {}
        ctx.listener_errors[#ctx.listener_errors + 1] =
          "listener error for " .. tostring(cache_key) .. ": " .. tostring(err)
      end
    end
  end
end

function M.clear(cache_key)
  ensure_cache()
  GLOBAL.CACHE[cache_key] = {}
  return GLOBAL.CACHE[cache_key]
end

function M.clearAll()
  ensure_cache()
  GLOBAL.CACHE = {}
end

function M.isEmpty(cache_key)
  ensure_cache()
  local t = GLOBAL.CACHE[cache_key]
  if type(t) ~= "table" then return true end
  return next(t) == nil
end

-- DIAG: number of listeners registered across all cache_keys.
-- Used by the harness preload to confirm initialize.lua ran.
function M.observer_count()
  ensure_cache()
  local n = 0
  for _, list in pairs(GLOBAL.CACHE_OBSERVER) do
    for _, fn in pairs(list) do
      if type(fn) == "function" then n = n + 1 end
    end
  end
  return n
end

return M
