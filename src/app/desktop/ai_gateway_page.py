"""Native multi-account authorization and gateway configuration."""
from __future__ import annotations

import base64
import copy
import json

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QFileDialog, QFormLayout, QPlainTextEdit, QPushButton, QScrollArea, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget)

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
        self.connection_info, self.model_rows = None, []
        self.connection_loading = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        box = QVBoxLayout(content)
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
        self.import_all = QPushButton("批量导入可用平台")
        self.import_all.clicked.connect(self.import_all_local)
        row.addWidget(self.import_all)
        account.content.addLayout(row)
        self.capabilities = label("读取平台能力…", muted=True)
        account.content.addWidget(self.capabilities)
        self.accounts = QTreeWidget()
        self.accounts.setHeaderLabels(["平台", "账号", "状态", "账号 ID"])
        self.accounts.setColumnWidth(0, 130)
        self.accounts.setColumnWidth(1, 180)
        self.accounts.setMinimumHeight(100)
        self.accounts.setMaximumHeight(130)
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
        box.addWidget(account)
        gateway = Card("兼容 API")
        base_row = QHBoxLayout()
        base_row.addWidget(label("Base URL"))
        self.base_url = QLineEdit(api.base_url + "/v1")
        self.base_url.setReadOnly(True)
        base_row.addWidget(self.base_url, 1)
        copy_base = QPushButton("复制地址")
        copy_base.clicked.connect(lambda: self.copy_text(self.base_url.text(), "Base URL 已复制"))
        base_row.addWidget(copy_base)
        gateway.content.addLayout(base_row)
        controls = QHBoxLayout()
        key = QPushButton("复制 API Key")
        key.clicked.connect(self.copy_key)
        controls.addWidget(key)
        self.query_models = QPushButton("刷新模型与接入信息")
        self.query_models.clicked.connect(self.load_models)
        controls.addWidget(self.query_models)
        export = QPushButton("导出统一配置")
        export.clicked.connect(self.export_config)
        controls.addWidget(export)
        controls.addStretch()
        gateway.content.addLayout(controls)
        self.connection_summary = label("打开页面后自动查询目录；查询目录不会发送聊天请求。", muted=True)
        gateway.content.addWidget(self.connection_summary)
        self.connection_tabs = QTabWidget()
        setup_scroll = QScrollArea()
        setup_scroll.setWidgetResizable(True)
        setup_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        setup = QWidget()
        setup_scroll.setWidget(setup)
        setup_box = QVBoxLayout(setup)
        fields = QFormLayout()
        fields.addRow("API 格式", label("OpenAI Chat Completions"))
        self.chat_url = QLineEdit(api.base_url + "/v1/chat/completions")
        self.chat_url.setReadOnly(True)
        fields.addRow("完整请求 URL", self.chat_url)
        self.models_url = QLineEdit(api.base_url + "/v1/models")
        self.models_url.setReadOnly(True)
        fields.addRow("模型目录 URL", self.models_url)
        self.messages_url = QLineEdit(api.base_url + "/v1/messages")
        self.messages_url.setReadOnly(True)
        fields.addRow("Anthropic URL", self.messages_url)
        choose = QHBoxLayout()
        self.model_picker = QComboBox()
        self.model_picker.setMinimumContentsLength(24)
        self.model_picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.model_picker.currentIndexChanged.connect(self.model_changed)
        choose.addWidget(self.model_picker, 1)
        self.copy_model_button = QPushButton("复制模型 ID")
        self.copy_model_button.clicked.connect(self.copy_model)
        self.copy_model_button.setEnabled(False)
        choose.addWidget(self.copy_model_button)
        fields.addRow("选择模型", choose)
        self.model_id = QLineEdit()
        self.model_id.setReadOnly(True)
        self.model_id.setPlaceholderText("从当前目录选择后原样粘贴，保留平台/前缀")
        fields.addRow("模型 ID（原样填）", self.model_id)
        fields.addRow("认证方式", label("Authorization: Bearer <API_KEY>；使用上方“复制 API Key”取得密钥。", muted=True))
        setup_box.addLayout(fields)
        self.model_note = label("正在等待可用模型目录。", muted=True)
        self.model_note.setTextFormat(Qt.TextFormat.PlainText)
        self.model_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        setup_box.addWidget(self.model_note)
        self.client_help = label("Trae 自定义模型：关闭“完整 URL”时填 Base URL；打开时填完整请求 URL。模型 ID 必须包含平台前缀，不能只填 deepseek-v4.1-flash。", muted=True)
        self.client_help.setTextFormat(Qt.TextFormat.PlainText)
        self.client_help.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        setup_box.addWidget(self.client_help)
        copy_guide = QPushButton("复制当前模型接入说明")
        copy_guide.clicked.connect(self.copy_connection_guide)
        setup_box.addWidget(copy_guide, 0, Qt.AlignmentFlag.AlignLeft)
        setup_box.addStretch()
        self.connection_tabs.addTab(setup_scroll, "客户端怎么填")
        model_tab = QWidget()
        model_box = QVBoxLayout(model_tab)
        filters = QHBoxLayout()
        self.model_search = QLineEdit()
        self.model_search.setPlaceholderText("搜索模型 ID / 平台，例如 deepseek、trae")
        self.model_search.textChanged.connect(self.filter_models)
        filters.addWidget(self.model_search, 1)
        self.model_platform = QComboBox()
        self.model_platform.addItem("全部平台", "")
        self.model_platform.currentIndexChanged.connect(self.filter_models)
        filters.addWidget(self.model_platform)
        model_box.addLayout(filters)
        self.models = QTreeWidget()
        self.models.setHeaderLabels(["完整模型 ID（原样填写）", "平台", "兼容格式", "使用范围"])
        self.models.setColumnWidth(0, 330)
        self.models.setColumnWidth(1, 130)
        self.models.setColumnWidth(2, 170)
        self.models.setMinimumHeight(170)
        self.models.currentItemChanged.connect(self.model_selected)
        self.models.itemDoubleClicked.connect(lambda *_: self.copy_model())
        model_box.addWidget(self.models, 1)
        self.model_count = label("尚未加载模型。", muted=True)
        model_box.addWidget(self.model_count)
        self.catalog_model_note = label("选择一个模型查看完整使用限制。", muted=True)
        self.catalog_model_note.setTextFormat(Qt.TextFormat.PlainText)
        self.catalog_model_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        model_box.addWidget(self.catalog_model_note)
        self.connection_tabs.addTab(model_tab, "可用模型")
        provider_tab = QWidget()
        provider_box = QVBoxLayout(provider_tab)
        provider_box.addWidget(label("只展示当前服务实际启用的能力；有签到或本机登录态不代表已开放聊天。", muted=True))
        self.provider_table = QTreeWidget()
        self.provider_table.setHeaderLabels(["平台", "启用账号", "目录模型", "接入状态与使用限制"])
        self.provider_table.setColumnWidth(0, 150)
        self.provider_table.setColumnWidth(1, 85)
        self.provider_table.setColumnWidth(2, 85)
        self.provider_table.setMinimumHeight(170)
        self.provider_table.currentItemChanged.connect(self.provider_selected)
        provider_box.addWidget(self.provider_table, 1)
        self.provider_note = label("选择平台查看完整接入状态与使用限制。", muted=True)
        self.provider_note.setTextFormat(Qt.TextFormat.PlainText)
        self.provider_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        provider_box.addWidget(self.provider_note)
        self.connection_tabs.addTab(provider_tab, "平台能力")
        example_tab = QWidget()
        example_box = QVBoxLayout(example_tab)
        example_box.addWidget(label("示例随所选模型变化；<API_KEY> 是占位符，请替换为复制的统一密钥。此处不会自动发送请求。", muted=True))
        self.example = QPlainTextEdit()
        self.example.setReadOnly(True)
        self.example.setMinimumHeight(170)
        example_box.addWidget(self.example, 1)
        example_actions = QHBoxLayout()
        self.copy_example_button = QPushButton("复制完整请求示例")
        self.copy_example_button.clicked.connect(lambda: self.copy_text(self.example.toPlainText(), "请求示例已复制（密钥为占位符）"))
        self.copy_json_button = QPushButton("复制请求 JSON")
        self.copy_json_button.clicked.connect(self.copy_request_json)
        example_actions.addWidget(self.copy_example_button)
        example_actions.addWidget(self.copy_json_button)
        example_actions.addStretch()
        example_box.addLayout(example_actions)
        self.connection_tabs.addTab(example_tab, "请求示例")
        self.connection_tabs.setMinimumHeight(320)
        gateway.content.addWidget(self.connection_tabs, 1)
        box.addWidget(gateway, 1)
        self.status = label("", muted=True)
        box.addWidget(self.status)
        self.model_changed()

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
        self.chat_url.setText(data["base_url"].rstrip("/") + "/chat/completions")
        self.models_url.setText(data["base_url"].rstrip("/") + "/models")
        self.messages_url.setText(data["base_url"].rstrip("/") + "/messages")
        self.status.setText(f"已保存 {len(data['accounts'])} 个账号，可继续扫码添加其他账号。")
        self.load_models()

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

    def import_all_local(self):
        self.local.setEnabled(False)
        self.import_all.setEnabled(False)
        self.status.setText("正在批量读取并加密保存可用平台的本机登录态…")

        def imported(data):
            self.refresh()
            rows = data.get("results") or []
            summary = "；".join(f"{row.get('name', row.get('provider', '平台'))}：{row.get('message', '')}"
                                  for row in rows)
            self.status.setText(f"批量导入完成：新增或更新 {data.get('imported', 0)} 个账号，失败 {data.get('failed', 0)} 个。" +
                                ((" " + summary) if summary else ""))

        self.api.post("/ai-gateway/api/import-local", imported, body={},
                      done=lambda: (self.local.setEnabled(True), self.import_all.setEnabled(True)))

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
        if self.connection_loading:
            return
        self.connection_loading = True
        self.query_models.setEnabled(False)
        self.previous_model = self.model_picker.currentData()
        self.connection_info, self.model_rows = None, []
        self.model_picker.clear()
        self.models.clear()
        self.provider_table.clear()
        self.model_count.setText("正在查询模型目录…")
        self.connection_summary.setText("正在查询可用模型、完整端点和平台限制；不会发送聊天请求。")
        self.model_note.setText("正在读取当前服务的模型目录与使用限制…")
        self.api.get("/ai-gateway/api/connection", self.connection_loaded, quiet=True,
                     error=self.connection_failed, done=self.connection_finished)

    def connection_finished(self):
        self.connection_loading = False
        self.query_models.setEnabled(True)

    def connection_failed(self, message):
        self.connection_summary.setText("接入信息查询失败：" + message)
        self.model_note.setText("请重试“刷新模型与接入信息”。若提示 Not Found，需完整退出旧后台服务并重启应用以加载新版接口。")
        self.model_count.setText("目录未加载成功，暂不能确认可用模型；不会使用上次目录冒充本次结果。")
        self.model_changed()

    @staticmethod
    def format_names(row):
        names = {"openai-chat-completions": "OpenAI Chat", "anthropic-messages": "Anthropic"}
        return " / ".join(names.get(value, value) for value in row.get("api_formats", [])) or "未开放"

    def connection_loaded(self, data):
        self.connection_info = copy.deepcopy(data)
        self.model_rows = data.get("models", [])
        self.base_url.setText(data["base_url"])
        self.chat_url.setText(data["endpoints"]["chat_completions"])
        self.models_url.setText(data["endpoints"]["models"])
        self.messages_url.setText(data["endpoints"]["messages"])
        self.model_picker.blockSignals(True)
        for row in self.model_rows:
            self.model_picker.addItem(row["provider_name"] + " · " + row["id"].split("/", 1)[-1], row["id"])
            item = QTreeWidgetItem([row["id"], row["provider_name"], self.format_names(row),
                                   "仅单条 user；不适合 Agent" if row.get("agent_compatible") is False else "以所选上游模型能力为准"])
            item.setData(0, Qt.ItemDataRole.UserRole, row["id"])
            for column in range(4):
                item.setToolTip(column, row.get("usage_note", ""))
            self.models.addTopLevelItem(item)
        index = self.model_picker.findData(self.previous_model)
        if index < 0:
            index = self.model_picker.findData(data.get("example", {}).get("body", {}).get("model"))
        if index >= 0:
            self.model_picker.setCurrentIndex(index)
        self.model_picker.blockSignals(False)
        selected_provider = self.model_platform.currentData()
        self.model_platform.blockSignals(True)
        self.model_platform.clear()
        self.model_platform.addItem("全部平台", "")
        for row in data.get("providers", []):
            self.model_platform.addItem(row["name"], row["id"])
            item = QTreeWidgetItem([row["name"], str(row["enabled_accounts"]),
                                   str(row["model_count"]), row.get("usage_note", "")])
            item.setToolTip(3, row.get("usage_note", ""))
            item.setData(0, Qt.ItemDataRole.UserRole, row.get("usage_note", ""))
            self.provider_table.addTopLevelItem(item)
        index = self.model_platform.findData(selected_provider)
        if index >= 0:
            self.model_platform.setCurrentIndex(index)
        self.model_platform.blockSignals(False)
        fields = data.get("client_fields", {})
        guidance = ["Trae 自定义模型：关闭“完整 URL”时填 Base URL；打开时填上方完整请求 URL。",
                    fields.get("model_id_help", "模型 ID 必须保留平台/前缀，不能只填 deepseek-v4.1-flash。"),
                    fields.get("api_key_help", "使用本工具的统一 API Key，不要填写平台登录 token。")]
        self.client_help.setText("\n".join(dict.fromkeys(line for line in guidance if line)))
        summary = data.get("summary", {})
        self.connection_summary.setText(f"当前目录 {len(self.model_rows)} 个模型 · {summary.get('chat_provider_count', 0)} 个聊天平台 · "
                                        f"{summary.get('enabled_account_count', 0)} 个启用账号。统一使用一个 API Key；模型 ID 请完整复制。")
        self.filter_models()
        self.model_changed()

    def filter_models(self, *_):
        query = self.model_search.text().strip().casefold()
        provider = self.model_platform.currentData()
        shown = 0
        for index in range(self.models.topLevelItemCount()):
            item = self.models.topLevelItem(index)
            model_id = item.data(0, Qt.ItemDataRole.UserRole)
            matches = (not query or query in (item.text(0) + " " + item.text(1)).casefold())
            matches = matches and (not provider or model_id.startswith(provider + "/"))
            item.setHidden(not matches)
            shown += bool(matches)
        if not self.model_rows:
            self.model_count.setText("暂无可用模型。请在账号管理中导入并启用支持聊天的平台；平台能力页可查看未开放原因。")
        else:
            self.model_count.setText(f"显示 {shown} / {len(self.model_rows)} 个模型。选中即用于接入配置和请求示例；双击复制完整模型 ID。")

    def model_selected(self, item, _previous):
        if item:
            index = self.model_picker.findData(item.data(0, Qt.ItemDataRole.UserRole))
            if index >= 0:
                self.model_picker.setCurrentIndex(index)

    def provider_selected(self, item, _previous):
        self.provider_note.setText((item.text(0) + "：" + item.data(0, Qt.ItemDataRole.UserRole))
                                   if item else "选择平台查看完整接入状态与使用限制。")

    def selected_model(self):
        model_id = self.model_picker.currentData()
        return next((row for row in self.model_rows if row["id"] == model_id), None)

    def model_changed(self, *_):
        row = self.selected_model()
        self.model_id.setText(row["id"] if row else "")
        self.copy_model_button.setEnabled(bool(row))
        self.copy_example_button.setEnabled(bool(row))
        self.copy_json_button.setEnabled(bool(row))
        self.messages_url.setEnabled(bool(row and "anthropic-messages" in row.get("api_formats", [])))
        self.messages_url.setToolTip("所选模型未开放 Anthropic Messages，请使用 OpenAI Chat Completions。"
                                     if row and "anthropic-messages" not in row.get("api_formats", []) else "Anthropic Messages 的完整请求地址")
        if row:
            self.model_note.setText(row["provider_name"] + " · " + self.format_names(row) + "\n" + row.get("usage_note", ""))
        elif self.connection_info is not None:
            self.model_note.setText("当前目录为空，请先导入并启用支持聊天的平台账号；不要手填不存在的模型名。")
        self.catalog_model_note.setText(row.get("usage_note", "") if row else "选择一个模型查看完整使用限制。")
        if not row:
            self.example.setPlainText("先选择当前目录中的一个模型，即可查看包含完整 URL、认证方式和请求 JSON 的示例。")
            return
        body = self.request_body()
        self.example.setPlainText("POST " + self.chat_url.text() +
            "\nAuthorization: Bearer <API_KEY>\nContent-Type: application/json\n\n" + json.dumps(body, ensure_ascii=False, indent=2))

    def request_body(self):
        if not self.selected_model() or not self.connection_info:
            return None
        body = copy.deepcopy(self.connection_info.get("example", {}).get("body", {}))
        body["model"] = self.model_picker.currentData()
        return body

    def copy_text(self, value, message):
        QApplication.clipboard().setText(value)
        self.status.setText(message)

    def copy_model(self):
        row = self.selected_model()
        if row:
            self.copy_text(row["id"], "完整模型 ID 已复制：" + row["id"])

    def copy_request_json(self):
        body = self.request_body()
        if body:
            self.copy_text(json.dumps(body, ensure_ascii=False, indent=2), "请求 JSON 已复制")

    def copy_connection_guide(self):
        row = self.selected_model()
        if not row:
            self.status.setText("请先加载目录并选择模型。")
            return
        guide = "\n".join(["API 格式：OpenAI Chat Completions",
            "Base URL（完整 URL 关闭）：" + self.base_url.text(),
            "完整 URL（开关打开）：" + self.chat_url.text(),
            "模型 ID（原样填写）：" + row["id"],
            "模型显示名称：" + row["provider_name"] + " / " + row["id"].split("/", 1)[-1],
            "API 密钥：使用页面“复制 API Key”取得的统一密钥",
            "使用范围：" + row.get("usage_note", "")])
        self.copy_text(guide, "当前模型的接入说明已复制（不含密钥）")

    def export_config(self):
        def save(key, connection):
            path, _ = QFileDialog.getSaveFileName(self, "导出统一 API 配置", "ai-gateway-config.json",
                                                  "JSON 文件 (*.json);;所有文件 (*)")
            if not path:
                return
            data = {"base_url": self.base_url.text(), "api_key": key["api_key"],
                    "models": [row["id"] for row in connection.get("models", [])],
                    "connection": connection}
            data["base_url"] = connection["base_url"]
            try:
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
            except OSError as exc:
                self.status.setText(f"导出失败：{exc}")
            else:
                message = "已导出统一 API 配置（含 API Key）"
                if not data["models"]:
                    message += "；当前模型列表为空"
                self.status.setText(message)

        def got_connection(connection):
            self.api.get("/ai-gateway/api/key", lambda key: save(key, connection))

        self.api.get("/ai-gateway/api/connection", got_connection, quiet=True,
                     error=lambda message: self.status.setText("导出失败：无法取得当前目录与接入信息。" + message))
