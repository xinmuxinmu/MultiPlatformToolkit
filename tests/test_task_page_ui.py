"""Fixture-only regressions for the overview/settings task workflow."""
import base64
from copy import deepcopy
import io
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QCheckBox

from app.desktop.task_page import TaskPage
from app.desktop.task_components import KugouLoginDialog, TaskPicker, overview_data


class FakeApi:
    def __init__(self):
        self.calls = []
        self.save_callback = None
        self.save_done = None
        self.defer_save = False
        self.qr = None
        self.qr_status = {"stage": "waiting"}

    def get(self, path, callback=None, **kwargs):
        self.calls.append(("GET", path, kwargs))
        if path.endswith("qr-poll") and callback:
            callback(self.qr_status)
        if kwargs.get("done"): kwargs["done"]()

    def post(self, path, callback=None, **kwargs):
        self.calls.append(("POST", path, kwargs))
        if path.endswith("/save") and self.defer_save:
            self.save_callback, self.save_done = callback, kwargs.get("done")
            return
        if callback: callback(self.qr if path.endswith("qr-start") else {"ok": True})
        if kwargs.get("done"): kwargs["done"]()


def qq_state():
    return {"music": {"cookie": "fixture-not-a-real-cookie", "sign_enable": True,
                       "medal_enable": True, "medal_tasks_enable": True,
                       "card_pool_enable": True, "medal_selected_ids": None,
                       "medal_task_types": None},
            "cached": {"sign": {"signed": True}, "medal": {"ok": True,
                "signed": 1, "total": 1, "medals": [{"id": "fixture", "name": "合成歌手",
                    "signed": True, "task_total": 2, "task_done": 1,
                    "tasks": [{"id": "1", "name": "浏览活动", "type": "page_browsed", "finish": 1,
                               "need": 1, "done": True, "capability": "auto"},
                              {"id": "2", "name": "看视频", "type": "watch_ads_new", "finish": 0,
                               "need": 1, "done": False, "capability": "manual", "reason": "需要客户端实际播放"}]}]}}}


class TaskPageWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.api = FakeApi()
        self.page = TaskPage(self.api, "/qq", "QQ 音乐")
        self.page.loaded(qq_state())

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def option(self, key):
        return self.page.settings.findChild(QCheckBox, "config_" + key)

    def test_defaults_to_two_tabs_with_tasks_and_logs_visible_together(self):
        self.page.resize(1100, 720)
        self.page.show()
        self.app.processEvents()
        self.assertEqual([self.page.tabs.tabText(i) for i in range(self.page.tabs.count())], ["概览", "设置"])
        self.assertEqual(self.page.tabs.currentIndex(), 0)
        self.assertTrue(self.page.log.isVisible())
        self.assertTrue(self.page.missions.isVisible())
        self.assertGreater(self.page.missions.topLevelItemCount(), 1)
        self.assertFalse(self.page.settings.credentials.button.isChecked())
        self.assertFalse(self.page.settings.advanced_fold.button.isChecked())
        self.assertEqual(self.page.run_button.property("variant"), "primary")

    def test_raw_task_ids_are_not_exposed_as_configuration_fields(self):
        paths = self.page.settings.readers
        self.assertNotIn(("medal_selected_ids",), paths)
        self.assertNotIn(("medal_task_types",), paths)
        self.assertNotIn("medal_task_types", self.page.settings.advanced.config)

    def test_reloading_settings_hides_old_cards_before_deferred_delete(self):
        old_card = self.page.settings.layout.itemAt(0).widget()
        self.page.settings.set_config(qq_state()["music"])
        self.assertTrue(old_card.isHidden())
        self.assertTrue(self.page.settings.credentials.body.isHidden())

    def test_overview_derives_missing_medal_statistics(self):
        state = qq_state()
        state["cached"]["medal"].pop("signed")
        state["cached"]["medal"].pop("total")
        result = overview_data("/qq", state)
        self.assertIn(("勋章签到", "1/1"), result["metrics"])

    def test_refresh_preserves_unsaved_draft(self):
        self.option("sign_enable").setChecked(False)
        self.page.loaded(qq_state())
        self.assertFalse(self.page.current_config()["sign_enable"])
        self.assertTrue(self.page._dirty)

    def test_failed_save_preserves_draft_and_selections(self):
        self.option("sign_enable").setChecked(False)
        self.page.selection.medals.topLevelItem(0).setCheckState(0, Qt.CheckState.Unchecked)
        self.api.defer_save = True
        self.page.save()
        self.api.save_done()  # Request failed: success callback is intentionally absent.
        self.page.loaded(qq_state())
        self.assertFalse(self.page.current_config()["sign_enable"])
        self.assertEqual(self.page.current_config()["medal_selected_ids"], [])
        self.assertIn("保留", self.page.status.text())

    def test_successful_save_marks_selection_saved_only_after_response(self):
        self.page.selection.medals.topLevelItem(0).setCheckState(0, Qt.CheckState.Unchecked)
        self.api.defer_save = True
        self.page.save()
        self.assertTrue(self.page.selection._dirty)
        self.api.save_callback({"ok": True})
        self.assertFalse(self.page.selection._dirty)
        self.assertFalse(self.page._dirty)

    def test_edits_made_during_save_remain_unsaved(self):
        self.option("sign_enable").setChecked(False)
        self.api.defer_save = True
        self.page.save()
        self.option("medal_enable").setChecked(False)
        self.api.save_callback({"ok": True})
        self.assertTrue(self.page._dirty)
        self.assertFalse(self.page.current_config()["medal_enable"])

    def test_login_refresh_merges_credentials_without_losing_task_draft(self):
        self.option("sign_enable").setChecked(False)
        state = qq_state()
        state["music"]["cookie"] = "new-fixture-cookie"
        self.page.loaded(state, merge_login=True)
        self.assertFalse(self.page.current_config()["sign_enable"])
        self.assertEqual(self.page.current_config()["cookie"], "new-fixture-cookie")

    def test_ai_refresh_does_not_call_nonexistent_refresh_endpoint(self):
        page = TaskPage(self.api, "/ai-checkin", "AI 签到")
        page.loaded({"config": {"platforms": {"trae": True}, "schedule": {"enabled": True, "time": "10:20"}}})
        page.refresh_platform()
        self.assertEqual(self.api.calls[-1][1], "/ai-checkin/api/state")
        page.deleteLater()

    def test_new_scheduled_run_reloads_initial_logs(self):
        self.page.seq = 100
        self.page.logs_loaded({"logs": [], "seq": 2, "running": True})
        self.assertEqual(self.page.seq, 0)


class TaskComponentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def test_qr_login_uses_selected_app_and_saves_only_verified_success(self):
        raw = io.BytesIO()
        Image.new("RGB", (20, 20), "white").save(raw, "PNG")
        api = FakeApi()
        api.qr = {"key": "fixture-key", "img": base64.b64encode(raw.getvalue()).decode()}
        dialog = KugouLoginDialog(api, "lite")
        saves = []
        dialog.saved.connect(lambda: saves.append(True))
        dialog.start()
        self.assertEqual(api.calls[-1][1], "/kugou/api/qr-start")
        self.assertEqual(api.calls[-1][2]["params"], {"platform": "lite"})
        self.assertTrue(dialog.timer.isActive())
        dialog.poll()
        self.assertFalse(saves)
        api.qr_status = {"stage": "success", "msg": "登录成功"}
        dialog.poll()
        self.assertEqual(saves, [True])
        self.assertFalse(dialog.timer.isActive())
        dialog.close()
        dialog.deleteLater()

    def test_paid_tasks_stay_disabled_in_kugou_selector(self):
        picker = TaskPicker("/kugou")
        state = {"cached": {"medal": {"tasks": [{"task_id": "buy_product", "name": "购买商品", "finish": 0, "need": 1},
                                                 {"task_id": "share", "name": "分享任务", "finish": 0, "need": 1}]}}}
        picker.set_state(state, {"auto_all_nonpaid": False})
        self.assertFalse(picker.tree.topLevelItem(0).flags() & Qt.ItemFlag.ItemIsEnabled)
        self.assertTrue(picker.tree.topLevelItem(1).flags() & Qt.ItemFlag.ItemIsEnabled)
        picker.update_mode({"auto_all_nonpaid": True})
        self.assertFalse(picker.tree.topLevelItem(1).flags() & Qt.ItemFlag.ItemIsEnabled)
        picker.deleteLater()

    def test_overview_exposes_readable_state_without_cookie(self):
        output = overview_data("/qq", qq_state())
        self.assertNotIn("fixture-not-a-real-cookie", str(output))
        self.assertEqual(output["login"], "已登录")
        self.assertEqual(output["tasks"][1]["children"][1]["status"], "需手动")


if __name__ == "__main__": unittest.main()
