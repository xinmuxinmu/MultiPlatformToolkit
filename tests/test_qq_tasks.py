"""QQ Music regression tests; no saved credentials or live tasks are used."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from modules.qq_tasks import core


def task(task_id=1, typ="page_browsed", **fields):
    return {"TaskID": task_id, "TaskType": typ, "Title": "浏览活动页面",
            "AchievedTimes": 0, "TotalTimes": 1, "Status": 0,
            "NeedReportCondition": 1, "BtnJumpURL": "https://y.qq.com/activity/",
            "ExtInfoStr": "server-condition", **fields}


class TaskSchemaTests(unittest.TestCase):
    def test_flatten_old_and_new_task_lists_without_losing_same_type_rows(self):
        one, two = task(1), task(2)
        self.assertEqual(core._flatten_tasks({"Detail": [{"TaskList": [one, two]}]}),
                         [one, two])
        self.assertEqual(core._flatten_tasks({"Detail": {"TaskList": [one]},
                                              "TaskList": [one, two]}), [one, two])

    def test_default_selection_is_dynamic(self):
        self.assertIsNone(core._selected_task_types(None))
        self.assertEqual(core._selected_task_types([]), set())

    def test_new_free_browse_task_can_follow_server_condition(self):
        self.assertEqual(core._task_capability(task(typ="new_free_browse"))[0], "auto")
        self.assertEqual(core._task_capability(task(NeedReportCondition=0))[0], "manual")

    def test_paid_tasks_stay_blocked_even_when_reportable(self):
        for row in (task(typ="buy_product_singer"), task(typ="open_medal_week_supvip_singer"),
                    task(Title="金币兑换道具"), task(typ="mp_light_activity")):
            with self.subTest(row=row):
                self.assertEqual(core._task_capability(row)[0], "paid")

    def test_client_behaviors_are_never_replaced_by_blank_reports(self):
        for typ in ("watch_ads", "watch_ads_new", "watch_ads_spbox_singer",
                    "listen_song", "share_song", "share_medal", "calendar_reminder",
                    "mini_game_singer", "farm_game_plant_fan_flower", "medal_card_draw_card"):
            with self.subTest(typ=typ):
                self.assertEqual(core._task_capability(task(typ=typ))[0], "manual")

    def test_hidden_and_grey_tasks_are_not_executed(self):
        self.assertEqual(core._task_capability(task(Status=3))[0], "unavailable")
        self.assertEqual(core._task_capability(task(BtnState="1"))[0], "unavailable")

    def test_weibo_fallback_and_nested_official_url(self):
        self.assertEqual(core._task_url(task(typ="jump_external_app_singer",
                                            Ext={"containerID": "100808abcd"})),
                         "https://weibo.com/p/100808abcd")
        self.assertEqual(core._task_url(task(BtnJumpURL=(
            "qqmusic://qq.com/ui/openUrl?url=https%3A%2F%2Fy.qq.com%2Factivity%2F"))),
            "https://y.qq.com/activity/")
        for url in ("file:///C:/private", "javascript:alert(1)",
                    "https://qq.com.evil.test/", "https://user:pass@y.qq.com/"):
            self.assertEqual(core._task_url(task(BtnJumpURL=url)), "")

    def test_pool_ids_are_taken_from_each_task_url(self):
        self.assertEqual(core._pool_id(task(BtnJumpURL="https://y.qq.com/a?pool_id=123-2&x=1")),
                         "123-2")
        self.assertEqual(core._pool_id(task(BtnJumpURL="https://y.qq.com/a?pool_id=../bad")), "")

    def test_pool_ids_support_nested_official_urls_and_reject_other_hosts(self):
        self.assertEqual(core._pool_id(task(BtnJumpURL=(
            "qqmusic://qq.com/ui/openUrl?url="
            "https%3A%2F%2Fy.qq.com%2Fa%3Fpool_id%3D123-2%26x%3D1"))), "123-2")
        self.assertEqual(core._pool_id(task(BtnJumpURL=(
            "https://example.test/a?pool_id=123-2"))), "")

    def test_task_view_explains_reason_and_verification(self):
        row = core._task_view(task(typ="watch_ads_new"), verified=False)
        self.assertFalse(row["auto"])
        self.assertFalse(row["verified"])
        self.assertIn("SDK", row["reason"])


class TaskExecutionTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch.object(core.time, "sleep").start()
        self.addCleanup(patch.stopall)

    def test_visits_page_before_reporting_and_counts_only_verified_completion(self):
        before = task()
        after = task(AchievedTimes=1, Status=2)
        client = Mock()
        client.task_list.side_effect = [[before], [after]]
        client.report_task.return_value = (True, 0)
        result = core.complete_medal_tasks(client, "medal")
        client.visit_task.assert_called_once_with(before)
        client.report_task.assert_called_once_with("medal", before)
        names = [c[0] for c in client.method_calls]
        self.assertLess(names.index("visit_task"), names.index("report_task"))
        self.assertEqual(result["auto"], 1)

    def test_success_response_without_progress_is_pending_not_completed(self):
        client = Mock()
        client.task_list.return_value = [task()]
        client.report_task.return_value = (True, 0)
        result = core.complete_medal_tasks(client, "medal")
        self.assertEqual(result["auto"], 0)
        self.assertEqual(result["done"], 0)
        self.assertEqual(result["pending"], ["浏览活动页面"])

    def test_new_stage_is_reclassified_before_another_report(self):
        before = task(TotalTimes=2)
        next_stage = task(TotalTimes=2, AchievedTimes=1, BtnState=1)
        client = Mock()
        client.task_list.side_effect = [[before], [next_stage], [next_stage]]
        client.report_task.return_value = (True, 0)
        result = core.complete_medal_tasks(client, "medal")
        self.assertEqual(client.report_task.call_count, 1)
        self.assertEqual(client.visit_task.call_count, 1)
        self.assertEqual(result["auto"], 0)
        self.assertEqual(result["tasks"][0]["finish"], 1)

    def test_verify_failure_preserves_last_confirmed_stage_progress(self):
        before = task(TotalTimes=3)
        progress = task(TotalTimes=3, AchievedTimes=1)
        client = Mock()
        client.task_list.side_effect = [
            [before], [progress], core.QQError("offline"),
            core.QQError("offline"), core.QQError("offline"),
        ]
        client.report_task.return_value = (True, 0)
        result = core.complete_medal_tasks(client, "medal")
        self.assertEqual(result["tasks"][0]["finish"], 1)
        self.assertFalse(result["tasks"][0]["done"])
        self.assertFalse(result["tasks"][0]["verified"])
        self.assertEqual(result["auto"], 0)

    def test_verify_failure_keeps_confirmed_progress_without_faking_status(self):
        client = Mock()
        client.task_list.side_effect = [[task()], core.QQError("offline"), core.QQError("offline")]
        client.report_task.return_value = (True, 0)
        result = core.complete_medal_tasks(client, "medal")
        self.assertEqual(result["auto"], 0)
        self.assertFalse(result["tasks"][0]["done"])
        self.assertFalse(result["tasks"][0]["verified"])
        self.assertEqual(result["verify_error"], "offline")

    def test_report_timeout_is_not_retried_and_actual_completion_wins(self):
        client = Mock()
        client.task_list.side_effect = [[task()], [task(Status=2, AchievedTimes=1)]]
        client.report_task.side_effect = core.QQError("request timed out")
        result = core.complete_medal_tasks(client, "medal")
        self.assertEqual(client.report_task.call_count, 1)
        self.assertEqual(result["auto"], 1)
        self.assertEqual(result["fail"], 0)

    def test_explicit_selection_empty_skips_all_actions(self):
        client = Mock()
        client.task_list.return_value = [task()]
        result = core.complete_medal_tasks(client, "medal", [])
        client.visit_task.assert_not_called()
        client.report_task.assert_not_called()
        self.assertEqual(result["unselected"], 1)

    def test_selected_paid_ad_and_draw_tasks_do_not_send_reports_or_draws(self):
        rows = [task(1, "buy_product_singer"), task(2, "watch_ads_new"),
                task(3, "medal_card_draw_card", BtnJumpURL="https://y.qq.com/?pool_id=3-1")]
        client = Mock()
        client.task_list.return_value = rows
        result = core.complete_medal_tasks(client, "medal", [r["TaskType"] for r in rows])
        client.report_task.assert_not_called()
        client.draw_card.assert_not_called()
        self.assertEqual(len(result["manual"]), 3)
        self.assertEqual(result["card_pools"], ["3-1"])

    def test_zero_string_does_not_mean_already_praised(self):
        client = object.__new__(core.MusicClient)
        client._medal_call = Mock(side_effect=[
            (0, {"FirstTabDetail": {"ItemList": [{"ItemID": "target", "IsPraised": "0"}]}}),
            (0, {}),
        ])
        self.assertEqual(client.praise_rank("medal", 1), (1, 0))
        self.assertEqual(client._medal_call.call_count, 2)

    def test_manual_game_and_draw_never_send_resource_mutations(self):
        client = object.__new__(core.MusicClient)
        client._medal_call = Mock()
        for func in (client.draw_card, client.flip_card_challenge):
            with self.assertRaises(core.QQError):
                func("pool")
        client._medal_call.assert_not_called()

    def test_card_signin_requires_real_state_confirmation(self):
        client = object.__new__(core.MusicClient)
        client._medal_call = Mock(side_effect=[
            (0, {"HasSignIn": 0, "CurrLadderIndex": 0,
                 "SignInTask": [{"ExtInfoStr": "condition"}]}),
            (0, {}), (0, {"HasSignIn": 0}),
        ])
        with self.assertRaisesRegex(core.QQError, "仍未生效"):
            client.card_pool_signin("pool")

    def test_card_signin_returns_signed_and_already_separately(self):
        client = object.__new__(core.MusicClient)
        client._medal_call = Mock(side_effect=[
            (0, {"HasSignIn": 0, "CurrLadderIndex": 0,
                 "SignInTask": [{"ExtInfoStr": "condition"}]}),
            (0, {}), (0, {"HasSignIn": 1}),
        ])
        result = client.card_pool_signin("pool")
        self.assertTrue(result["signed"])
        self.assertFalse(result["already"])

    def test_missing_response_cannot_be_treated_as_success(self):
        for response in ({}, {"code": 0}, {"code": 0, "req_0": {}},
                         {"code": 0, "req_0": {"code": "invalid"}}):
            with self.subTest(response=response):
                if response.get("req_0", {}).get("code") == "invalid":
                    self.assertEqual(core._service_result(response)[0], -1)
                else:
                    with self.assertRaises(core.QQError):
                        core._service_result(response)

    def test_save_config_validates_and_preserves_empty_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(core, "MUSIC_CONFIG", Path(directory) / "music.json"):
                config = {**core.DEFAULT_MUSIC, "medal_task_types": [], "medal_selected_ids": []}
                core.save_music(config)
                self.assertEqual(core.load_music()["medal_task_types"], [])
                with self.assertRaises(ValueError):
                    core.save_music({**config, "sign_enable": "false"})
                self.assertEqual(json.loads(core.MUSIC_CONFIG.read_text("utf-8"))["sign_enable"], True)


if __name__ == "__main__":
    unittest.main()
