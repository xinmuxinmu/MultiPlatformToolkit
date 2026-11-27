"""Fixture-only smoke tests for the native chat pages; never touches user accounts."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
import tempfile
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtWidgets import QApplication

from app.desktop.chat_pages import AdvancedDialog, DouyinPage, ExportPage, attachments
from app.desktop.upload import DownloadRequest, UploadRequest


class FakeApi:
    def __init__(self):
        self.calls = []
        self.headers = {}

    def call(self, method, path, callback=None, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.endswith("/auth/check"):
            data = {"authenticated": True}
        elif path.endswith("/accounts"):
            data = {"active": "fixture", "accounts": [{"id": "fixture", "name": "测试账号"}]}
        elif path.endswith("/summary"):
            data = {"data": {"output_dir": "/fixture/export", "raw_size": 0}}
        elif path.endswith("/logs"):
            data = {"logs": [], "task": {"running": False}}
        elif path.endswith("/api/status"):
            data = {"conversations": 1, "messages": 200, "scrape": {"status": "idle"}, "scheduler": {"enabled": False}}
        elif path.endswith("/api/conversations"):
            data = {"items": [{"conv_id": "c1", "name": "本地会话", "message_count": 200}], "total": 1}
        elif path.endswith("/messages"):
            data = {"items": [{"seq": 101, "msg_id": "m101", "sender_name": "测试发送者", "content": "正文", "timestamp": 100},
                              {"seq": 200, "msg_id": "m200", "sender_name": "测试发送者", "content": "末尾", "timestamp": 200}],
                    "total": 200, "has_older": True, "has_newer": False}
        elif path.endswith("/api/search"):
            data = {"items": [{"seq": 20, "content": "搜索结果"}], "total": 1}
        elif path.endswith("/scrape/log"):
            data = {"log": "测试日志"}
        else:
            data = {"ok": True}
        if callback:
            callback(data)
        if kwargs.get("done"):
            kwargs["done"]()

    def get(self, path, callback=None, **kwargs):
        self.call("GET", path, callback, **kwargs)

    def post(self, path, callback=None, **kwargs):
        self.call("POST", path, callback, **kwargs)


class NativeChatPagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def page(self, kind=ExportPage, prefix="/weibo-chat"):
        api = FakeApi()
        page = kind(api, prefix, "测试平台") if kind is ExportPage else kind(api)
        self.addCleanup(page.deleteLater)
        return api, page

    def test_constructors_never_start_requests(self):
        for prefix in ("/weibo-chat", "/bili-chat", "/taobao"):
            api, page = self.page(prefix=prefix)
            self.assertEqual(api.calls, [])
        api, page = self.page(DouyinPage)
        self.assertEqual(api.calls, [])

    def test_export_local_pagination_and_search(self):
        api, page = self.page()
        page.load_raw({"convs": [{"uid": "c1", "name": "本地会话", "messages": [
            {"id": str(index), "text": f"message {index}", "ts": index * 1000} for index in range(205)]}]})
        self.assertEqual(page.messages.table.rowCount(), 5)
        self.assertTrue(page.messages.previous.isEnabled())
        page.page_messages(-1)
        self.assertEqual(page.messages.table.rowCount(), 100)
        page.messages.search.setText("message 204")
        page.search_messages()
        self.assertEqual(page.messages.table.rowCount(), 1)
        self.assertEqual(page.messages.rows[0]["id"], "204")
        page.conv_search.setText("message 203")
        self.assertEqual(page.conversations.count(), 1)
        self.assertEqual(api.calls, [])

    def test_export_start_parameters_and_taobao_body(self):
        api, page = self.page()
        page.only.setText("123")
        page.run_export()
        method, path, kwargs = api.calls[0]
        self.assertEqual((method, path), ("POST", "/weibo-chat/api/run"))
        self.assertEqual(kwargs["params"]["only_uid"], "123")
        api, page = self.page(prefix="/taobao")
        page.urls.setPlainText("https://example.test/chat1\nhttps://example.test/chat2")
        page.run_export()
        self.assertEqual(api.calls[0][2]["body"]["urls"], ["https://example.test/chat1", "https://example.test/chat2"])

    @patch("app.desktop.chat_pages.confirmed", return_value=False)
    def test_account_and_record_deletion_require_confirmation(self, confirm):
        api, page = self.page()
        page.show_accounts({"active": "a1", "accounts": [{"id": "a1", "name": "A"}]})
        page.delete_account()
        page._current = {"uid": "c1", "name": "C"}
        page.delete_conversation()
        self.assertEqual(api.calls, [])
        self.assertEqual(confirm.call_count, 2)

    def test_activation_only_reads_local_endpoints(self):
        api, page = self.page()
        page.on_activate()
        self.assertTrue(api.calls)
        self.assertTrue(all(method == "GET" for method, _, _ in api.calls))
        self.assertFalse(any(path.endswith("/account") for _, path, _ in api.calls))

    def test_douyin_message_paging_and_global_search(self):
        api, page = self.page(DouyinPage)
        page.on_activate()
        self.assertEqual(page.messages.table.rowCount(), 2)
        api.calls.clear()
        page.page_messages(-1)
        self.assertEqual(api.calls[-1][2]["params"]["before_seq"], 101)
        api.calls.clear()
        page.search_all.setChecked(True)
        page.messages.search.setText("搜索")
        page.search_messages()
        self.assertNotIn("conv_id", api.calls[-1][2]["params"])
        self.assertEqual(page.messages.rows[0]["content"], "搜索结果")

    def test_douyin_scrape_export_and_schedule_use_native_config(self):
        api, page = self.page(DouyinPage)
        page.filter.setText("会话甲,会话乙")
        page.scrape()
        self.assertEqual(api.calls[0][2]["body"]["filter"], "会话甲,会话乙")
        api.calls.clear()
        page.export()
        self.assertEqual(api.calls[0][2]["body"]["format"], "jsonl")
        api.calls.clear()
        page.schedule_enabled.setChecked(True)
        page.schedule_cron.setText("20 10 * * *")
        page.save_schedule()
        self.assertEqual(api.calls[0][2]["body"]["conversations"], ["会话甲", "会话乙"])

    def test_advanced_dialog_has_no_implicit_request(self):
        api = FakeApi()
        dialog = AdvancedDialog(api, "/douyin-chat", [("状态", "GET", "/panel/api/status", None)])
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(api.calls, [])
        dialog.execute()
        self.assertEqual(api.calls[0][1], "/douyin-chat/panel/api/status")

    def test_attachment_fields_are_native_paths(self):
        self.assertEqual(attachments({"pics": ["images/a.jpg"], "audio": "audio/a.mp3"}), ["images/a.jpg", "audio/a.mp3"])

    @patch("app.desktop.chat_pages.upload")
    @patch("app.desktop.chat_pages.confirmed", return_value=True)
    @patch("app.desktop.chat_pages.QInputDialog.getItem", return_value=("替换（覆盖目标账号的现有数据库）", True))
    @patch("app.desktop.chat_pages.QFileDialog.getOpenFileName", return_value=("fixture.db", ""))
    def test_database_import_explicit_target_and_replace_confirmation(self, dialog, mode, confirm, upload):
        api, page = self.page(DouyinPage)
        page.show_accounts({"active": "fixture", "accounts": [{"id": "fixture", "name": "测试账号"}]})
        page.import_database()
        self.assertEqual(upload.call_args.kwargs["params"], {"account_id": "fixture", "mode": "replace"})
        self.assertIn("覆盖", confirm.call_args.args[2])
        self.assertFalse(page.import_button.isEnabled())

    @patch("app.desktop.upload.httpx.Client")
    def test_upload_streams_sqlite_bytes_with_auth(self, client):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "fixture.db"
            content = b"SQLite format 3\x00" + b"fixture data"
            file.write_bytes(content)
            sent = {}
            def post(path, **kwargs):
                sent.update(path=path, content=kwargs["content"].read(), params=kwargs["params"], headers=kwargs["headers"])
                return SimpleNamespace(is_error=False, json=lambda: {"status": "imported"})
            client.return_value.__enter__.return_value.post.side_effect = post
            request = UploadRequest("http://127.0.0.1:18000", "/import", file,
                params={"mode": "merge"}, headers={"Authorization": "Bearer fixture"})
            results, errors, completed = [], [], []
            request.signals.result.connect(results.append)
            request.signals.error.connect(errors.append)
            request.signals.finished.connect(lambda: completed.append(True))
            request.run()
            self.assertEqual(sent["content"], content)
            self.assertEqual(sent["headers"]["Content-Type"], "application/octet-stream")
            self.assertEqual(sent["headers"]["Authorization"], "Bearer fixture")
            self.assertEqual(sent["params"], {"mode": "merge"})
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
            self.assertEqual(completed, [True])

    @patch("app.desktop.upload.httpx.Client")
    def test_upload_rejects_non_sqlite_before_network(self, client):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "fixture.db"
            file.write_bytes(b"not a sqlite database")
            request = UploadRequest("http://127.0.0.1:18000", "/import", file)
            errors = []
            request.signals.error.connect(errors.append)
            request.run()
            self.assertEqual(len(errors), 1)
            client.assert_not_called()

    @patch("app.desktop.chat_pages.download")
    @patch("app.desktop.chat_pages.QFileDialog.getSaveFileName", return_value=("fixture.png", ""))
    @patch("app.desktop.chat_pages.QDialog.exec", return_value=1)
    def test_screenshot_uses_binary_download_and_current_sequence_range(self, dialog, save, download):
        api, page = self.page(DouyinPage)
        page.show_conversations({"items": [{"conv_id": "c1", "name": "测试会话"}], "total": 1})
        page.screenshot()
        self.assertEqual(download.call_args.args[1], "/douyin-chat/api/conversations/c1/screenshot")
        self.assertEqual(download.call_args.kwargs["params"]["start_seq"], 101)
        self.assertEqual(download.call_args.kwargs["params"]["end_seq"], 200)
        self.assertEqual(download.call_args.args[2], "fixture.png")
        self.assertFalse(page.screenshot_button.isEnabled())

    @patch("app.desktop.upload.httpx.Client")
    def test_png_response_is_saved_as_exact_binary_bytes(self, client):
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (2, 2), "green").save(output, format="PNG")
        png = output.getvalue()
        response = SimpleNamespace(is_error=False, headers={"content-type": "image/png"},
            iter_bytes=lambda: iter([png[:3], png[3:11], png[11:]]))
        client.return_value.__enter__.return_value.stream.return_value.__enter__.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.png"
            worker = DownloadRequest("http://127.0.0.1:18000", "/image", target, headers={"Authorization": "Bearer fixture"})
            results, errors = [], []
            worker.signals.result.connect(results.append)
            worker.signals.error.connect(errors.append)
            worker.run()
            self.assertEqual(errors, [])
            self.assertEqual(target.read_bytes(), png)
            self.assertEqual(results[0]["bytes"], len(png))
            with Image.open(target) as image:
                self.assertEqual(image.size, (2, 2))

    @patch("app.desktop.upload.httpx.Client")
    def test_error_json_never_overwrites_an_existing_png(self, client):
        response = SimpleNamespace(is_error=True, read=lambda: b'{"detail":"fixture error"}',
            json=lambda: {"detail": "fixture error"})
        client.return_value.__enter__.return_value.stream.return_value.__enter__.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.png"
            target.write_bytes(b"original image")
            worker = DownloadRequest("http://127.0.0.1:18000", "/image", target)
            errors = []
            worker.signals.error.connect(errors.append)
            worker.run()
            self.assertEqual(target.read_bytes(), b"original image")
            self.assertEqual(errors, ["fixture error"])
            self.assertEqual(list(Path(directory).glob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
