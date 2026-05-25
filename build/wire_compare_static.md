# NPPS4 ↔ SIF1 client wire-compare (static-diff)

Compares NPPS4 Pydantic response model fields (top-level) against fields the SIF1 client listener layer reads. **Listener evidence is empirical**; NPPS4 type declarations are informative-only per PLAN Prior 10 (9 of 11 RootModel[list[X]] cases contradict listener evidence).

- Endpoints compared (in both NPPS4 + harness): **66**
- Endpoints where client reads field NPPS4 doesn't emit: **30** (server bug candidates)
- Endpoints where NPPS4 emits field client never reads: **55** (dead field / over-fetch candidates)

## Per-endpoint findings

### `gdpr.get`
- NPPS4 source: `npps4/game/gdpr.py` (return type: `GDPRGetResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `_fs`
  - `resume`
  - `views`
- **NPPS4 emits, client never reads:**
  - `server_timestamp`
- Agreement on 2 field(s): `enable_gdpr`, `is_eea`

### `unit.deckInfo`
- NPPS4 source: `npps4/game/unit.py` (return type: `UnitDeckInfoResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `active_deck_index`
  - `unit_deck_list`
- **NPPS4 emits, client never reads:**
  - `deck_name`
  - `main_flag`
  - `unit_deck_id`
  - `unit_owning_user_ids`

### `download.update`
- NPPS4 source: `npps4/game/download.py` (return type: `DownloadUpdateResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `package_list`
  - `url_list`
- **NPPS4 emits, client never reads:**
  - `size`
  - `url`
  - `version`

### `secretbox.pon`
- NPPS4 source: `npps4/game/secretbox.py` (return type: `SecretboxPonResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `free_gift_rewards`
  - `limit_bonus_rewards`
- **NPPS4 emits, client never reads:**
  - `museum_info`
  - `present_cnt`
  - `server_timestamp`
- Agreement on 16 field(s): `accomplished_achievement_list`, `added_achievement_list`, `after_user_info`, `before_user_info`, `button_list`, `gauge_info`...

### `profile.liveCnt`
- NPPS4 source: `npps4/game/profile.py` (return type: `ProfileLiveCountResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `live_cnt`
  - `live_count_list`
- **NPPS4 emits, client never reads:**
  - `clear_cnt`
  - `difficulty`

### `challenge.challengeInfo`
- NPPS4 source: `npps4/game/challenge.py` (return type: `ChallengeInfoResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `base_info`
  - `challenge_info`
- **NPPS4 emits, client never reads:**
  - `root`

### `handover.exec`
- NPPS4 source: `npps4/game/handover.py` (return type: `None`)
- **Client reads, NPPS4 doesn't emit:**
  - `session_key`
  - `user_id`

### `album.albumAll`
- NPPS4 source: `npps4/game/album.py` (return type: `AlbumAllResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `album_list`
- **NPPS4 emits, client never reads:**
  - `all_max_flag`
  - `favorite_point`
  - `highest_love_per_unit`
  - `love_max_flag`
  - `rank_level_max_flag`
  - `rank_max_flag`
  - `sign_flag`
  - `total_love`
  - `unit_id`

### `common.recoveryEnergy`
- NPPS4 source: `npps4/game/common.py` (return type: `CommonRecoveryEnergyResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `license_recover_end_time`
- **NPPS4 emits, client never reads:**
  - `before_game_coin`
  - `before_sns_coin`
  - `energy_max`
  - `present_cnt`
  - `server_timestamp`
  - `training_energy_max`
- Agreement on 6 field(s): `after_game_coin`, `after_sns_coin`, `energy_full_time`, `item_list`, `over_max_energy`, `training_energy`

### `achievement.unaccomplishList`
- NPPS4 source: `npps4/game/achievement.py` (return type: `AchievementUnaccomplishedResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `__root__`
- **NPPS4 emits, client never reads:**
  - `achievement_list`
  - `count`
  - `filter_category_id`
  - `is_last`

### `profile.cardRanking`
- NPPS4 source: `npps4/game/profile.py` (return type: `ProfileCardRankingResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `card_ranking_list`
- **NPPS4 emits, client never reads:**
  - `rank`
  - `sign_flag`
  - `total_love`
  - `unit_id`

### `live.reward`
- NPPS4 source: `npps4/game/live.py` (return type: `LiveRewardResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `reward_item_list`
- **NPPS4 emits, client never reads:**
  - `museum_info`
  - `present_cnt`
  - `server_timestamp`
- Agreement on 28 field(s): `accomplished_achievement_list`, `added_achievement_list`, `after_user_info`, `base_reward_info`, `before_user_info`, `can_send_friend_request`...

### `user.userInfo`
- NPPS4 source: `npps4/game/user.py` (return type: `UserInfoResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `id`
- **NPPS4 emits, client never reads:**
  - `ad_status`
  - `birth`
  - `server_timestamp`
- Agreement on 1 field(s): `user`

### `album.seriesAll`
- NPPS4 source: `npps4/game/album.py` (return type: `AlbumSeriesAllResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `album_series_list`
- **NPPS4 emits, client never reads:**
  - `series_id`
  - `unit_list`

### `download.event`
- NPPS4 source: `npps4/game/download.py` (return type: `DownloadCommonResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `url_list`
- **NPPS4 emits, client never reads:**
  - `size`
  - `url`

### `download.additional`
- NPPS4 source: `npps4/game/download.py` (return type: `DownloadCommonResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `url_list`
- **NPPS4 emits, client never reads:**
  - `url`
- Agreement on 1 field(s): `size`

### `download.batch`
- NPPS4 source: `npps4/game/download.py` (return type: `DownloadCommonResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `url_list`
- **NPPS4 emits, client never reads:**
  - `url`
- Agreement on 1 field(s): `size`

### `handover.kidCheck`
- NPPS4 source: `npps4/game/handover.py` (return type: `KIDCheckResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `status_code`
- **NPPS4 emits, client never reads:**
  - `server_timestamp`
- Agreement on 1 field(s): `user_info`

### `handover.kidInfo`
- NPPS4 source: `npps4/game/handover.py` (return type: `KIDInfoResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `status_code`
- **NPPS4 emits, client never reads:**
  - `server_timestamp`
- Agreement on 1 field(s): `auth_url`

### `marathon.marathonInfo`
- NPPS4 source: `npps4/game/marathon.py` (return type: `MarathonInfoResponse`)
- **Client reads, NPPS4 doesn't emit:**
  - `marathon_info_list`
- **NPPS4 emits, client never reads:**
  - `root`

### `ad.changeAd`
- NPPS4 source: `npps4/game/ad.py` (return type: `None`)
- **Client reads, NPPS4 doesn't emit:**
  - `ad_status`

### `handover.kidHandover`
- NPPS4 source: `npps4/game/handover.py` (return type: `None`)
- **Client reads, NPPS4 doesn't emit:**
  - `result`

### `handover.kidRegister`
- NPPS4 source: `npps4/game/handover.py` (return type: `None`)
- **Client reads, NPPS4 doesn't emit:**
  - `status_code`

### `live.gameover`
- NPPS4 source: `npps4/game/live.py` (return type: `None`)
- **Client reads, NPPS4 doesn't emit:**
  - `result`

### `profile.profileRegister`
- NPPS4 source: `npps4/game/profile.py` (return type: `None`)
- **Client reads, NPPS4 doesn't emit:**
  - `after_user_info`

### `unit.deck`
- NPPS4 source: `npps4/game/unit.py` (return type: `None`)
- **Client reads, NPPS4 doesn't emit:**
  - `unit_deck_list`

### `unit.removableSkillEquipment`
- NPPS4 source: `npps4/game/unit.py` (return type: `None`)
- **Client reads, NPPS4 doesn't emit:**
  - `after_user_info`

### `unit.setDisplayRank`
- NPPS4 source: `npps4/game/unit.py` (return type: `None`)
- **Client reads, NPPS4 doesn't emit:**
  - `after`

### `user.changeNavi`
- NPPS4 source: `npps4/game/user.py` (return type: `None`)
- **Client reads, NPPS4 doesn't emit:**
  - `result`

### `user.setNotificationToken`
- NPPS4 source: `npps4/game/user.py` (return type: `None`)
- **Client reads, NPPS4 doesn't emit:**
  - `result`

### `reward.openAll`
- NPPS4 source: `npps4/game/reward.py` (return type: `RewardOpenAllResponse`)
- **NPPS4 emits, client never reads:**
  - `accomplished_achievement_list`
  - `added_achievement_list`
  - `after_user_info`
  - `before_user_info`
  - `museum_info`
  - `new_achievement_cnt`
  - `order`
  - `present_cnt`
  - `reward_num`
  - `server_timestamp`
  - `unaccomplished_achievement_cnt`
  - `upper_limit`
- Agreement on 4 field(s): `class_system`, `opened_num`, `reward_item_list`, `total_num`

### `unit.merge`
- NPPS4 source: `npps4/game/unit.py` (return type: `UnitMergeResponse`)
- **NPPS4 emits, client never reads:**
  - `accomplished_achievement_list`
  - `added_achievement_list`
  - `before`
  - `before_user_info`
  - `bonus_value`
  - `evolution_bonus_type`
  - `museum_info`
  - `new_achievement_cnt`
  - `present_cnt`
  - `server_timestamp`
  - `unaccomplished_achievement_cnt`
  - `use_game_coin`
- Agreement on 6 field(s): `after`, `after_user_info`, `get_exchange_point_list`, `unit_removable_skill`, `unlocked_multi_unit_scenario_ids`, `unlocked_subscenario_ids`

### `unit.rankUp`
- NPPS4 source: `npps4/game/unit.py` (return type: `UnitRankUpResponse`)
- **NPPS4 emits, client never reads:**
  - `accomplished_achievement_list`
  - `added_achievement_list`
  - `before`
  - `before_user_info`
  - `museum_info`
  - `new_achievement_cnt`
  - `present_cnt`
  - `server_timestamp`
  - `unaccomplished_achievement_cnt`
  - `unlocked_subscenario_ids`
  - `use_game_coin`
- Agreement on 4 field(s): `after`, `after_user_info`, `get_exchange_point_list`, `unit_removable_skill`

### `achievement.rewardOpenAll`
- NPPS4 source: `npps4/game/achievement.py` (return type: `AchievementRewardOpenAllResponse`)
- **NPPS4 emits, client never reads:**
  - `accomplished_achievement_list`
  - `added_achievement_list`
  - `before_user_info`
  - `new_achievement_cnt`
  - `opened_num`
  - `present_cnt`
  - `server_timestamp`
  - `unaccomplished_achievement_cnt`
  - `unit_support_list`
- Agreement on 3 field(s): `after_user_info`, `is_last`, `reward_item_list`

### `profile.profileInfo`
- NPPS4 source: `npps4/game/profile.py` (return type: `ProfileInfoResponse`)
- **NPPS4 emits, client never reads:**
  - `center_unit_info`
  - `friend_status`
  - `is_alliance`
  - `navi_unit_info`
  - `setting_award_id`
  - `setting_background_id`
  - `user_info`

### `achievement.rewardOpen`
- NPPS4 source: `npps4/game/achievement.py` (return type: `AchievementRewardOpenResponse`)
- **NPPS4 emits, client never reads:**
  - `before_user_info`
  - `new_achievement_cnt`
  - `present_cnt`
  - `server_timestamp`
  - `unaccomplished_achievement_cnt`
  - `unit_support_list`
- Agreement on 4 field(s): `accomplished_achievement_list`, `added_achievement_list`, `after_user_info`, `reward_item_list`

### `ranking.player`
- NPPS4 source: `npps4/game/ranking.py` (return type: `RankingResponse`)
- **NPPS4 emits, client never reads:**
  - `items`
  - `page`
  - `present_cnt`
  - `rank`
  - `server_timestamp`
  - `total_cnt`

### `live.partyList`
- NPPS4 source: `npps4/game/live.py` (return type: `LivePartyListResponse`)
- **NPPS4 emits, client never reads:**
  - `party_list`
  - `server_timestamp`
  - `training_energy`
  - `training_energy_max`

### `personalnotice.get`
- NPPS4 source: `npps4/game/personalnotice.py` (return type: `PersonalNoticeGetResponse`)
- **NPPS4 emits, client never reads:**
  - `contents`
  - `notice_id`
  - `title`
  - `type`
- Agreement on 1 field(s): `has_notice`

### `reward.rewardHistory`
- NPPS4 source: `npps4/game/reward.py` (return type: `RewardHistoryResponse`)
- **NPPS4 emits, client never reads:**
  - `ad_info`
  - `history`
  - `item_count`
  - `limit`

### `unit.sale`
- NPPS4 source: `npps4/game/unit.py` (return type: `UnitSaleResponse`)
- **NPPS4 emits, client never reads:**
  - `before_user_info`
  - `detail`
  - `server_timestamp`
  - `total`
- Agreement on 4 field(s): `after_user_info`, `get_exchange_point_list`, `reward_box_flag`, `unit_removable_skill`

### `lbonus.execute`
- NPPS4 source: `npps4/game/lbonus.py` (return type: `LoginBonusResponse`)
- **NPPS4 emits, client never reads:**
  - `museum_info`
  - `present_cnt`
  - `server_timestamp`
- Agreement on 14 field(s): `accomplished_achievement_list`, `ad_info`, `added_achievement_list`, `after_user_info`, `calendar_info`, `class_system`...

### `scenario.reward`
- NPPS4 source: `npps4/game/scenario.py` (return type: `ScenarioRewardResponse`)
- **NPPS4 emits, client never reads:**
  - `museum_info`
  - `present_cnt`
  - `server_timestamp`
- Agreement on 10 field(s): `accomplished_achievement_list`, `added_achievement_list`, `after_user_info`, `before_user_info`, `class_system`, `clear_scenario`...

### `subscenario.reward`
- NPPS4 source: `npps4/game/subscenario.py` (return type: `SubScenarioRewardResponse`)
- **NPPS4 emits, client never reads:**
  - `museum_info`
  - `present_cnt`
  - `server_timestamp`
- Agreement on 8 field(s): `after_user_info`, `base_reward_info`, `before_user_info`, `class_system`, `clear_subscenario`, `item_reward_info`...

### `unit.exchangePointRankUp`
- NPPS4 source: `npps4/game/unit.py` (return type: `UnitExchangeRankUpResponse`)
- **NPPS4 emits, client never reads:**
  - `museum_info`
  - `present_cnt`
  - `server_timestamp`
- Agreement on 9 field(s): `accomplished_achievement_list`, `added_achievement_list`, `after`, `after_exchange_point`, `after_user_info`, `before`...

### `announce.checkState`
- NPPS4 source: `npps4/game/announce.py` (return type: `AnnounceStateResponse`)
- **NPPS4 emits, client never reads:**
  - `present_cnt`
  - `server_timestamp`
- Agreement on 1 field(s): `has_unread_announce`

### `exchange.usePoint`
- NPPS4 source: `npps4/game/exchange.py` (return type: `ExchangeUsePointResponse`)
- **NPPS4 emits, client never reads:**
  - `present_cnt`
  - `server_timestamp`
- Agreement on 3 field(s): `after_user_info`, `before_user_info`, `exchange_reward`

### `login.topInfo`
- NPPS4 source: `npps4/game/login.py` (return type: `TopInfoResponse`)
- **NPPS4 emits, client never reads:**
  - `present_cnt`
  - `server_timestamp`
- Agreement on 19 field(s): `ad_flag`, `exchange_badge_cnt`, `friend_action_cnt`, `friend_greet_cnt`, `friend_new_cnt`, `friend_variety_cnt`...

### `ranking.live`
- NPPS4 source: `npps4/game/ranking.py` (return type: `RankingResponse`)
- **NPPS4 emits, client never reads:**
  - `present_cnt`
  - `server_timestamp`
- Agreement on 4 field(s): `items`, `page`, `rank`, `total_cnt`

### `reward.rewardList`
- NPPS4 source: `npps4/game/reward.py` (return type: `RewardListResponse`)
- **NPPS4 emits, client never reads:**
  - `limit`
  - `order`
- Agreement on 3 field(s): `ad_info`, `item_count`, `items`

### `tos.tosCheck`
- NPPS4 source: `npps4/game/tos.py` (return type: `TOSCheckResponse`)
- **NPPS4 emits, client never reads:**
  - `server_timestamp`
  - `tos_type`
- Agreement on 2 field(s): `is_agreed`, `tos_id`

### `unit.removableSkillSell`
- NPPS4 source: `npps4/game/unit.py` (return type: `UnitRemovableSkillSellResponse`)
- **NPPS4 emits, client never reads:**
  - `reward_box_flag`
  - `total`
- Agreement on 1 field(s): `after_user_info`

### `achievement.initialAccomplishedList`
- NPPS4 source: `npps4/game/achievement.py` (return type: `AchievementUnaccomplishedResponse`)
- **NPPS4 emits, client never reads:**
  - `filter_category_id`
- Agreement on 3 field(s): `achievement_list`, `count`, `is_last`

### `event.eventList`
- NPPS4 source: `npps4/game/event.py` (return type: `EventListResponse`)
- **NPPS4 emits, client never reads:**
  - `server_timestamp`
- Agreement on 1 field(s): `target_list`

### `friend.list`
- NPPS4 source: `npps4/game/friend.py` (return type: `FriendListResponse`)
- **NPPS4 emits, client never reads:**
  - `server_timestamp`
- Agreement on 3 field(s): `friend_list`, `item_count`, `new_friend_list`

### `friend.search`
- NPPS4 source: `npps4/game/friend.py` (return type: `FriendSearchResponse`)
- **NPPS4 emits, client never reads:**
  - `server_timestamp`
- Agreement on 5 field(s): `center_unit_info`, `friend_status`, `is_alliance`, `setting_award_id`, `user_info`

### `handover.kidStatus`
- NPPS4 source: `npps4/game/handover.py` (return type: `KIDStatusResponse`)
- **NPPS4 emits, client never reads:**
  - `server_timestamp`
- Agreement on 1 field(s): `has_klab_id`

### `live.play`
- NPPS4 source: `npps4/game/live.py` (return type: `LivePlayResponse`)
- **NPPS4 emits, client never reads:**
  - `server_timestamp`
- Agreement on 10 field(s): `auto_play`, `available_live_resume`, `can_activate_effect`, `energy_full_time`, `is_marathon_event`, `live_list`...

### `live.preciseScore`
- NPPS4 source: `npps4/game/live.py` (return type: `LivePreciseScoreResponse`)
- **NPPS4 emits, client never reads:**
  - `server_timestamp`
- Agreement on 4 field(s): `can_activate_effect`, `off`, `on`, `rank_info`

### `login.login`
- NPPS4 source: `npps4/game/login.py` (return type: `LoginResponse`)
- **NPPS4 emits, client never reads:**
  - `server_timestamp`
- Agreement on 5 field(s): `authorize_token`, `idfa_enabled`, `review_version`, `skip_login_news`, `user_id`

### `login.unitSelect`
- NPPS4 source: `npps4/game/login.py` (return type: `StarterUnitSelectResponse`)
- **NPPS4 emits, client never reads:**
  - `unit_id`

### `notice.noticeFriendVariety`
- NPPS4 source: `npps4/game/notice.py` (return type: `NoticeFriendVarietyResponse`)
- **NPPS4 emits, client never reads:**
  - `server_timestamp`
- Agreement on 2 field(s): `item_count`, `notice_list`

### `payment.productList`
- NPPS4 source: `npps4/game/payment.py` (return type: `PaymentProductListResponse`)
- **NPPS4 emits, client never reads:**
  - `show_point_shop`
- Agreement on 5 field(s): `product_list`, `restriction_info`, `sns_product_list`, `subscription_list`, `under_age_info`

### `payment.receipt`
- NPPS4 source: `npps4/game/payment.py` (return type: `PaymentReceiptResponse`)
- **NPPS4 emits, client never reads:**
  - `server_timestamp`
- Agreement on 2 field(s): `product`, `status`

### `reward.open`
- NPPS4 source: `npps4/game/reward.py` (return type: `RewardOpenResponse`)
- **NPPS4 emits, client never reads:**
  - `present_cnt`
- Agreement on 10 field(s): `accomplished_achievement_list`, `added_achievement_list`, `after_user_info`, `before_user_info`, `class_system`, `new_achievement_cnt`...

### `user.getNavi`
- NPPS4 source: `npps4/game/user.py` (return type: `UserGetNaviResponse`)
- **NPPS4 emits, client never reads:**
  - `server_timestamp`
- Agreement on 1 field(s): `user`

