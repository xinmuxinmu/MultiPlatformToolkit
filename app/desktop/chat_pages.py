"""Native Qt chat controls; no HTML shell or embedded web engine is required."""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from .widgets import JsonTree
from .upload import download, upload


def button(text, callback, layout):
    item = QPushButton(text)
    item.clicked.connect(lambda checked=False: callback())
    layout.addWidget(item)
    return item


def text_of(message):
    value = message.get("text", message.get("content", "")) or ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    if message.get("recall"):
        value = "[消息已撤回] " + value
    if message.get("voice_transcription"):
        value += "\n[语音转文字] " + str(message["voice_transcription"])
    return value


def time_of(message):
    value = message.get("ts", message.get("timestamp", 0)) or 0
    try:
        value = float(value)
        if value > 1e11:
            value /= 1000
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S") if value else "—"
    except (ValueError, TypeError, OSError, OverflowError):
        return str(value)


def attachments(message):
    result = []
    pictures = message.get("pics") or []
    if isinstance(pictures, str):
        pictures = [pictures]
    result.extend(pictures)
    for key in ("audio", "audioRaw", "video", "media_local_path"):
        if message.get(key):
            result.append(message[key])
    return list(dict.fromkeys(str(value) for value in result if value))


def confirmed(parent, title, text):
    return QMessageBox.question(parent, title, text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes


class AdvancedDialog(QDialog):
    """Explicit local API access keeps less common module operations available."""
    def __init__(self, api, prefix, operations, parent=None):
        super().__init__(parent)
        self.api, self.prefix, self.operations = api, prefix.rstrip("/"), operations
        self.setWindowTitle("高级操作 · 本机 Python 服务")
        self.resize(800, 650)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("选择操作后可编辑路径、查询参数和 JSON 请求体；点击执行才会发送请求。"))
        self.operation = QComboBox()
        for label, method, path, body in operations:
            self.operation.addItem(label)
        layout.addWidget(self.operation)
        form = QFormLayout()
        self.method = QComboBox()
        self.method.addItems(["GET", "POST", "DELETE"])
        self.path = QLineEdit()
        self.params = QLineEdit("{}")
        self.body = QPlainTextEdit()
        self.body.setMaximumHeight(150)
        form.addRow("方法", self.method)
        form.addRow("路径", self.path)
        form.addRow("查询参数（JSON 对象）", self.params)
        form.addRow("请求体（JSON）", self.body)
        layout.addLayout(form)
        self.result = JsonTree()
        layout.addWidget(self.result, 1)
        row = QHBoxLayout()
        button("执行", self.execute, row)
        button("关闭", self.close, row)
        row.addStretch()
        layout.addLayout(row)
        self.operation.currentIndexChanged.connect(self.choose)
        self.choose(0)

    def choose(self, index):
        _, method, path, body = self.operations[index]
        self.method.setCurrentText(method)
        self.path.setText(self.prefix + path)
        self.params.setText("{}")
        self.body.setPlainText(json.dumps(body, ensure_ascii=False, indent=2) if body is not None else "null")

    def execute(self):
        path = self.path.text().strip()
        if not path.startswith(self.prefix + "/") or ".." in path or "://" in path:
            QMessageBox.warning(self, "路径不正确", "高级操作只允许访问当前模块的本机 API。")
            return
        try:
            params = json.loads(self.params.text() or "{}")
            body = json.loads(self.body.toPlainText() or "null")
            if not isinstance(params, dict):
                raise ValueError("查询参数必须是 JSON 对象")
        except ValueError as exc:
            QMessageBox.warning(self, "JSON 不正确", str(exc))
            return
        if (self.method.currentText() == "DELETE" or "/delete" in path or "/clear" in path) and not confirmed(
                self, "确认执行删除", "该操作会删除或清除本机数据，确定继续？"):
            return
        self.api.call(self.method.currentText(), path, self.result.show_data,
                      body=body, params=params)


