"""Desktop QR login flow for the shared Douyin account store."""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)


class DouyinQRLoginDialog(QDialog):
    def __init__(self, api, parent=None, on_saved=None):
        super().__init__(parent)
        self.api = api
        self.on_saved = on_saved
        self._closed = False
        self._active = False
        self.setWindowTitle("抖音扫码登录")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        self.message = QLabel("正在检查抖音账号登录权限…")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        actions = QHBoxLayout()
        actions.addStretch()
        self.cancel_button = QPushButton("关闭")
        self.cancel_button.clicked.connect(self.reject)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)

        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.poll)
        self.finished.connect(lambda _result: self.timer.stop())
        self.api.get("/douyin-chat/api/auth/check", self.auth_checked, quiet=True,
                     error=self.request_failed)

    def auth_checked(self, result):
        if result.get("need_password") and not result.get("authenticated"):
            password, accepted = QInputDialog.getText(
                self, "解锁抖音账号", "本机管理密码", QLineEdit.EchoMode.Password)
            if not accepted:
                self.reject()
                return
            self.message.setText("正在验证密码…")
            self.api.post("/douyin-chat/api/auth/login", self.authenticated,
                          body={"password": password}, quiet=True, error=self.request_failed)
            return
        self.start_login()

    def authenticated(self, result):
        self.api.headers["Authorization"] = "Bearer " + str(result["token"])
        self.start_login()

    def start_login(self):
        if self._closed:
            return
        self.message.setText("正在打开抖音登录窗口…")
        self.api.post("/douyin-chat/panel/api/accounts/browser-login/start",
                      self.login_started, quiet=True, error=self.request_failed)

    def login_started(self, _result):
        self._active = True
        self.message.setText("请在弹出的抖音窗口中扫码。登录成功后会自动保存账号。")
        self.timer.start()

    def poll(self):
        if not self._closed:
            self.api.get("/douyin-chat/panel/api/accounts/browser-login/status",
                         self.show_status, quiet=True, error=self.request_failed)

    def show_status(self, result):
        status = result.get("status")
        self.message.setText(str(result.get("message") or status or "正在等待登录…"))
        if status == "done" and result.get("account"):
            self._active = False
            account = result["account"]
            if self.on_saved:
                self.on_saved(account)
            self.accept()
        elif status in ("failed", "cancelled"):
            self._active = False
            self.timer.stop()

    def request_failed(self, message):
        self.message.setText(str(message))
        if not self._active:
            self.timer.stop()

    def reject(self):
        self._closed = True
        if self._active:
            self.api.post("/douyin-chat/panel/api/accounts/browser-login/cancel",
                          quiet=True)
            self._active = False
        super().reject()


def open_douyin_qr_login(api, parent=None, on_saved=None):
    dialog = DouyinQRLoginDialog(api, parent, on_saved)
    dialog.exec()
