import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest.mock import patch
import shiboken6

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.desktop.service_pages import (
    OperationsPage, SparkPage, StarskyPage, operation_is_delete, schema_example,
)


class FakeApi:
    def __init__(self):
        self.headers = {}
        self.requests = []
        self.catalog = {"operations": []}
        self.responses = {}

    def call(self, method, path, callback=None, **kwargs):
        self.requests.append({"method": method, "path": path, "headers": dict(self.headers), **kwargs})
        if callback and path == "/api/desktop/catalog":
            callback(self.catalog)
        elif self.responses.get(path):
            response = self.responses[path].pop(0)
            if isinstance(response, Exception):
                if kwargs.get("error"):
                    kwargs["error"](str(response))
            elif callback:
                callback(response)
        if kwargs.get("done"):
            kwargs["done"]()

    def get(self, path, callback=None, **kwargs):
        self.call("GET", path, callback, **kwargs)

    def post(self, path, callback=None, **kwargs):
        self.call("POST", path, callback, **kwargs)


class ServicePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.api = FakeApi()
        self.pages = []

    def tearDown(self):
        for page in self.pages:
            page.close()
            if shiboken6.isValid(page):
                shiboken6.delete(page)
        self.app.processEvents()

    def page(self, kind):
        page = kind(self.api)
        self.pages.append(page)
        return page

    def test_construction_performs_no_requests(self):
        for kind in (SparkPage, StarskyPage, OperationsPage):
            self.page(kind)
        self.assertEqual(self.api.requests, [])

    def test_activation_and_poll_only_read_state(self):
        spark = self.page(SparkPage)
        spark.on_activate()
        spark.poll()
        self.assertEqual([request["path"] for request in self.api.requests],
                         ["/spark/Api/Account/List", "/spark/Time/getlist", "/spark/Time/progress"])
        self.assertTrue(all(request["method"] == "GET" for request in self.api.requests))
        self.api.requests.clear()
        starsky = self.page(StarskyPage)
        starsky.on_activate()
        starsky.poll()
        self.assertEqual([request["path"] for request in self.api.requests], ["/starsky/api/status"])

    def test_operations_selection_does_not_execute(self):
        self.api.catalog = {"operations": [{"method": "POST", "path": "/danger/send", "summary": "发送", "parameters": []}]}
        page = self.page(OperationsPage)
        page.on_activate()
        page.operation_list.setCurrentRow(0)
        page.poll()
        self.assertEqual([request["path"] for request in self.api.requests], ["/api/desktop/catalog"])

    def test_dynamic_parameters_encoding_json_and_headers(self):
        operation = {"method": "POST", "path": "/items/{name}", "parameters": [
            {"name": "name", "in": "path", "required": True, "schema": {"type": "string"}},
            {"name": "limit", "in": "query", "schema": {"type": "integer"}},
            {"name": "enabled", "in": "query", "schema": {"type": "boolean", "default": False}},
            {"name": "X-Mode", "in": "header", "schema": {"type": "string"}},
        ], "requestBody": {"required": True, "content": {"application/json": {"schema": {
            "type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}}}}}
        page = self.page(OperationsPage)
        page._catalog_loaded({"operations": [operation]})
        page.operation_list.setCurrentRow(0)
        page.parameter_controls[0][1].setText("中文/a")
        page.parameter_controls[1][1].setText("12")
        page.parameter_controls[3][1].setText("test")
        page.body.setPlainText('{"text":"hello"}')
        page.token.setText("my-token")
        page.execute()
        request = self.api.requests[-1]
        self.assertEqual(request["path"], "/items/%E4%B8%AD%E6%96%87%2Fa")
        self.assertEqual(request["params"], {"limit": 12, "enabled": False})
        self.assertEqual(request["body"], {"text": "hello"})
        self.assertEqual(request["headers"], {"Authorization": "Bearer my-token", "X-Mode": "test"})
        self.assertEqual(self.api.headers, {"Authorization": "Bearer my-token"})

    def test_required_and_numeric_parameters_validate(self):
        page = self.page(OperationsPage)
        page._catalog_loaded({"operations": [{"method": "GET", "path": "/items/{id}", "parameters": [
            {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}]}]})
        page.operation_list.setCurrentRow(0)
        with self.assertRaisesRegex(ValueError, "必填"):
            page.build_request()
        page.parameter_controls[0][1].setText("true")
        with self.assertRaisesRegex(ValueError, "整数"):
            page.build_request()

    def test_declined_delete_and_sending_do_not_run(self):
        operations = self.page(OperationsPage)
        operations._catalog_loaded({"operations": [{"method": "DELETE", "path": "/items/1"}]})
        operations.operation_list.setCurrentRow(0)
        with patch("app.desktop.service_pages._confirm", return_value=False):
            operations.execute()
        self.assertEqual(self.api.requests, [])
        spark = self.page(SparkPage)
        spark._tasks_loaded({"code": 200, "data": {"tasks": [{"task_id": "22:00_friend", "name": "friend"}]}})
        spark.tasks.setCurrentItem(spark.tasks.topLevelItem(0))
        with patch("app.desktop.service_pages._confirm", return_value=False):
            spark.run_task()
            spark.delete_task()
        self.assertEqual(self.api.requests, [])

    def test_starsky_config_is_native_and_poll_preserves_draft(self):
        page = self.page(StarskyPage)
        page._status_loaded({"config": {"软件路径": "before.exe", "自动启动": "0", "等待超时": "42", "签到后关闭软件": "1"}, "state": {}}, True)
        self.assertFalse(page.auto_start.isChecked())
        self.assertEqual(page.wait.value(), 42)
        page.executable.setText("draft.exe")
        page._status_loaded({"config": {"软件路径": "before.exe"}, "state": {"running": True}})
        self.assertEqual(page.executable.text(), "draft.exe")
        page.save()
        self.assertEqual(self.api.requests[-1]["body"]["软件路径"], "draft.exe")

    def spark_tasks(self):
        page = self.page(SparkPage)
        page._tasks_loaded({"data": {"tasks": [
            {"task_id": f"22:00_friend{index}", "name": f"friend{index}"} for index in range(3)]}})
        return page

    def test_spark_select_all_invert_clear_and_refresh_keep_all_selections(self):
        page = self.spark_tasks()
        page.select_tasks("all")
        self.assertEqual(len(page._selected_tasks(False)), 3)
        self.assertEqual(page.task_selected_count.text(), "已选 3 项")
        page._tasks_loaded({"data": {"tasks": [
            {"task_id": f"22:00_friend{index}", "name": f"friend{index}"} for index in range(3)]}})
        self.assertEqual(len(page._selected_tasks(False)), 3)
        page.select_tasks("invert")
        self.assertEqual(page._selected_tasks(False), [])
        self.assertFalse(page.run_button.isEnabled())
        page.select_tasks("all")
        page.select_tasks("none")
        self.assertEqual(page._selected_tasks(False), [])
        self.assertEqual(self.api.requests, [])

    def test_spark_runs_all_selected_once_and_can_stop(self):
        page = self.spark_tasks()
        page.select_tasks("all")
        with patch("app.desktop.service_pages._confirm", return_value=True):
            page.run_task()
            page.run_task()
        self.assertEqual(len(self.api.requests), 1)
        self.assertEqual(self.api.requests[0]["method"], "POST")
        self.assertEqual(self.api.requests[0]["path"], "/spark/Time/runBatch")
        self.assertEqual(len(self.api.requests[0]["body"]["task_ids"]), 3)
        self.assertFalse(page.init_button.isEnabled())
        page.stop_sending()
        self.assertEqual(self.api.requests[-1]["path"], "/spark/Time/batchCancel")
        page._progress_loaded({"data": {"running": False}})
        self.assertTrue(page.run_button.isEnabled())
        self.assertTrue(page.init_button.isEnabled())

    def test_spark_send_error_unlocks_controls(self):
        page = self.spark_tasks()
        page.select_tasks("all")
        self.api.responses["/spark/Time/runBatch"] = [ValueError("浏览器未初始化")]
        with patch("app.desktop.service_pages._confirm", return_value=True):
            page.run_task()
        self.assertTrue(page.run_button.isEnabled())
        self.assertIn("浏览器未初始化", page.status.text())

    def test_spark_batch_delete_continues_and_reports_partial_failure(self):
        page = self.spark_tasks()
        page.select_tasks("all")
        self.api.responses["/spark/Time/del"] = [{"data": "deleted"}, ValueError("不存在"), {"data": "deleted"}]
        with patch("app.desktop.service_pages._confirm", return_value=True):
            page.delete_task()
        for _ in range(6):
            self.app.processEvents()
        deleted = [request for request in self.api.requests if request["path"] == "/spark/Time/del"]
        self.assertEqual([request["params"]["task_id"] for request in deleted],
                         [f"22:00_friend{index}" for index in range(3)])
        self.assertIn("成功 2 项，失败 1 项", page.status.text())
        self.assertFalse(page._batch_busy)

    def test_spark_selected_friends_add_every_target_and_preserve_checks(self):
        page = self.page(SparkPage)
        friends = {"data": {"list": {"Alice": ["", "3天", "uid-a"], "Bob": ["", "", "uid-b"]}}}
        page._friends_loaded(friends)
        page.select_friends("all")
        self.assertEqual(page.friend_selected_count.text(), "已选 2 位")
        page._friends_loaded(friends)
        self.assertEqual(len(page.friends.checked_rows()), 2)
        page.use_friend_selection()
        self.assertTrue(page.use_selected_friends.isChecked())
        self.assertFalse(page.friend.isEnabled())
        page.message.setPlainText("hello")
        self.api.responses["/spark/Time/add"] = [{"data": "added"}, {"data": "added"}]
        with patch("app.desktop.service_pages._confirm", return_value=True):
            page.add_task()
        for _ in range(5):
            self.app.processEvents()
        added = [request for request in self.api.requests if request["path"] == "/spark/Time/add"]
        self.assertEqual([request["params"]["name"] for request in added], ["Alice", "Bob"])
        self.assertIn("成功 2 项", page.status.text())
        page.select_friends("invert")
        self.assertEqual(page.friends.checked_rows(), [])

    def test_spark_account_change_clears_friend_targets(self):
        page = self.spark_tasks()
        accounts = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
        page._accounts_loaded({"data": {"active_id": "a", "accounts": accounts}})
        page._friends_loaded({"data": {"list": {"Alice": ["", "", "uid-a"]}}})
        page.select_friends("all")
        page.select_tasks("all")
        page._accounts_loaded({"data": {"active_id": "b", "accounts": accounts}})
        self.assertEqual(page.friends.topLevelItemCount(), 0)
        self.assertEqual(page._selected_tasks(False), [])
        self.assertEqual(page.friend_selected_count.text(), "已选 0 位")

    def test_spark_init_and_login_are_separate_and_include_selected_account(self):
        page = self.page(SparkPage)
        page.accounts.addItem("测试账号", "fixture-account")
        page.initialize_browser()
        self.assertEqual(self.api.requests[-1]["path"], "/spark/Api/Init")
        self.assertEqual(self.api.requests[-1]["params"], {"account_id": "fixture-account"})
        self.assertIn("初始化", page.status.text())
        page.login()
        self.assertEqual(self.api.requests[-1]["path"], "/spark/Api/Account/BrowserLogin")
        self.assertEqual(self.api.requests[-1]["body"], {"account_id": "fixture-account"})
        page._browser_error("找不到浏览器，请检查路径")
        self.assertFalse(page._login_pending)
        self.assertIn("找不到浏览器", page.status.text())

    def test_spark_tasks_cannot_target_unactivated_account(self):
        page = self.spark_tasks()
        page._accounts_loaded({"data": {"active_id": "a", "accounts": [
            {"id": "a", "name": "A"}, {"id": "b", "name": "B"}]}})
        page.accounts.setCurrentIndex(1)
        page.select_tasks("all")
        with patch("app.desktop.service_pages.QMessageBox.information") as notice:
            page.run_task()
            page.delete_task()
        self.assertEqual(notice.call_count, 2)
        self.assertEqual(self.api.requests, [])

    def test_schema_examples_and_legacy_delete_names(self):
        self.assertEqual(schema_example({"type": "object", "required": ["count"], "properties": {
            "count": {"type": "integer", "minimum": 2}, "optional": {"type": "string"}}}), {"count": 2})
        self.assertTrue(operation_is_delete({"method": "GET", "path": "/spark/Time/del"}))
        self.assertTrue(operation_is_delete({"method": "GET", "path": "/spark/Api/Account/Remove"}))
        self.assertFalse(operation_is_delete({"method": "GET", "path": "/spark/Time/getlist"}))


if __name__ == "__main__":
    unittest.main()
