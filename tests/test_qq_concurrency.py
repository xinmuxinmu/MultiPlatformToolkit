"""QQ 刷新与执行互斥回归：内存状态、假接口，无真实账号或磁盘配置。"""
from copy import deepcopy
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.qq_tasks import core, routes


class ObservedLock:
    def __init__(self):
        self.lock = threading.Lock()
        self.enter_attempted = threading.Event()

    def acquire(self, *args, **kwargs):
        return self.lock.acquire(*args, **kwargs)

    def release(self):
        self.lock.release()

    def __enter__(self):
        self.enter_attempted.set()
        self.acquire()
        return self

    def __exit__(self, *_args):
        self.release()


class QQConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.original_core_lock = core.OPERATION_LOCK
        self.original_route_lock = routes._refresh_lock
        self.operation_lock = ObservedLock()
        self.config = {**core.DEFAULT_MUSIC, "cookie": "offline-placeholder",
                       "sign_enable": False, "medal_enable": True, "card_pool_enable": False}
        self.cache = {}
        self.saves = []
        self.patches = [
            patch.object(core, "OPERATION_LOCK", self.operation_lock),
            patch.object(routes, "_refresh_lock", self.operation_lock),
            patch.object(core, "load_music", return_value=self.config),
            patch.object(core, "load_state", side_effect=lambda: deepcopy(self.cache)),
            patch.object(core, "save_state", side_effect=self.save_state),
            patch.object(core.MusicClient, "sign_status", return_value={"signed": False}),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def save_state(self, value):
        self.cache.clear()
        self.cache.update(deepcopy(value))
        self.saves.append(deepcopy(value))

    def test_routes_use_the_task_operation_lock(self):
        # 补丁前的引用也必须绑定同一个模块级锁，确保真实入口已接线。
        self.assertIs(self.original_route_lock, self.original_core_lock)

    def test_earlier_refresh_cannot_overwrite_later_completed_task(self):
        refresh_read = threading.Event()
        release_refresh = threading.Event()
        task_started = threading.Event()
        task_finished = threading.Event()
        self.addCleanup(release_refresh.set)
        pending = {"ok": True, "medals": [{"id": "one", "tasks": [{"done": False}]}]}
        complete = {"ok": True, "medals": [{"id": "one", "tasks": [{"done": True}]}],
                    "done": 1, "fail": 0, "skipped": 0}
        failures = []

        def refresh_medals(_cookie):
            refresh_read.set()
            if not release_refresh.wait(3):
                raise AssertionError("test did not release refresh")
            return deepcopy(pending)

        def run_job():
            task_started.set()
            try:
                job._run()
            except Exception as exc:
                failures.append(exc)
            finally:
                task_finished.set()

        def run_refresh():
            try:
                routes.refresh()
            except Exception as exc:
                failures.append(exc)

        job = core.Task()
        with patch.object(core, "refresh_medals", side_effect=refresh_medals), \
                patch.object(job, "_run_medals", return_value=complete) as run_medals:
            refresher = threading.Thread(target=run_refresh, daemon=True)
            runner = threading.Thread(target=run_job, daemon=True)
            refresher.start()
            try:
                self.assertTrue(refresh_read.wait(2))
                runner.start()
                self.assertTrue(task_started.wait(2))
                self.assertTrue(self.operation_lock.enter_attempted.wait(2))
                # 刷新仍持锁；真实任务尚不能开始网络操作或保存状态。
                self.assertFalse(task_finished.is_set())
                run_medals.assert_not_called()
            finally:
                release_refresh.set()
                refresher.join(3)
                if runner.ident is not None:
                    runner.join(3)
            self.assertFalse(refresher.is_alive())
            self.assertFalse(runner.is_alive())
            self.assertEqual(failures, [])
            run_medals.assert_called_once()
        self.assertEqual(len(self.saves), 2)
        self.assertFalse(self.saves[0]["medal"]["medals"][0]["tasks"][0]["done"])
        self.assertTrue(self.saves[1]["medal"]["medals"][0]["tasks"][0]["done"])
        self.assertTrue(self.cache["medal"]["medals"][0]["tasks"][0]["done"])

    def test_refresh_while_operation_is_running_returns_conflict_without_reading(self):
        with self.operation_lock, patch.object(core, "refresh_medals") as refresh:
            response = routes.refresh()
        self.assertEqual(response.status_code, 409)
        refresh.assert_not_called()
        self.assertEqual(self.saves, [])

    def test_saving_settings_does_not_wait_for_operation_lock(self):
        with self.operation_lock, patch.object(core, "save_music") as save:
            result = routes.save({"music": {"schedule_time": "10:05"}})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(save.call_args.args[0]["schedule_time"], "10:05")


if __name__ == "__main__":
    unittest.main()
