from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("APP_DISABLE_SCHEDULES", "1")

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QLineEdit
from app.desktop.api import Api
from app.desktop.catalog import resolve_schema, api_catalog
from app.desktop.task_page import TaskPage
from app.desktop.widgets import ConfigEditor, safe_value
from app.desktop.window import DesktopWindow

APP = QApplication.instance() or QApplication([])


class FakeApi:
    def __init__(self): self.calls = []
    def get(self, path, callback=None, **kw): self.calls.append(("GET", path, kw))
    def post(self, path, callback=None, **kw):
        self.calls.append(("POST", path, kw))
        if callback: callback({"ok": True})


class DesktopTests(unittest.TestCase):
    def test_window_uses_native_widgets_and_no_webengine(self):
        with patch.object(Api, "get") as get:
            window = DesktopWindow("http://127.0.0.1:18000")
            self.assertGreaterEqual(window.pages.count(), 15)
            self.assertEqual(get.call_args.args[0], "/qq/api/state")
            self.assertFalse(any("QtWebEngine" in name for name in sys.modules))
            window.close()

    def test_config_editor_round_trips_lists_null_and_secret_strings(self):
        config = {"cookie": "fixture-not-a-real-cookie", "on": True,
                  "count": 2, "gap": 2.5, "ids": None, "more": {"ids": ["1"]}}
        editor = ConfigEditor()
        editor.set_config(config)
        self.assertEqual(editor.value(), config)
        self.assertEqual(editor.findChild(QLineEdit, "config_cookie").echoMode(), QLineEdit.EchoMode.Password)
        self.assertNotIn("fixture-not", json.dumps(safe_value(config)))

    def test_task_run_saves_selected_ai_platforms_before_execution(self):
        api = FakeApi()
        page = TaskPage(api, "/ai-checkin", "test")
        page.loaded({"config": {"platforms": {"trae": True, "doubao": False}}})
        page.run(dry=True)
        posts = [call for call in api.calls if call[0] == "POST"]
        self.assertEqual([call[1] for call in posts], ["/ai-checkin/api/save", "/ai-checkin/api/run"])
        self.assertEqual(posts[1][2]["body"], {"targets": ["trae"], "dry": True})

    def test_task_logs_read_actual_backend_fields(self):
        page = TaskPage(FakeApi(), "/qq", "test")
        page.logs_loaded({"logs": [{"id": 1, "t": "12:00", "m": "真实状态"}], "seq": 1})
        self.assertEqual(page.log.toPlainText(), "12:00 真实状态")

    def test_schema_expansion_is_complete_and_cycle_safe(self):
        resolved = resolve_schema({"$ref": "#/components/schemas/A"},
                                  {"A": {"type": "object", "properties": {"child": {"$ref": "#/components/schemas/A"}}}})
        self.assertEqual(resolved["properties"]["child"]["type"], "object")

    def test_light_dark_light_updates_palette_and_stays_readable(self):
        from app.desktop import theme
        from PySide6.QtGui import QPalette
        theme.apply_theme("light")
        original = APP.palette().color(QPalette.ColorRole.Window).name()
        theme.apply_theme("dark")
        dark = APP.palette().color(QPalette.ColorRole.Window).name()
        self.assertNotEqual(original, dark)
        self.assertIn("QCheckBox::indicator:checked", APP.styleSheet())
        self.assertNotIn("palette(", APP.styleSheet())
        theme.apply_theme("light")
        self.assertEqual(APP.palette().color(QPalette.ColorRole.Window).name(), original)

    def test_windows_schedule_uses_python_and_no_powershell(self):
        from install_ai_task import task_xml
        import xml.etree.ElementTree as ET
        xml = task_xml("09:30", True)
        ET.fromstring(xml)
        self.assertNotIn("powershell", xml.lower())
        self.assertIn("modules.ai_checkin.runner", xml)
        self.assertIn("09:30:00", xml)
        with self.assertRaises(ValueError): task_xml("29:10")


if __name__ == "__main__": unittest.main()
