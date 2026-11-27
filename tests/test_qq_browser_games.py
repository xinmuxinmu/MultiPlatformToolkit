"""Offline browser fixtures; no QQ account, network or reward requests."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.qq_tasks.browser_games import (BrowserGameError, play_browser_game,
                                           supports_browser_game)


URL = "https://game.y.qq.com/tmegames/web_pages/qm_tonglou/index.html?singerId=123"


class BrowserGameURLTests(unittest.TestCase):
    def test_only_known_production_page_is_allowed(self):
        self.assertTrue(supports_browser_game(URL))
        for url in (
            URL.replace("https:", "http:"),
            URL.replace("game.y.qq.com", "game.y.qq.com.example.org"),
            URL.replace("game.y.qq.com", "user:password@game.y.qq.com"),
            URL.replace("game.y.qq.com", "game.y.qq.com:8443"),
            URL.replace("qm_tonglou", "other_game"),
            URL + "&preview", URL + "&mock=1", URL + "&page=result",
            URL + "&previewMode=1", URL + "&debug=1", URL + "#preview",
            "not-a-url", "https://game.y.qq.com:invalid/",
        ):
            with self.subTest(url=url):
                self.assertFalse(supports_browser_game(url))


class BrowserGamePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            from modules.signin.core import find_browser
            cls.pw = sync_playwright().start()
            options = {"headless": True}
            browser_path = find_browser()
            if browser_path:
                options["executable_path"] = str(browser_path)
            cls.browser = cls.pw.chromium.launch(**options)
        except Exception as exc:
            if hasattr(cls, "pw"):
                cls.pw.stop()
            raise unittest.SkipTest("Offline browser unavailable: " + type(exc).__name__)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 430, "height": 900})
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()

    def load(self, body, script=""):
        html = """<!doctype html><meta charset='utf-8'><style>
          .game-stage{position:relative;width:360px;height:500px;background:#eee}
          .stage-base{position:absolute;width:100px;height:40px;left:120px;top:400px}
          .stage-hanging-block{position:absolute;width:100px;height:40px;left:120px;top:80px}
        </style>""" + body + "<script>window.adClicks=0;window.starts=0;window.releases=0;" + script + "</script>"
        self.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        self.page.goto(URL)

    def test_real_clicks_skip_ads_and_wait_for_result_without_claiming_credit(self):
        self.load("""
          <button class='home-start'>开始应援</button>
          <section class='game-stage' style='display:none'>
            <div class='stage-base'></div><div class='stage-hanging-block'></div>
          </section>
          <button class='boost-claim' onclick='adClicks++'>看广告领取</button>
          <button class='failure-revive' onclick='adClicks++'>看广告复活</button>
          <button class='boost-skip' style='display:none'>不领，继续盖楼</button>
          <button class='failure-give-up' style='display:none'>放弃</button>
          <main class='settlement-page' style='display:none'>本局结算</main>
        """, """
          const stage=document.querySelector('.game-stage');
          const skip=document.querySelector('.boost-skip');
          const giveUp=document.querySelector('.failure-give-up');
          document.querySelector('.home-start').onclick=function(){starts++;this.style.display='none';stage.style.display='block'};
          stage.onclick=function(){releases++;stage.style.display='none';(releases===1?skip:giveUp).style.display='block'};
          skip.onclick=function(){this.style.display='none';stage.style.display='block'};
          giveUp.onclick=function(){this.style.display='none';document.querySelector('.settlement-page').style.display='block'};
        """)
        result = play_browser_game(self.page, timeout_seconds=5)
        self.assertTrue(result["played"])
        self.assertTrue(result["settled"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["released"], 2)
        self.assertEqual(self.page.evaluate("[starts,releases,adClicks]"), [1, 2, 0])

    def test_no_free_stamina_never_clicks_ad(self):
        self.load("<button class='home-ad-stamina' onclick='adClicks++'>看广告得体力</button>")
        with self.assertRaisesRegex(BrowserGameError, "免费体力"):
            play_browser_game(self.page, timeout_seconds=2)
        self.assertEqual(self.page.evaluate("adClicks"), 0)

    def test_disabled_start_is_not_submitted(self):
        self.load("<button class='home-start' aria-disabled='true' onclick='starts++'>开始应援</button>")
        with self.assertRaisesRegex(BrowserGameError, "暂不可开始"):
            play_browser_game(self.page, timeout_seconds=2)
        self.assertEqual(self.page.evaluate("starts"), 0)

    def test_boot_error_does_not_start_game(self):
        self.load("<main class='boot-error'>请求 SDK 未就绪</main><button class='home-start' onclick='starts++'>开始</button>")
        with self.assertRaisesRegex(BrowserGameError, "SDK"):
            play_browser_game(self.page, timeout_seconds=2)
        self.assertEqual(self.page.evaluate("starts"), 0)

    def test_existing_result_cannot_be_reported_as_new_play(self):
        self.load("<main class='settlement-page'>旧结算</main>")
        with self.assertRaisesRegex(BrowserGameError, "未确认本次实际游玩"):
            play_browser_game(self.page, timeout_seconds=2)

    def test_loading_timeout_is_not_a_success(self):
        self.load("<main class='loading'>加载中</main>")
        with self.assertRaisesRegex(BrowserGameError, "超时"):
            play_browser_game(self.page, timeout_seconds=0.2)

    def test_invalid_duration_does_not_click(self):
        self.load("<button class='home-start' onclick='starts++'>开始</button>")
        for value in (0, -1, float("nan"), float("inf"), 301, "invalid"):
            with self.subTest(value=value), self.assertRaises(BrowserGameError):
                play_browser_game(self.page, timeout_seconds=value)
        self.assertEqual(self.page.evaluate("starts"), 0)


if __name__ == "__main__":
    unittest.main()
