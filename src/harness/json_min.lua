-- json_min.lua
-- Minimal pure-Lua JSON encode/decode for Lua 5.1 / LuaJIT.
-- Not a fast or fully RFC-compliant implementation; just enough to
-- round-trip the harness's job-in / trace-out blobs.

local M = {}

-- ---- encode ---------------------------------------------------------------
local encode_value

local function encode_string(s)
  local out = {'"'}
  for i = 1, #s do
    local b = s:byte(i)
    if b == 34 then out[#out + 1] = '\\"'
    elseif b == 92 then out[#out + 1] = '\\\\'
    elseif b == 8 then out[#out + 1] = '\\b'
    elseif b == 9 then out[#out + 1] = '\\t'
    elseif b == 10 then out[#out + 1] = '\\n'
    elseif b == 12 then out[#out + 1] = '\\f'
    elseif b == 13 then out[#out + 1] = '\\r'
    elseif b < 32 then
      out[#out + 1] = string.format("\\u%04x", b)
    else
      out[#out + 1] = string.char(b)
    end
  end
  out[#out + 1] = '"'
  return table.concat(out)
end

local function is_array(t)
  local n = 0
  for k, _ in pairs(t) do
    if type(k) ~= "number" then return false, 0 end
    if k > n then n = k end
  end
  -- Treat tables that have integer keys 1..n with no gaps as arrays.
  for i = 1, n do
    if t[i] == nil then return false, 0 end
  end
  return true, n
end

local function encode_table(t)
  if next(t) == nil then return "[]" end  -- empty -> array by convention
  local arr, n = is_array(t)
  if arr then
    local parts = {}
    for i = 1, n do
      parts[i] = encode_value(t[i])
    end
    return "[" .. table.concat(parts, ",") .. "]"
  end
  local parts = {}
  for k, v in pairs(t) do
    if type(k) == "string" then
      parts[#parts + 1] = encode_string(k) .. ":" .. encode_value(v)
    else
      parts[#parts + 1] = encode_string(tostring(k)) .. ":" .. encode_value(v)
    end
  end
  return "{" .. table.concat(parts, ",") .. "}"
end

encode_value = function(v)
  local tv = type(v)
  if v == nil then return "null"
  elseif tv == "boolean" then return v and "true" or "false"
  elseif tv == "number" then
    if v ~= v then return "null" end             -- NaN
    if v == math.huge or v == -math.huge then return "null" end
    if v % 1 == 0 and v > -1e15 and v < 1e15 then
      return string.format("%d", v)
    end
    return string.format("%.17g", v)
  elseif tv == "string" then return encode_string(v)
  elseif tv == "table" then return encode_table(v)
  else return "null" end
end

M.encode = encode_value

-- ---- decode ---------------------------------------------------------------
local decode_value

local function skip_ws(s, i)
  while i <= #s do
    local c = s:byte(i)
    if c == 32 or c == 9 or c == 10 or c == 13 then i = i + 1
    else return i end
  end
  return i
end

local function decode_string(s, i)
  -- s[i] == '"'
  local out = {}
  i = i + 1
  while i <= #s do
    local c = s:byte(i)
    if c == 34 then return table.concat(out), i + 1 end
    if c == 92 then
      local n = s:byte(i + 1)
      if n == 34 then out[#out + 1] = '"' i = i + 2
      elseif n == 92 then out[#out + 1] = "\\" i = i + 2
      elseif n == 47 then out[#out + 1] = "/" i = i + 2
      elseif n == 98 then out[#out + 1] = "\b" i = i + 2
      elseif n == 102 then out[#out + 1] = "\f" i = i + 2
      elseif n == 110 then out[#out + 1] = "\n" i = i + 2
      elseif n == 114 then out[#out + 1] = "\r" i = i + 2
      elseif n == 116 then out[#out + 1] = "\t" i = i + 2
      elseif n == 117 then
        local hex = s:sub(i + 2, i + 5)
        local code = tonumber(hex, 16) or 0
        if code < 0x80 then
          out[#out + 1] = string.char(code)
        elseif code < 0x800 then
          out[#out + 1] = string.char(0xC0 + math.floor(code / 0x40),
                                      0x80 + (code % 0x40))
        else
          out[#out + 1] = string.char(0xE0 + math.floor(code / 0x1000),
                                      0x80 + math.floor(code / 0x40) % 0x40,
                                      0x80 + (code % 0x40))
        end
        i = i + 6
      else
        error("bad escape at " .. i)
      end
    else
      out[#out + 1] = string.char(c)
      i = i + 1
    end
  end
  error("unterminated string")
end

local function decode_number(s, i)
  local j = i
  while j <= #s do
    local c = s:byte(j)
    if (c >= 48 and c <= 57) or c == 45 or c == 43 or c == 46
       or c == 101 or c == 69 then
      j = j + 1
    else break end
  end
  local n = tonumber(s:sub(i, j - 1))
  return n, j
end

local function decode_array(s, i)
  local arr = {}
  i = skip_ws(s, i + 1)
  if s:byte(i) == 93 then return arr, i + 1 end
  while true do
    local v
    v, i = decode_value(s, i)
    arr[#arr + 1] = v
    i = skip_ws(s, i)
    local c = s:byte(i)
    if c == 44 then i = skip_ws(s, i + 1)
    elseif c == 93 then return arr, i + 1
    else error("bad array sep at " .. i) end
  end
end

local function decode_object(s, i)
  local obj = {}
  i = skip_ws(s, i + 1)
  if s:byte(i) == 125 then return obj, i + 1 end
  while true do
    if s:byte(i) ~= 34 then error("expected string key at " .. i) end
    local k
    k, i = decode_string(s, i)
    i = skip_ws(s, i)
    if s:byte(i) ~= 58 then error("expected ':' at " .. i) end
    i = skip_ws(s, i + 1)
    local v
    v, i = decode_value(s, i)
    obj[k] = v
    i = skip_ws(s, i)
    local c = s:byte(i)
    if c == 44 then i = skip_ws(s, i + 1)
    elseif c == 125 then return obj, i + 1
    else error("bad object sep at " .. i) end
  end
end

decode_value = function(s, i)
  i = skip_ws(s, i)
  local c = s:byte(i)
  if c == 123 then return decode_object(s, i)
  elseif c == 91 then return decode_array(s, i)
  elseif c == 34 then return decode_string(s, i)
  elseif c == 116 and s:sub(i, i + 3) == "true" then return true, i + 4
  elseif c == 102 and s:sub(i, i + 4) == "false" then return false, i + 5
  elseif c == 110 and s:sub(i, i + 3) == "null" then return nil, i + 4
  else return decode_number(s, i) end
end

function M.decode(s)
  local v, _ = decode_value(s, 1)
  return v
end

return M
