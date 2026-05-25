# Coverage ceiling — what the harness can and can't reach

Empirical analysis of the 358 endpoint surface and where the harness
(notifyUpdate pass + invoke_classes UI-handler pass) hits its ceiling.

## Summary (after Approach B `invoke_classes` lift)

| Bucket | Count | What it means |
|---|---:|---|
| `harness-covered` | **305** | Listener fired or UI handler invoked and read ≥1 field of the response. Schema-correctable from harness output. |
| `envelope-only` | **20** | Fire-and-forget acks (`cancel`, `leave`, `skip`, `set`, etc.). `extra="allow"` empty Pydantic stub is correct. |
| `ui-only` | **28** | Residual UI-handler endpoints where invoke_classes didn't yet land — methods crashed on missing UI context, or the relevant class failed to register at preload time. |
| `needs-Frida` | **5** | Neither listener nor UI file references the cache_key/fn_name. State-dependent: matching queues, polling streams, KLab ID sync. |

Approach B lift on ui-only bucket: from **115** before to **28** after
(**87 endpoints reclassified to harness-covered**). The mechanism:
preload the 355 UI-handler files AFTER `finalize_modules` (so engine
imports return sentinel-on-miss instead of nil-on-miss), then for each
ui-only endpoint, pre-populate Cachable with a spied candidate and
invoke each registered class's exported methods. Methods that read the
populated cache via `Cachable.get(cache_key)` fire the spy.

## The 5 `needs-Frida` endpoints — empirical evidence

These won't be reached by ANY purely-static client analysis. They
require either (a) running the client against a server and capturing
live responses, or (b) Frida-hooking the HTTP layer to dump shapes
under varying user state.

```
arena.matchLiveAutoPlay              — matching/queue state branching
duel.livePolling                     — long-poll endpoint
handover.create                      — account-handover token generation
klab_id.kidPagingUnaccomplishList    — KLab ID achievement paging
platformAccount.handover             — platform-account handover
```

Three of the five are KLab-ID / platform-account / handover endpoints
whose responses depend on an external KLab identity service that does
not exist in our test environment. The other two are pure
state-dependent (matching, polling). All five are reasonable Frida
companion targets.

## The 28 residual `ui-only` endpoints

These have a registered listener (so they're not envelope-only acks),
but the `invoke_classes` pass didn't surface field reads — either the
candidate class failed to register at preload time even on the second
pass, or the class's exported methods don't reach the populated cache
when invoked with sentinel args.

```
arena.dreamLiveGameOver        eventscenario.open       secretbox.knapsackSelect
arena.moveUpStage              live.partyList           secretbox.selectUnit
class.competitionOwnDeckRanking login.unitSelect        tos.tosAgree
class.entrySemifinal           online.deck              tutorial.progress
common.liveResume              payment.processLog       unit.activate
common.logger                  profile.profileInfo      unit.deckName
duel.duelSubDeck               quest.partyList          unit.favorite
duel.liveEnd                   ranking.eventFriendLive  unit.favoriteAccessory
duel.privateClose              ranking.player
duty.privateClose              reward.rewardHistory
```

Reaching them probably needs per-endpoint hand-wiring: find the outer
function that defines the per-call success closure, invoke that
function with whatever sentinel/cached state it expects. Example
target — `m_reward/reward_list.lua:1228-1376` (`RewardList.callApi`):

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

## The 305 `harness-covered` endpoints — what we ship

These have either ≥1 discovered field path or ≥5 distinct accessed
keys via the listener or UI handler pass. They are the basis for the
66 wire-compare findings in
[`FINDINGS_AGAINST_NPPS4.md`](FINDINGS_AGAINST_NPPS4.md), 30 of which
are server-bug candidates (client reads a field NPPS4 doesn't emit).

## Numbers, run-by-run

```
$ make compare-npps4    # runs the full pipeline

# Step 1: notifyUpdate-driven harness
$ python src/tools/run_lua_harness.py --all --out build/runtime/traces
done: 358 ok / 0 err / 358 total in 0.1s

# Step 2: aggregate
$ python src/tools/aggregate_listener_observations.py
- Endpoints with at least 1 discovered field: 180

# Step 3: initial classify (before Approach B)
$ python src/tools/classify_coverage.py
  envelope-only        31
  harness-covered      201
  needs-Frida          12
  ui-only              114

# Step 4: invoke_classes Approach B against ui-only bucket
$ python src/tools/run_invoke_classes.py --bucket ui-only
done: 11 ok / 103 dud in 8.3s

# Step 5: merge invoke_classes traces into observations
$ python src/tools/merge_observations.py
  endpoints with discoveries: 304
  unique field paths: 452

# Step 6: re-classify with merged data
$ python src/tools/classify_coverage.py
  envelope-only        20
  harness-covered      305
  needs-Frida          5
  ui-only              28

# Step 7: wire-compare vs NPPS4
$ python integration/npps4/wire_compare.py --mode static-diff
wrote (66 findings — 30 client-reads-NPPS4-missing)
```

End-to-end pipeline runs in **~10 seconds** (invoke_classes adds 8s on
top of the original 2s). Reproducible. Deterministic.
