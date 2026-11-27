"""Render native UI with synthetic fixtures; never load actual accounts or chats."""
from __future__ import annotations
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ['APP_DISABLE_SCHEDULES'] = '1'
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    output = Path(sys.argv[1] if len(sys.argv) > 1 else '_tmp_ui_preview').resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='toolkit-ui-fixture-') as temporary:
        os.environ['APP_DATA_DIR'] = temporary
        from unittest.mock import patch
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtWidgets import QApplication
        from app.desktop.window import DesktopWindow
        from modules.douyin_fans.core import DEFAULT_CONFIG, REFERENCE_TASKS
        from app.desktop import theme
        class DemoApi(QObject):
            error = Signal(str)
            def __init__(self, base_url, parent=None):
                super().__init__(parent)
                self.base_url, self.headers = base_url, {}
            def get(self, path, callback=None, **kw):
                if path == '/qq/api/state':
                    value = {'music': {'cookie': 'synthetic-fixture', 'sign_enable': True, 'medal_enable': True,
                         'medal_tasks_enable': True, 'card_pool_enable': True, 'medal_task_types': None, 'medal_selected_ids': None},
                         'cached': {'sign': {'signed': False}, 'medal': {'medals': [
                           {'id': 'demo', 'name': '示例歌手', 'level': 11, 'signed': False, 'task_done': 1, 'task_total': 3,
                            'tasks': [
                             {'id': '1', 'type': 'page_browsed', 'name': '浏览歌手主页', 'finish': 1, 'need': 1, 'done': True, 'auto': True, 'capability': 'auto', 'reason': '已读取官方进度', 'verified': True},
                             {'id': '2', 'type': 'listen_song', 'name': '听歌 20 分钟', 'finish': 0, 'need': 1, 'done': False, 'auto': False, 'capability': 'manual', 'reason': '需要在官方客户端真实收听', 'verified': False},
                             {'id': '3', 'type': 'watch_ads', 'name': '观看广告', 'finish': 0, 'need': 1, 'done': False, 'auto': False, 'capability': 'manual', 'reason': '需要客户端广告验证', 'verified': False}]}]}}}
                elif path == '/douyin-fans/api/state':
                    value = {'config': dict(DEFAULT_CONFIG), 'accounts': {'accounts': []}, 'tasks': [dict(x, source='user_screenshot', status='未同步') for x in REFERENCE_TASKS]}
                elif path.endswith('/api/logs'):
                    value = {'logs': [], 'seq': 0, 'running': False}
                elif path.endswith('/api/accounts'):
                    value = {'accounts': [], 'active': ''}
                elif path.endswith('/api/auth/check'):
                    value = {'authenticated': True}
                elif path.endswith('/api/summary'):
                    value = {'data': {}, 'task': {}}
                elif path.endswith('/api/state'):
                    value = {'config': {}, 'cached': {}, 'running': False}
                else:
                    value = {}
                if callback: callback(value)
                if kw.get('done'): kw['done']()
            def post(self, *args, **kwargs):
                raise RuntimeError('UI preview must not perform writes')
            def call(self, method, path, callback=None, **kwargs):
                if method != 'GET': raise RuntimeError('UI preview must be read-only')
                return self.get(path, callback, **kwargs)
        app = QApplication.instance() or QApplication([])
        reports = []
        with patch('app.desktop.window.Api', DemoApi):
            window = DesktopWindow('http://127.0.0.1:0')
            window.show()
            for name in ('light', 'dark'):
                window.apply_theme(name)
                for width, height in ((1240, 820), (880, 620)):
                    window.resize(width, height)
                    window.select_page(0)
                    app.processEvents()
                    filename = output / f'{name}_{width}_overview.png'
                    window.grab().save(str(filename))
                    reports.append({'file': str(filename), 'requested_width': width, 'actual_width': window.width(), 'actual_height': window.height()})
                    page = window.pages.widget(0)
                    if hasattr(page, 'tabs'):
                        page.tabs.setCurrentIndex(1)
                        app.processEvents()
                        window.grab().save(str(output / f'{name}_{width}_settings.png'))
                        page.tabs.setCurrentIndex(0)
            window.resize(1240, 820)
            window.select_page(8)
            app.processEvents()
            window.grab().save(str(output / 'fans_light.png'))
            window.close()
        import json
        print(json.dumps(reports))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
