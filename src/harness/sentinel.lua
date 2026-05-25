-- sentinel.lua
-- Recursive permissive sentinel for V5 listener-driven harness.
--
-- A sentinel is a table that absorbs nearly every Lua operation
-- without crashing, while still logging field-read paths. Used to
-- back fields that the listener tries to read but the candidate
-- response did not declare. Lets a listener run to completion
-- past undeclared reads so its full field-access intent is logged
-- instead of stopping at the first nil.
--
-- Truthiness: sentinels are tables, hence truthy. Listeners that
-- guard on `if x then ... read fields ... end` enter the branch,
-- which is the desired behavior for discovery.
--
-- Equality: __eq only fires when BOTH operands have the same
-- metatable, so `sentinel == literal_value` falls through to
-- raw equality (returns false). That is exactly what we want
-- for `if x.mode == const.SOMETHING then ...` -- the branch
-- is skipped, no spurious work.
--
-- Variants (Session 9 Tier 1):
-- A sentinel may carry a variant config (from sentinel_variants.lua)
-- that perturbs __ipairs / __len / __lt / __le / __index to take
-- branches the baseline variant cannot reach. The variant rides on
-- the sentinel via rawget(self, "__variant__") and is inherited by
-- every child sentinel; once the variant is set at construction the
-- whole subtree of returned sentinels shares it. See sentinel_variants
-- for the variant semantics.

local M = {}

local sv = require("sentinel_variants")

-- Forward declaration so the metatable's __index can recurse.
local make_sentinel

local function variant_from(operands_a, operands_b)
  if type(operands_a) == "table" then
    local v = rawget(operands_a, "__variant__")
    if v then return v end
  end
  if type(operands_b) == "table" then
    local v = rawget(operands_b, "__variant__")
    if v then return v end
  end
  return nil
end

local _mt
_mt = {
  __index = function(self, k)
    local prefix = rawget(self, "__prefix__") or ""
    local log = rawget(self, "__log__")
    local variant = rawget(self, "__variant__")
    local key_repr = type(k) == "string" and k or ("[" .. tostring(k) .. "]")
    local path = (prefix == "") and key_repr or (prefix .. "." .. key_repr)
    if log then log[#log + 1] = path end
    -- Variant: bool-typed fields take a real boolean so `if x.is_complete`
    -- branches deterministically per variant. The field name heuristic in
    -- sentinel_variants.is_bool_like_key keeps coercion to wire fields
    -- the SIF1 codebase actually expresses as booleans -- otherwise
    -- coercing a bool into a non-bool read site would crash the listener
    -- when the read continues (e.g. `is_complete.subfield`).
    if variant and variant.bool ~= nil and sv.is_bool_like_key(k) then
      return variant.bool
    end
    -- Memoize child sentinels per key. Without this, every `cached.foo`
    -- access constructs a fresh sentinel table, and listener guards
    -- like `if last_seen == cached.foo then return end; last_seen = cached.foo`
    -- never short-circuit (raw table inequality between two distinct
    -- sentinels), so the body re-runs and the discovery surface grows
    -- with every notifyUpdate. Memoization makes table identity stable
    -- across re-reads, matching real-game semantics where the response
    -- table doesn't reshape between reads.
    local children = rawget(self, "__children__")
    if not children then
      children = {}
      rawset(self, "__children__", children)
    end
    local existing = children[k]
    if existing ~= nil then return existing end
    local child = make_sentinel(log, path, variant)
    children[k] = child
    return child
  end,
  __newindex = function() end,
  __call = function(self, ...)
    local prefix = rawget(self, "__prefix__") or ""
    local log = rawget(self, "__log__")
    local variant = rawget(self, "__variant__")
    return make_sentinel(log, prefix .. "()", variant)
  end,
  __add = function() return 0 end,
  __sub = function() return 0 end,
  __mul = function() return 0 end,
  __div = function() return 0 end,
  __mod = function() return 0 end,
  __pow = function() return 0 end,
  __unm = function() return 0 end,
  __concat = function(a, b)
    -- One side may be a real string/number; coerce sentinel to "".
    local function s(x) return (type(x) == "string" or type(x) == "number") and tostring(x) or "" end
    return s(a) .. s(b)
  end,
  -- __eq only triggers when both have the same metatable; comparing a
  -- sentinel against any non-sentinel falls back to raw inequality
  -- (returns false), which is the desired behavior for branch guards.
  __eq = function() return false end,
  __lt = function(a, b)
    local v = variant_from(a, b)
    if v then return v.cmp_gt end
    return false
  end,
  __le = function(a, b)
    local v = variant_from(a, b)
    if v then return v.cmp_gt end
    return false
  end,
  __len = function(self)
    local variant = rawget(self, "__variant__")
    if variant then return variant.list_len end
    return 0
  end,
  __tostring = function() return "" end,
  -- Yield 1 phantom sentinel by default (more if the variant requests
  -- a longer list). Without this, listener bodies gated on
  -- `for k,v in pairs(cached.undeclared_field)` never fire and every
  -- per-element read inside is lost. The yielded child sentinel
  -- inherits log/path/variant, so inner reads (`ev.title`,
  -- `ev.unit_id`) land as `parent.[1].title` etc. -- and
  -- merge_observations strips `.[N]` segments so these don't inflate
  -- the final discovered set. Symmetric with __ipairs below, except
  -- __pairs defaults to 1 (always at least one iteration) while
  -- __ipairs respects variant.list_len exclusively (0 by default --
  -- the fuzz tier opts into multi-element iteration explicitly).
  __pairs = function(self)
    local variant = rawget(self, "__variant__")
    local n = (variant and variant.list_len and variant.list_len > 0)
      and variant.list_len or 1
    local prefix = rawget(self, "__prefix__") or ""
    local log = rawget(self, "__log__")
    local i = 0
    return function()
      i = i + 1
      if i > n then return nil end
      local path = prefix == "" and ("[" .. i .. "]") or (prefix .. ".[" .. i .. "]")
      if log then log[#log + 1] = path end
      return i, make_sentinel(log, path, variant)
    end, self, nil
  end,
  __ipairs = function(self)
    local variant = rawget(self, "__variant__")
    local n = (variant and variant.list_len) or 0
    if n <= 0 then
      return function() return nil end, self, 0
    end
    local prefix = rawget(self, "__prefix__") or ""
    local log = rawget(self, "__log__")
    local i = 0
    return function()
      i = i + 1
      if i > n then return nil end
      local path = prefix == "" and ("[" .. i .. "]") or (prefix .. ".[" .. i .. "]")
      if log then log[#log + 1] = path end
      return i, make_sentinel(log, path, variant)
    end, self, 0
  end,
  -- A marker so external code can tell sentinels from spies.
  __sentinel = true,
}

make_sentinel = function(log, prefix, variant)
  local t = {
    __prefix__ = prefix or "",
    __log__ = log,
    __variant__ = variant,
  }
  return setmetatable(t, _mt)
end

M.new = make_sentinel

-- Convenience wrapper: resolve a variant by name (defaulting to baseline)
-- and build a fresh root sentinel for it.
function M.new_with_variant(log, prefix, variant_name)
  return make_sentinel(log, prefix, sv.get(variant_name))
end

M.is_sentinel = function(x)
  if type(x) ~= "table" then return false end
  local mt = getmetatable(x)
  return mt == _mt
end

return M
