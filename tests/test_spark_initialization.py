"""Exercise browser setup without importing account stores or opening browsers."""
import ast
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock


SOURCE = Path(__file__).resolve().parents[1] / "modules" / "spark" / "spark_main.py"


def load_function(name, namespace):
    node = next(node for node in ast.parse(SOURCE.read_text(encoding="utf-8")).body
                if isinstance(node, ast.FunctionDef) and node.name == name)
    node.decorator_list = []
    node.args.defaults = [ast.Constant(None) for _ in node.args.defaults]
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace[name]


class SparkInitializationTests(unittest.TestCase):
    def context(self):
        runtime = SimpleNamespace(driver=None, douyin=None, init=False, Login_is_bool=False,
                                  active_account_id="a", login_pending=None, account_lock=threading.Lock())
        names = ("_create_account_driver", "Douyin", "load_friends", "_douyin_logged_in",
                 "start_scheduler", "restore_tasks", "_fill_missing_sec_uids",
                 "_try_auto_login_with_saved_cookies", "_save_current_cookies_to_account",
                 "_mark_browser_alive", "_quit_browser", "unload_tasks", "_start_browser_session")
        ctx = {name: Mock(name=name) for name in names}
        ctx.update(runtime=runtime, _user_cache={}, config={"browser_name": "edge"},
                   normalize_browser_name=lambda value: value, get_browser_label=lambda value: "Edge",
                   init_lock=threading.Lock(), require_auth=lambda _: None,
                   progress=Mock(), account_store=Mock(), group_store=Mock())
        ctx["progress"].snapshot.return_value = {"running": False}
        ctx["_create_account_driver"].return_value = (Mock(name="driver"), None)
        return ctx

    def test_initialized_browser_without_login_explicitly_requests_login(self):
        ctx = self.context()
        ctx["Douyin"].return_value.ensure_chat_page.return_value = False
        result = load_function("_start_browser_session", ctx)("a")
        self.assertTrue(result["initialized"])
        self.assertFalse(result["logged_in"])
        self.assertIn("尚未登录", result["data"])
        ctx["_try_auto_login_with_saved_cookies"].assert_called_once()
        ctx["_save_current_cookies_to_account"].assert_not_called()

    def test_navigation_exception_closes_partial_session_and_reports_failure(self):
        ctx = self.context()
        ctx["Douyin"].return_value.ensure_chat_page.side_effect = RuntimeError("session closed")
        result = load_function("_start_browser_session", ctx)("a")
        self.assertEqual(result["code"], 500)
        self.assertIn("session closed", result["data"])
        ctx["_quit_browser"].assert_called_once()
        ctx["start_scheduler"].assert_not_called()

    def test_init_uses_selected_account_before_opening_browser(self):
        ctx = self.context()
        def set_active(account_id):
            ctx["runtime"].active_account_id = account_id
        ctx["account_store"].set_active_account.side_effect = set_active
        ctx["_start_browser_session"].return_value = {"code": 200}
        self.assertEqual(load_function("Init", ctx)(None, "b"), {"code": 200})
        ctx["account_store"].get_account.assert_called_once_with("b")
        ctx["unload_tasks"].assert_called_once()
        ctx["group_store"].load_groups.assert_called_once()
        ctx["_start_browser_session"].assert_called_once_with(account_id="b")

    def test_init_blocks_reopening_during_send_or_pending_login(self):
        for state in ("sending", "login"):
            with self.subTest(state=state):
                ctx = self.context()
                if state == "sending":
                    ctx["progress"].snapshot.return_value = {"running": True}
                else:
                    ctx["runtime"].login_pending = {"account_id": "a"}
                self.assertEqual(load_function("Init", ctx)(None, "a")["code"], 400)
                ctx["_start_browser_session"].assert_not_called()

    def test_init_reuses_live_driver_and_does_not_claim_login(self):
        ctx = self.context()
        ctx["runtime"].init = True
        ctx["runtime"].driver = Mock()
        result = load_function("Init", ctx)(None, "a")
        self.assertEqual(result["code"], 200)
        self.assertFalse(result["logged_in"])
        self.assertIn("尚未登录", result["data"])
        ctx["_start_browser_session"].assert_not_called()

    def batch_context(self):
        ctx = self.context()
        ctx.update(scheduled_tasks={"one": {}, "two": {}}, risk_state={},
                   check_driver=Mock(return_value=None), threading=Mock(), _batch_send_worker=Mock())
        ctx["progress"].begin.return_value = {"job_id": "fixture-batch"}
        return ctx

    def test_batch_deduplicates_task_ids_before_starting_worker(self):
        ctx = self.batch_context()
        result = load_function("run_batch_once", ctx)({"task_ids": ["one", "one", "two"]})
        self.assertEqual(result["code"], 200)
        ctx["progress"].begin.assert_called_once_with("batch", 2, "共 2 个任务")
        self.assertEqual(ctx["threading"].Thread.call_args.kwargs["args"], (["one", "two"],))
        ctx["threading"].Thread.return_value.start.assert_called_once()

    def test_batch_rejects_stale_or_malformed_ids_without_starting(self):
        for ids, code in [(["missing"], 404), ([{}], 400), ([], 400)]:
            with self.subTest(ids=ids):
                ctx = self.batch_context()
                result = load_function("run_batch_once", ctx)({"task_ids": ids})
                self.assertEqual(result["code"], code)
                ctx["threading"].Thread.assert_not_called()

    def test_batch_does_not_start_second_worker_while_sending(self):
        ctx = self.batch_context()
        ctx["progress"].snapshot.return_value = {"running": True}
        result = load_function("run_batch_once", ctx)({"task_ids": ["one"]})
        self.assertEqual(result["code"], 400)
        ctx["threading"].Thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
