# Findings against NPPS4 (static-diff, JP v9.11 client)

The harness produced **86 endpoints with NPPS4 disagreement** — **35
of them have at least one field the client reads that NPPS4 doesn't
emit (server-bug candidates)** and **75** have at least one field
NPPS4 emits with no observed client read (the harness saw no read of
the field, but its coverage is bounded — a UI-handler closure the
harness didn't exercise could still read them; treat as "unconfirmed
by harness", not "dead"). Below are 12 of the highest-signal findings,
with concrete source citations on both sides. Every "client reads"
claim cites a `:line` reference in the decompiled client tree; every
"NPPS4 emits" claim cites the Pydantic class that ships in NPPS4
main.

The comparison runs at **full path depth**: the priors extractor
recursively expands referenced Pydantic models (e.g. a field typed
`UserInfoData` flattens into the per-field paths
`after_user_info.energy_full_time`, `after_user_info.level`, ...), so
nested disagreements surface rather than collapsing into top-level
agreement.

These numbers reflect both the baseline notifyUpdate-driven discovery
**and** the Approach B `invoke_classes` pass: for the (originally)
~133 ui-only endpoints, the harness invokes their UI-handler class
methods after pre-populating the Cachable cache with a spied
candidate, surfacing field reads inside the UI handler closures.
After the pass, 132 ui-only endpoints remain — most because the
listener body reads only declared fields (so the bucket is correct;
they just don't yield novel discoveries), not because the pass failed.

The full machine-readable diff is in
[`../build/wire_compare_static.md`](../build/wire_compare_static.md).
For independent confirmation against NPPS4's own bug reports, see
[`CORROBORATION_WITH_NPPS4_ISSUES.md`](CORROBORATION_WITH_NPPS4_ISSUES.md)
— findings #5 + #7b together explain
[NPPS4 issue #22 "Cleared/Fav Pts"](https://github.com/DarkEnergyProcessor/NPPS4/issues/22),
an open user-reported crash trace.

## Pattern A: `RootModel[list[X]]` mismatches (Prior 10 in PLAN)

**Pattern.** NPPS4 declares the response as
`pydantic.RootModel[list[X]]` — i.e. the entire response body is a
bare list. The wire actually wraps the list under a named field, and
the client listener destructures via that name. **NPPS4 cannot serve
this endpoint to the real client without changing the response model.**

### 1. `download.update`

- **NPPS4** (`npps4/game/download.py:73`): `class DownloadUpdateResponse(pydantic.RootModel[list[DownloadUpdateInfo]])`
- **Client** (`common/svapi/download.lua:324`): `L8_2.package_list = A3_2`
- **Client also reads** `response_data.url_list` per harness trace
- **Fix:** declare `class DownloadUpdateResponse(BaseModel)` with fields `package_list: list[DownloadUpdateInfo]` and `url_list: list[DownloadUrlInfo]` (or whatever the per-element shape turns out to be — needs Frida).

### 2. `unit.deckInfo`

- **NPPS4** (`npps4/game/unit.py:72`): `class UnitDeckInfoResponse(pydantic.RootModel[list[UnitDeckInfo]])`
- **Client** (`common/svapi/unit.lua:248`): `L5_2.unit_deck_list = A3_2`
- **Client also reads** (via invoke_classes Approach B): `active_deck_index`
- **Fix:** wrap as `BaseModel` with fields `active_deck_index: int` and `unit_deck_list: list[UnitDeckInfo]`.

### 3. `album.albumAll`

- **NPPS4** (`npps4/game/album.py:23`): `class AlbumAllResponse(pydantic.RootModel[list[AlbumInfo]])`
- **Client** (harness trace): `response_data.album_list`
- **Fix:** wrap as `BaseModel` with field `album_list: list[AlbumInfo]`.

### 4. `achievement.unaccomplishList`

- **NPPS4** (`npps4/game/achievement.py`): `RootModel[list[...]]` shape, exposes top-level `achievement_list` + `count`
- **Client** (harness trace): reads `response_data` as a list directly (synthetic `__root__` marker matched), and ALSO destructures wrapped form
- **Verdict:** ambiguous from harness alone — `__root__` may be a sentinel artifact. Worth checking with Frida.

### 5. `profile.cardRanking`

- **NPPS4** (`npps4/game/profile.py`): `ProfileCardRankingResponse` exposes top-level `rank`, `sign_flag`
- **Client** (harness trace): reads `response_data.card_ranking_list`
- **Fix:** wrap NPPS4 response as `card_ranking_list: list[ProfileCardRankingItem]`.

## Pattern B: missing top-level fields

**Pattern.** NPPS4's response class is correctly a `BaseModel`, but
a field the client expects to find is not declared on the class.
Listener body crashes at runtime against NPPS4 today.

### 6. `live.reward` — missing `reward_item_list`

- **NPPS4** (`npps4/game/live.py:289`): `LiveRewardResponse` — no `reward_item_list`
- **Client** (`m_boot/initialize.lua:574,1303,1306`): `L3_3 = A0_3.reward_item_list` (3 distinct call sites)
- **Agreement:** 28 fields including `accomplished_achievement_list`, `added_achievement_list`, `daily_reward_info`, `effort_point`, `event_info`, `limited_effort_box`, etc.
- **Fix:** add `reward_item_list: list[LiveRewardItem]` to `LiveRewardResponse`. The element shape needs Frida or scraper evidence.

### 7. `secretbox.pon` — missing `limit_bonus_rewards` + `free_gift_rewards`

- **NPPS4** (`npps4/game/secretbox.py:49`): `SecretboxPonResponse` — neither field declared
- **Client** (`common/unit/unit_deck_cache_listener.lua:272,274,278`): `L2_3 = A0_3.limit_bonus_rewards`, with iteration over `.items` underneath
- **Client also reads** (invoke_classes pass): `free_gift_rewards`
- **Client field shape from harness trace:** `limit_bonus_rewards.items.{accessory_owning_user_id, ...}` — i.e. `limit_bonus_rewards: {items: list[Item]}`
- **Fix:** add `limit_bonus_rewards: SecretboxLimitBonusRewards` with `items: list[ItemInfo]`, and `free_gift_rewards: SecretboxFreeGiftRewards`.

### 8. `common.recoveryEnergy` — missing `license_recover_end_time`

- **NPPS4** (`npps4/game/common.py:21`): `CommonRecoveryEnergyResponse` — no `license_recover_end_time`
- **Client** (`common/energy.lua:89, 107, 114`): `L1_2 = L0_2.license_recover_end_time` (3 call sites)
- **Note:** this same field is read on `after_user_info` blocks across many endpoints — the fix is on `UserDiffMixin` or `UserInfo`, not on `CommonRecoveryEnergyResponse` directly.
- **Fix:** add `license_recover_end_time: int | None` to `UserInfo` / `UserDiffMixin`. One change → multiple endpoints fixed.

## Pattern B (cont.) — new findings from invoke_classes

These were discovered ONLY by the Approach B `invoke_classes` pass —
i.e. they live in UI-handler closures, not Cachable listener bodies.

### 7b. `profile.liveCnt` — missing `live_cnt` + `live_count_list`

- **NPPS4** (`npps4/game/profile.py`): `ProfileLiveCountResponse` exposes `clear_cnt`, `difficulty`
- **Client** (invoke_classes pass): reads `live_cnt`, `live_count_list`
- **Fix:** add `live_cnt: int | None` and `live_count_list: list[LiveCountInfo]` to the response.

### 7c. `challenge.challengeInfo` — missing top-level wrapper

- **NPPS4** (`npps4/game/challenge.py`): `ChallengeInfoResponse` declares `root`
- **Client** (invoke_classes pass): reads `base_info`, `challenge_info` at top level
- **Fix:** add `base_info: ChallengeBaseInfo` and `challenge_info: ChallengeInfoData` to the response (the `root` field NPPS4 emits is probably never read).

### 7d. `handover.exec` — missing entire response

- **NPPS4** (`npps4/game/handover.py`): handler returns `None` — no response model declared
- **Client** (invoke_classes pass): reads `session_key`, `user_id`
- **Fix:** add `class HandoverExecResponse(BaseModel): session_key: str; user_id: int` and return it.

### 7e. `duel.top` — 13 newly-discovered fields

- **Client** (invoke_classes pass): reads `asset_bgm_id`, `buff_is_enabled`, `difficulty_list`, `duel_id`, `duel_user_info`, `fever_skill_list[].duel_fever_skill_id`, `fever_skill_list[].level`, etc.
- **NPPS4:** `duel.top` not yet implemented in main.
- **Suggested approach:** start with these 13 field names + the nested `fever_skill_list[]` shape as the response model skeleton.

## Tradeoff disclosure

These findings come from listener + invoke_classes evidence on JP
v9.11 client. **They are NOT comprehensive**:

- The harness exercises listeners with **synthetic candidate responses**
  — listeners that gate on `if x.some_flag then ... read fields ... end`
  will only fire the read-path if `some_flag` is truthy in our candidate.
  The variant-aware sentinel covers some of this (`baseline`, `list_one`,
  `true_bool`, `false_bool` variants), but not all.
- The invoke_classes pass invokes up to 16 methods per class (200 per
  endpoint) of every UI class that references each endpoint, with
  sentinel args. Methods that need real UI state (e.g. a scene with a
  node tree) crash on the first unstubbed call — pcall absorbs them,
  but their field reads are lost. The 132 residual `ui-only` endpoints
  partly fell into this gap; the rest had listener bodies that
  successfully fired but only read schema-declared fields, yielding no
  novel discoveries (so they show up as ui-only-but-confirmed rather
  than ui-only-blocked).
- The harness was last run against JP v9.11 client. If your reference
  is a different client version, re-run the harness against your tree
  before trusting any specific field claim.

## Reproducing these findings

```bash
git clone https://github.com/furidosu/sif1-harness
cd sif1-harness
ln -sfn /path/to/your/decompiled-client/all assets/decompiled/all
git clone --depth 1 https://github.com/DarkEnergyProcessor/NPPS4 ./npps4
git clone --depth 1 https://github.com/DarkEnergyProcessor/NPPS4-DLAPI ./npps4-dlapi
make compare-npps4
```

Whole pipeline: **~20 seconds**. Output: `build/wire_compare_static.md`
with all 86 findings (35 client-reads-NPPS4-missing, 75 NPPS4-emits
with no observed client read).
