"""Preserve target-side QQ fixes while syncing features; offline mocks only."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from modules.qq_tasks import core


CARD_URL = ("https://i2.y.qq.com/n3/client/pages/draw_cards/index.html"
            "?pool_id=123-1&from=medal")


def browse_task(**changes):
    return {"TaskID": "offline-ladder", "TaskType": "page_browsed",
            "Title": "免费浏览任务", "Status": 0, "AchievedTimes": 0,
            "TotalTimes": 3, "NeedReportCondition": 1,
            "BtnJumpURL": "https://y.qq.com/", "ExtInfoStr": "offline-first", **changes}


class QQSyncRegressionTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch.object(core.time, "sleep").start()
        self.addCleanup(patch.stopall)

    def test_pool_id_is_extracted_from_nested_official_url(self):
        for url in (CARD_URL, "qqmusic://qq.com/ui/openUrl?url=" + quote(CARD_URL, safe="")):
            with self.subTest(url=url):
                value = {"TaskType": "medal_card_draw_card", "BtnJumpURL": url}
                self.assertEqual(core._pool_id(value), "123-1")

    def test_pool_id_is_not_accepted_from_external_url(self):
        for host in ("evil.example", "i2.y.qq.com.evil.example"):
            external = CARD_URL.replace("i2.y.qq.com", host)
            for url in (external, "qqmusic://qq.com/ui/openUrl?url=" + quote(external, safe="")):
                with self.subTest(url=url):
                    value = {"TaskType": "medal_card_draw_card", "BtnJumpURL": url}
                    self.assertEqual(core._pool_id(value), "")

    def test_ladder_paid_or_changed_type_is_not_visited_or_reported_again(self):
        before = browse_task()
        cases = [{"Title": "购买商品"}, {"TaskType": "homepage_browsed"}]
        for changes in cases:
            with self.subTest(changes=changes):
                refreshed = {**before, "AchievedTimes": 1,
                             "ExtInfoStr": "offline-next", **changes}
                client = Mock()
                client.task_list.side_effect = [[deepcopy(before)], [deepcopy(refreshed)],
                                                [deepcopy(refreshed)]]
                client.report_task.return_value = (True, 0)
                out = core.complete_medal_tasks(client, "offline-medal")
                client.visit_task.assert_called_once_with(before)
                client.report_task.assert_called_once_with("offline-medal", before)
                self.assertEqual(client.task_list.call_count, 3)
                self.assertEqual(out["tasks"][0]["finish"], 1)
                self.assertFalse(out["tasks"][0]["done"])

    def test_failed_final_readback_retains_last_confirmed_ladder_progress(self):
        before = browse_task()
        progress = {**before, "AchievedTimes": 1, "ExtInfoStr": "offline-next"}
        client = Mock()
        client.task_list.side_effect = [
            [deepcopy(before)],
            [deepcopy(progress)],
            [deepcopy(progress)],  # No further growth: do not perform a third action.
            core.QQError("offline final read failed"),
            core.QQError("offline final retry failed"),
        ]
        client.report_task.return_value = (True, 0)
        out = core.complete_medal_tasks(client, "offline-medal")
        self.assertEqual(client.visit_task.call_count, 2)
        self.assertEqual(client.report_task.call_count, 2)
        self.assertEqual(client.visit_task.call_args_list[0].args[0]["ExtInfoStr"], "offline-first")
        self.assertEqual(client.visit_task.call_args_list[1].args[0]["ExtInfoStr"], "offline-next")
        self.assertEqual(client.task_list.call_count, 5)
        self.assertEqual(out["tasks"][0]["finish"], 1)
        self.assertFalse(out["tasks"][0]["verified"])
        self.assertFalse(out["tasks"][0]["done"])
        self.assertEqual(out["auto"], 0)
        self.assertEqual(out["done"], 0)
        self.assertTrue(out["verify_error"])


if __name__ == "__main__":
    unittest.main()
