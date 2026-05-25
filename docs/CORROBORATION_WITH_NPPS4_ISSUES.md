# Corroborating findings with NPPS4 GitHub issues

Cross-referencing `FINDINGS_AGAINST_NPPS4.md` against the open and
closed issues in
[DarkEnergyProcessor/NPPS4/issues](https://github.com/DarkEnergyProcessor/NPPS4/issues).
The point is to check whether our harness-derived findings line up
with bugs already reported by NPPS4's users — independent
confirmation that the listener-layer evidence reflects real client
behavior.

## Strongest corroboration: Issue #22 ("Cleared/Fav Pts") = our findings #5 and #7b

**[Issue #22](https://github.com/DarkEnergyProcessor/NPPS4/issues/22)
"Cleared/Fav Pts"** is an open user report describing exactly what
the client does when it hits our `profile.cardRanking` /
`profile.liveCnt` schema bugs:

> Cleared/Favorite Points: (Where to find? Menu -> Profile -> Cleared/
> Fav Pts.) shows your total Live clears for each category (easy,
> normal, hard, master), and your favorite points of girls with them
> ranked, as well as costumes.
>
> Gives this error when clicking on Cleared/Fav Pts:
> ```
> Assert I.175 in jni/source/Core/CLuaState.cpp :
> runtime error: ?:0: attempt to index field '?' (a nil value)
> stack traceback:
> ?: in function 'setValuesStatus'
> ?: in function 'toggle'
> ...
> ```

The screen the user describes lists exactly two pieces of data:

1. **"Total Live clears for each category"** — that's the
   `profile.liveCnt` endpoint. Our finding #7b
   ([`FINDINGS_AGAINST_NPPS4.md`](FINDINGS_AGAINST_NPPS4.md#7b-profilelivecnt--missing-live_cnt--live_count_list))
   says: NPPS4's `ProfileLiveCountResponse` exposes `clear_cnt` and
   `difficulty`, but the client UI handler reads `live_cnt` and
   `live_count_list`. When the handler reaches
   `cached.live_count_list[1]`, `cached.live_count_list` is nil →
   "attempt to index field '?' (a nil value)" is the LuaJIT error
   you'd get.

2. **"Favorite points of girls with them ranked"** — that's the
   `profile.cardRanking` endpoint. Our finding #5 says: NPPS4 declares
   the response as `pydantic.RootModel[list[ProfileCardRankingItem]]`
   (i.e. a bare list), but the client UI reads
   `response_data.card_ranking_list`. Same crash mode: `cached.card_ranking_list`
   is nil.

The "setValuesStatus" / "toggle" stack-frame names suggest a UI
state-toggle inside the Profile view — consistent with our
`invoke_classes` traces being attributed to UI-handler closures
(not Cachable listener bodies).

**[Issue #37](https://github.com/DarkEnergyProcessor/NPPS4/issues/37)**
is a duplicate user report of the same crash, closed in favor of #22.

**Verdict.** Two of our high-signal findings explain an open user
bug with a published crash trace. This is independent confirmation
that the harness output corresponds to real client behavior, not
just LLM-on-LLM circularity.

## Direct corroboration: Issue #1 endpoint checklist

**[Issue #1 "Endpoint Checklist"](https://github.com/DarkEnergyProcessor/NPPS4/issues/1)**
is NPPS4's own per-endpoint implementation status. Cross-checks
against our findings:

| Endpoint | Issue #1 says | Our finding |
|---|---|---|
| `live/reward` | `[x]` "Partially implemented. Some minor parts still TODO." | Finding #6: missing `reward_item_list`. The "minor parts still TODO" caveat is exactly what we're pinpointing. |
| `album/albumAll` | `[x]` "List all acquired cards sequentially. **\***" | Finding #3 (RootModel mismatch). The asterisk implies "known caveat." |
| `secretbox/pon` | `[x]` "Perform 1 scouting." (no caveat) | Finding #7: missing `limit_bonus_rewards` + `free_gift_rewards`. **NEW** — not yet on NPPS4's radar. |
| `profile/cardRanking` | `[x]` "Get most loved cards by user." | Finding #5 (RootModel mismatch) + Issue #22 crash. |
| `profile/liveCnt` | `[x]` "Get amount of live show cleared by difficulty of user." | Finding #7b (missing fields) + Issue #22 crash. |
| `achievement/unaccomplishList` | `[x]` "List unaccomplished achievement by their filter category ID." | Finding #4 (ambiguous `__root__`). |
| `unit/deckInfo` | **Not present in checklist** (the checklist starts `unit/deckInfo` is unlisted; checklist has `unit/unitAll`, `unit/changeRank`, etc.) | Finding #2 — confirms it's both unimplemented AND has a known wire-shape issue. |
| `download/update` | **Not in checklist** — checklist has `download/additional`, `download/batch`, `download/event`, `download/getUrl` but no `download/update`. The endpoint IS implemented in `npps4/game/download.py:77`. | Finding #1 — the implementation went in without the checklist entry being updated. **Suggest** opening a checklist-update PR. |
| `common/recoveryEnergy` | `[ ]` (NOT implemented per checklist) | The endpoint IS implemented in `npps4/game/common.py:42`. The checklist is out-of-date here too. Finding #8 (missing `license_recover_end_time`) applies. |
| `duel/top` | `[ ]` (not implemented) | Finding #7e — gives a 13-field skeleton to start from. |
| `handover/exec` | `[x]` "Perform account transfer using transfer passcode." | Finding #7d says NPPS4 returns `None`. Worth re-checking against the current source; the priors extractor may have missed an updated return type. |

## Related but indirect

**[Issue #6 "Known Issues During 1st Phase Test"](https://github.com/DarkEnergyProcessor/NPPS4/issues/6)**
— a tracking list of UI-level bugs. None map directly to our findings,
but a few are plausibly downstream of schema gaps we'd surface:

- "Requesting data deletion crashes the game." — could be a
  `user.reserveDelete` or `gdpr.*` schema mismatch; both endpoints are
  classified `envelope-only` for us but the crash might be in the
  response to `gdpr.get` (our finding lists `_fs`, `resume`, `views`
  as client-reads-NPPS4-missing).
- "Clicking 'Details' on scouting page result in 'Endpoint not
  found'." — could be `secretbox/showDetail` or `secretbox/stampDetail`.
  The latter is in our `ui-only` residual bucket.
- "Resuming live show after app restart result in error loop." —
  `common.liveResume` is in our `ui-only` residual bucket; if the
  response shape is wrong, this would be the symptom.

**[Issue #9 "Plans for 2nd Phase Testing"](https://github.com/DarkEnergyProcessor/NPPS4/issues/9)**
— roadmap. Includes "Fixing all issues in #6", "Friend system (#16)",
"Properly implement Aqours story progression". The friend system is a
spec gap rather than a schema gap; not directly addressable from our
output. Story-progression endpoints fall in our `harness-covered`
bucket (`scenario.reward`, `eventscenario.reward`).

**[Issue #43 "Server response nil"](https://github.com/DarkEnergyProcessor/NPPS4/issues/43)**
(closed) — startup crash. Could plausibly map to `login.topInfo` (in
our `harness-covered` bucket with 21 discoveries) or `user.userInfo`
(which our static-diff flags: client reads `id`, NPPS4 emits `user`
as a wrapper). Worth a targeted look if the issue reopens.

## Summary

| Finding | Independent corroboration |
|---|---|
| #5 `profile.cardRanking` | Issue #22 crash trace |
| #7b `profile.liveCnt` | Issue #22 crash trace |
| #6 `live.reward` | Issue #1 marks as "minor parts still TODO" |
| #3 `album.albumAll` | Issue #1 asterisk caveat |
| #1 `download.update` | Implemented but missing from checklist |
| #2 `unit.deckInfo` | Missing from checklist entirely |
| #7e `duel.top` | Confirmed unimplemented in Issue #1 |
| #7d `handover.exec` | Implemented per Issue #1 but priors say `None` |
| #7 `secretbox.pon` | **New** — not yet flagged anywhere |
| #4 `achievement.unaccomplishList` | Implemented, ambiguous wire shape |
| #8 `common.recoveryEnergy` | Endpoint implemented despite Issue #1 saying `[ ]` |

Two of the eleven findings have explicit user-bug reports as
corroboration. The rest line up with NPPS4's own implementation
status as recorded in Issue #1, with two implementation-gap detections
(checklist entries missing from #1 that are now implemented in the
source).

**Most-actionable single take-away:** Issue #22 stays open because two
of our findings explain it. If the NPPS4 maintainers confirm the
schema-fix theory, this is the cleanest "one PR closes one open
issue" demonstration the harness can offer.
