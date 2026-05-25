-- preload.lua
-- Loads m_boot/initialize.lua + the ~18 model files that register
-- Cachable listeners. Run ONCE per harness process boot, before any
-- endpoint dispatch.
--
-- After this script runs:
--   - GLOBAL.CACHE_OBSERVER has every static listener registered
--   - import("...") for any module the model files registered via
--     define() returns the real module table
--
-- Anything that fails to load logs a warning but does not crash
-- the harness -- a model that fails to preload just means listeners
-- it would have registered will not fire (degraded but still useful).

local M = {}

-- Model files that register listeners (via grep of `addListener`) or
-- define handler closures consumed by bulkSend callbacks. m_boot/
-- initialize.lua holds the bulk (91 listeners inside setupUpdaters);
-- the rest add a handful each, mostly for arena / class / duel
-- transient state, plus the UI-shell files (header / footer /
-- local_notification) that register top-level user_info listeners
-- and the bulkSend-driven model files.
--
-- Each file is loaded under pcall; a crash mid-body is absorbed and
-- only the listeners registered BEFORE the crash count. That is
-- desirable -- the alternative (excluding crash-prone files) leaves
-- known-good listeners on the floor.
M.MODEL_FILES = {
  -- Bulk of the listeners (91 inside setupUpdaters()).
  "m_boot/initialize.lua",
  -- m_*/model and m_*/view files that register listeners at module
  -- body level (fire at preload time).
  "m_basicsettings/start.lua",
  "m_arena/model/base.lua",
  "m_arena/model/matching.lua",
  "m_arena/model/result.lua",
  "m_album/model/album.lua",
  "m_history/payment_history_model.lua",
  "m_duel/model/assist_log.lua",
  "m_duel/model/deck.lua",
  "m_duel/model/duel.lua",
  "m_duel/model/live_log.lua",
  "m_class/model/challenge_mission.lua",
  "m_class/model/competition_final.lua",
  "m_class/model/competition.lua",
  "m_login/action.lua",
  "m_online/online.lua",
  "m_top/view.lua",
  "m_quest/view/exchange.lua",
  "m_quest/view/elements/exchange_info.lua",
  -- common/model/*.lua and common/unit/*.lua: shared listeners that
  -- live outside the m_* feature trees. Together these add another
  -- ~15 listeners covering accessory / event_reward / live_bonus /
  -- museum / unit_deck cache_keys, which several endpoints depend on
  -- for downstream field reads.
  "common/model/accessory.lua",
  "common/model/ad_reward.lua",
  "common/model/badge_model.lua",
  "common/model/cheer_deck.lua",
  "common/model/concert/base.lua",
  "common/model/event_reward.lua",
  "common/model/live/bonus.lua",
  "common/model/museum/base.lua",
  "common/unit/unit_deck_cache.lua",
  "common/unit/unit_deck_cache_listener.lua",
  -- Session 9 Tier 2 Approach A: the UI-shell files register listeners
  -- on top-level cache_keys (`$userInfo`, etc.) that pure model files
  -- don't reach. These are the 4 `addListener` files NOT in the
  -- original preload set. cachable.lua is intentionally NOT loaded --
  -- we use our own port (cachable.lua in scripts/lua_harness/).
  "common/footer.lua",
  "common/header.lua",
  "common/local_notification.lua",
  "common/elements/live_customize_button.lua",
}

-- Session 9 Tier 2 Approach B: bulkSend-calling files. Loaded so their
-- module-table exports (e.g. SecretboxModel.loadMainPage) are present
-- in stubs.modules after preload. The handler-invoke driver then calls
-- each function; the (now non-empty) svapi.bulkSend stub fires the
-- success_cb against a sentinel envelope and the spy logs every field
-- the handler reads. Loaded under pcall: file-body errors get absorbed
-- and the exports defined BEFORE the error point remain callable.
M.BULKSEND_FILES = {
  "common/klabid/disconnect.lua",
  "common/model/achievement/achievement.lua",
  "common/model/subscription.lua",
  "common/polling.lua",
  "common/webview_command.lua",
  "m_item/model/item_list.lua",
  "m_item/start.lua",
  "m_item/view/buff_item_dialog.lua",
  "m_location/model/location.lua",
  "m_player_profile/user.lua",
  "m_reward/reward_history.lua",
  "m_reward/reward_list.lua",
  "m_secretbox/model/secretbox.lua",
  "m_shop/shop_base.lua",
  "m_sns/model/sns.lua",
  "m_tutorial/live_menu/guest.lua",
  "m_unit/unit_menu_stage.lua",
}

-- Approach B (Session 10): UI handler files. Each one is referenced by at
-- least one ui-only endpoint per build/ui_handler_map.json. Loading them
-- at preload time accomplishes two things:
--   1. Module-body listener registrations fire (those that don't sit
--      inside an inner function).
--   2. Their define("ClassName", ...) calls make their exported methods
--      reachable via import("ClassName"), so a future handler_invoke
--      driver can call them.
-- common/cachable.lua is excluded -- we use our own port. Files known to
-- crash mid-body are pcall-wrapped like every other preload file.
M.UI_HANDLER_FILES = {
  "common/add_type_helper.lua",
  "common/background_flash.lua",
  "common/count_down_timer.lua",
  "common/debug_ghost_player.lua",
  "common/download/include.lua",
  "common/energy.lua",
  "common/event/base_event_form.lua",
  "common/event/bonus_effect.lua",
  "common/event/chat/chat_action.lua",
  "common/event/chat/chat_common.lua",
  "common/event_cache.lua",
  "common/event_scenario_helper.lua",
  "common/function_voice.lua",
  "common/game_mode.lua",
  "common/gauge_form.lua",
  "common/general_reward_dialog.lua",
  "common/idol_skill_detail_form.lua",
  "common/live/live_info_form.lua",
  "common/live_error.lua",
  "common/live_goal.lua",
  "common/live_state.lua",
  "common/live_stub.lua",
  "common/live_title.lua",
  "common/logger/energy.lua",
  "common/lp_factor.lua",
  "common/mail_additional.lua",
  "common/marquee.lua",
  "common/mock.lua",
  "common/model/award.lua",
  "common/model/background.lua",
  "common/model/battle/battle.lua",
  "common/model/battle/battle_info.lua",
  "common/model/battle/end_room.lua",
  "common/model/card_asset.lua",
  "common/model/challenge/challenge.lua",
  "common/model/class/class_info.lua",
  "common/model/data_transfer.lua",
  "common/model/duty/duty_info.lua",
  "common/model/energy.lua",
  "common/model/enhance_item.lua",
  "common/model/event_model.lua",
  "common/model/exchange_point.lua",
  "common/model/festival/festival.lua",
  "common/model/festival/festival_info.lua",
  "common/model/item/event_unit_rankup_item.lua",
  "common/model/live/accuracy.lua",
  "common/model/live/custom_note.lua",
  "common/model/live/live_se.lua",
  "common/model/live/play_log.lua",
  "common/model/live/precise_log.lua",
  "common/model/live/random_live_asset.lua",
  "common/model/live_customize/active_setting.lua",
  "common/model/live_customize/background_setting.lua",
  "common/model/live_customize/live_customize.lua",
  "common/model/live_customize/live_customize_cache_pool.lua",
  "common/model/live_customize/note_icon_setting.lua",
  "common/model/live_customize/note_speed_setting.lua",
  "common/model/live_customize/other_setting.lua",
  "common/model/live_customize/precise_setting.lua",
  "common/model/live_customize/result_setting.lua",
  "common/model/live_customize/tap_se_setting.lua",
  "common/model/live_customize/timing_adjust_setting.lua",
  "common/model/live_customize/volume_setting.lua",
  "common/model/live_party_list.lua",
  "common/model/live_status.lua",
  "common/model/marathon.lua",
  "common/model/marathon_info.lua",
  "common/model/member_tag.lua",
  "common/model/multi_unit_scenario.lua",
  "common/model/navi/special_cutin.lua",
  "common/model/product.lua",
  "common/model/scenario.lua",
  "common/model/school_idol_skill.lua",
  "common/model/shop_time.lua",
  "common/model/stamp/stamp.lua",
  "common/model/subscenario.lua",
  "common/model/top_info.lua",
  "common/model/unit.lua",
  "common/model/unit/center_skill.lua",
  "common/model/unit/costume.lua",
  "common/model/unit/deck.lua",
  "common/model/unit/hidden_unit_list.lua",
  "common/model/unit/lesson.lua",
  "common/model/unit/merge.lua",
  "common/model/unit/multi_unit.lua",
  "common/model/unit/navi_asset.lua",
  "common/model/unit/preset_deck.lua",
  "common/model/unit/rank_up.lua",
  "common/model/unit/recommend_deck.lua",
  "common/model/unit/skill_target.lua",
  "common/model/unit/unit_level.lua",
  "common/model/unit/unit_list_cache.lua",
  "common/model/unit/unit_skill.lua",
  "common/model/unit_lineup.lua",
  "common/model/unit_lineup_filter.lua",
  "common/model/unit_rarity.lua",
  "common/model/unit_type.lua",
  "common/model/user/navi.lua",
  "common/model/user/user_info.lua",
  "common/model/user_birthday.lua",
  "common/navi/model.lua",
  "common/nowloading.lua",
  "common/payment_service_button.lua",
  "common/pulldown.lua",
  "common/result/base_reward_form.lua",
  "common/result/event_point_reward_form.lua",
  "common/result/unit_reward_form.lua",
  "common/result_reward.lua",
  "common/reward_dialog.lua",
  "common/reward_result_dialog.lua",
  "common/scenario/unlock_dialog.lua",
  "common/shader/background/background_shader_param.lua",
  "common/skill_slot_form.lua",
  "common/top_info_once.lua",
  "common/ui/uiCellAnim.lua",
  "common/ui/uiProgressBar.lua",
  "common/ui/uiSWFPlayer.lua",
  "common/ui/uiScore.lua",
  "common/ui/ui_movie.lua",
  "common/unit/album_status_form.lua",
  "common/unit/card_image_form.lua",
  "common/unit/costume_list_options_form.lua",
  "common/unit/member_tag_form.lua",
  "common/unit/new_unit_flag_cache.lua",
  "common/unit/popup_detail.lua",
  "common/unit/skill_detail_popup.lua",
  "common/unit/support_unit_picker.lua",
  "common/unit/unit_basic_info_form.lua",
  "common/unit/unit_deck_mock.lua",
  "common/unit/unit_detail_arrow.lua",
  "common/unit/unit_detail_content_form.lua",
  "common/unit/unit_detail_form.lua",
  "common/unit/unit_icon_form.lua",
  "common/unit/unit_list_options_form.lua",
  "common/unit/unit_status_form.lua",
  "m_achievement/result_manager.lua",
  "m_achievement/view/detail.lua",
  "m_achievement/view/detail_new_arrivals.lua",
  "m_achievement/view/detail_reward.lua",
  "m_album/album_detail.lua",
  "m_album/album_options_form.lua",
  "m_album/flick_area_task.lua",
  "m_album/loading_with_option.lua",
  "m_album/view/multi_card.lua",
  "m_album/view/series.lua",
  "m_arena/elements/common/card_3d.lua",
  "m_background/background_preview.lua",
  "m_basicsettings/birthday_registration_dialog.lua",
  "m_basicsettings/model/push.lua",
  "m_basicsettings/name_change_dialog.lua",
  "m_battle_event/model/lp_factor.lua",
  "m_battle_event/room/form.lua",
  "m_challenge/model/lp_factor.lua",
  "m_class/model/class.lua",
  "m_class/view/challenge_reset.lua",
  "m_class/view/deck.lua",
  "m_class/view/elements/accuracy.lua",
  "m_class/view/elements/challenge_confirm.lua",
  "m_class/view/elements/challenge_mission.lua",
  "m_class/view/elements/challenge_mission_confirm.lua",
  "m_class/view/elements/competition.lua",
  "m_class/view/elements/competition_live_info_dialog.lua",
  "m_class/view/elements/competition_live_jacket_button.lua",
  "m_class/view/elements/competition_result.lua",
  "m_class/view/elements/competition_user_result.lua",
  "m_class/view/elements/final_promise.lua",
  "m_class/view/elements/final_result_view.lua",
  "m_class/view/elements/final_vote.lua",
  "m_class/view/elements/jacket_list.lua",
  "m_class/view/elements/live_mission_item.lua",
  "m_class/view/elements/mission.lua",
  "m_class/view/elements/mission_item.lua",
  "m_class/view/elements/rank_item.lua",
  "m_class/view/elements/result_jacket.lua",
  "m_class/view/elements/select_area.lua",
  "m_class/view/elements/select_jacket.lua",
  "m_class/view/elements/spectate_result_view.lua",
  "m_class/view/live_menu.lua",
  "m_class/view/result.lua",
  "m_class/view/room/room.lua",
  "m_class/view/room/room_player.lua",
  "m_class/view/top.lua",
  "m_duel/controller.lua",
  "m_duel/view/crowd_result.lua",
  "m_duel/view/crowd_room.lua",
  "m_duel/view/deck.lua",
  "m_duel/view/elements/confirm.lua",
  "m_duel/view/elements/difficulty.lua",
  "m_duel/view/elements/energy.lua",
  "m_duel/view/elements/goal.lua",
  "m_duel/view/elements/live_menu_focus_item.lua",
  "m_duel/view/elements/private_info.lua",
  "m_duel/view/elements/private_setting.lua",
  "m_duel/view/elements/result_info.lua",
  "m_duel/view/elements/result_player_myitem.lua",
  "m_duel/view/elements/room_player.lua",
  "m_duel/view/elements/room_player_list.lua",
  "m_duel/view/elements/stamp_board.lua",
  "m_duel/view/live_menu.lua",
  "m_duel/view/priority.lua",
  "m_duel/view/top.lua",
  "m_event/battle.lua",
  "m_event/card.lua",
  "m_event/challenge.lua",
  "m_event/challenge/bonus_form.lua",
  "m_event/event.lua",
  "m_event/festival.lua",
  "m_event/festival/arrange_form.lua",
  "m_event/festival/arrange_list.lua",
  "m_event_menu/model/event_menu.lua",
  "m_event_menu/view/top.lua",
  "m_exchange/model/base.lua",
  "m_exchange/model/exchange.lua",
  "m_exchange/model/exchange_filter.lua",
  "m_exchange/view/detail.lua",
  "m_exchange/view/elements/arena_point.lua",
  "m_exchange/view/elements/cost.lua",
  "m_exchange/view/elements/duel_point.lua",
  "m_exchange/view/elements/item.lua",
  "m_exchange/view/elements/list.lua",
  "m_exchange/view/elements/point.lua",
  "m_exchange/view/elements/select.lua",
  "m_exchange/view/elements/unit_form.lua",
  "m_exchange/view/top.lua",
  "m_favorite_ranking/favorite_ranking.lua",
  "m_favorite_ranking/ranking_mode.lua",
  "m_friends/helper/friends_helper.lua",
  "m_friends/model/friend.lua",
  "m_friends/view/friends_list.lua",
  "m_friends/view/friends_search.lua",
  "m_item/view/item_list.lua",
  "m_live/character.lua",
  "m_live/festival_bonus.lua",
  "m_live/festival_guest_bonus.lua",
  "m_live/live_guest_bonus.lua",
  "m_live/note.lua",
  "m_live/quest_guest_bonus.lua",
  "m_live/skill.lua",
  "m_live/star.lua",
  "m_live/touch_controller.lua",
  "m_live/view/character.lua",
  "m_live/view/character_wheel.lua",
  "m_live_adjust/character.lua",
  "m_live_adjust/character_wheel.lua",
  "m_live_adjust/live_adjust_manager.lua",
  "m_live_adjust/note_manager.lua",
  "m_live_custom/live_custom_helper.lua",
  "m_live_custom/live_customize_dialog.lua",
  "m_live_custom/note_customize/note_customize_dialog.lua",
  "m_live_custom/note_customize/note_customize_item.lua",
  "m_live_custom/note_preview_form.lua",
  "m_live_custom/view/live_background.lua",
  "m_live_custom/view/note_icon.lua",
  "m_live_custom/view/note_speed.lua",
  "m_live_custom/view/other.lua",
  "m_live_custom/view/precise.lua",
  "m_live_custom/view/tap_se.lua",
  "m_live_custom/view/volume.lua",
  "m_live_menu/common/deck_detail_form.lua",
  "m_live_menu/common/deck_status.lua",
  "m_live_menu/common/focus_form.lua",
  "m_live_menu/common/guest_form.lua",
  "m_live_menu/common/icon.lua",
  "m_live_menu/common/idol_skill_form.lua",
  "m_live_menu/common/unit_detail.lua",
  "m_live_menu/dialog/friend_request.lua",
  "m_live_menu/live_list/finish.lua",
  "m_live_menu/live_list/jacket.lua",
  "m_live_menu/live_list/live_cost_form.lua",
  "m_live_menu/live_list/live_info.lua",
  "m_live_menu/live_list/live_list.lua",
  "m_live_menu/live_list/live_list_options_form.lua",
  "m_live_menu/live_list/live_menu_api.lua",
  "m_live_menu/live_list/pager.lua",
  "m_live_menu/result/live.lua",
  "m_live_menu/result/unit.lua",
  "m_live_menu/result_tutorial.lua",
  "m_login/model/comeback_bonus/comeback_achieve.lua",
  "m_museum/view/top.lua",
  "m_online/view/result.lua",
  "m_quest/model/exchange.lua",
  "m_quest/model/exchange/popup/cond_increase.lua",
  "m_quest/model/exchange/popup/cond_unlock.lua",
  "m_quest/model/free.lua",
  "m_quest/model/free_live.lua",
  "m_quest/model/lp_factor.lua",
  "m_quest/model/main.lua",
  "m_quest/model/main_live.lua",
  "m_quest/model/map.lua",
  "m_quest/model/pin.lua",
  "m_quest/model/quest.lua",
  "m_quest/view/briefing.lua",
  "m_quest/view/detail.lua",
  "m_quest/view/elements/event_info.lua",
  "m_quest/view/elements/exchange/popup.lua",
  "m_quest/view/elements/exchange_dialog.lua",
  "m_quest/view/elements/exchange_item.lua",
  "m_quest/view/elements/live_info.lua",
  "m_quest/view/elements/live_jacket.lua",
  "m_quest/view/elements/mission_info.lua",
  "m_quest/view/elements/pin.lua",
  "m_quest/view/priority.lua",
  "m_quest/view/result.lua",
  "m_ranking/ranking_list.lua",
  "m_reward/common.lua",
  "m_reward/view/reward_select_unit_detail_form.lua",
  "m_reward/view/reward_select_unit_form.lua",
  "m_reward/view/reward_unit_detail_dialog.lua",
  "m_scenario/collabo_arrange_view.lua",
  "m_scenario/result/dialog.lua",
  "m_scenario_menu/dialog.lua",
  "m_scenario_menu/model/scenario_menu.lua",
  "m_secretbox/view/animation.lua",
  "m_secretbox/view/elements/background.lua",
  "m_secretbox/view/priority.lua",
  "m_shop/model/recover.lua",
  "m_shop/model/subscription_item.lua",
  "m_shop/view/age_dialog.lua",
  "m_shop/view/elements/recover_confirm.lua",
  "m_shop/view/elements/recover_item.lua",
  "m_shop/view/elements/recover_select_number.lua",
  "m_shop/view/shop_dialog.lua",
  "m_shop/view/subscription_dialog.lua",
  "m_stamp/model/stamp_filter.lua",
  "m_stamp/view/display.lua",
  "m_stamp/view/select_myset.lua",
  "m_stamp/view/stamp_options_form.lua",
  "m_team/model/lp_factor.lua",
  "m_team/model/team_event.lua",
  "m_team/view/deck.lua",
  "m_team/view/elements/achieved_effect.lua",
  "m_team/view/elements/confirm_popup.lua",
  "m_team/view/elements/current_goal.lua",
  "m_team/view/elements/current_mission.lua",
  "m_team/view/elements/difficulty.lua",
  "m_team/view/elements/door_effect.lua",
  "m_team/view/elements/goal_gauge.lua",
  "m_team/view/elements/help_power.lua",
  "m_team/view/elements/live_info.lua",
  "m_team/view/elements/mission_progress_effect.lua",
  "m_team/view/elements/private_info.lua",
  "m_team/view/elements/result_rank.lua",
  "m_team/view/elements/room_player.lua",
  "m_team/view/elements/user_rank.lua",
  "m_team/view/history.lua",
  "m_team/view/mission.lua",
  "m_team/view/priority.lua",
  "m_team/view/room.lua",
  "m_team/view/top.lua",
  "m_top/banner.lua",
  "m_top/top_functions.lua",
  "m_tutorial/tutorial.lua",
  "m_unit/deck/name_form.lua",
  "m_unit/deck/recommend_form.lua",
  "m_unit/view/multi_unit/confirm_dialog.lua",
}

-- Returns the list of svapi/<name>.lua module names present on disk.
-- The harness loads all of them at preload so their idempotency
-- guards (`L = svapi.<name>; if L then return end`) read nil before
-- stubs.finalize_modules stamps modules with sentinel-on-miss
-- semantics for listener-time fallthrough.
local function list_svapi_modules(source_root)
  local svapi_dir = source_root .. "/common/svapi"
  local out = {}
  -- popen + ls is portable enough -- the harness only runs on macOS/Linux
  -- where the project sits, and we control the path.
  local handle = io.popen("ls " .. svapi_dir .. "/*.lua 2>/dev/null")
  if not handle then return out end
  for path in handle:lines() do
    local name = path:match("([^/]+)%.lua$")
    if name and name ~= "_util" and name ~= "include" then
      out[#out + 1] = name
    end
  end
  handle:close()
  table.sort(out)
  return out
end

-- M.run(source_root, on_warning)
--   source_root : absolute path to the source/all directory
--   on_warning  : optional function(string) called on per-file failures
-- Returns a summary table with per-file ok/err results.
function M.run(source_root, on_warning)
  on_warning = on_warning or function(_msg) end
  local report = {
    loaded = 0,
    failed = 0,
    files = {},
    svapi_loaded = 0,
    svapi_failed = 0,
    svapi_files = {},
    listeners_before = 0,
    listeners_after = 0,
  }

  local Cachable = require("cachable")
  report.listeners_before = Cachable.observer_count()

  -- Phase 0: load all svapi/<mod>.lua dispatch files. We deliberately
  -- skip common/svapi/_util.lua -- its cacheResponse calls into
  -- MuseumModel.getInstance() which crashes on our empty stubs, and
  -- the work it does (setting TopInfo.PresentCount, etc.) is not
  -- needed for discovery. Our stubs.lua svapi.cacheResponse already
  -- implements the V5-relevant subset (set cache + notifyUpdate).
  for _, mod in ipairs(list_svapi_modules(source_root)) do
    local path = source_root .. "/common/svapi/" .. mod .. ".lua"
    local chunk, err = loadfile(path)
    if not chunk then
      report.svapi_failed = report.svapi_failed + 1
      report.svapi_files[#report.svapi_files + 1] =
        {mod = mod, ok = false, err = err}
      on_warning("preload: svapi/" .. mod .. " loadfile: " .. tostring(err))
    else
      local ok, run_err = pcall(chunk)
      if not ok then
        report.svapi_failed = report.svapi_failed + 1
        report.svapi_files[#report.svapi_files + 1] =
          {mod = mod, ok = false, err = run_err}
        on_warning("preload: svapi/" .. mod .. " body: " .. tostring(run_err))
      else
        report.svapi_loaded = report.svapi_loaded + 1
        report.svapi_files[#report.svapi_files + 1] = {mod = mod, ok = true}
      end
    end
  end

  local function load_one(rel)
    local path = source_root .. "/" .. rel
    local chunk, load_err = loadfile(path)
    if not chunk then
      report.failed = report.failed + 1
      report.files[#report.files + 1] = {file = rel, ok = false, err = load_err}
      on_warning("preload: loadfile failed for " .. rel .. ": " ..
        tostring(load_err))
      return
    end
    local ok, run_err = pcall(chunk)
    if not ok then
      report.failed = report.failed + 1
      report.files[#report.files + 1] = {file = rel, ok = false, err = run_err}
      on_warning("preload: body error in " .. rel .. ": " .. tostring(run_err))
    else
      report.loaded = report.loaded + 1
      report.files[#report.files + 1] = {file = rel, ok = true}
    end
  end

  for _, rel in ipairs(M.MODEL_FILES) do load_one(rel) end
  -- Tier 2 Approach B: also load the bulkSend-calling files so their
  -- module-table exports (SecretboxModel, RewardListModel, etc.) are
  -- callable by handler_invoke. These files don't typically register
  -- listeners at body level -- their value is the exported functions
  -- that build batches and call bulkSend (now instrumented in stubs.lua).
  for _, rel in ipairs(M.BULKSEND_FILES) do load_one(rel) end
  -- NOTE: M.UI_HANDLER_FILES is NOT loaded here. They get loaded by
  -- M.run_ui_handlers() AFTER finalize_modules() so they benefit from
  -- the sentinel-on-miss semantics on engine imports like `const` /
  -- `dbapi` / `Underscore` that they routinely dereference at body
  -- time. Loading them here would fail with "attempt to index nil" on
  -- every uninitialized helper.

  -- m_boot/initialize.lua only DEFINES setupUpdaters; the 91 addListener
  -- calls live inside its body. m_boot/start.lua line 552 invokes it as
  -- `import("boot").initialize.setupUpdaters()` during the real client's
  -- boot sequence. We do the equivalent here so the listeners actually
  -- register. Wrapped in pcall: if setupUpdaters errors midway, every
  -- listener registered before the error still counts.
  local boot = (rawget(_G, "import") or function() return nil end)("boot")
  if type(boot) == "table" and type(boot.initialize) == "table"
      and type(boot.initialize.setupUpdaters) == "function" then
    local ok, err = pcall(boot.initialize.setupUpdaters)
    if not ok then
      report.setupUpdaters_err = tostring(err)
      on_warning("preload: setupUpdaters error: " .. tostring(err))
    else
      report.setupUpdaters_ok = true
    end
  else
    report.setupUpdaters_missing = true
    on_warning("preload: boot.initialize.setupUpdaters not found")
  end

  report.listeners_after = Cachable.observer_count()
  return report
end

-- Loads M.UI_HANDLER_FILES against a permissive module registry.
-- Intended to be called AFTER stubs.finalize_modules() so that
-- engine imports (`const`, `dbapi`, `Underscore`, `Datetime`, etc.)
-- return sentinel-on-miss instead of nil-on-miss.
-- ALSO retries any MODEL_FILES / BULKSEND_FILES that failed during
-- the first preload pass — many of them crashed for the same
-- nil-on-miss reason and define() never fired.
-- Returns {loaded, failed, retry_loaded, retry_failed, files = [...]}.
function M.run_ui_handlers(source_root, on_warning, first_preload)
  on_warning = on_warning or function(_msg) end
  local report = {
    loaded = 0, failed = 0,
    retry_loaded = 0, retry_failed = 0,
    files = {},
  }
  local function load_one(rel)
    local path = source_root .. "/" .. rel
    local chunk, load_err = loadfile(path)
    if not chunk then
      return false, load_err
    end
    local ok, run_err = pcall(chunk)
    return ok, run_err
  end

  -- Retry files that failed in the first preload pass.
  if first_preload and first_preload.files then
    for _, entry in ipairs(first_preload.files) do
      if not entry.ok then
        local ok, err = load_one(entry.file)
        if ok then
          report.retry_loaded = report.retry_loaded + 1
        else
          report.retry_failed = report.retry_failed + 1
        end
        report.files[#report.files + 1] =
          {file = entry.file, ok = ok, err = err, retry = true}
      end
    end
  end

  if M.UI_HANDLER_FILES then
    for _, rel in ipairs(M.UI_HANDLER_FILES) do
      local ok, err = load_one(rel)
      if ok then
        report.loaded = report.loaded + 1
      else
        report.failed = report.failed + 1
      end
      report.files[#report.files + 1] =
        {file = rel, ok = ok, err = err}
    end
  end

  -- After all new files are loaded, re-invoke setupUpdaters once
  -- more in case the second-pass loads registered any new module
  -- dependency it needs. Idempotent: re-registering the same
  -- listener is a no-op in our Cachable port (set semantics).
  local boot = (rawget(_G, "import") or function() return nil end)("boot")
  if type(boot) == "table" and type(boot.initialize) == "table"
      and type(boot.initialize.setupUpdaters) == "function" then
    pcall(boot.initialize.setupUpdaters)
  end

  return report
end

return M
