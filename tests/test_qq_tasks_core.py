"""QQ 音乐任务路由及真实回读约束；所有网络和账号存储均为离线替身。"""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.qq_tasks import core


GAME_URL = "https://game.y.qq.com/tmegames/web_pages/qm_tonglou/index.html?singerId=1"
COMPANION_URL = ("https://i2.y.qq.com/n3/client/pages/ai_player_companion/index.html"
                 "?roleId=123&bizId=456")
CARD_URL = "https://i2.y.qq.com/n3/client/pages/draw_cards/index.html?pool_id=123-1"
FLIP_URL = ("https://y.qq.com/m/client/starlight_game/index.html"
            "?_hidehd=1&biz=singermedal&issue=974436")


def task(kind="page_browsed", **extra):
    return {"TaskID": kind, "TaskType": kind, "Title": "免费任务", "Status": 0,
            "AchievedTimes": 0, "TotalTimes": 1, "NeedReportCondition": 1,
            "BtnJumpURL": "https://y.qq.com/", "ExtInfoStr": "offline-proof", **extra}


def client_for(before, after=None):
    client = Mock()
    client.task_list.side_effect = [deepcopy(before), deepcopy(before if after is None else after)]
    client.report_task.return_value = (True, 0)
    return client


class TaskRoutingTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch.object(core.time, "sleep").start()
        self.addCleanup(patch.stopall)

    def test_supported_actions_use_their_real_action_not_condition_report(self):
        cases = [("set_medal_role_ai_player_singer", COMPANION_URL, "set_free_companion"),
                 ("mini_game", GAME_URL, "play_browser_game"),
                 ("medal_card_draw_card", CARD_URL, "draw_card")]
        for kind, url, method in cases:
            with self.subTest(kind=kind):
                before = task(kind, BtnJumpURL=url)
                client = client_for([before], [{**before, "Status": 2, "AchievedTimes": 1}])
                out = core.complete_medal_tasks(client, "1", allow_card_draw=True)
                getattr(client, method).assert_called_once()
                client.report_task.assert_not_called()
                self.assertEqual(out["auto"], 1)
                self.assertTrue(out["tasks"][0]["verified"])

    def test_flip_task_uses_server_entry_nested_in_ext_info(self):
        before = task(
            "mini_game_singer", BtnJumpURL="none",
            ExtInfoStr=(
                '{"BtnJumpURL":"https://y.qq.com/m/client/starlight_game/'
                'index.html?_hidehd=1&biz=singermedal&issue=974436"}'))
        self.assertEqual(core._task_url(before), FLIP_URL)
        self.assertEqual(core._task_capability(before)[0], "auto")
        client = client_for([before], [{**before, "Status": 2, "AchievedTimes": 1}])
        result = core.complete_medal_tasks(client, "974436")
        client.play_browser_game.assert_called_once_with(before)
        client.report_task.assert_not_called()
        self.assertEqual(result["auto"], 1)

    def test_successful_action_without_server_progress_stays_pending(self):
        before = task("mini_game", BtnJumpURL=GAME_URL)
        client = client_for([before])
        out = core.complete_medal_tasks(client, "1")
        self.assertEqual(out["auto"], 0)
        self.assertFalse(out["tasks"][0]["done"])
        self.assertEqual(out["pending"], ["免费任务"])

    def test_failed_readback_never_invents_completed_status(self):
        before = task("set_medal_role_ai_player_singer", BtnJumpURL=COMPANION_URL)
        client = client_for([before])
        client.task_list.side_effect = [[before], core.QQError("offline"), core.QQError("offline")]
        out = core.complete_medal_tasks(client, "1")
        self.assertEqual(out["auto"], 0)
        self.assertFalse(out["tasks"][0]["verified"])
        self.assertFalse(out["tasks"][0]["done"])
        self.assertTrue(out["verify_error"])

    def test_manual_and_paid_tasks_cannot_be_promoted_by_report_flag(self):
        kinds = ["watch_ads", "listen_song", "share_song", "calendar_reminder",
                 "farm_game_plant_fan_flower", "mini_game_singer", "ai_sing_singer",
                 "buy_product_singer"]
        before = [task(kind) for kind in kinds]
        client = client_for(before)
        core.complete_medal_tasks(client, "1")
        client.report_task.assert_not_called()
        client.visit_task.assert_not_called()
        client.play_browser_game.assert_not_called()
        client.draw_card.assert_not_called()
        client.set_free_companion.assert_not_called()

    def test_draw_requires_explicit_resource_switch_and_is_bounded_to_one(self):
        before = task("medal_card_draw_card", BtnJumpURL=CARD_URL, TotalTimes=8)
        client = client_for([before])
        core.complete_medal_tasks(client, "1")
        client.draw_card.assert_not_called()
        client = client_for([before])
        core.complete_medal_tasks(client, "1", allow_card_draw=True)
        client.draw_card.assert_called_once_with("123-1", allow_existing_chances=True)

    def test_explicit_task_selection_is_respected(self):
        before = task("mini_game", BtnJumpURL=GAME_URL)
        for selected in ([], ["page_browsed"]):
            client = client_for([before])
            out = core.complete_medal_tasks(client, "1", selected)
            client.play_browser_game.assert_not_called()
            self.assertEqual(out["unselected"], 1)

    def test_already_completed_task_does_not_write(self):
        before = task("mini_game", BtnJumpURL=GAME_URL, Status="2")
        client = client_for([before])
        out = core.complete_medal_tasks(client, "1")
        client.play_browser_game.assert_not_called()
        self.assertEqual(out["auto"], 0)

    def test_real_action_failure_is_reported_once_and_other_tasks_continue(self):
        before = [task("mini_game", BtnJumpURL=GAME_URL), task()]
        after = [before[0], {**before[1], "Status": 2}]
        client = client_for(before, after)
        client.play_browser_game.side_effect = core.QQError("无免费体力")
        out = core.complete_medal_tasks(client, "1")
        client.play_browser_game.assert_called_once()
        client.visit_task.assert_called_once()
        self.assertEqual(out["fail"], 1)
        self.assertEqual(out["auto"], 1)

    def test_browser_error_does_not_leak_url_secret_into_failure_message(self):
        secret = "sentinel-private-task-token"
        before = task("mini_game", BtnJumpURL=(
            "https://game.y.qq.com/tmegames/web_pages/qm_tonglou/index.html"
            f"?singerId=1&token={secret}"))
        client = core.MusicClient("uin=123; qqmusic_key=offline")
        client.task_list = Mock(side_effect=[[before], [before]])

        browser = MagicMock()
        page = MagicMock()
        page.goto.side_effect = RuntimeError(
            f"Navigation failed at {before['BtnJumpURL']}")
        browser.new_context.return_value.new_page.return_value = page
        pw = MagicMock()
        pw.chromium.launch.return_value = browser
        sync_playwright = MagicMock()
        sync_playwright.return_value.__enter__.return_value = pw
        playwright = ModuleType("playwright")
        playwright.__path__ = []
        playwright_sync_api = ModuleType("playwright.sync_api")
        playwright_sync_api.sync_playwright = sync_playwright

        with patch.dict(sys.modules, {"playwright": playwright,
                                      "playwright.sync_api": playwright_sync_api}), \
             patch("modules.signin.core.find_browser", return_value=None), \
             patch("app.ui_settings.is_headless", return_value=True):
            result = core.complete_medal_tasks(client, "1")

        self.assertEqual(result["fail"], 1)
        self.assertNotIn(secret, repr(result["failed"]))
        self.assertEqual(result["failed"], [
            "免费任务（浏览任务页面失败，请刷新任务进度确认状态）"])


