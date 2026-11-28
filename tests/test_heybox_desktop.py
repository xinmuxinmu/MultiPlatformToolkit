"""Exercise native SMS login with queued local responses; no account or network."""
from copy import deepcopy
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QCheckBox, QLineEdit, QPushButton
from app.desktop.task_page import TaskPage
from app.desktop.task_components import overview_data

SEND = "/heybox/api/app-login/send-code"
VERIFY = "/heybox/api/app-login/verify"
OPEN = "/heybox/api/app-login/captcha/open"
RESULT = "/heybox/api/app-login/captcha/result/bridge-1"
STATUS = "/heybox/api/app-login/status"
PHONE = "13800138000"


class FakeApi:
    base_url = "http://127.0.0.1:18000"

    def __init__(self):
        self.calls, self.plans, self.pending = [], [], []
        self.login_status = {"stage": "idle"}
        self.state = {
            "config": {"cookie": "pkey=web-secret", "heybox_id": "12345",
                       "sign_enable": True, "share_enable": True},
            "app_logged_in": False, "cached": {"nickname": "缓存账号"},
        }

    def plan(self, path, data=None, *, error=None, defer=False):
        self.plans.append((path, deepcopy(data), error, defer))

    def get(self, path, callback=None, **kwargs):
        self._call("GET", path, callback, **kwargs)

    def post(self, path, callback=None, **kwargs):
        self._call("POST", path, callback, **kwargs)

    def _call(self, method, path, callback, *, body=None, done=None, error=None, **kwargs):
        self.calls.append((method, path, deepcopy(body)))
        if self.plans and self.plans[0][0] == path:
            _, data, failure, defer = self.plans.pop(0)
        elif method == "GET" and path == STATUS:
            data, failure, defer = self.login_status, None, False
        elif method == "GET" and path.endswith("/state"):
            data = {**self.state, "app_login": self.login_status}
            failure, defer = None, False
        elif method == "GET" and path.endswith("/logs"):
            data = {"logs": [], "seq": 0, "running": False, "done": False}
            failure, defer = None, False
        else:
            raise AssertionError("Unexpected request: " + path)
        request = (path, deepcopy(data), failure, callback, error, done)
        if defer:
            self.pending.append(request)
        else:
            self._complete(request)

    def _complete(self, request):
        path, data, failure, callback, error, done = request
        if failure is not None:
            if error:
                error(failure)
        else:
            if path in (SEND, VERIFY) and isinstance(data, dict):
                self.login_status = deepcopy(data)
                if data.get("stage") == "success":
                    self.state["app_logged_in"] = True
            if callback:
                callback(data)
        if done:
            done()

    def complete(self):
        self._complete(self.pending.pop(0))

    def requests(self, path):
        return [body for _, requested, body in self.calls if requested == path]


class HeyboxDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.api = FakeApi()
        self.page = TaskPage(self.api, "/heybox", "小黑盒")
        self.page.loaded(self.api.state)
        self.form = self.page.heybox_login_form
        # Tests advance polling explicitly, so no timer can outlive its FakeApi plan.
        self.form._timer.stop()

    def tearDown(self):
        self.form._timer.stop()
        self.page.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.application.processEvents()

    def send(self, **kwargs):
        self.api.plan(SEND, {"ok": True, "stage": "code",
                             "msg": "验证码已发送，请输入短信验证码"}, **kwargs)
        self.form.phone.setText(PHONE)
        self.form.send_button.click()

    @staticmethod
    def challenge(action):
        return {"ok": True, "stage": "captcha", "captcha_required": True,
                "captcha_id": "challenge-" + action, "captcha_for": action,
                "captcha_app_id": "1234567890", "expires_in": 90,
                "msg": "请完成人机验证"}

    def plan_bridge(self):
        self.api.plan(OPEN, {"ok": True, "bridge_id": "bridge-1",
                             "url": "/heybox/app-login/captcha/bridge-1"})

    def test_web_cookie_does_not_imply_app_login_and_form_is_native(self):
        self.assertIn("App", self.page.account_login.text())
        self.assertNotEqual(self.page.account_login.text(), "已登录")
        self.assertIsInstance(self.form.phone, QLineEdit)
        self.assertIsInstance(self.form.code, QLineEdit)
        labels = [button.text() for button in self.page.account_settings.findChildren(QPushButton)]
        self.assertNotIn("快捷登录", labels)
        self.page.run()
        self.assertEqual(self.page.tabs.currentIndex(), 1)
        self.assertIn("登录", self.page.status.text())
        self.assertEqual(self.api.calls, [])

    def test_send_and_login_finish_inside_desktop_without_opening_browser(self):
        with patch("app.desktop.heybox_login.QDesktopServices.openUrl") as open_url:
            self.send()
            self.assertEqual(self.api.requests(SEND), [{"phone": PHONE}])
            self.assertTrue(self.form.code.isEnabled())
            self.form.code.setText("123456")
            self.api.plan(VERIFY, {"ok": True, "stage": "success",
                                  "msg": "App 登录成功，任务数据已验证"})
            self.form.verify_button.click()
        open_url.assert_not_called()
        self.assertEqual(self.api.requests(VERIFY), [{"phone": PHONE, "code": "123456"}])
        self.assertEqual(self.form.code.text(), "")
        self.assertTrue(self.page.state["app_logged_in"])
        self.assertIn("App 登录态已保存", self.page.account_login.text())
        self.assertTrue(any(path.endswith("/state") for _, path, _ in self.api.calls))

    def test_invalid_phone_and_code_do_not_make_requests(self):
        self.form.phone.setText("bad-phone")
        self.form.send_code()
        self.assertEqual(self.api.calls, [])
        self.send()
        previous = len(self.api.calls)
        self.form.code.setText("12a")
        self.form.verify_code()
        self.assertEqual(len(self.api.calls), previous)

    def test_phone_formats_are_normalized_before_sending(self):
        for entered, expected in (("+86 138-0013-8000", "+8613800138000"),
                                  ("86 13800138000", "+8613800138000"),
                                  ("+44 7700-900123", "+447700900123")):
            with self.subTest(phone=entered):
                api = FakeApi()
                form = type(self.form)(api)
                form._timer.stop()
                api.plan(SEND, {"ok": True, "stage": "code"})
                form.phone.setText(entered)
                form.send_button.click()
                self.assertEqual(api.requests(SEND), [{"phone": expected}])
                form.deleteLater()

    def test_send_processing_after_timeout_recovers_without_another_sms_request(self):
        self.api.plan(SEND, error="本次请求超时")
        self.api.plan(STATUS, {"stage": "sending", "msg": "正在发送验证码"})
        self.form.phone.setText(PHONE)
        self.form.send_button.click()
        self.assertFalse(self.form.phone.isEnabled())
        self.api.plan(STATUS, {"stage": "code", "msg": "验证码已发送", "resend_in": 45})
        self.form._tick()
        self.assertTrue(self.form.code.isEnabled())
        self.assertTrue(self.form.verify_button.isEnabled())
        self.assertFalse(self.form.send_button.isEnabled())
        self.assertEqual(len(self.api.requests(SEND)), 1)
        self.assertEqual(len(self.api.requests(STATUS)), 2)

    def test_verify_processing_after_timeout_refreshes_login_and_clears_code(self):
        self.send()
        self.form.code.setText("123456")
        self.api.plan(VERIFY, error="本次请求超时")
        self.api.plan(STATUS, {"stage": "verifying", "msg": "正在登录"})
        self.form.verify_button.click()
        self.assertFalse(self.form.verify_button.isEnabled())
        self.api.state["app_logged_in"] = True
        self.api.login_status = {"stage": "success", "msg": "App 登录成功"}
        self.api.plan(STATUS, self.api.login_status)
        self.form._tick()
        self.form._tick()
        self.assertTrue(self.page.state["app_logged_in"])
        self.assertEqual(self.form.code.text(), "")
        self.assertEqual(len(self.api.requests(VERIFY)), 1)
        self.assertEqual(len(self.api.requests("/heybox/api/state")), 1)

    def test_busy_request_disables_controls_and_prevents_duplicate_submission(self):
        self.send(defer=True)
        self.assertFalse(self.form.send_button.isEnabled())
        self.assertFalse(self.form.verify_button.isEnabled())
        self.assertFalse(self.form.phone.isEnabled())
        self.assertFalse(self.form.code.isEnabled())
        self.form.send_code()
        self.form.verify_code()
        self.form.open_captcha()
        self.assertEqual(len(self.api.requests(SEND)), 1)
        self.assertEqual(len(self.api.calls), 1)
        self.api.complete()
        self.assertTrue(self.form.code.isEnabled())

    def test_send_captcha_resumes_original_phone_once(self):
        self.api.plan(SEND, self.challenge("send"))
        self.plan_bridge()
        self.form.phone.setText(PHONE)
        with patch("app.desktop.heybox_login.QDesktopServices.openUrl", return_value=True) as open_url:
            self.form.send_button.click()
        self.assertEqual(open_url.call_args.args[0].toString(),
                         self.api.base_url + "/heybox/app-login/captcha/bridge-1")
        self.assertEqual(self.api.requests(OPEN), [{"captcha_id": "challenge-send", "captcha_for": "send"}])
        self.api.plan(RESULT, {"ok": True, "stage": "ready",
                               "captcha": {"ticket": "ticket-1", "randstr": "rand-1"}})
        self.api.plan(SEND, {"ok": True, "stage": "code"})
        self.form.poll_captcha()
        self.form.poll_captcha()
        self.assertEqual(self.api.requests(SEND), [
            {"phone": PHONE},
            {"phone": PHONE, "captcha": {"ticket": "ticket-1", "randstr": "rand-1",
                                          "captcha_id": "challenge-send"}},
        ])
        self.assertEqual(self.api.requests(VERIFY), [])

    def test_verify_captcha_resumes_code_without_resending_sms(self):
        self.send()
        self.form.code.setText("123456")
        self.api.plan(VERIFY, self.challenge("verify"))
        self.plan_bridge()
        with patch("app.desktop.heybox_login.QDesktopServices.openUrl", return_value=True):
            self.form.verify_button.click()
        self.api.plan(RESULT, {"ok": True, "stage": "ready",
                               "captcha": {"ticket": "ticket-2", "randstr": "rand-2"}})
        self.api.plan(VERIFY, {"ok": True, "stage": "success"})
        self.form.poll_captcha()
        self.form.poll_captcha()
        self.assertEqual(len(self.api.requests(SEND)), 1)
        self.assertEqual(self.api.requests(VERIFY), [
            {"phone": PHONE, "code": "123456"},
            {"phone": PHONE, "code": "123456", "captcha": {
                "ticket": "ticket-2", "randstr": "rand-2", "captcha_id": "challenge-verify"}},
        ])
        self.assertTrue(self.page.state["app_logged_in"])

    def test_browser_failure_keeps_captcha_retry_available(self):
        self.api.plan(SEND, self.challenge("send"))
        self.plan_bridge()
        self.form.phone.setText(PHONE)
        with patch("app.desktop.heybox_login.QDesktopServices.openUrl", return_value=False):
            self.form.send_button.click()
        self.assertTrue(self.form.captcha_button.isEnabled())
        self.assertIn("无法打开", self.form.status.text())
        self.assertEqual(len(self.api.requests(SEND)), 1)

    def test_login_error_recovers_status_and_keeps_code_for_retry(self):
        self.send()
        self.form.code.setText("123456")
        self.api.plan(VERIFY, error="短信验证码错误，请重新输入")
        self.form.verify_button.click()
        self.assertEqual(self.form.code.text(), "123456")
        self.assertTrue(self.form.code.isEnabled())
        self.assertTrue(self.form.verify_button.isEnabled())
        self.assertIn("验证码错误", self.form.status.text())
        self.assertEqual(len(self.api.requests(STATUS)), 1)
        self.assertFalse(self.page.state["app_logged_in"])

    def test_waiting_captcha_keeps_original_request_without_resending(self):
        self.api.plan(SEND, self.challenge("send"))
        self.plan_bridge()
        self.form.phone.setText(PHONE)
        with patch("app.desktop.heybox_login.QDesktopServices.openUrl", return_value=True):
            self.form.send_button.click()
        self.api.plan(RESULT, {"ok": True, "stage": "waiting"}, defer=True)
        self.form.poll_captcha()
        self.form.poll_captcha()
        self.assertEqual(len(self.api.requests(RESULT)), 1)
        self.api.complete()
        self.assertEqual(len(self.api.requests(SEND)), 1)
        self.assertTrue(self.form.captcha_button.isEnabled())

    def test_cancel_captcha_clears_pending_code_and_stops_polling(self):
        self.api.plan(SEND, self.challenge("send"))
        self.plan_bridge()
        self.form.phone.setText(PHONE)
        with patch("app.desktop.heybox_login.QDesktopServices.openUrl", return_value=True):
            self.form.send_button.click()
        self.form.code.setText("123456")
        self.api.plan("/heybox/api/app-login/cancel", {"ok": True, "stage": "idle"})
        self.form.cancel_button.click()
        self.form.poll_captcha()
        self.assertEqual(self.form.code.text(), "")
        self.assertTrue(self.form.phone.isEnabled())
        self.assertTrue(self.form.send_button.isEnabled())
        self.assertEqual(self.api.requests(RESULT), [])
        self.assertEqual(len(self.api.requests(SEND)), 1)

    def test_expired_captcha_restores_retry_without_submitting_proof(self):
        self.api.plan(SEND, self.challenge("send"))
        self.plan_bridge()
        self.form.phone.setText(PHONE)
        with patch("app.desktop.heybox_login.QDesktopServices.openUrl", return_value=True):
            self.form.send_button.click()
        self.api.plan(RESULT, {"ok": True, "stage": "expired"})
        self.api.plan(STATUS, {"stage": "expired", "msg": "登录流程已过期"})
        self.form.poll_captcha()
        self.form.poll_captcha()
        self.assertTrue(self.form.send_button.isEnabled())
        self.assertTrue(self.form.phone.isEnabled())
        self.assertIn("过期", self.form.status.text())
        self.assertEqual(len(self.api.requests(SEND)), 1)

    def test_refresh_replacing_challenge_never_relabels_old_proof(self):
        self.api.plan(SEND, self.challenge("send"))
        self.plan_bridge()
        self.form.phone.setText(PHONE)
        with patch("app.desktop.heybox_login.QDesktopServices.openUrl", return_value=True):
            self.form.send_button.click()
        replacement = {**self.challenge("send"), "captcha_id": "a-different-challenge"}
        self.form.update_state({"app_login": replacement})
        self.api.plan(RESULT, {"ok": True, "stage": "ready",
                               "captcha": {"ticket": "old-ticket", "randstr": "old-rand"}})
        self.form.poll_captcha()
        self.assertEqual(len(self.api.requests(SEND)), 1)
        self.assertEqual(self.api.requests(VERIFY), [])

    def test_success_refresh_preserves_unsaved_task_settings(self):
        enabled = self.page.settings.findChild(QCheckBox, "config_sign_enable")
        self.assertIsNotNone(enabled)
        enabled.setChecked(False)
        self.assertTrue(self.page._dirty)
        self.send()
        self.form.code.setText("123456")
        self.api.plan(VERIFY, {"ok": True, "stage": "success"})
        self.form.verify_button.click()
        self.assertFalse(enabled.isChecked())
        self.assertTrue(self.page._dirty)
        self.assertFalse(any(path.endswith("/save") for _, path, _ in self.api.calls))

    def test_last_run_error_does_not_discard_cached_tasks(self):
        state = {**self.api.state, "app_logged_in": True, "cached": {
            "nickname": "缓存账号", "tasks": [{"title": "签到", "done": False}],
        }}
        info = overview_data("/heybox", state, {"error": "上次请求失败"})
        self.assertEqual(len(info["tasks"]), 1)
        self.assertIn("上次请求失败", info["notice"])
        self.assertNotIn("web-secret", str(info))

    def test_finished_task_poll_refreshes_tasks_and_preserves_login_prompt(self):
        self.api.state["app_logged_in"] = True
        self.api.state["cached"] = {"tasks": [
            {"title": "分享帖子", "done": True},
        ]}
        self.page._was_running = True
        finished = {"seq": 0, "logs": [], "running": False, "done": True,
                    "summary": {"ok": 3, "fail": 0, "status": "success"}}
        self.page.logs_loaded(finished)
        self.assertTrue(self.page.state["cached"]["tasks"][0]["done"])
        self.assertFalse(self.page._was_running)
        self.form._state = {"stage": "code"}
        self.page.status.setText("请输入短信验证码")
        self.page.logs_loaded(finished)
        self.assertEqual(self.page.status.text(), "请输入短信验证码")


if __name__ == "__main__":
    unittest.main()
