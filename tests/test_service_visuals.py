import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest.mock import Mock
import shiboken6

from PySide6.QtWidgets import QApplication, QHBoxLayout, QPushButton

from app.desktop.fans_page import FansPage
from app.desktop.service_pages import OperationsPage, SparkPage, StarskyPage


class ServiceVisualTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_toolbar_button_count_and_primary_position(self):
        for page_type in (SparkPage, StarskyPage, OperationsPage, FansPage):
            api = Mock(headers={})
            page = page_type(api)
            for row in page.findChildren(QHBoxLayout):
                buttons = [row.itemAt(index).widget() for index in range(row.count())
                           if isinstance(row.itemAt(index).widget(), QPushButton)]
                self.assertLessEqual(len(buttons), 5, page_type.__name__)
                primary = [button for button in buttons if button.property("variant") == "primary"]
                self.assertLessEqual(len(primary), 1, page_type.__name__)
                if primary:
                    self.assertIs(primary[0], buttons[-1], page_type.__name__)
            api.get.assert_not_called()
            api.post.assert_not_called()
            page.close()
            shiboken6.delete(page)
        self.app.processEvents()

    def test_empty_spark_lists_show_next_step_and_records_are_readable(self):
        page = SparkPage(Mock(headers={}))
        page._accounts_loaded({"data": {"accounts": []}})
        page._tasks_loaded({"data": {"tasks": []}})
        page._friends_loaded({"data": {"list": {}}})
        self.assertIn("新增账号", page.status.text())
        self.assertEqual(page.task_stack.currentIndex(), 1)
        self.assertEqual(page.friend_stack.currentIndex(), 1)
        page.result.show_data({"data": {"running": True, "done": 1, "total": 3, "current": "测试好友"}})
        self.assertIn("测试好友", page.result.summary.toPlainText())
        self.assertIn("已完成 1 / 3", page.result.summary.toPlainText())
        self.assertEqual(page.result.tabs.currentIndex(), 0)
        page.close()
        shiboken6.delete(page)


if __name__ == "__main__":
    unittest.main()
