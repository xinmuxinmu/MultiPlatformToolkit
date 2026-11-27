"""Native multi-account authorization and gateway configuration."""
from __future__ import annotations

import base64

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .task_components import Card, label


class LoginDialog(QDialog):
    def __init__(self, api, provider, parent):
        super().__init__(parent)
        self.api, self.provider, self.session_id = api, provider, None
        self.pending = self.closed = False
        self.setWindowTitle(provider["name"] + " · 扫码添加账号")
        self.setMinimumWidth(410)
        box = QVBoxLayout(self)
        self.status = label("正在获取授权二维码…", muted=True)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumHeight(290)
        box.addWidget(self.status)
        box.addWidget(self.image)
        box.addWidget(label("用手机扫描后按平台提示完成授权。每次授权添加一个账号。", muted=True))
        self.url = QLineEdit()
        self.url.setReadOnly(True)
        box.addWidget(self.url)
        row = QHBoxLayout()
        copy = QPushButton("复制授权链接")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.url.text()))
        row.addWidget(copy)
        row.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        box.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setInterval(2500)
        self.timer.timeout.connect(self.poll)
        self.finished.connect(self.cleanup)
        api.post("/ai-gateway/api/login/" + provider["id"], self.created, body={},
                 quiet=True, error=self.failed)

    def created(self, data):
        self.session_id = data["id"]
        if self.closed:
            self.api.call("DELETE", "/ai-gateway/api/login/" + self.session_id, quiet=True)
            return
        self.url.setText(data["auth_url"])
        pixmap = QPixmap()
        pixmap.loadFromData(base64.b64decode(data["qr_data"].split(",", 1)[1]))
        self.image.setPixmap(pixmap.scaled(290, 290, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.FastTransformation))
        self.status.setText("等待扫码授权，二维码有效期约 5 分钟。")
        self.timer.start()

    def poll(self):
        if self.pending or self.closed:
            return
        self.pending = True
        self.api.get("/ai-gateway/api/login/" + self.session_id, self.polled, quiet=True,
                     done=lambda: setattr(self, "pending", False), error=self.failed)

    def polled(self, data):
        if self.closed:
            return
        status = data.get("status")
        if status == "authorized":
            self.status.setText("账号已添加")
            self.accept()
        elif status in ("expired", "denied"):
            self.timer.stop()
            self.status.setText("授权已过期或被取消，请关闭后重新扫码。")
        else:
            self.status.setText("已扫码，请在手机确认授权。" if status == "scanned" else "等待扫码授权…")

    def failed(self, message):
        if self.closed:
            return
        self.timer.stop()
        self.status.setText(message)

    def cleanup(self, _result):
        self.closed = True
        self.timer.stop()
        if self.session_id:
            self.api.call("DELETE", "/ai-gateway/api/login/" + self.session_id, quiet=True)