class DrawCardTests(unittest.TestCase):
    def setUp(self):
        self.client = core.MusicClient("uin=123; qqmusic_key=offline")
        self.home = {"SVipPoolInfo": {"SVipPool": False, "SinglePrice": 0, "TenPrice": 0},
                     "StatusInfo": {"Status": 0}, "RemainDraws": 3}
        self.call = Mock(side_effect=[(0, self.home), (0, {"OrderID": "offline-order"}),
                                     (0, {"Cards": [{"ID": "offline-card"}], "RemainDraws": 2})])
        self.client._medal_call = self.call

    def test_default_never_uses_account_resources(self):
        with self.assertRaises(core.QQError):
            self.client.draw_card("123-1")
        self.call.assert_not_called()

    def test_normal_pool_uses_exactly_one_chance_without_purchase(self):
        out = self.client.draw_card("123-1", allow_existing_chances=True)
        self.assertEqual(out["drawn"], 1)
        self.assertEqual([c.args[1] for c in self.call.call_args_list],
                         ["GetCardHomepage", "GenerateOrderID", "DrawCard"])
        self.assertEqual(self.call.call_args.args[2], {
            "PoolID": "123-1", "Num": 1, "OrderID": "offline-order", "InviterEncUin": ""})

    def test_paid_missing_closed_or_empty_pool_stops_before_order(self):
        homes = [{}, {**self.home, "SVipPoolInfo": {"SVipPool": True}},
                 {**self.home, "SVipPoolInfo": {"SVipPool": "false"}},
                 {**self.home, "SVipPoolInfo": {"SVipPool": False}},
                 {**self.home, "SVipPoolInfo": {"SVipPool": False, "SinglePrice": 1, "TenPrice": 0}},
                 {**self.home, "StatusInfo": {"Status": 1}}, {**self.home, "RemainDraws": 0}]
        for home in homes:
            with self.subTest(home=home):
                self.call.reset_mock(side_effect=True)
                self.call.side_effect = [(0, home)]
                with self.assertRaises(core.QQError):
                    self.client.draw_card("123-1", allow_existing_chances=True)
                self.assertEqual(self.call.call_count, 1)

    def test_rejected_or_ambiguous_draw_is_never_retried(self):
        for result in [(8, {}), (0, {}), (0, {"Cards": [{}], "RemainDraws": 3})]:
            self.call.reset_mock(side_effect=True)
            self.call.side_effect = [(0, self.home), (0, {"OrderID": "offline-order"}), result]
            with self.assertRaises(core.QQError):
                self.client.draw_card("123-1", allow_existing_chances=True)
            self.assertEqual(self.call.call_count, 3)


