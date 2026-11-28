"""Native SMS form; the browser is used only for a CAPTCHA challenge."""
from __future__ import annotations

from copy import deepcopy
import re
import time

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget


class HeyboxLoginWidget(QWidget):
    saved = Signal()
    message = Signal(str)
    PREFIX = "/heybox/api/app-login"

    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.api = api
        self._busy = False
        self._state = {}
        self._pending_request = None
        self._bridge_id = ""
        self._bridge_challenge = None
        self._cooldown_until = 0.0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.phone = QLineEdit()
        self.phone.setObjectName("heyboxPhone")
        self.phone.setPlaceholderText("请输入手机号")
        self.phone.setMaxLength(24)
        phone_row = QHBoxLayout()
        phone_row.addWidget(self.phone, 1)
        self.send_button = QPushButton("发送验证码")
        self.send_button.clicked.connect(self.send_code)
        phone_row.addWidget(self.send_button)
        form.addRow("手机号", phone_row)
        self.code = QLineEdit()
        self.code.setObjectName("heyboxSmsCode")
        self.code.setPlaceholderText("请输入收到的短信验证码")
        self.code.setMaxLength(8)
        self.code.returnPressed.connect(self.verify_code)
        code_row = QHBoxLayout()
        code_row.addWidget(self.code, 1)
        self.verify_button = QPushButton("登录并验证")
        self.verify_button.setProperty("variant", "primary")
        self.verify_button.setStyleSheet("QPushButton:disabled { background: palette(alternate-base); "
                                        "color: palette(placeholder-text); border-color: palette(shadow); }")
        self.verify_button.clicked.connect(self.verify_code)
        code_row.addWidget(self.verify_button)
        form.addRow("短信验证码", code_row)
        layout.addLayout(form)
        self.status = QLabel("在这里接收短信并登录，成功后自动保存 App 登录态。")
        self.status.setWordWrap(True)
        self.status.setProperty("role", "muted")
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.captcha_button = QPushButton("重新打开人机验证")
        self.captcha_button.clicked.connect(self.open_captcha)
        actions.addWidget(self.captcha_button)
        self.cancel_button = QPushButton("取消本次登录")
        self.cancel_button.clicked.connect(self.cancel)
        actions.addWidget(self.cancel_button)
        actions.addStretch()
        layout.addLayout(actions)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._controls()

    def _notice(self, text):
        self.status.setText(str(text))
        self.message.emit(str(text))

    @property
    def in_progress(self):
        return self._busy or self._state.get("stage") in ("sending", "verifying", "captcha", "code")

    def _controls(self):
        stage = self._state.get("stage", "idle")
        challenge = stage == "captcha"
        waiting = self._busy or stage in ("sending", "verifying")
        cooldown = max(0, int(self._cooldown_until - time.monotonic() + .99))
        self.phone.setEnabled(not waiting and not challenge)
        self.code.setEnabled(not waiting and not challenge)
        self.send_button.setEnabled(not waiting and not challenge and not cooldown)
        self.send_button.setText(f"{cooldown} 秒后重发" if cooldown else "发送验证码")
        self.verify_button.setEnabled(not waiting and stage == "code")
        self.captcha_button.setVisible(challenge)
        self.captcha_button.setEnabled(not waiting)
        self.cancel_button.setVisible(stage in ("captcha", "code", "expired", "error"))
        self.cancel_button.setEnabled(not waiting)

    def update_state(self, state):
        # A regular page refresh must not replace an in-flight form submission.
        if self._busy:
            return
        data = state.get("app_login") or {}
        if data:
            previous = self._state
            if (previous.get("stage") == "captcha"
                    and (data.get("captcha_id"), data.get("captcha_for"))
                    != (previous.get("captcha_id"), previous.get("captcha_for"))):
                self._bridge_id = ""
                self._bridge_challenge = None
                self._pending_request = None
            self._apply_state(data)
        elif state.get("app_logged_in"):
            self._notice("App 登录态已保存，可执行每日任务。")

    def _apply_state(self, data):
        self._state = deepcopy(data)
        if "resend_in" in data:
            self._cooldown_until = time.monotonic() + max(0, int(data["resend_in"] or 0))
        if data.get("msg"):
            self._notice(data["msg"])
        if data.get("stage") not in ("captcha", "sending", "verifying"):
            self._bridge_id = ""
        self._controls()

    def _request(self, method, path, callback, *, body=None, error=None):
        if self._busy:
            return
        self._busy = True
        self._controls()
        outcome = {}

        def failed(text):
            outcome["error"] = str(text)

        def finished():
            self._busy = False
            self._controls()
            if "data" in outcome:
                callback(outcome["data"])
            else:
                (error or self._recover_error)(outcome.get("error") or "请求未完成，请重试")

        kwargs = dict(quiet=True, done=finished, error=failed)
        if method == "post":
            kwargs["body"] = body or {}
        getattr(self.api, method)(path, lambda data: outcome.update(data=data), **kwargs)

    def _recover_error(self, text):
        def recovered(data):
            self._apply_state(data)
            self._notice(text)
            if data.get("stage") not in ("captcha", "sending", "verifying"):
                self._pending_request = None

        self._request("get", self.PREFIX + "/status", recovered,
                      error=lambda _: self._notice(text))

    def _phone_value(self):
        phone = self.phone.text().strip()
        normalized = re.sub(r"[\s-]", "", phone)
        if re.fullmatch(r"861[0-9]{10}", normalized):
            normalized = "+" + normalized
        if not re.fullmatch(r"(?:1[0-9]{10}|\+[0-9]{8,15})", normalized):
            self._notice("请填写正确的手机号，海外号码请包含 + 和国家或地区区号。")
            self.phone.setFocus()
            return None
        return normalized

    def send_code(self):
        if self._busy or not self.send_button.isEnabled():
            return
        phone = self._phone_value()
        if phone:
            self._submit("send", {"phone": phone})

    def verify_code(self):
        if self._busy or not self.verify_button.isEnabled():
            return
        phone, code = self._phone_value(), self.code.text().strip()
        if not phone:
            return
        if not re.fullmatch(r"\d{4,8}", code):
            self._notice("请输入 4 至 8 位短信验证码。")
            self.code.setFocus()
            return
        self._submit("verify", {"phone": phone, "code": code})

    def _submit(self, action, body):
        self._bridge_id = ""
        self._pending_request = (action, deepcopy(body))
        self._notice("正在发送验证码…" if action == "send" else "正在登录并验证任务数据…")
        path = "/send-code" if action == "send" else "/verify"
        self._request("post", self.PREFIX + path,
                      lambda data: self._submitted(action, data), body=body)

    def _submitted(self, action, data):
        self._apply_state(data)
        stage = data.get("stage")
        if stage == "captcha":
            self.open_captcha()
            return
        self._pending_request = None
        if action == "send" and stage == "code":
            if "resend_in" not in data:
                self._cooldown_until = time.monotonic() + 60
            self.code.clear()
            self.code.setFocus()
        if stage == "success":
            self.code.clear()
            self.saved.emit()
        self._controls()

    def open_captcha(self):
        if self._busy or self._state.get("stage") != "captcha":
            return
        if not self._pending_request:
            action = self._state.get("captcha_for")
            phone = self._phone_value()
            if not phone:
                self._notice("本次验证缺少手机号，请取消后重新发送验证码。")
                return
            body = {"phone": phone}
            if action == "verify":
                code = self.code.text().strip()
                if not re.fullmatch(r"\d{4,8}", code):
                    self._notice("本次验证缺少短信验证码，请取消后重新登录。")
                    return
                body["code"] = code
            self._pending_request = (action, body)
        self._bridge_id = ""
        self._bridge_challenge = (self._state.get("captcha_id"), self._state.get("captcha_for"))
        self._request("post", self.PREFIX + "/captcha/open", self._captcha_opened, body={
            "captcha_id": self._state.get("captcha_id", ""),
            "captcha_for": self._state.get("captcha_for", ""),
        })

    def _captcha_opened(self, data):
        path, bridge_id = str(data.get("url") or ""), str(data.get("bridge_id") or "")
        if not path.startswith("/heybox/app-login/captcha/") or not bridge_id:
            self._notice("人机验证页面无效，请重试。")
            return
        if not QDesktopServices.openUrl(QUrl(self.api.base_url + path)):
            self._notice("无法打开人机验证页，请检查默认浏览器，再点“重新打开人机验证”。")
            return
        self._bridge_id = bridge_id
        self._notice("请在验证页完成人机验证，通过后软件会自动继续本次操作。")

    def poll_captcha(self):
        if self._busy or not self._bridge_id:
            return
        self._request("get", self.PREFIX + "/captcha/result/" + self._bridge_id,
                      self._captcha_result, error=self._captcha_poll_error)

    def _captcha_poll_error(self, text):
        self._bridge_id = ""
        self._notice(str(text) + "；可重新打开人机验证。")

    def _captcha_result(self, data):
        stage = data.get("stage")
        if stage == "waiting":
            return
        self._bridge_id = ""
        if stage != "ready" or not self._pending_request:
            self._recover_error("人机验证已过期，请重试或取消后重新登录。")
            return
        action, body = self._pending_request
        challenge = (self._state.get("captcha_id"), self._state.get("captcha_for"))
        if self._bridge_challenge != challenge or action != challenge[1]:
            self._recover_error("登录步骤已变化，请重新操作。")
            return
        proof = data.get("captcha") or {}
        body = {**body, "captcha": {"ticket": proof.get("ticket", ""),
                 "randstr": proof.get("randstr", ""), "captcha_id": self._state.get("captcha_id", "")}}
        self._submit(action, body)

    def cancel(self):
        if self._busy:
            return

        def cancelled(data):
            self._pending_request = None
            self._bridge_id = ""
            self.code.clear()
            self._apply_state(data)
            self.phone.setFocus()

        self._request("post", self.PREFIX + "/cancel", cancelled)

    def _tick(self):
        self._controls()
        if not self._busy and self._state.get("stage") in ("sending", "verifying"):
            self._request("get", self.PREFIX + "/status", self._background_status,
                          error=self._notice)
        else:
            self.poll_captcha()

    def _background_status(self, data):
        if data.get("stage") in ("sending", "verifying"):
            self._apply_state(data)
        elif self._pending_request:
            self._submitted(self._pending_request[0], data)
        else:
            previous = self._state.get("stage")
            self._apply_state(data)
            if data.get("stage") == "success" and previous != "success":
                self.code.clear()
                self.saved.emit()
