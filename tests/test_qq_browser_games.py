"""Offline browser fixtures; no QQ account, network or reward requests."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.qq_tasks.browser_games import (BrowserGameError, play_browser_game,
                                           supports_browser_game, supports_flip_game)


URL = "https://game.y.qq.com/tmegames/web_pages/qm_tonglou/index.html?singerId=123"
FLIP_URL = ("https://y.qq.com/m/client/starlight_game/index.html"
            "?_hidehd=1&biz=singermedal&issue=974436")


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

    def test_flip_page_requires_the_official_singer_medal_entry(self):
        self.assertTrue(supports_flip_game(FLIP_URL))
        for url in (
            FLIP_URL.replace("https:", "http:"),
            FLIP_URL.replace("y.qq.com", "evil.y.qq.com"),
            FLIP_URL.replace("issue=974436", "issue="),
            FLIP_URL.replace("biz=singermedal", "biz=medalcard"),
            FLIP_URL + "&debug=1", FLIP_URL + "#preview",
            FLIP_URL.replace("index.html", "other.html"),
        ):
            with self.subTest(url=url):
                self.assertFalse(supports_flip_game(url))


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

    def load_flip(self, labels=None):
        labels = labels or ["a", "b", "c", "a", "d", "b",
                            "e", "c", "d", "f", "e", "f"]
        cards = "".join(
            f"<div class='starlight-card' data-index='{index}'>"
            "<div class='card-face card-front'><img class='card-front__image' src='' alt=''></div>"
            "<div class='card-face card-back'></div></div>"
            for index in range(len(labels)))
        body = (
            "<main class='select-page'><div class='select-page__option'>简单</div>"
            "<img class='select-page__remain-icon' src='change_active.test.png'><button class='select-page__confirm-btn'>确认选择</button></main>"
            "<main class='game-page' style='display:none'><div class='game-board game-board--visible'>"
            + cards + "</div><button class='game-bottom-btns__btn--ad' onclick='window.adClicks++'>"
            + "看广告</button><main class='result-modal' style='display:none'><div class='result-modal__info'>完成</div></main></main>")
        script = (
            f"const faces={labels!r};let open=[];let matched=0;window.adClicks=0;"
            "document.querySelector('.select-page__confirm-btn').onclick=()=>{"
            "document.querySelector('.select-page').style.display='none';"
            "document.querySelector('.game-page').style.display='block'};"
            "document.querySelectorAll('.starlight-card').forEach((card,i)=>{"
            "card.onclick=()=>{if(card.classList.contains('matched')||card.classList.contains('flipped'))return;"
            "card.classList.add('flipped');card.querySelector('img').src='https://cards.invalid/'+faces[i]+'.png';"
            "open.push(i);if(open.length===2){const [a,b]=open;const ca=document.querySelectorAll('.starlight-card')[a];"
            "const cb=document.querySelectorAll('.starlight-card')[b];if(faces[a]===faces[b]){"
            "ca.classList.add('matched');cb.classList.add('matched');matched+=2;open=[];"
            "if(matched===faces.length)document.querySelector('.result-modal').style.display='block';"
            "}else{setTimeout(()=>{ca.classList.remove('flipped');cb.classList.remove('flipped');open=[]},180)}}};});")
        self.context.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html",
            body="<!doctype html><meta charset='utf-8'>" + body + "<script>" + script + "</script>"))
        self.page.goto(FLIP_URL)

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

    def test_flip_game_uses_revealed_cards_and_never_clicks_ad(self):
        self.load_flip()
        result = play_browser_game(self.page, timeout_seconds=8)
        self.assertTrue(result["played"])
        self.assertTrue(result["settled"])
        self.assertEqual(result["matched_pairs"], 6)
        self.assertEqual(self.page.evaluate("window.adClicks"), 0)

    def test_flip_game_without_free_attempt_does_not_start(self):
        self.load_flip()
        self.page.locator(".select-page__remain-icon").evaluate(
            "el => el.setAttribute('src', 'change.test.png')")
        with self.assertRaisesRegex(BrowserGameError, "没有可用的免费翻卡次数"):
            play_browser_game(self.page, timeout_seconds=3)
        self.assertTrue(self.page.locator(".select-page").is_visible())

    def test_flip_waits_for_official_config_before_starting(self):
        self.load_flip()
        self.page.locator(".select-page__confirm-btn").evaluate(
            "el => { el.disabled = true; setTimeout(() => { el.disabled = false }, 120) }")
        result = play_browser_game(self.page, timeout_seconds=4)
        self.assertTrue(result["settled"])

    def test_flip_does_not_treat_failure_modal_as_success(self):
        self.load_flip(["a", "a", "b", "b", "c", "c",
                        "d", "d", "e", "e", "f", "f"])
        self.page.locator(".result-modal__info").evaluate("el => el.remove()")
        with self.assertRaisesRegex(BrowserGameError, "等待官方结算"):
            play_browser_game(self.page, timeout_seconds=5)


if __name__ == "__main__":
    unittest.main()
