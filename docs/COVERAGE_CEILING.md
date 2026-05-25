# Coverage ceiling — what the harness can and can't reach

Empirical analysis of the 358 endpoint surface and where the harness
(notifyUpdate pass + invoke_classes UI-handler pass + static
field-extraction pass) hits its ceiling.

## Summary (after Approach D `extract_ui_field_reads` lift)

| Bucket | Count | What it means |
|---|---:|---|
| `harness-covered` | **247** | Listener fired, UI handler invoked, OR static extraction harvested ≥1 field path from a per-call success closure (with optional helper-following one hop into same-file functions). Schema-correctable from harness output. |
| `envelope-only` | **31** | Fire-and-forget acks (`cancel`, `leave`, `skip`, `set`, etc.). `extra="allow"` empty Pydantic stub is correct. |
| `ui-only` | **63** | Static extraction found no anchor (no UI file references the cache_key / fn_name, the file has no `function(arg) ... arg.response_data` pattern, or the destructure flows through patterns the extractor doesn't handle yet — multi-LHS assignments, cross-file helpers). Remaining residual after Approaches B + D. |
| `needs-Frida` | **17** | Neither listener nor UI file references the cache_key/fn_name. State-dependent: matching queues, polling streams, handover token flow, KLab ID sync, download URL signing. |

Each `harness-covered` field path carries a confidence label on
`runtime_listener_observations.json`:

- `runtime_discovered_field_names` — the union (used for wire-compare).
- `static_extracted_verified` — found by static extraction AND a
  runtime listener also reads it (highest confidence).
- `static_extracted_unverified` — found only by static extraction. The
  listener doesn't reach it (the field lives in pure UI code post-
  listener); the static evidence is still source-grounded but
  wire-compare should treat it as lower confidence.

What counts as "discovered": a path the listener read that the schema
(scraper-derived or LLM-synthesized) did not declare. The aggregator
strips `.[N]` indices and shares namespace with declared paths, so
listener reads against already-declared list elements (`events.[1].id`
against a schema that says `events: list[Event]` with `Event.id`)
correctly count as confirmation, not novel discovery.

## The 17 `needs-Frida` endpoints — empirical evidence

These won't be reached by ANY purely-static client analysis. They
require either (a) running the client against a server and capturing
live responses, or (b) Frida-hooking the HTTP layer to dump shapes
under varying user state.

```
arena.matchLiveAutoPlay              — matching/queue state branching
common.checkNgword                   — ngword filter (server-side dict)
download.event                       — DLAPI signed-URL endpoints
download.getUrl
download.luaDownload
duel.livePolling                     — long-poll endpoint
handover.create                      — account-handover token generation
handover.start
klab_id.kidInitialAccomplishedList   — KLab ID achievement state
klab_id.kidPagingUnaccomplishList
klab_id.sync
klab_id.syncActivate
platformAccount.handover             — platform-account handover
platformAccount.isConnectedLlAccount
platformAccount.isConnectedPfAccount
unit.deck
user.showAllItem
```

The bulk are KLab-ID / platform-account / handover endpoints whose
responses depend on an external KLab identity service that does not
exist in our test environment. The rest are pure state-dependent
(matching, polling, signed-URL flows). All are reasonable Frida
companion targets.

## The 132 residual `ui-only` endpoints

