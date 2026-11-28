"""酷狗任务顺序与游戏勋章任务回归测试。"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.kugou import core


def _medal_info(tasks):
    return {
        "today": 0,
        "tasks": tasks,
        "task_done": 0,
        "task_total": len(tasks),
    }


class KugouCoreTests(unittest.TestCase):
    def test_game_medal_tasks_are_skipped_without_outside_finish(self):
        logs = []
        info = _medal_info([{
            "task_id": "play_game:FvxYRx",
            "name": "「上新」勋章皮肤配对挑战",
            "finish": 0,
            "need": 1,
        }])

        class MedalClient:
            def outside_finish(self, *args, **kwargs):
                raise AssertionError("游戏任务不应走通用外部上报")

        daily = core.KugouDaily(
            {"auto_all_nonpaid": True, "userid": "123"},
            lambda level, message: logs.append((level, message)),
        )
        with patch.object(daily, "_fetch_medal", side_effect=[info, info]):
            _, done, failed, skipped = daily._run_medal_tasks(MedalClient(), 188254)

        self.assertEqual((done, failed, skipped), (0, 0, 1))
        self.assertTrue(any("未接入自动闯关" in message for _, message in logs))

    def test_run_executes_medal_flow_before_vip_claims(self):
        events = []
        state = {
            "progress": {
                "date": core.today_str(),
                "listen": False,
                "ad": 0,
            }
        }

        class Client:
            def my_info(self):
                events.append("my_info")
                return {"data": {"nickname": "测试账号"}}

            def listen_song(self):
                events.append("listen")
                return {"status": 1}

            def ad_vip(self):
                events.append("ad")
                return {"status": 1}

        class MedalClient:
            def sign_in_additional(self, singer_id):
                events.append("medal")
                return {"errcode": 30001}

        cfg = {
            "userid": "123",
            "token": "token",
            "medal_enable": True,
            "medal_tasks_enable": False,
            "listen_enable": True,
            "ad_enable": True,
            "ad_times": 1,
        }
        daily = core.KugouDaily(cfg, lambda *_: None)
        medal_info = _medal_info([{
            "task_id": "sign_in",
            "name": "每日守护",
            "finish": 1,
            "need": 1,
        }])

        with (
            patch.object(daily, "_client", return_value=Client()),
            patch.object(daily, "_medal_client", return_value=MedalClient()),
            patch.object(daily, "_fetch_medal", return_value=medal_info),
            patch.object(daily, "_sleep"),
            patch.object(core, "load_state", return_value=state),
            patch.object(core, "save_state"),
            patch.object(core.time, "sleep"),
        ):
            result = daily.run()

        self.assertEqual(result["fail"], 0)
        self.assertLess(events.index("medal"), events.index("ad"))
        self.assertLess(events.index("ad"), events.index("listen"))
