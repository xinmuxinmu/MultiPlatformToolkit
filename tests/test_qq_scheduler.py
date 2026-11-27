"""QQ 音乐定时离线回归：临时目录、假任务，不读取账号配置或访问网络。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.qq_tasks import scheduler


class FakeTask:
    def __init__(self):
        self.running = False
        self.calls = 0
        self.accept = True
        self.error = None

    def start(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.accept


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.records = Path(self.temp.name) / "runs"
        self.config = {"cookie": "offline-placeholder", "schedule_enabled": True,
                       "schedule_time": "09:30"}
        self.task = FakeTask()
        self.now = datetime(2026, 11, 27, 10, 0, tzinfo=scheduler.SHANGHAI)
        self.subject = self.make_scheduler()

    def make_scheduler(self, task=None):
        return scheduler.DailyScheduler(config_loader=lambda: self.config,
                                        task=task or self.task, record_dir=self.records,
                                        clock=lambda: self.now, interval=60)

    def record(self, date="2026-11-27"):
        return json.loads((self.records / (date + ".json")).read_text("utf-8"))

    def test_config_validation(self):
        scheduler.validate_config({})
        for values in ({"schedule_enabled": 1}, {"schedule_enabled": "true"},
                       {"schedule_time": "9:30"}, {"schedule_time": "24:00"},
                       {"schedule_time": "00:60"}, {"schedule_time": None},
                       {"schedule_time": "09:30\n"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                scheduler.validate_config(values)

    def test_disabled_early_logged_out_and_no_tasks_do_not_claim_date(self):
        scenarios = [
            {"schedule_enabled": False},
            {"schedule_time": "10:01"},
            {"cookie": ""},
            {"sign_enable": False, "medal_enable": False, "card_pool_enable": False},
        ]
        initial = dict(self.config)
        for changes in scenarios:
            with self.subTest(changes=changes):
                self.config = {**initial, **changes}
                self.assertFalse(self.subject.tick())
                self.assertFalse(self.records.exists())
        self.assertEqual(self.task.calls, 0)

    def test_uses_beijing_date_and_exact_scheduled_minute(self):
        self.now = datetime(2026, 11, 27, 1, 29, tzinfo=timezone.utc)
        self.assertFalse(self.subject.tick())
        self.now = datetime(2026, 11, 27, 1, 30, tzinfo=timezone.utc)
        self.assertTrue(self.subject.tick())
        record = self.record()
        self.assertEqual(record["date"], "2026-11-27")
        self.assertEqual(record["timezone"], "Asia/Shanghai")
        self.assertEqual(record["status"], "started")
        self.assertTrue(record["started_at"].endswith("+08:00"))
        self.assertNotIn("offline-placeholder", json.dumps(record))

    def test_late_start_runs_once_and_restart_does_not_repeat(self):
        self.assertTrue(self.subject.tick())
        self.assertFalse(self.subject.tick())
        restarted = self.make_scheduler()
        self.assertFalse(restarted.tick())
        self.assertEqual(self.task.calls, 1)
        self.now = datetime(2026, 11, 28, 10, 0, tzinfo=scheduler.SHANGHAI)
        self.assertTrue(restarted.tick())
        self.assertEqual(self.task.calls, 2)

    def test_busy_task_defers_without_consuming_the_day(self):
        self.task.running = True
        self.assertFalse(self.subject.tick())
        self.assertFalse(self.records.exists())
        self.task.running = False
        self.assertTrue(self.subject.tick())
        self.assertEqual(self.task.calls, 1)

    def test_start_race_releases_claim_and_retries_when_idle(self):
        self.task.accept = False
        self.assertFalse(self.subject.tick())
        self.assertFalse((self.records / "2026-11-27.json").exists())
        self.task.accept = True
        self.assertTrue(self.subject.tick())
        self.assertEqual(self.task.calls, 2)

    def test_independent_schedulers_cannot_trigger_the_same_day_twice(self):
        other_task = FakeTask()
        other = self.make_scheduler(other_task)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda item: item.tick(), [self.subject, other]))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.task.calls + other_task.calls, 1)

    def test_existing_crash_or_unreadable_claim_prevents_automatic_retry(self):
        self.records.mkdir()
        path = self.records / "2026-11-27.json"
        for content in ('{"date":"2026-11-27","status":"claimed"}', ""):
            with self.subTest(content=content):
                path.write_text(content, "utf-8")
                self.assertFalse(self.subject.tick())
        self.assertEqual(self.subject.snapshot()["last_trigger"]["status"], "unreadable")
        self.assertEqual(self.task.calls, 0)

    def test_start_exception_is_reserved_and_error_text_is_not_persisted(self):
        self.task.error = RuntimeError("private-offline-error")
        with self.assertRaises(RuntimeError):
            self.subject.tick()
        self.assertFalse(self.make_scheduler().tick())
        record = self.record()
        self.assertEqual(record["status"], "start_error")
        self.assertEqual(record["error"], "RuntimeError")
        self.assertNotIn("private-offline-error", json.dumps(record))

    def test_time_changes_apply_before_trigger_and_next_day_after_trigger(self):
        self.config["schedule_time"] = "11:00"
        self.assertFalse(self.subject.tick())
        self.config["schedule_time"] = "09:00"
        self.assertTrue(self.subject.tick())
        self.config.update(schedule_enabled=False, schedule_time="09:59")
        self.assertFalse(self.subject.tick())
        self.config["schedule_enabled"] = True
        self.assertFalse(self.subject.tick())
        self.now = datetime(2026, 11, 28, 9, 58, tzinfo=scheduler.SHANGHAI)
        self.assertFalse(self.subject.tick())
        self.now = datetime(2026, 11, 28, 9, 59, tzinfo=scheduler.SHANGHAI)
        self.assertTrue(self.subject.tick())

    def test_claim_is_durable_before_task_starts(self):
        def check_start():
            self.assertEqual(self.record()["status"], "claimed")
            return True
        with patch.object(self.task, "start", side_effect=check_start):
            self.assertTrue(self.subject.tick())

    def test_initial_disk_failure_never_starts_and_can_retry(self):
        with patch.object(scheduler.os, "fsync", side_effect=OSError("offline disk full")):
            with self.assertRaises(OSError):
                self.subject.tick()
        self.assertEqual(self.task.calls, 0)
        self.assertFalse((self.records / "2026-11-27.json").exists())
        self.assertTrue(self.subject.tick())

    def test_post_start_disk_failure_keeps_claim_and_prevents_repeat(self):
        with patch.object(self.subject, "_write_record", side_effect=OSError("offline disk full")):
            with self.assertRaises(OSError):
                self.subject.tick()
        self.assertEqual(self.record()["status"], "claimed")
        self.assertFalse(self.make_scheduler().tick())
        self.assertEqual(self.task.calls, 1)

    def test_start_stop_are_idempotent_and_allow_restart(self):
        self.config["schedule_enabled"] = False
        checked = threading.Event()
        with patch.object(self.subject, "tick", side_effect=lambda: checked.set()):
            self.assertTrue(self.subject.start())
            self.addCleanup(self.subject.stop)
            self.assertTrue(checked.wait(2))
            self.assertFalse(self.subject.start())
            self.assertTrue(self.subject.snapshot()["active"])
            self.subject.stop()
            self.subject.stop()
            self.assertFalse(self.subject.snapshot()["active"])
            checked.clear()
            self.assertTrue(self.subject.start())
            self.assertTrue(checked.wait(2))
        self.subject.stop()
        self.assertEqual(self.task.calls, 0)

    def test_route_rejects_invalid_schedule_without_saving(self):
        from modules.qq_tasks import routes
        defaults = {**routes.core.DEFAULT_MUSIC, "schedule_enabled": False,
                    "schedule_time": scheduler.DEFAULT_TIME}
        with patch.object(routes.core, "DEFAULT_MUSIC", defaults), \
                patch.object(routes.core, "load_music", return_value=dict(defaults)), \
                patch.object(routes.core, "save_music") as save:
            response = routes.save({"music": {"schedule_time": "25:00"}})
        self.assertEqual(response.status_code, 400)
        save.assert_not_called()

    def test_disabled_schedule_does_not_block_manual_route(self):
        from modules.qq_tasks import routes
        self.config["schedule_enabled"] = False
        self.assertFalse(self.subject.tick())
        with patch.object(routes.core, "TASK", self.task):
            self.assertEqual(routes.run(), {"ok": True, "error": ""})
        self.assertEqual(self.task.calls, 1)
        self.assertFalse(self.records.exists())


if __name__ == "__main__":
    unittest.main()