class ConfigAndDispatchTests(unittest.TestCase):
    def test_card_signin_runs_alone_without_signing_unselected_medals(self):
        cfg = {**core.DEFAULT_MUSIC, "cookie": "uin=123; qqmusic_key=offline",
               "sign_enable": False, "medal_enable": False, "card_pool_enable": True,
               "medal_selected_ids": ["one"]}
        client = Mock()
        client.medals.return_value = [{"id": "one", "name": "A"}, {"id": "two", "name": "B"}]
        client.task_list.return_value = [task("medal_card_draw_card", BtnJumpURL=CARD_URL)]
        client.card_pool_signin.return_value = {"ok": True, "signed": True, "already": False}
        job = core.Task()
        with patch.object(core, "load_music", return_value=cfg), \
             patch.object(core, "load_state", return_value={"medal": {"existing": True}}), \
             patch.object(core, "save_state") as save, \
             patch.object(core, "MusicClient", return_value=client), patch.object(core.time, "sleep"):
            job._run()
        client.task_list.assert_called_once_with("one")
        client.card_pool_signin.assert_called_once_with("123-1")
        client.sign.assert_not_called()
        client.medal_sign.assert_not_called()
        client.draw_card.assert_not_called()
        self.assertEqual(save.call_args.args[0]["medal"], {"existing": True})
        self.assertEqual(job.summary["card_pool"]["done"], 1)

    def test_card_discovery_error_is_recorded_as_failure_not_empty_success(self):
        cfg = {**core.DEFAULT_MUSIC, "cookie": "uin=123; qqmusic_key=offline",
               "sign_enable": False, "medal_enable": False, "card_pool_enable": True,
               "medal_selected_ids": ["one"]}
        client = Mock()
        client.medals.return_value = [{"id": "one", "name": "A"}]
        client.task_list.side_effect = core.QQError("offline")
        job = core.Task()
        with patch.object(core, "load_music", return_value=cfg), \
             patch.object(core, "load_state", return_value={}), patch.object(core, "save_state"), \
             patch.object(core, "MusicClient", return_value=client):
            job._run()
        client.card_pool_signin.assert_not_called()
        self.assertEqual(job.summary["card_pool"]["fail"], 1)
        self.assertEqual(len(job.summary["card_pool"]["discovery_errors"]), 1)

    def test_validated_config_roundtrip_keeps_manual_selection_and_new_fields(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(core, "MUSIC_CONFIG", Path(temp) / "music.json"):
            cfg = {**core.DEFAULT_MUSIC, "cookie": "uin=123; qqmusic_key=offline",
                   "auto_all_free": False, "medal_task_types": ["mini_game"],
                   "medal_selected_ids": ["one"], "schedule_enabled": True, "schedule_time": "23:45"}
            core.save_music(cfg)
            self.assertEqual(core.load_music(), cfg)
            for extra in ({"schedule_time": "25:00"}, {"auto_all_free": "false"},
                          {"card_draw_enable": 1}, {"schedule_enabled": 1}):
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    core.save_music({**cfg, **extra})
            self.assertEqual(core.load_music(), cfg)

    def test_auto_mode_discovers_new_types_but_keeps_artist_selection(self):
        for automatic, expected in ((True, None), (False, ["page_browsed"])):
            cfg = {**core.DEFAULT_MUSIC, "cookie": "uin=123; qqmusic_key=offline",
                   "auto_all_free": automatic, "medal_task_types": ["page_browsed"],
                   "medal_selected_ids": ["one"], "card_draw_enable": True}
            job = core.Task()
            with patch.object(core, "load_music", return_value=cfg), \
                 patch.object(core, "load_state", return_value={}), \
                 patch.object(core, "save_state"), \
                 patch.object(core.MusicClient, "sign_status", return_value={"signed": True, "continue_days": 1}), \
                 patch.object(job, "_run_medals", return_value={}) as run:
                job._run()
            self.assertTrue(job.done)
            run.assert_called_once_with(cfg["cookie"], True, expected, ["one"], allow_card_draw=True)


if __name__ == "__main__":
    unittest.main()
