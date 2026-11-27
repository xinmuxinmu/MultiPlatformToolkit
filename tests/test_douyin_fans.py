import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import quote
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from modules.douyin_fans import core


class FansCoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.patches = [patch.object(core, "HOME", root / "fans"),
                        patch.object(core, "DOUYIN_CHAT_HOME", root / "douyin"),
                        patch.object(core, "_BUSY", False), patch.object(core, "_LAST_RESULT", None)]
        for item in self.patches:
            item.start()
        self.config = {**core.DEFAULT_CONFIG, "circle_url": "https://www.douyin.com/circle/fixture"}

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def observed(self, text="团热度打卡签到 5亲密度 立即签到", *, artist=True, login=False):
        return {"title": "封茗囧菌的圈子", "circle_marker": True, "artist_marker": artist,
                "login_prompt": login, "rows": [{"id": "heat_checkin", "found": True, "text": text,
                "controls": [{"index": 2, "text": "立即签到", "disabled": False}], "links": []}]}

    def heat(self, snapshot):
        return next(task for task in snapshot["tasks"] if task["id"] == "heat_checkin")

    def test_official_url_validation_blocks_spoofing_and_chat(self):
        for value in ("http://www.douyin.com/circle/a", "https://douyin.com.evil.test/circle", "https://evil.test/?douyin.com", "https://user@douyin.com/", "https://www.douyin.com:8443/circle", "file:///etc/passwd", "https://www.douyin.com/im", "https://www.douyin.com/%69m/", "https://www.douyin.com/#/chat"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.official_url(value)
        self.assertEqual(core.official_url("https://v.douyin.com/fixture/"), "https://v.douyin.com/fixture/")
        self.assertEqual(core.official_url("https://www.iesdouyin.com/share/fixture"), "https://www.iesdouyin.com/share/fixture")

    def test_initial_screenshot_tasks_are_never_live_or_complete(self):
        with patch.object(core, "BrowserWorker") as worker:
            state = core.state()
        worker.assert_not_called()
        self.assertEqual(state["config"]["artist"], "封茗囧菌")
        self.assertEqual(len(state["tasks"]), 10)
        self.assertTrue(all(task["source"] == "user_screenshot" and task["status"] == "未同步" for task in state["tasks"]))
        self.assertTrue(all(not task["can_auto_checkin"] for task in state["tasks"]))

    def test_missing_circle_and_paid_tasks_never_start_browser(self):
        with patch.object(core, "BrowserWorker") as worker:
            with patch.object(core, "load_config", return_value={**self.config, "circle_url": ""}), self.assertRaisesRegex(ValueError, "官方圈子"):
                core.start("refresh")
            core.save_config(self.config)
            with self.assertRaisesRegex(ValueError, "钻石"):
                core.start("open-task", "gift")
        worker.assert_not_called()

    def share_fixture(self):
        data = {"isRightToken": True, "shareInfo": {"AppId": "tte9a89f1576a4b96b01", "Title": "封茗囧菌的圈子", "HostId": 2329,
                "Query": "pages%2Ffandom_detail%2Findex%3Ffandom_id%3D27933289767426%26music_aid%3D6797607257627428865",
                "Uid": "private-fixture-uid", "Did": "private-fixture-device"},
                "appInfo": {"Name": "趣追"}, "appName": "douyinlite"}
        return '<script type="application/json" id="RENDER_DATA">' + quote(json.dumps(data)) + '</script>'

    def test_public_microapp_share_parses_real_official_field_names(self):
        entry = core.parse_microapp_share(self.share_fixture(), core.DEFAULT_CIRCLE_URL)
        self.assertEqual(entry["app_name"], "趣追")
        self.assertEqual(entry["circle_title"], "封茗囧菌的圈子")
        self.assertEqual(entry["app_id"], "tte9a89f1576a4b96b01")
        self.assertEqual(entry["fandom_id"], "27933289767426")
        self.assertEqual(entry["music_aid"], "6797607257627428865")
        self.assertEqual(entry["page_path"], "pages/fandom_detail/index")
        self.assertEqual(entry["host_app"], "douyinlite")
        self.assertFalse(entry["web_tasks_exposed"])
        self.assertNotIn("private-fixture", json.dumps(entry))

    def test_microapp_refresh_does_not_open_browser_or_claim_task_sync(self):
        config = {**self.config, "circle_url": core.DEFAULT_CIRCLE_URL}
        entry = core.parse_microapp_share(self.share_fixture(), core.DEFAULT_CIRCLE_URL)
        worker = core.BrowserWorker()
        with patch.object(core, "resolve_share_url", return_value=entry), patch.object(worker, "_ensure_browser") as browser:
            result = worker._perform(Mock(), "refresh", config, "")
        self.assertEqual(result["status"], "client_miniapp_entry")
        self.assertFalse(result["verified"])
        browser.assert_not_called()
        saved = core._read_json(core._snapshot_path(config), {})
        self.assertIsNone(saved["observed_at"])
        self.assertTrue(all(task["status"] == "未同步" for task in saved["tasks"]))

    def test_microapp_checkin_never_clicks_or_opens_browser(self):
        config = {**self.config, "circle_url": core.DEFAULT_CIRCLE_URL}
        entry = core.parse_microapp_share(self.share_fixture(), core.DEFAULT_CIRCLE_URL)
        worker = core.BrowserWorker()
        with patch.object(core, "resolve_share_url", return_value=entry), patch.object(worker, "_ensure_browser") as browser:
            result = worker._perform(Mock(), "checkin", config, "")
        self.assertEqual(result["status"], "manual_required")
        self.assertFalse(result["verified"])
        browser.assert_not_called()

    def test_invalid_microapp_token_and_external_redirect_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "无效"):
            core.parse_microapp_share('<script id="RENDER_DATA">{"isRightToken":false}</script>', core.DEFAULT_CIRCLE_URL)
        response = Mock(status_code=302, headers={"location": "https://evil.test/"})
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.get.return_value = response
        with patch("httpx.Client", return_value=client), self.assertRaises(ValueError):
            core.resolve_share_url(core.DEFAULT_CIRCLE_URL)
        client.get.assert_called_once()

    def test_checkin_requires_free_context_artist_and_no_login(self):
        self.assertTrue(self.heat(core.interpret_page(self.observed(), self.config))["can_auto_checkin"])
        for observed in (self.observed("每日签到 1钻立即签到"), self.observed("邀请好友签到 立即签到"),
                         self.observed(artist=False), self.observed(login=True)):
            with self.subTest(observed=observed):
                self.assertFalse(self.heat(core.interpret_page(observed, self.config))["can_auto_checkin"])

    def test_other_task_completion_is_not_used_as_checkin_evidence(self):
        observed = self.observed("邀请好友今日已签到，获得亲密度")
        self.assertNotEqual(self.heat(core.interpret_page(observed, self.config))["status"], "网页显示已签到")

    def test_playback_is_observed_without_becoming_completion(self):
        observed = self.observed()
        observed["playback"] = [{"kind": "audio", "current_time": 300, "duration": 400, "paused": False}]
        snapshot = core.interpret_page(observed, self.config)
        self.assertEqual(snapshot["playback"][0]["current_time"], 300)
        self.assertIn("不代表", snapshot["playback"][0]["note"])
        self.assertEqual(next(task for task in snapshot["tasks"] if task["id"] == "listen")["status"], "网页未找到")

    def test_account_metadata_does_not_expose_cookies(self):
        directory = core.DOUYIN_CHAT_HOME / "accounts"
        directory.mkdir(parents=True)
        (directory / "index.json").write_text(json.dumps({"active": "123", "accounts": {"123": {"name": "fixture", "uid": "123", "cookies": "do-not-expose"}, "../bad": {"name": "bad"}}}), encoding="utf-8")
        accounts = core.shared_accounts()
        self.assertEqual(len(accounts["accounts"]), 1)
        self.assertNotIn("cookies", accounts["accounts"][0])
        self.assertNotIn("do-not-expose", json.dumps(accounts))

    def test_sign_click_needs_persisted_signed_state(self):
        before = core.interpret_page(self.observed(), self.config)
        after = core.interpret_page(self.observed("团热度每日签到 今日已签到"), self.config)
        worker = core.BrowserWorker()
        page = Mock()
        page.locator.return_value.nth.return_value.is_visible.return_value = True
        page.locator.return_value.nth.return_value.is_enabled.return_value = True
        page.locator.return_value.nth.return_value.inner_text.return_value = "立即签到"
        for expected, snapshot_after in ((False, before), (True, after)):
            with patch.object(worker, "_ensure_browser", return_value=page), patch.object(worker, "_navigate"), \
                 patch.object(core, "_inspect", side_effect=[copy.deepcopy(before), copy.deepcopy(snapshot_after)]):
                result = worker._perform(Mock(), "checkin", self.config, "")
            self.assertEqual(result["verified"], expected)
            self.assertEqual(result["status"], "success" if expected else "unconfirmed")

    def test_unavailable_checkin_does_not_click(self):
        worker = core.BrowserWorker()
        page = Mock()
        unavailable = core.interpret_page(self.observed("每日签到 1钻 立即签到"), self.config)
        with patch.object(worker, "_ensure_browser", return_value=page), patch.object(worker, "_navigate"), \
             patch.object(core, "_inspect", return_value=unavailable):
            result = worker._perform(Mock(), "checkin", self.config, "")
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "manual_required")
        page.locator.assert_not_called()

    def test_api_reads_local_state_and_rejects_unconfigured_execution(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from modules.douyin_fans.routes import router
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client, patch.object(core, "BrowserWorker") as worker:
            response = client.get("/douyin-fans/api/state")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["tasks"][0]["status"], "未同步")
            with patch.object(core, "load_config", return_value={**self.config, "circle_url": ""}):
                self.assertEqual(client.post("/douyin-fans/api/checkin").status_code, 400)
            self.assertEqual(client.post("/douyin-fans/api/config", json={"circle_url": "https://evil.test/"}).status_code, 400)
        worker.assert_not_called()

    def test_actual_dom_parser_on_isolated_static_circle_fixture(self):
        from playwright.sync_api import sync_playwright
        from modules.signin.core import find_browser
        executable = find_browser()
        if not executable:
            self.skipTest("local Chromium browser not installed")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=executable)
            try:
                page = browser.new_page()
                page.set_content('''<h1>封茗囧菌的圈子</h1><p>粉丝团 1095 粉丝</p>
                  <article><h2>团热度打卡签到</h2><p>签到5亲密度</p><button>立即签到</button></article>
                  <article><h2>听TA的歌</h2><p>0 / 20分钟</p><button>去完成</button></article>
                  <article><h2>评论</h2><p>0 / 4次</p><button>去评论</button></article>
                  <article><h2>点亮灯牌</h2><p>1钻100亲密度</p><button>立即购买</button></article>
                  <audio controls></audio>''')
                observed = page.evaluate(core.DOM_INSPECT, {"artist": "封茗囧菌", "specs": [
                    {"id": task["id"], "pattern": task["pattern"]} for task in core.REFERENCE_TASKS]})
                snapshot = core.interpret_page(observed, self.config)
                self.assertTrue(self.heat(snapshot)["can_auto_checkin"])
                self.assertTrue(snapshot["page"]["artist_marker"])
                listen = next(task for task in snapshot["tasks"] if task["id"] == "listen")
                self.assertEqual(listen["progress"], {"current": 0, "target": 20})
                self.assertEqual(len(snapshot["playback"]), 1)
                self.assertFalse(next(task for task in snapshot["tasks"] if task["id"] == "light")["can_auto_checkin"])
            finally:
                browser.close()


class FansNativePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.pages = []

    def tearDown(self):
        import shiboken6
        for page in self.pages:
            page.close()
            if shiboken6.isValid(page):
                shiboken6.delete(page)
        self.app.processEvents()

    def test_native_activation_and_poll_do_not_run_tasks(self):
        from app.desktop.fans_page import FansPage
        api = Mock()
        page = FansPage(api)
        self.pages.append(page)
        self.assertEqual(api.method_calls, [])
        page.on_activate()
        page._running = True
        page.poll()
        self.assertTrue(all(call.args[0] == "/douyin-fans/api/state" for call in api.get.call_args_list))
        api.post.assert_not_called()
        page.close()

    def test_poll_preserves_unsaved_circle_url(self):
        from app.desktop.fans_page import FansPage
        page = FansPage(Mock())
        self.pages.append(page)
        page.circle_url.setText("https://www.douyin.com/circle/draft")
        page._state_loaded({"config": {"circle_url": "old"}, "tasks": core.initial_tasks(), "running": False})
        self.assertEqual(page.circle_url.text(), "https://www.douyin.com/circle/draft")
        self.assertEqual(page.tasks.topLevelItemCount(), 10)
        page.close()


if __name__ == "__main__":
    unittest.main()
