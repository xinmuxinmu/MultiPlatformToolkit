"""Exercise model selection and client setup without keys or upstream requests."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from app.desktop.ai_gateway_page import AIGatewayPage
from modules.ai_gateway.connection import build_connection_info


BASE = "http://127.0.0.1:18765"
STATE = {
    "base_url": BASE + "/v1",
    "providers": [
        {"id": "workbuddy", "name": "WorkBuddy", "capabilities": {"chat": True}},
        {"id": "trae", "name": "Trae CN", "capabilities": {"chat": True}},
        {"id": "zhipu", "name": "智谱清言", "capabilities": {"chat": True}},
        {"id": "qoder", "name": "Qoder", "capabilities": {"chat": False}},
    ],
    "accounts": [{"id": "account-test", "provider": "workbuddy", "label": "Mock account", "enabled": True}],
}
MODELS = [{"id": "workbuddy/deepseek-v4.1-flash"},
          {"id": "trae/Doubao-Seed-Code"}, {"id": "zhipu/glm-5.3-flash"}]


class FakeApi:
    base_url = BASE

    def __init__(self):
        self.calls = []
        self.info = build_connection_info(STATE["base_url"], STATE["providers"], STATE["accounts"], MODELS)
        self.defer = False
        self.failure = None
        self.pending = []

    def get(self, path, callback=None, *, done=None, error=None, **_):
        self.calls.append(path)
        if path == "/ai-gateway/api/state":
            value = deepcopy(STATE)
        elif path == "/ai-gateway/api/connection":
            if self.defer:
                self.pending.append((callback, done, error))
                return
            self.complete((callback, done, error))
            return
        elif path == "/ai-gateway/api/key":
            value = {"api_key": "mock-export-key"}
        else:
            raise AssertionError("Unexpected request: " + path)
        if callback:
            callback(value)
        if done:
            done()

    def complete(self, request=None):
        callback, done, error = request or self.pending.pop(0)
        if self.failure:
            if error:
                error(self.failure)
        elif callback:
            callback(deepcopy(self.info))
        if done:
            done()


class GatewayDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.api = FakeApi()
        self.page = AIGatewayPage(self.api)
        self.page.refresh()

    def tearDown(self):
        self.application.clipboard().clear()
        self.page.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.application.processEvents()

    def select(self, model):
        index = self.page.model_picker.findData(model)
        self.assertGreaterEqual(index, 0)
        self.page.model_picker.setCurrentIndex(index)

    def test_activation_loads_client_details_and_catalog_without_reading_key(self):
        self.assertEqual(self.api.calls, ["/ai-gateway/api/state", "/ai-gateway/api/connection"])
        self.assertEqual(self.page.models.topLevelItemCount(), 3)
        self.assertEqual(self.page.provider_table.topLevelItemCount(), 4)
        self.assertEqual(self.page.base_url.text(), BASE + "/v1")
        self.assertEqual(self.page.chat_url.text(), BASE + "/v1/chat/completions")
        self.assertEqual(self.page.models_url.text(), BASE + "/v1/models")
        self.assertIn("完整 URL", self.page.client_help.text())
        self.assertTrue(self.page.query_models.isEnabled())
        self.assertNotIn("mock-export-key", self.page.example.toPlainText())

    def test_copy_keeps_platform_prefix_and_example_follows_selected_model(self):
        self.select("trae/Doubao-Seed-Code")
        self.page.copy_model()
        self.assertEqual(self.application.clipboard().text(), "trae/Doubao-Seed-Code")
        self.assertEqual(self.page.model_id.text(), "trae/Doubao-Seed-Code")
        self.assertFalse(self.page.messages_url.isEnabled())
        self.assertIn("system", self.page.model_note.text())
        self.assertIn("Agent", self.page.model_note.text())
        self.page.copy_request_json()
        body = json.loads(self.application.clipboard().text())
        self.assertEqual(body["model"], "trae/Doubao-Seed-Code")
        self.assertEqual(set(body), {"model", "messages", "stream"})
        self.assertEqual(len(body["messages"]), 1)
        self.assertNotIn("/ai-gateway/api/key", self.api.calls)

    def test_search_and_provider_filter_intersect_and_selection_updates_setup(self):
        self.page.model_search.setText("deepseek")
        shown = [self.page.models.topLevelItem(i) for i in range(3)
                 if not self.page.models.topLevelItem(i).isHidden()]
        self.assertEqual([item.text(0) for item in shown], ["workbuddy/deepseek-v4.1-flash"])
        self.page.model_platform.setCurrentIndex(self.page.model_platform.findData("trae"))
        self.assertIn("0 / 3", self.page.model_count.text())
        self.page.model_search.clear()
        self.assertIn("1 / 3", self.page.model_count.text())
        self.page.models.setCurrentItem(self.page.models.topLevelItem(1))
        self.assertEqual(self.page.model_id.text(), "trae/Doubao-Seed-Code")

    def test_copy_guide_explains_full_url_and_does_not_read_or_contain_key(self):
        self.select("workbuddy/deepseek-v4.1-flash")
        self.page.copy_connection_guide()
        guide = self.application.clipboard().text()
        self.assertIn(BASE + "/v1/chat/completions", guide)
        self.assertIn("workbuddy/deepseek-v4.1-flash", guide)
        self.assertIn("完整 URL 关闭", guide)
        self.assertNotIn("mock-export-key", guide)
        self.assertNotIn("/ai-gateway/api/key", self.api.calls)

    def test_pending_refresh_clears_stale_model_and_rejects_duplicate_query(self):
        self.api.defer = True
        self.page.load_models()
        count = len(self.api.calls)
        self.page.load_models()
        self.assertEqual(len(self.api.calls), count)
        self.assertEqual(self.page.model_id.text(), "")
        self.assertFalse(self.page.copy_model_button.isEnabled())
        self.assertFalse(self.page.query_models.isEnabled())
        self.api.complete()
        self.assertTrue(self.page.copy_model_button.isEnabled())
        self.assertTrue(self.page.query_models.isEnabled())

    def test_failed_refresh_is_distinct_from_empty_catalog_and_never_uses_stale_model(self):
        self.api.failure = "Not Found"
        self.page.load_models()
        self.assertIn("查询失败", self.page.connection_summary.text())
        self.assertIn("后台服务", self.page.model_note.text())
        self.assertEqual(self.page.model_id.text(), "")
        self.assertFalse(self.page.copy_json_button.isEnabled())
        self.assertTrue(self.page.query_models.isEnabled())

    def test_removed_selection_falls_back_to_server_example_instead_of_first_model(self):
        self.select("zhipu/glm-5.3-flash")
        self.api.info = build_connection_info(STATE["base_url"], STATE["providers"], STATE["accounts"],
                                               [{"id": "trae/Doubao-Seed-Code"}, {"id": "workbuddy/default"}])
        self.page.load_models()
        self.assertEqual(self.page.model_id.text(), "workbuddy/default")

    def test_empty_catalog_explains_account_setup_and_disables_copy(self):
        self.api.info = build_connection_info(STATE["base_url"], STATE["providers"], [], [])
        self.page.load_models()
        self.assertIn("暂无可用模型", self.page.model_count.text())
        self.assertIn("导入", self.page.model_note.text())
        self.assertFalse(self.page.copy_model_button.isEnabled())
        self.assertFalse(self.page.copy_example_button.isEnabled())

    def test_key_is_only_read_on_explicit_copy_or_export(self):
        self.page.copy_key()
        self.assertEqual(self.application.clipboard().text(), "mock-export-key")
        self.assertEqual(self.api.calls.count("/ai-gateway/api/key"), 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with patch("app.desktop.ai_gateway_page.QFileDialog.getSaveFileName", return_value=(str(path), "JSON")):
                self.page.export_config()
            config = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(config["base_url"], BASE + "/v1")
        self.assertEqual(config["api_key"], "mock-export-key")
        self.assertEqual(config["models"], [row["id"] for row in MODELS])
        self.assertEqual(config["connection"]["endpoints"]["chat_completions"], BASE + "/v1/chat/completions")
        self.assertNotIn("mock-export-key", json.dumps(config["connection"]))

    def test_failed_export_does_not_fetch_key_or_write_empty_fake_catalog(self):
        self.api.failure = "目录暂不可用"
        with patch("app.desktop.ai_gateway_page.QFileDialog.getSaveFileName") as dialog:
            self.page.export_config()
        dialog.assert_not_called()
        self.assertNotIn("/ai-gateway/api/key", self.api.calls)
        self.assertIn("导出失败", self.page.status.text())


if __name__ == "__main__":
    unittest.main()