class AIGatewayPage(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api, self.initialized, self.providers, self.dialogs = api, False, {}, []
        box = QVBoxLayout(self)
        box.setContentsMargins(16, 16, 16, 16)
        box.setSpacing(12)
        top = QHBoxLayout()
        top.addWidget(label("AI 多账号与反代", section=True), 1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        top.addWidget(refresh)
        box.addLayout(top)
        account = Card("账号管理")
        row = QHBoxLayout()
        self.platform = QComboBox()
        self.platform.currentIndexChanged.connect(self.selection_changed)
        row.addWidget(self.platform, 1)
        self.login = QPushButton("扫码添加账号")
        self.login.clicked.connect(self.start_login)
        row.addWidget(self.login)
        self.local = QPushButton("导入本机登录态")
        self.local.clicked.connect(self.import_local)
        row.addWidget(self.local)
        account.content.addLayout(row)
        self.capabilities = label("读取平台能力…", muted=True)
        account.content.addWidget(self.capabilities)
        self.accounts = QTreeWidget()
        self.accounts.setHeaderLabels(["平台", "账号", "状态", "账号 ID"])
        self.accounts.setColumnWidth(0, 130)
        self.accounts.setColumnWidth(1, 180)
        account.content.addWidget(self.accounts, 1)
        actions = QHBoxLayout()
        toggle = QPushButton("启用 / 停用选中账号")
        toggle.clicked.connect(self.toggle_account)
        actions.addWidget(toggle)
        remove = QPushButton("移除选中账号")
        remove.clicked.connect(self.remove_account)
        actions.addWidget(remove)
        actions.addStretch()
        account.content.addLayout(actions)
        box.addWidget(account, 2)
        gateway = Card("兼容 API")
        self.base_url = QLineEdit(api.base_url + "/v1")
        self.base_url.setReadOnly(True)
        gateway.content.addWidget(self.base_url)
        controls = QHBoxLayout()
        key = QPushButton("复制 API Key")
        key.clicked.connect(self.copy_key)
        controls.addWidget(key)
        models = QPushButton("查询可用模型")
        models.clicked.connect(self.load_models)
        controls.addWidget(models)
        controls.addStretch()
        gateway.content.addLayout(controls)
        gateway.content.addWidget(label("支持 OpenAI Chat Completions 和 Anthropic Messages 的流式回复、工具调用。"
            "模型使用“平台/模型”格式；同平台多个启用账号依次轮换。凭据由 Windows 当前用户加密保存。", muted=True))
        self.models = QPlainTextEdit()
        self.models.setReadOnly(True)
        self.models.setMaximumHeight(100)
        self.models.setPlaceholderText("添加账号后查询模型；X-AI-Account 请求头可指定上表的账号 ID。")
        gateway.content.addWidget(self.models)
        box.addWidget(gateway, 1)
        self.status = label("", muted=True)
        box.addWidget(self.status)

    def on_activate(self):
        if not self.initialized:
            self.refresh()

    def refresh(self):
        self.api.get("/ai-gateway/api/state", self.loaded)

    def loaded(self, data):
        self.initialized = True
        selected = self.platform.currentData()
        self.providers = {p["id"]: p for p in data["providers"]}
        self.platform.blockSignals(True)
        self.platform.clear()
        for p in self.providers.values():
            self.platform.addItem(p["name"], p["id"])
        idx = self.platform.findData(selected)
        if idx >= 0:
            self.platform.setCurrentIndex(idx)
        self.platform.blockSignals(False)
        self.selection_changed()
        self.accounts.clear()
        for account in data["accounts"]:
            provider = self.providers.get(account["provider"], {})
            item = QTreeWidgetItem([provider.get("name", account["provider"]), account["label"],
                                   "已启用" if account["enabled"] else "已停用", account["id"]])
            item.setData(0, Qt.ItemDataRole.UserRole, account)
            self.accounts.addTopLevelItem(item)
        self.base_url.setText(data["base_url"])
        self.status.setText(f"已保存 {len(data['accounts'])} 个账号，可继续扫码添加其他账号。")

    def selection_changed(self, *_):
        p = self.providers.get(self.platform.currentData(), {})
        caps = p.get("capabilities", {})
        self.login.setEnabled(bool(caps.get("login")))
        self.local.setEnabled(bool(caps.get("local_import")))
        supported = [title for key, title in (("login", "扫码授权"), ("local_import", "本地导入"),
                     ("chat", "聊天反代"), ("checkin", "HTTP 签到")) if caps.get(key)]
        self.capabilities.setText(("支持：" + "、".join(supported) if supported else "暂未接入") +
                                  ("。" + caps["reason"] if caps.get("reason") else ""))

    def start_login(self):
        p = self.providers.get(self.platform.currentData())
        if not p or not p["capabilities"].get("login"):
            return
        dialog = LoginDialog(self.api, p, self)
        self.dialogs.append(dialog)
        dialog.accepted.connect(self.refresh)
        dialog.finished.connect(lambda _: self.dialogs.remove(dialog) if dialog in self.dialogs else None)
        dialog.open()

    def import_local(self):
        self.local.setEnabled(False)
        self.status.setText("正在读取并解密本机登录态…")
        def imported(data):
            self.refresh()
            self.status.setText(f"已导入 {data['count']} 个账号")
        self.api.post("/ai-gateway/api/import/" + self.platform.currentData(), imported, body={},
                      done=self.selection_changed)

    def selected_account(self):
        item = self.accounts.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def toggle_account(self):
        account = self.selected_account()
        if account:
            self.api.call("PATCH", "/ai-gateway/api/accounts/" + account["id"], lambda _: self.refresh(),
                          body={"enabled": not account["enabled"]})

    def remove_account(self):
        account = self.selected_account()
        if account:
            self.api.call("DELETE", "/ai-gateway/api/accounts/" + account["id"], lambda _: self.refresh())

    def copy_key(self):
        def copied(data):
            QApplication.clipboard().setText(data["api_key"])
            self.status.setText("网关 API Key 已复制")
        self.api.get("/ai-gateway/api/key", copied)

    def load_models(self):
        self.models.setPlainText("正在查询平台模型…")
        self.api.get("/ai-gateway/api/models", lambda d: self.models.setPlainText(
            "\n".join(m["id"] for m in d["data"]) or "暂无可用模型，请先添加支持反代的平台账号。"))