class MessagePane(QWidget):
    searchRequested = Signal()
    previousRequested = Signal()
    nextRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.root = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索消息正文、发送者…")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self.searchRequested.emit)
        row.addWidget(self.search, 1)
        button("搜索", self.searchRequested.emit, row)
        self.previous = button("更早", self.previousRequested.emit, row)
        self.next = button("更新", self.nextRequested.emit, row)
        self.position = QLabel("0 条")
        row.addWidget(self.position)
        layout.addLayout(row)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["时间", "发送者", "消息内容", "附件"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 155)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(3, 70)
        self.table.itemSelectionChanged.connect(self.show_detail)
        self.table.cellDoubleClicked.connect(lambda row, column: self.open_attachment() if column == 3 else self.show_detail())
        layout.addWidget(self.table, 1)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("选择一条消息查看完整正文；双击附件列打开本机媒体。")
        self.detail.setMaximumHeight(130)
        layout.addWidget(self.detail)
        self.open_button = QPushButton("打开选中消息的附件")
        self.open_button.clicked.connect(self.open_attachment)
        self.open_button.setEnabled(False)
        layout.addWidget(self.open_button)

    def show_rows(self, rows, *, label="", older=False, newer=False, root=None):
        self.rows, self.root = rows, Path(root) if root else None
        self.table.setRowCount(len(rows))
        for index, item in enumerate(rows):
            sender = item.get("fromName") or item.get("sender_name") or item.get("from") or item.get("sender_uid") or "系统"
            if item.get("conv_name"):
                sender = f"{item['conv_name']} · {sender}"
            values = [time_of(item), str(sender), text_of(item).replace("\n", " ⏎ ")[:1000],
                      str(len(attachments(item))) if attachments(item) else "—"]
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(value))
        self.position.setText(label or f"{len(rows)} 条")
        self.previous.setEnabled(older)
        self.next.setEnabled(newer)
        self.detail.clear()
        self.open_button.setEnabled(False)

    def selected_message(self):
        row = self.table.currentRow()
        return self.rows[row] if 0 <= row < len(self.rows) else None

    def show_detail(self):
        item = self.selected_message()
        if not item:
            return
        paths = attachments(item)
        self.detail.setPlainText(text_of(item) + ("\n\n本机附件：\n" + "\n".join(paths) if paths else ""))
        self.open_button.setEnabled(bool(paths))

    def open_attachment(self):
        item = self.selected_message()
        paths = attachments(item or {})
        if not paths or not self.root:
            return
        value = paths[0]
        if len(paths) > 1:
            value, ok = QInputDialog.getItem(self, "选择附件", "附件", paths, 0, False)
            if not ok:
                return
        root = self.root.resolve()
        target = (root / value).resolve()
        if target != root and root not in target.parents:
            QMessageBox.warning(self, "附件路径不正确", "附件不在当前账号的数据目录内。")
            return
        if not target.is_file():
            QMessageBox.information(self, "附件不存在", "该附件尚未下载，或原文件已被移动。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))


class ExportPage(QWidget):
    """Weibo/Bilibili/Taobao accounts, exports and local native chat browsing."""
    PAGE_SIZE = 100

    def __init__(self, api, prefix, title, parent=None):
        super().__init__(parent)
        self.api, self.prefix = api, prefix.rstrip("/")
        self._activated = False
        self._pending = set()
        self._seq = 0
        self._raw_stamp = None
        self._active = ""
        self._running = False
        self._convs = []
        self._current = None
        self._page = 0
        self.output_dir = ""
        self.taobao = self.prefix == "/taobao"
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        row = QHBoxLayout()
        self.accounts = QComboBox()
        self.accounts.setMinimumWidth(180)
        row.addWidget(self.accounts, 1)
        button("切换账号", self.switch_account, row)
        button("添加", self.add_account, row)
        button("改名", self.rename_account, row)
        button("删除", self.delete_account, row)
        button("登录", lambda: self.post("/api/login", refresh=False), row)
        button("检测登录", lambda: self.get("account", "/api/account", self.show_account), row)
        layout.addLayout(row)
        self.status = QLabel("选择页面后加载本机记录")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        browser = QWidget()
        browser_layout = QVBoxLayout(browser)
        splitter = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.conv_search = QLineEdit()
        self.conv_search.setPlaceholderText("搜索会话或消息全文…")
        self.conv_search.setClearButtonEnabled(True)
        self.conv_search.textChanged.connect(self.filter_conversations)
        left_layout.addWidget(self.conv_search)
        self.conversations = QListWidget()
        self.conversations.currentItemChanged.connect(self.choose_conversation)
        left_layout.addWidget(self.conversations, 1)
        button("删除选中会话记录", self.delete_conversation, left_layout)
        splitter.addWidget(left)
        self.messages = MessagePane()
        self.messages.searchRequested.connect(self.search_messages)
        self.messages.previousRequested.connect(lambda: self.page_messages(-1))
        self.messages.nextRequested.connect(lambda: self.page_messages(1))
        splitter.addWidget(self.messages)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 800])
        browser_layout.addWidget(splitter)
        tabs.addTab(browser, "聊天记录")
        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        options = QGroupBox("导出设置")
        form = QFormLayout(options)
        self.mode = QComboBox()
        self.mode.addItem("增量更新（保留历史）", "incremental")
        self.mode.addItem("全量重新抓取", "full")
        form.addRow("模式", self.mode)
        self.max_dm, self.max_group = QSpinBox(), QSpinBox()
        for spin, value in ((self.max_dm, 100000), (self.max_group, 3000)):
            spin.setRange(20, 1000000)
            spin.setValue(value)
        self.only = QLineEdit()
        self.only.setPlaceholderText("留空处理全部；填写会话 UID / 淘宝会话标识")
        form.addRow("单个会话", self.only)
        if not self.taobao:
            form.addRow("私聊消息上限", self.max_dm)
            form.addRow("群聊消息上限", self.max_group)
        self.urls = QPlainTextEdit()
        self.urls.setPlaceholderText("可留空自动发现；或每行一个淘宝聊天链接")
        self.urls.setMaximumHeight(110)
        if self.taobao:
            form.addRow("淘宝聊天链接", self.urls)
        controls_layout.addWidget(options)
        row = QHBoxLayout()
        self.run_button = button("开始导出", self.run_export, row)
        self.stop_button = button("停止", lambda: self.post("/api/stop", refresh=False), row)
        button("刷新本机记录", self.refresh, row)
        button("打开导出目录", self.open_output, row)
        row.addStretch()
        controls_layout.addLayout(row)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.document().setMaximumBlockCount(2000)
        self.logs.setPlaceholderText("导出过程日志")
        controls_layout.addWidget(self.logs, 1)
        tabs.addTab(controls, "导出与日志")
        row = QHBoxLayout()
        button("刷新", self.refresh, row)
        button("打开导出目录", self.open_output, row)
        row.addStretch()
        layout.addLayout(row)

    def get(self, key, path, callback, **kwargs):
        if key in self._pending:
            return
        self._pending.add(key)
        self.api.get(self.prefix + path, callback, done=lambda: self._pending.discard(key), **kwargs)

    def post(self, path, body=None, params=None, refresh=True):
        self.api.post(self.prefix + path, lambda data: self.refresh() if refresh else self.poll(), body=body, params=params)

    def on_activate(self):
        if not self._activated:
            self._activated = True
            self.refresh()

    def refresh(self):
        self.get("accounts", "/api/accounts", self.show_accounts)
        self.get("summary", "/api/summary", self.show_summary)
        self.poll()

    def poll(self):
        if not self._activated:
            return
        self.get("logs", "/api/logs", self.show_logs, params={"since": self._seq}, quiet=True)

    def show_accounts(self, data):
        self._active = str(data.get("active") or "")
        self.accounts.blockSignals(True)
        self.accounts.clear()
        for account in data.get("accounts") or []:
            label = account.get("name") or account.get("nickname") or account.get("id") or "账号"
            label += " · 当前" if str(account.get("id")) == self._active else ""
            self.accounts.addItem(label, str(account.get("id")))
        index = self.accounts.findData(self._active)
        self.accounts.setCurrentIndex(max(0, index))
        self.accounts.blockSignals(False)

    def show_account(self, data):
        self.status.setText(("已登录：" + str(data.get("name") or data.get("uid") or "当前账号")) if data.get("logged_in") else "登录态不可用，请点击登录。")

    def show_summary(self, data):
        summary = data.get("data") or {}
        self.output_dir = summary.get("output_dir") or ""
        self.status.setText(f"本机记录：{summary.get('conversations', 0)} 个会话 / {summary.get('messages', 0)} 条消息 · {summary.get('exported_at') or '尚未导出'}")
        stamp = (self._active, self.output_dir, summary.get("raw_mtime"))
        if summary.get("raw_size") and stamp != self._raw_stamp:
            self.get("raw:" + self.output_dir, "/raw.json", lambda payload: self.load_raw(payload, stamp))
        elif not summary.get("raw_size"):
            self._raw_stamp = stamp
            self._convs = []
            self.filter_conversations()

    def load_raw(self, payload, stamp=None):
        if not isinstance(payload, dict) or not isinstance(payload.get("convs"), list):
            return
        if stamp and stamp[1] != self.output_dir:
            return
        self._raw_stamp = stamp
        self._convs = payload["convs"]
        self.filter_conversations()

    def show_logs(self, data):
        for entry in data.get("logs") or []:
            self._seq = max(self._seq, int(entry.get("seq") or 0))
            self.logs.appendPlainText(str(entry.get("line") or ""))
        task = data.get("task") or {}
        running = bool(task.get("running"))
        self.run_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        if self._running and not running:
            self.refresh()
        self._running = running
        if running:
            self.status.setText("正在运行：" + str(task.get("kind") or "导出"))

    def switch_account(self):
        account = self.accounts.currentData()
        if account and account != self._active:
            self._raw_stamp = None
            self._convs = []
            self.filter_conversations()
            self.post("/api/accounts/switch", {"id": account})

    def add_account(self):
        value, ok = QInputDialog.getText(self, "添加账号", "账号备注名")
        if ok:
            self.post("/api/accounts/create", {"name": value.strip()})

    def rename_account(self):
        account = self.accounts.currentData()
        if not account:
            return
        value, ok = QInputDialog.getText(self, "账号改名", "新的备注名")
        if ok and value.strip():
            self.post("/api/accounts/rename", {"id": account, "name": value.strip()})

    def delete_account(self):
        account = self.accounts.currentData()
        if account and confirmed(self, "删除账号", "将删除该账号在本机的登录态和全部导出记录。确定继续？"):
            self._raw_stamp = None
            self.post("/api/accounts/delete", {"id": account})

    def filter_conversations(self):
        selected = str((self._current or {}).get("uid") or "")
        needle = self.conv_search.text().strip().casefold()
        self.conversations.blockSignals(True)
        self.conversations.clear()
        selected_item = None
        for conv in self._convs:
            if needle and needle not in str(conv.get("name") or "").casefold() and not any(needle in text_of(message).casefold() for message in conv.get("messages") or []):
                continue
            item = QListWidgetItem(f"{'群 · ' if conv.get('isGroup') else ''}{conv.get('name') or conv.get('uid')}  ({len(conv.get('messages') or [])})")
            item.setData(Qt.ItemDataRole.UserRole, conv)
            self.conversations.addItem(item)
            if str(conv.get("uid")) == selected:
                selected_item = item
        self.conversations.blockSignals(False)
        if self.conversations.count():
            self.conversations.setCurrentItem(selected_item or self.conversations.item(0))
        else:
            self._current = None
            self.messages.show_rows([])

    def choose_conversation(self, item, previous=None):
        self._current = item.data(Qt.ItemDataRole.UserRole) if item else None
        self._page = max(0, math.ceil(len((self._current or {}).get("messages") or []) / self.PAGE_SIZE) - 1)
        self.messages.search.clear()
        self.render_messages()

    def search_messages(self):
        self._page = 0
        self.render_messages()

    def page_messages(self, direction):
        self._page = max(0, self._page + direction)
        self.render_messages()

    def render_messages(self):
        needle = self.messages.search.text().strip().casefold()
        rows = (self._current or {}).get("messages") or []
        if needle:
            rows = [item for item in rows if needle in (text_of(item) + " " + str(item.get("fromName") or "")).casefold()]
        pages = max(1, math.ceil(len(rows) / self.PAGE_SIZE))
        self._page = min(self._page, pages - 1)
        start = self._page * self.PAGE_SIZE
        self.messages.show_rows(rows[start:start + self.PAGE_SIZE],
            label=f"{self._page + 1}/{pages} 页 · {len(rows)} 条", older=self._page > 0,
            newer=self._page + 1 < pages, root=self.output_dir)

    def delete_conversation(self):
        conv = self._current
        if conv and confirmed(self, "删除本机会话记录", f"确定删除「{conv.get('name') or conv.get('uid')}」的本机导出记录？平台聊天不会被删除。"):
            self._raw_stamp = None
            self.post("/api/records/delete", {"id": str(conv.get("uid") or conv.get("name") or "")})

    def run_export(self):
        mode = self.mode.currentData()
        if mode == "full" and not confirmed(self, "全量导出", "全量模式会重新抓取所选范围，并替换该范围内的旧消息基线。确定继续？"):
            return
        if self.taobao:
            self.post("/api/run", {"mode": mode, "only": self.only.text().strip(),
                "urls": [value.strip() for value in self.urls.toPlainText().splitlines() if value.strip()]}, refresh=False)
        else:
            self.post("/api/run", params={"mode": mode, "max_dm": self.max_dm.value(),
                "max_group": self.max_group.value(), "only_uid": self.only.text().strip()}, refresh=False)

    def open_output(self):
        if self.output_dir and Path(self.output_dir).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.output_dir).resolve())))
        else:
            QMessageBox.information(self, "尚未导出", "完成首次导出后即可打开记录目录。")


