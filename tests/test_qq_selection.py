"""Native QQ selection must preserve edits until a save succeeds."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

from app.desktop.qq_selection import QQSelectionWidget


class QQSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = QQSelectionWidget()
        self.config = {"medal_selected_ids": None, "medal_task_types": None}
        self.state = {"music": self.config, "cached": {"medal": {"medals": [{
            "id": "fixture", "name": "合成歌手", "tasks": [
                {"type": "page_browsed", "name": "浏览任务", "capability": "auto"},
                {"type": "buy_product_singer", "name": "购买任务", "capability": "paid"},
            ]}]}}}
        self.widget.set_state(self.state)

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_untouched_default_remains_dynamic_null(self):
        self.assertEqual(self.widget.update_config(self.config), self.config)

    def test_empty_selection_is_not_replaced_by_defaults(self):
        self.widget.medals.topLevelItem(0).setCheckState(0, Qt.CheckState.Unchecked)
        self.assertEqual(self.widget.update_config(self.config)["medal_selected_ids"], [])

    def test_failed_save_preserves_pending_selection_during_refresh(self):
        self.widget.medals.topLevelItem(0).setCheckState(0, Qt.CheckState.Unchecked)
        self.widget.update_config(self.config)
        self.widget.set_state(self.state)  # A read does not mean the previous save succeeded.
        self.assertEqual(self.widget.update_config(self.config)["medal_selected_ids"], [])
        self.widget.mark_saved()
        self.widget.set_state(self.state)
        self.assertEqual(self.widget.medals.topLevelItem(0).checkState(0), Qt.CheckState.Checked)

    def test_paid_tasks_are_disabled_and_not_selected(self):
        row = self.widget.tasks.topLevelItem(1)
        self.assertFalse(row.flags() & Qt.ItemFlag.ItemIsEnabled)
        self.assertEqual(row.checkState(0), Qt.CheckState.Unchecked)


if __name__ == "__main__":
    unittest.main()
