-- sentinel_variants.lua
-- Session 9 Tier 1: variant config table consumed by sentinel.lua.
--
-- Each variant perturbs a small set of sentinel return semantics so a
-- listener that gates discovery behind a conditional branch (ipairs,
-- numeric comparison, bool-flag check, length check) can be coerced
-- to take the branch and reveal nested field reads.
--
-- Variants are run once per endpoint by run_lua_harness_fuzz.py. Per-run
-- traces are written under build/runtime/traces_fuzz/ and aggregated by
-- aggregate_fuzz_observations.py into runtime_fuzz_observations.json.
--
-- HOW EACH VARIANT IS DESIGNED TO MOVE THE NEEDLE:
--
--   baseline   -- same shape as the V5 listener-driven trace; included so
--                fuzz-only paths are easy to isolate.
--   list_one   -- __ipairs / __len yield exactly 1 phantom element so
--                guards like `if #x.list > 0 then read x.list[1].y end`
--                fire on a sentinel-backed list.
--   list_many  -- 3 phantoms, exposes branches gated on `>= 2` or `> 1`
--                and gives listeners that bind multiple iterator vars
--                (`for i, v in ipairs ... if i == 2 then ...`) a chance.
--   true_bool  -- __index returns `true` for keys that look bool-shaped
--                (`is_*`, `has_*`, `*_flag`, `*_enabled`, `*_visible`)
--                so positive-branch listener bodies run.
--   false_bool -- same heuristic but returns `false` so negative-branch
--                bodies run (e.g. `if not x.is_admin then ... end`).
--   cmp_gt     -- sentinel < x returns true so a listener doing
--                `if x.score > threshold then ...` falls into the gated
--                branch.
--
-- The bool variants are best-effort: a listener that does
-- `cached.is_enabled.subfield` will crash mid-listener under either bool
-- variant (true/false has no __index). pcall absorbs the crash and the
-- spy still logged response_data.is_enabled before it. Net: bool
-- variants reveal positive- and negative-branch reads where the listener
-- gates on a bool literal and doesn't deep-traverse the same key.

local M = {}

local VARIANTS = {
  baseline   = { name = "baseline",   list_len = 0, bool = nil,   cmp_gt = false },
  list_one   = { name = "list_one",   list_len = 1, bool = nil,   cmp_gt = false },
  list_many  = { name = "list_many",  list_len = 3, bool = nil,   cmp_gt = false },
  true_bool  = { name = "true_bool",  list_len = 0, bool = true,  cmp_gt = false },
  false_bool = { name = "false_bool", list_len = 0, bool = false, cmp_gt = false },
  cmp_gt     = { name = "cmp_gt",     list_len = 0, bool = nil,   cmp_gt = true  },
}

M.VARIANTS = VARIANTS

-- Ordered for reproducible iteration in callers (Lua pairs order is undefined).
M.ORDER = { "baseline", "list_one", "list_many", "true_bool", "false_bool", "cmp_gt" }

function M.get(name)
  if not name or name == "" then return VARIANTS.baseline end
  return VARIANTS[name] or VARIANTS.baseline
end

-- A field name *looks* bool-shaped when matched by one of these patterns.
-- Used by sentinel.lua's __index when variant.bool ~= nil. Conservative
-- by design: matching too broadly would coerce non-bool reads into bools
-- and crash listeners on `.subfield` access; the patterns below are the
-- standard SIF1 wire conventions (is_complete, has_award, auto_flag,
-- ok_visible, ok_enabled).
function M.is_bool_like_key(k)
  if type(k) ~= "string" then return false end
  return k:match("^is_") ~= nil
      or k:match("^has_") ~= nil
      or k:match("^can_") ~= nil
      or k:match("_flag$") ~= nil
      or k:match("_enabled$") ~= nil
      or k:match("_visible$") ~= nil
end

return M