class DouyinPage(QWidget):
    """Native Douyin account/control panel and paginated SQLite-backed browser."""
    PREFIX = "/douyin-chat"
    PANEL = "/douyin-chat/panel"

    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.api = api
        self._activated = False
        self._authorized = False
        self._pending = set()
        self._active = ""
        self._conv_page = 1
        self._conv_total = 0
        self._current = None
        self._message_rows = []
        self._search_page = 1
        self._search_mode = False
        self._browser_login = False
        self._schedule_loaded = False
        self._scrape_status = ""
        self._last_log = ""
        layout = QVBoxLayout(self)
        title = QLabel("抖音聊天 · 导出与管理")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        account_row = QHBoxLayout()
        self.accounts = QComboBox()
        account_row.addWidget(self.accounts, 1)
        button("切换账号", self.switch_account, account_row)
        button("扫码添加 / 登录", self.start_login, account_row)
        button("取消登录", lambda: self.post("/api/accounts/browser-login/cancel"), account_row)
        button("改名", self.rename_account, account_row)
        button("删除", self.delete_account, account_row)
        layout.addLayout(account_row)
        self.status = QLabel("选择页面后读取本机状态")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        browse = QWidget()
        browse_layout = QVBoxLayout(browse)
        split = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.conv_search = QLineEdit()
        self.conv_search.setPlaceholderText("搜索会话名，回车查询")
        self.conv_search.returnPressed.connect(self.search_conversations)
        left_layout.addWidget(self.conv_search)
        self.conversations = QListWidget()
        self.conversations.currentItemChanged.connect(self.choose_conversation)
        left_layout.addWidget(self.conversations, 1)
        row = QHBoxLayout()
        self.conv_previous = button("上一页", lambda: self.page_conversations(-1), row)
        self.conv_next = button("下一页", lambda: self.page_conversations(1), row)
        self.conv_position = QLabel("0 个")
        row.addWidget(self.conv_position)
        left_layout.addLayout(row)
        button("删除选中会话记录", self.delete_conversation, left_layout)
        split.addWidget(left)
        self.messages = MessagePane()
        self.search_all = QCheckBox("在全部会话中搜索消息")
        self.messages.layout().insertWidget(1, self.search_all)
        self.messages.searchRequested.connect(self.search_messages)
        self.messages.previousRequested.connect(lambda: self.page_messages(-1))
        self.messages.nextRequested.connect(lambda: self.page_messages(1))
        split.addWidget(self.messages)
        split.setStretchFactor(1, 1)
        split.setSizes([260, 800])
        browse_layout.addWidget(split)
        tabs.addTab(browse, "聊天记录")
        manage = QWidget()
        manage_layout = QVBoxLayout(manage)
        form = QFormLayout()
        self.incremental = QCheckBox("只补齐新消息（增量）")
        self.incremental.setChecked(True)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("留空按默认范围；多个会话名用逗号分隔")
        self.format = QComboBox()
        self.format.addItem("ChatLab JSONL", "jsonl")
        self.format.addItem("ChatLab JSON", "json")
        self.format.addItem("完整 SQLite 数据库", "database")
        form.addRow(self.incremental)
        form.addRow("抓取 / 导出会话", self.filter)
        form.addRow("导出格式", self.format)
        manage_layout.addLayout(form)
        row = QHBoxLayout()
        self.scrape_button = button("开始抓取", self.scrape, row)
        self.stop_button = button("停止抓取", lambda: self.post("/api/scrape/stop"), row)
        button("刷新平台会话列表", lambda: self.post("/api/conversations/refresh"), row)
        button("停止刷新", lambda: self.post("/api/conversations/refresh/stop"), row)
        button("生成导出文件", self.export, row)
        manage_layout.addLayout(row)
        schedule = QGroupBox("定时抓取（应用运行期间生效）")
        schedule_layout = QHBoxLayout(schedule)
        self.schedule_enabled = QCheckBox("启用")
        self.schedule_cron = QLineEdit("0 10 * * *")
        self.schedule_cron.setPlaceholderText("cron：分 时 日 月 周")
        schedule_layout.addWidget(self.schedule_enabled)
        schedule_layout.addWidget(self.schedule_cron, 1)
        button("保存定时", self.save_schedule, schedule_layout)
        manage_layout.addWidget(schedule)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.document().setMaximumBlockCount(1500)
        manage_layout.addWidget(self.logs, 1)
        tabs.addTab(manage, "采集 / 导出 / 定时")
        row = QHBoxLayout()
        button("刷新本机记录", self.refresh, row)
        button("打开导出目录", self.open_output, row)
        button("高级操作", self.advanced, row)
        self.import_button = button("导入 SQLite 数据库", self.import_database, row)
        self.screenshot_button = button("导出聊天长图", self.screenshot, row)
        row.addStretch()
        layout.addLayout(row)

    def get(self, key, path, callback, panel=True, **kwargs):
        if key in self._pending:
            return
        self._pending.add(key)
        self.api.get((self.PANEL if panel else self.PREFIX) + path, callback,
            done=lambda: self._pending.discard(key), **kwargs)

    def post(self, path, body=None, callback=None):
        self.api.post(self.PANEL + path, callback or (lambda data: self.refresh()), body=body)

    def on_activate(self):
        if not self._activated:
            self._activated = True
            self.get("auth", "/api/auth/check", self.auth_checked, panel=False)

    def auth_checked(self, data):
        if data.get("authenticated"):
            self._authorized = True
            self.refresh()
            return
        password, ok = QInputDialog.getText(self, "解锁抖音聊天", "本机管理密码", QLineEdit.EchoMode.Password)
        if ok:
            self.api.post(self.PREFIX + "/api/auth/login", self.authenticated, body={"password": password})
        else:
            self.status.setText("抖音聊天已加密；再次点击刷新可解锁。")

    def authenticated(self, data):
        self.api.headers["Authorization"] = "Bearer " + str(data["token"])
        self._authorized = True
        self.refresh()

    def refresh(self):
        if not self._authorized:
            self.get("auth", "/api/auth/check", self.auth_checked, panel=False)
            return
        self.get("accounts", "/api/accounts", self.show_accounts)
        self.load_conversations()
        self.poll()

    def poll(self):
        if not self._activated or not self._authorized:
            return
        self.get("status", "/api/status", self.show_status, quiet=True)
        self.get("log", "/api/scrape/log", self.show_log, params={"lines": 100}, quiet=True)
        if self._browser_login:
            self.get("login", "/api/accounts/browser-login/status", self.login_status, quiet=True)

    def show_status(self, data):
        scrape, exported, scheduler = data.get("scrape") or {}, data.get("export") or {}, data.get("scheduler") or {}
        self.status.setText(f"本机：{data.get('conversations', 0)} 个会话 / {data.get('messages', 0)} 条消息 · {scrape.get('message') or scrape.get('status') or '就绪'} · {exported.get('message') or ''}")
        running = scrape.get("status") == "running"
        self.scrape_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        if self._scrape_status == "running" and not running:
            self.load_conversations()
        self._scrape_status = scrape.get("status")
        if not self._schedule_loaded:
            self.schedule_enabled.setChecked(bool(scheduler.get("enabled")))
            self.schedule_cron.setText(scheduler.get("schedule") or "0 10 * * *")
            self._schedule_loaded = True

    def show_log(self, data):
        value = str(data.get("log") or "")
        if value != self._last_log:
            self._last_log = value
            self.logs.setPlainText(value)
            bar = self.logs.verticalScrollBar()
            bar.setValue(bar.maximum())

    def show_accounts(self, data):
        self._active = str(data.get("active") or "")
        self.accounts.clear()
        for item in data.get("accounts") or []:
            label = item.get("name") or item.get("nickname") or item.get("id") or "账号"
            self.accounts.addItem(label + (" · 当前" if str(item.get("id")) == self._active else ""), str(item.get("id")))
        self.accounts.setCurrentIndex(max(0, self.accounts.findData(self._active)))

    def switch_account(self):
        aid = self.accounts.currentData()
        if aid and aid != self._active:
            self._conv_page = 1
            self._current = None
            self.messages.show_rows([])
            def switched(data):
                self._active = aid
                self.refresh()
            self.post("/api/accounts/switch", {"id": aid}, callback=switched)

    def start_login(self):
        self._browser_login = True
        self.post("/api/accounts/browser-login/start", callback=lambda data: self.status.setText("浏览器已启动，请扫码登录；完成后会自动添加账号。"))

    def login_status(self, data):
        self.status.setText(data.get("message") or str(data.get("status") or ""))
        if data.get("status") not in ("starting", "waiting_scan", "finalizing"):
            self._browser_login = False
            self.refresh()

    def rename_account(self):
        aid = self.accounts.currentData()
        name, ok = QInputDialog.getText(self, "账号改名", "新的备注名")
        if aid and ok and name.strip():
            self.post(f"/api/accounts/{quote(aid, safe='')}/rename", {"name": name.strip()})

    def delete_account(self):
        aid = self.accounts.currentData()
        if aid and confirmed(self, "删除抖音账号", "将删除该账号在本机的聊天、登录态，以及关联的火花任务和好友缓存。确定继续？"):
            self._conv_page = 1
            self.post(f"/api/accounts/{quote(aid, safe='')}/delete")

    def load_conversations(self):
        self.get("conversations", "/api/conversations", self.show_conversations, panel=False,
            params={"search": self.conv_search.text().strip() or None, "page": self._conv_page, "page_size": 100})

    def search_conversations(self):
        self._conv_page = 1
        self.load_conversations()

    def page_conversations(self, direction):
        self._conv_page = max(1, self._conv_page + direction)
        self.load_conversations()

    def show_conversations(self, data):
        self._conv_total = int(data.get("total") or 0)
        selected = str((self._current or {}).get("conv_id") or "")
        self.conversations.blockSignals(True)
        self.conversations.clear()
        selected_item = None
        for conv in data.get("items") or []:
            item = QListWidgetItem(f"{conv.get('name') or conv.get('conv_id')}  ({conv.get('message_count') or 0})")
            item.setData(Qt.ItemDataRole.UserRole, conv)
            self.conversations.addItem(item)
            if str(conv.get("conv_id")) == selected:
                selected_item = item
        self.conversations.blockSignals(False)
        pages = max(1, math.ceil(self._conv_total / 100))
        self.conv_position.setText(f"{self._conv_page}/{pages} 页")
        self.conv_previous.setEnabled(self._conv_page > 1)
        self.conv_next.setEnabled(self._conv_page < pages)
        if self.conversations.count():
            self.conversations.setCurrentItem(selected_item or self.conversations.item(0))
        else:
            self._current = None
            self.messages.show_rows([])

    def choose_conversation(self, item, previous=None):
        self._current = item.data(Qt.ItemDataRole.UserRole) if item else None
        self._search_mode = False
        self.messages.search.clear()
        if self._current:
            self.load_messages()

    def load_messages(self, params=None):
        if not self._current:
            return
        cid = str(self._current["conv_id"])
        active = self._active
        self.get("messages:" + active + ":" + cid, f"/api/conversations/{quote(cid, safe='')}/messages",
            lambda data: self.show_messages(data) if self._active == active and self._current and str(self._current.get("conv_id")) == cid else None,
            panel=False, params={"page_size": 100, **(params or {})})

    def media_root(self):
        from app.paths import DOUYIN_CHAT_HOME
        return (Path(DOUYIN_CHAT_HOME) / "accounts" / self._active / "chat" / "media") if self._active else Path(DOUYIN_CHAT_HOME) / "media"

    def show_messages(self, data):
        self._message_rows = data.get("items") or []
        self.messages.show_rows(self._message_rows, label=f"{len(self._message_rows)} / {data.get('total', 0)} 条",
            older=bool(data.get("has_older")), newer=bool(data.get("has_newer")), root=self.media_root())

    def search_messages(self):
        self._search_page = 1
        self._search_mode = bool(self.messages.search.text().strip())
        self.load_search() if self._search_mode else self.load_messages()

    def load_search(self):
        params = {"q": self.messages.search.text().strip(), "page": self._search_page, "page_size": 100}
        if self._current and not self.search_all.isChecked():
            params["conv_id"] = str(self._current["conv_id"])
        context = (self._active, str((self._current or {}).get("conv_id") or ""),
                   self.messages.search.text().strip(), self._search_page)
        def received(data):
            current = (self._active, str((self._current or {}).get("conv_id") or ""),
                       self.messages.search.text().strip(), self._search_page)
            if current == context and self._search_mode:
                self.show_search(data)
        self.get("search:" + repr(context), "/api/search", received, panel=False, params=params)

    def show_search(self, data):
        self._message_rows = data.get("items") or []
        pages = max(1, math.ceil(int(data.get("total") or 0) / 100))
        self.messages.show_rows(self._message_rows, label=f"{self._search_page}/{pages} 页 · {data.get('total', 0)} 条",
            older=self._search_page > 1, newer=self._search_page < pages, root=self.media_root())

    def page_messages(self, direction):
        if self._search_mode:
            self._search_page = max(1, self._search_page + direction)
            self.load_search()
        elif self._message_rows:
            seqs = [int(item["seq"]) for item in self._message_rows if item.get("seq") is not None]
            if seqs:
                self.load_messages({"before_seq": min(seqs)} if direction < 0 else {"after_seq": max(seqs)})

    def scrape(self):
        if not self.incremental.isChecked() and not confirmed(self, "全量抓取", "本次将重新抓取指定范围的历史记录，耗时可能较长。确定继续？"):
            return
        self.post("/api/scrape", {"incremental": self.incremental.isChecked(), "filter": self.filter.text().strip()})

    def export(self):
        self.post("/api/export", {"format": self.format.currentData(), "filter": self.filter.text().strip()})

    def save_schedule(self):
        self.post("/api/schedule", {"enabled": self.schedule_enabled.isChecked(), "cron": self.schedule_cron.text().strip(),
            "incremental": self.incremental.isChecked(), "conversations": [item.strip() for item in self.filter.text().split(",") if item.strip()] or None})

    def delete_conversation(self):
        if self._current and confirmed(self, "删除本机会话", "将删除该会话的本机聊天记录，平台上的聊天不会被删除。确定继续？"):
            cid = quote(str(self._current["conv_id"]), safe="")
            self.api.post(self.PREFIX + f"/api/conversations/{cid}/delete", lambda data: self.refresh())

    def open_output(self):
        from app.paths import DOUYIN_CHAT_HOME
        target = Path(DOUYIN_CHAT_HOME) / "accounts" / self._active / "exports" if self._active else Path(DOUYIN_CHAT_HOME) / "exports"
        if target.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.resolve())))
        else:
            QMessageBox.information(self, "尚无导出文件", "请先在「采集 / 导出 / 定时」中生成导出文件。")

    def import_database(self):
        aid = self.accounts.currentData() or self._active
        if not aid:
            QMessageBox.information(self, "需要账号", "请先扫码添加账号，再选择导入目标账号。")
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择 SQLite 聊天数据库", "", "SQLite 数据库 (*.db *.sqlite *.sqlite3);;所有文件 (*)")
        if not path:
            return
        choice, ok = QInputDialog.getItem(self, "选择导入方式", "目标账号：" + self.accounts.currentText(),
            ["合并（只补齐新记录，保留现有记录）", "替换（覆盖目标账号的现有数据库）"], 0, False)
        if not ok:
            return
        mode = "replace" if choice.startswith("替换") else "merge"
        prompt = ("将覆盖所选账号当前的本机聊天数据库。请确认目标账号及文件无误，确定继续？" if mode == "replace" else
                  "将把文件中的会话和消息合并进所选账号的本机数据库。确定继续？")
        if not confirmed(self, "确认数据库导入", prompt):
            return
        self.import_button.setEnabled(False)
        self.status.setText("正在后台上传并导入数据库，请保持应用运行…")
        upload(self.api, self.PANEL + "/api/database/import", path,
               params={"account_id": aid, "mode": mode}, callback=lambda data: self.database_imported(data),
               done=lambda: self.import_button.setEnabled(True))

    def database_imported(self, data):
        self.status.setText("数据库导入完成。")
        self.refresh()

    def screenshot(self):
        if not self._current:
            QMessageBox.information(self, "请先选择会话", "在聊天记录列表选择会话后，即可导出指定消息区间的 PNG 长图。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("导出聊天长图 · PNG")
        dialog.resize(560, 430)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        conversation = QComboBox()
        selected = str(self._current["conv_id"])
        for index in range(self.conversations.count()):
            data = self.conversations.item(index).data(Qt.ItemDataRole.UserRole)
            conversation.addItem(str(data.get("name") or data.get("conv_id")), str(data["conv_id"]))
        conversation.setCurrentIndex(max(0, conversation.findData(selected)))
        start, end = QSpinBox(), QSpinBox()
        for control in (start, end):
            control.setRange(0, 2147483647)
        seqs = [int(item["seq"]) for item in self._message_rows if item.get("seq") is not None]
        start.setValue(min(seqs) if seqs else 0)
        end.setValue(max(seqs) if seqs else 100)
        theme = QComboBox()
        for label, value in (("深色", "dark"), ("浅色", "light"), ("紫色", "purple")):
            theme.addItem(label, value)
        title, subtitle, self_uid = QLineEdit(self._current.get("name") or "聊天记录"), QLineEdit(), QLineEdit()
        title.setMaxLength(100)
        subtitle.setMaxLength(100)
        self_uid.setPlaceholderText("可选：填写本人 UID，以区分自己的消息气泡")
        width, scale = QSpinBox(), QSpinBox()
        width.setRange(320, 1200)
        width.setValue(520)
        scale.setRange(1, 3)
        scale.setValue(2)
        for label, widget in (("会话", conversation), ("起始消息序号（含）", start), ("结束消息序号（含）", end),
                              ("主题", theme), ("标题", title), ("副标题（可留空）", subtitle),
                              ("本人 UID", self_uid), ("宽度（逻辑像素）", width), ("清晰度倍率", scale)):
            form.addRow(label, widget)
        layout.addLayout(form)
        note = QLabel("默认填入当前消息页的序号。单张序号跨度上限 2000；图片过长时请缩小范围或降低倍率。")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if end.value() < start.value() or end.value() - start.value() > 2000:
            QMessageBox.warning(self, "区间不正确", "结束序号不得小于起始序号，且序号跨度不得超过 2000。")
            return
        file, _ = QFileDialog.getSaveFileName(self, "保存 PNG 长图", "聊天记录.png", "PNG 图片 (*.png)")
        if not file:
            return
        if not file.lower().endswith(".png"):
            file += ".png"
        self.screenshot_button.setEnabled(False)
        self.status.setText("正在后台生成并保存聊天长图…")
        params = {"start_seq": start.value(), "end_seq": end.value(), "theme": theme.currentData(),
                  "title": title.text().strip(), "subtitle": subtitle.text().strip(), "self_uid": self_uid.text().strip(),
                  "width": width.value(), "scale": scale.value()}
        download(self.api, self.PREFIX + "/api/conversations/" + quote(conversation.currentData(), safe="") + "/screenshot",
                 file, params=params, callback=lambda data: self.status.setText("长图已保存：" + data["path"]),
                 done=lambda: self.screenshot_button.setEnabled(True))

    def advanced(self):
        operations = [
            ("会话发现状态 / 列表", "GET", "/api/conversations/refresh/status", None),
            ("查看保存的会话勾选", "GET", "/api/conversations/selected", None),
            ("保存会话勾选", "POST", "/api/conversations/selected", {"section": "scraper", "conversations": []}),
            ("火花好友同步来源", "GET", "/api/spark-friends", None),
            ("图片下载设置", "GET", "/api/config/download-images", None),
            ("设置下载图片", "POST", "/api/config/download-images", {"enabled": True}),
            ("补齐图片", "POST", "/api/media/backfill", None),
            ("图片补齐状态", "GET", "/api/media/backfill/status", None),
            ("补齐视频", "POST", "/api/media/videos/backfill", None),
            ("视频补齐状态", "GET", "/api/media/videos/status", None),
            ("补齐语音转写", "POST", "/api/voice-transcriptions/backfill", {"conversations": None}),
            ("语音转写日志", "GET", "/api/voice-transcriptions/log", None),
            ("导入 Cookie", "POST", "/api/login/cookie-import", {"cookies": ""}),
            ("清除登录态", "POST", "/api/login/clear", None),
            ("设置 / 移除管理密码", "POST", "/api/password", {"password": ""}),
            ("Server酱通知配置", "POST", "/api/notify/serverchan", {"sendkey": ""}),
            ("测试通知", "POST", "/api/notify/test", None),
            ("自定义筛选", "POST", "/api/custom-filter", {"action": "add", "value": ""}),
        ]
        operations = [(label, method, "/panel" + path, body) for label, method, path, body in operations]
        operations.extend([
            ("全库消息搜索（编辑查询参数）", "GET", "/api/search", None),
            ("会话日期消息（替换会话 ID 和查询参数）", "GET", "/api/conversations/CONVERSATION_ID/messages/by-date", None),
            ("消息合并转发（替换消息 ID）", "GET", "/api/messages/MESSAGE_ID/forward", None),
            ("会话每日消息统计（替换会话 ID）", "GET", "/api/conversations/CONVERSATION_ID/stats/daily", None),
        ])
        AdvancedDialog(self.api, self.PREFIX, operations, self).exec()