These have a registered listener (so they're not envelope-only acks)
and the listener body fired against the spied response, but every read
landed on a field the schema already declared — no novel paths. The
response is presumably unpacked in a UI-handler closure that
`invoke_classes` didn't successfully exercise on those endpoints.

Reaching them needs per-endpoint hand-wiring: find the outer function
that defines the per-call success closure, invoke that function with
whatever sentinel/cached state it expects. Example target —
`m_reward/reward_list.lua:1228-1376` (`RewardList.callApi`):

```lua
-- m_reward/reward_list.lua:1289-1334
L9_2 = L8_1.reward.no_guard.rewardList  -- the svapi binding
function L10_2(A0_3)                     -- the per-call success closure
  local L1_3 = A0_3.response_data
  local L2_3 = L1_3.item_count            -- ← these reads are invisible
  L1_2.item_count = L2_3                  --   to BOTH the cache listener
  ...                                     --   AND the invoke_classes pass
  local L4_3 = L1_3.items                 --   because L10_2 only exists
  for L6_3, L7_3 in ipairs(L4_3) do       --   while L40_1 (=callApi) runs;
    table.insert(L1_2.item_list, L7_3)    --   invoke_classes called other
  end                                     --   methods that don't enter
end                                       --   this branch.
```

Tractable but not done — open question for the NPPS4 collaboration
whether the additional time investment is worth it.

### Approach D — static field-extraction (the actual lift)

`src/tools/extract_ui_field_reads.py` walks every UI file that
references an endpoint's `cache_key` or `fn_name` and harvests field
names off the per-call success closure's response_data anchor:

```
function(A0_3)                    -- the success_cb
  local L0_3 = A0_3.response_data
  local L1_3 = L0_3.item_count    -- harvested: response_data.item_count
  for _, L2_3 in pairs(L0_3.items) do
    L0_3.item_list[#L0_3.item_list + 1] = L2_3.unit_id  -- response_data.items
  end
end
```

Five precision passes keep noise low:

1. **Anchor specificity** — only harvest from inner `function(<arg>) ... end`
   bodies where `<arg>.response_data` is read; co-mingled UI state on
   unrelated variables is ignored.
2. **Single-pass taint with de-taint on reassign** — decompiled
   bytecode reuses local names aggressively. Walking the body line-by-
   line with a taint map that's CLEARED when the local is rebound to
   an untainted RHS prevents bogus `foo.bar.baz` chains from sticking
   to locals after they were overwritten with an unrelated value.
3. **For-loop iteration taint** — `for K, V in pairs(tainted)` taints
   the value var; element-index `.[N]` collapses in the merger so
   `cached.unit_list.[1].id` and `cached.unit_list.id` end up at the
   same path.
4. **Helper-following one hop** — when the closure passes a tainted
   local to a same-file function (with one hop of `<local> = <fn>`
   aliasing to resolve decompiler-inserted indirections), recurse into
   that helper's body using its first arg pre-tainted at the call-site
   prefix. Cycle-break via `visited` set; depth-cap = 2.
5. **Corpus filter** — a field name appearing on >25% of all m_* UI
   files is flagged as a UI-wide token (`appear`, `middle`, `ok`,
   `is_open`) and dropped. Also drops paths containing `response_data`
   as a non-leading segment (artifact of recursing an envelope-shaped
   helper against a subtree arg).

Listener verification (orthogonal layer) re-fires the listener pass
with the harvested fields populated in the candidate response and
cross-checks against the production listener observation. Fields
touched by a listener are tagged `static_extracted_verified` (high
confidence); others are `static_extracted_unverified` (still
source-grounded but listener doesn't reach the read site, which is
expected for ui-only endpoints by definition).

Measured lift:

- **69 endpoints** moved from `ui-only` to `harness-covered` (132 → 63)
- **112 additional unique discovered field paths** surfaced in
  `runtime_listener_observations.json` (81 → 193)

End-to-end pipeline still runs in ~20s (the static-extraction pass
takes ~6s including corpus-frequency precompute).

### What we tried: Approach C (`invoke_stub`) — confirmed dead end

`src/tools/run_invoke_stub.py` + the `dispatch_invoke_stub` path in
`src/harness/harness.lua` drive `svapi.<module>.<action>Stub(...)`
directly and fire the returned `descriptor.on_success` against a spied
envelope. Hypothesis: the per-call success closure lives on the
descriptor, so invoking it bypasses the outer-function discovery
problem that bounds `invoke_classes`.

Probed against all 131 ui-only endpoints in a 2026-05-25 experiment:

- **122/131** return `Stub not found` — those endpoints don't expose
  a `<action>Stub` builder under `svapi.<module>`. Structurally
  unreachable through this path.
- **9/131** have a Stub: `battle.battleInfo`, `concert.livePartyList`,
  `livese.liveseInfo`, `marathon.marathonInfo`,
  `notice.noticeFriendGreeting`, `quest.questInfo`, `reward.rewardHistory`,
  `scenario.scenarioStatus`, `tos.tosCheck`.

For those 9, even after patching the dispatcher to (a) seed
`response_data` from the schema-derived candidate and (b) replace
`user_cb` with a real function that walks each table arg via
`pairs()`+`__index` to depth 3, the only paths logged were top-level
declared fields (e.g. `response_data.tos_id`, `response_data.is_agreed`,
`response_data.party_list.[1]`). **Zero novel discoveries; zero bucket
moves.**

Why this is a ceiling, not a tuning knob: `pairs()` over a spied table
only yields keys that exist in the underlying candidate, so the walk
can confirm declared fields but cannot discover undeclared ones. Novel
discovery requires the destructuring code to ask for a specific
undeclared key by name (e.g. `data.item_count` in the `RewardList`
example above) — and that code lives in per-screen closures inside
the *outer* module function, not on the Stub descriptor.

Net: `invoke_stub` is kept in-tree as a diagnostic but is not wired
into the Makefile / `merge_observations.py`. The shelf-tool's
docstring overstates its reach — kept for the rare case where a future
endpoint's `on_success` does more than envelope plumbing, but not part
of the production pipeline.

## The 247 `harness-covered` endpoints — what we ship

These have either ≥1 discovered field path (a listener read of a field
the schema didn't declare, OR a static-extraction harvest from a UI
file's success closure) or ≥5 distinct accessed keys via the
listener/UI-handler pass. They are the basis for the wire-compare
findings in [`FINDINGS_AGAINST_NPPS4.md`](FINDINGS_AGAINST_NPPS4.md):

- **86 endpoints** in both NPPS4 + harness with disagreement
- **35** client-reads-NPPS4-doesn't-emit (server bug candidates)
- **75** NPPS4-emits-with-no-observed-client-read (no harness evidence
  of a client read — may still be read in UI closures the harness
  didn't exercise; not proof of "dead" fields)

The wire-compare now compares at full path depth (post-Q7 fix):
`event_list.[1].subtitle` versus a NPPS4 `EventV1` that declares
`title` but not `subtitle` correctly surfaces as a nested
disagreement. Previously the comparison collapsed both sides to
top-level field names and hid these.

## Numbers, run-by-run

```
$ make compare-npps4    # runs the full pipeline

# Step 1: notifyUpdate-driven harness
$ python src/tools/run_lua_harness.py --all --out build/runtime/traces
done: 358 ok / 0 err / 358 total in ~2s

# Step 2: aggregate
$ python src/tools/aggregate_listener_observations.py
- Endpoints with at least 1 discovered field: ~70

# Step 3: initial classify (before Approach B)
$ python src/tools/classify_coverage.py
  envelope-only        ~31
  harness-covered      ~177
  needs-Frida          ~17
  ui-only              ~133

# Step 4: invoke_classes Approach B against ui-only bucket
$ python src/tools/run_invoke_classes.py --bucket ui-only
done: ~12 ok / ~120 dud in ~8s

# Step 5: merge invoke_classes traces into observations
$ python src/tools/merge_observations.py
  endpoints with discoveries: ~71
  unique field paths: ~81

# Step 6: static field-extraction over UI source (Approach D)
$ python src/tools/extract_ui_field_reads.py --bucket ui-only
done: 64 endpoints with >=1 kept field, 44 with 0;
      263 fields kept, 54 dropped by corpus filter,
      ~40 verified by listener; ~4s

# Step 7: merge invoke_classes + static traces into observations
$ python src/tools/merge_observations.py
  endpoints with discoveries: 140
  unique field paths: 193

# Step 8: re-classify with merged data (full union of all three passes)
$ python src/tools/classify_coverage.py
  envelope-only        31
  harness-covered      247
  needs-Frida          17
  ui-only              63

# Step 9: wire-compare vs NPPS4
$ python integration/npps4/wire_compare.py --mode static-diff
wrote build/wire_compare_static.md
```

End-to-end pipeline runs in **~20 seconds** (notifyUpdate ~2s,
invoke_classes ~8s, static extraction ~4s, rest ~6s combined).
Reproducible. Deterministic except for non-deterministic `next()`
iteration order in invoke_classes method selection, which can move
±2 endpoints between buckets between runs.
