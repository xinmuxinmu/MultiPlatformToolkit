"""Native Qt pages for spark automation, StarSky and the complete service API."""
from __future__ import annotations

import json
import re
import time
from urllib.parse import quote

from PySide6.QtCore import QTimer, Qt, QTime
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSpinBox, QSplitter, QTabWidget, QTimeEdit,
    QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .widgets import JsonTree, secret


def _payload(data):
    return data.get("data", data) if isinstance(data, dict) else data


def _button(text, callback, layout, variant="secondary"):
    button = QPushButton(text)
    button.setProperty("variant", variant)
    button.clicked.connect(callback)
    layout.addWidget(button)
    return button


def _label(text, role="muted"):
    label = QLabel(text)
    label.setProperty("role", role)
    label.setWordWrap(True)
    return label


def _page_layout(widget, margin=16):
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(margin, margin, margin, margin)
    layout.setSpacing(12)
    return layout


class _Card(QFrame):
    def __init__(self, title="", parent=None):
        super().__init__(parent)
        self.setProperty("variant", "card")
        self.box = _page_layout(self, 12)
        self.box.setSpacing(8)
        if title:
            self.box.addWidget(_label(title, "sectionTitle"))


def _menu_button(layout, entries):
    button = QPushButton("更多")
    button.setProperty("menuButton", True)
    menu = QMenu(button)
    for label, callback in entries:
        if label is None:
            menu.addSeparator()
        else:
            action = menu.addAction(label)
            action.triggered.connect(callback)
    button.setMenu(menu)
    layout.addWidget(button)
    return button


def _empty_stack(widget, message):
    stack = QStackedWidget()
    stack.addWidget(widget)
    empty = QWidget()
    box = _page_layout(empty)
    box.addStretch()
    label = _label(message)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    box.addWidget(label)
    box.addStretch()
    stack.addWidget(empty)
    stack.setCurrentIndex(1)
    return stack


class _ResultView(QWidget):
    """Readable status first; the exact response remains on a secondary tab."""
    def __init__(self, empty="暂无执行记录，完成操作后可在这里查看结果。"):
        super().__init__()
        box = _page_layout(self, 0)
        self.tabs = QTabWidget()
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setPlainText(empty)
        self.raw = JsonTree()
        self.tabs.addTab(self.summary, "状态与记录")
        self.tabs.addTab(self.raw, "详细结果")
        box.addWidget(self.tabs)

    def show_data(self, data):
        self.raw.show_data(data)
        value = _payload(data)
        lines = []
        if isinstance(value, str):
            lines.append(value)
        elif isinstance(value, dict):
            if value.get("log_tail"):
                lines.extend(str(line) for line in value["log_tail"])
            else:
                for key in ("message", "msg", "note", "stage", "current", "last_line", "error"):
                    if value.get(key):
                        prefix = {"stage": "当前步骤", "current": "当前好友"}.get(key)
                        lines.append((prefix + "：" if prefix else "") + str(value[key]))
                if "running" in value:
                    lines.insert(0, "正在执行" if value["running"] else "当前没有执行中的任务")
                if "total" in value:
                    lines.append(f"已完成 {value.get('done', 0)} / {value['total']}；成功 {value.get('ok', 0)}，失败 {value.get('fail', 0)}")
                if not lines and isinstance(value.get("state"), dict):
                    lines.append("正在签到" if value["state"].get("running") else "当前未执行签到")
        self.summary.setPlainText("\n\n".join(lines) if lines else "操作已完成，可在「详细结果」查看返回内容。")


class _FriendsTable(QTreeWidget):
    def __init__(self):
        super().__init__()
        self.setHeaderLabels(["好友", "火花", "账号标识"])
        self.setColumnWidth(0, 150)
        self.setColumnWidth(1, 90)
        self.header().setStretchLastSection(True)
        self.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self._press_item = None
        self._press_check = None
        self.itemClicked.connect(self._toggle_clicked)

    def mousePressEvent(self, event):
        self._press_item = self.itemAt(event.position().toPoint()) if event.button() == Qt.MouseButton.LeftButton else None
        self._press_check = self._press_item.checkState(0) if self._press_item else None
        super().mousePressEvent(event)

    def _toggle_clicked(self, item, column):
        # The native indicator already toggles itself. Clicking the rest of a
        # row toggles only when the indicator did not, so neither path doubles.
        if item is self._press_item and item.checkState(0) == self._press_check:
            item.setCheckState(0, Qt.CheckState.Unchecked if self._press_check == Qt.CheckState.Checked else Qt.CheckState.Checked)
        self._press_item = self._press_check = None

    def checked_rows(self):
        return [self.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole)
                for index in range(self.topLevelItemCount())
                if self.topLevelItem(index).checkState(0) == Qt.CheckState.Checked]

    def choose(self, mode):
        blocked = self.blockSignals(True)
        for index in range(self.topLevelItemCount()):
            item = self.topLevelItem(index)
            checked = mode == "all" or (mode == "invert" and item.checkState(0) != Qt.CheckState.Checked)
            item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.blockSignals(blocked)

    def show_data(self, data):
        selected = {row["key"] for row in self.checked_rows()}
        blocked = self.blockSignals(True)
        self.clear()
        value = _payload(data)
        friends = value.get("list", {}) if isinstance(value, dict) else {}
        if not isinstance(friends, (dict, list)):
            friends = {}
        records = friends.items() if isinstance(friends, dict) else ((item.get("name") or item.get("username") or "", item) for item in friends if isinstance(item, dict))
        for name, detail in records:
            fire = detail[1] if isinstance(detail, list) and len(detail) > 1 else detail.get("fire", "") if isinstance(detail, dict) else ""
            uid = detail[2] if isinstance(detail, list) and len(detail) > 2 else detail.get("id", "") if isinstance(detail, dict) else ""
            item = QTreeWidgetItem([str(name), str(fire or "—"), str(uid or "—")])
            key = "uid:" + str(uid) if uid else "name:" + str(name)
            item.setData(0, Qt.ItemDataRole.UserRole, {"key": key, "name": str(name), "uid": str(uid or "")})
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if key in selected else Qt.CheckState.Unchecked)
            for column in range(3):
                item.setToolTip(column, item.text(column))
            self.addTopLevelItem(item)
        self.blockSignals(blocked)


def _confirm(parent, title, text):
    return QMessageBox.question(parent, title, text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes


class SparkPage(QWidget):
    """Account, browser login, friend and scheduled-message controls."""
    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.api = api
        self._loaded = False
        self._login_pending = False
        self._poll_at = 0.0
        self._polling = False
        self._batch_busy = False
        self._browser_busy = False
        self._send_running = False
        self._active_account_id = None
        layout = _page_layout(self)
        account_box = _Card("账号与登录")
        account_row = QHBoxLayout()
        account_row.setSpacing(8)
        self.accounts = QComboBox()
        self.accounts.setMinimumWidth(80)
        self.accounts.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.accounts.setMinimumContentsLength(10)
        account_row.addWidget(self.accounts, 1)
        self.switch_button = _button("切换账号", self.switch_account, account_row)
        _menu_button(account_row, [("刷新账号", self.refresh), ("新增账号", self.add_account),
            ("修改备注", self.rename_account), ("检查登录", self.check_login),
            ("取消登录", self.cancel_login), (None, None), ("删除账号", self.delete_account)])
        self.init_button = _button("初始化浏览器", self.initialize_browser, account_row)
        self.login_button = _button("登录账号", self.login, account_row, "primary")
        account_box.box.addLayout(account_row)
        self.status = _label("选择账号后登录，再添加每日消息任务。")
        account_box.box.addWidget(self.status)
        layout.addWidget(account_box)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        task_page = QWidget()
        task_layout = _page_layout(task_page, 12)
        task_actions = QHBoxLayout()
        self.task_count = _label("暂无任务")
        task_actions.addWidget(self.task_count)
        task_actions.addStretch()
        _menu_button(task_actions, [("停止本次发送", self.stop_sending), ("删除所选任务", self.delete_task)])
        self.run_button = _button("执行所选", self.run_task, task_actions)
        _button("添加任务", lambda: self.tabs.setCurrentWidget(self.add_page), task_actions, "primary")
        task_layout.addLayout(task_actions)
        task_selection = QHBoxLayout()
        _button("全选", lambda: self.select_tasks("all"), task_selection)
        _button("反选", lambda: self.select_tasks("invert"), task_selection)
        _button("清空", lambda: self.select_tasks("none"), task_selection)
        self.task_selected_count = _label("已选 0 项")
        task_selection.addWidget(self.task_selected_count)
        task_selection.addStretch()
        task_layout.addLayout(task_selection)
        self.tasks = QTreeWidget()
        self.tasks.setHeaderLabels(["好友", "时间", "消息", "类型", "上次结果", "下次执行"])
        self.tasks.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.tasks.itemSelectionChanged.connect(self._update_task_selection)
        self.tasks.header().setStretchLastSection(False)
        self.tasks.header().setMinimumSectionSize(50)
        for column, width in enumerate((100, 65, 140, 70, 110, 135)):
            self.tasks.setColumnWidth(column, width)
        self.tasks.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.task_stack = _empty_stack(self.tasks, "还没有定时任务\n登录账号后，点「添加任务」设置好友和发送时间。")
        task_layout.addWidget(self.task_stack, 1)
        self.tabs.addTab(task_page, "定时任务")

        self.add_page = QWidget()
        add_page_layout = _page_layout(self.add_page, 12)
        add_scroll = QScrollArea()
        add_scroll.setWidgetResizable(True)
        add_page_layout.addWidget(add_scroll, 1)
        add_content = QWidget()
        add_layout = _page_layout(add_content, 12)
        add_group = _Card("添加每日消息")
        add_form = QFormLayout()
        add_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        add_form.setSpacing(8)
        self.friend = QComboBox()
        self.friend.setEditable(True)
        self.friend.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.friend.setPlaceholderText("好友昵称")
        add_form.addRow("发送给", self.friend)
        target_row = QHBoxLayout()
        self.use_selected_friends = QCheckBox("用所选好友")
        self.use_selected_friends.toggled.connect(self._update_friend_selection)
        target_row.addWidget(self.use_selected_friends)
        self.friend_targets_summary = _label("尚未勾选好友")
        target_row.addWidget(self.friend_targets_summary, 1)
        _button("选择好友", lambda: self.tabs.setCurrentWidget(self.friend_page), target_row)
        add_form.addRow(target_row)
        self.task_time = QTimeEdit(QTime(22, 0))
        self.task_time.setDisplayFormat("HH:mm")
        add_form.addRow("每天时间", self.task_time)
        self.message = QPlainTextEdit()
        self.message.setFixedHeight(80)
        self.message.setPlaceholderText("输入要发送给好友的消息")
        add_form.addRow("消息", self.message)
        self.send_type = QComboBox()
        for label, value in (("文字", "text"), ("表情包", "sticker"), ("文字与表情包", "both")):
            self.send_type.addItem(label, value)
        add_form.addRow("发送类型", self.send_type)
        self.sticker = QLineEdit()
        self.sticker.setPlaceholderText("发送表情包时填写")
        add_form.addRow("表情地址", self.sticker)
        add_group.box.addLayout(add_form)
        add_actions = QHBoxLayout()
        add_actions.addStretch()
        self.add_button = _button("添加任务", self.add_task, add_actions, "primary")
        add_page_layout.addLayout(add_actions)
        add_layout.addWidget(add_group)
        add_layout.addStretch()
        add_scroll.setWidget(add_content)
        self.tabs.addTab(self.add_page, "添加任务")

        self.friend_page = QWidget()
        friend_layout = _page_layout(self.friend_page, 12)
        friend_actions = QHBoxLayout()
        self.friend_count = _label("尚未加载好友")
        friend_actions.addWidget(self.friend_count)
        friend_actions.addStretch()
        _menu_button(friend_actions, [("重新获取好友", lambda: self.refresh_friends(True))])
        _button("加载好友", self.refresh_friends, friend_actions)
        _button("用所选好友", self.use_friend_selection, friend_actions, "primary")
        friend_layout.addLayout(friend_actions)
        friend_selection = QHBoxLayout()
        _button("全选", lambda: self.select_friends("all"), friend_selection)
        _button("反选", lambda: self.select_friends("invert"), friend_selection)
        _button("清空", lambda: self.select_friends("none"), friend_selection)
        self.friend_selected_count = _label("已选 0 位")
        friend_selection.addWidget(self.friend_selected_count)
        friend_selection.addStretch()
        friend_layout.addLayout(friend_selection)
        self.friends = _FriendsTable()
        self.friends.itemChanged.connect(self._update_friend_selection)
        self.friend_stack = _empty_stack(self.friends, "还没有好友记录\n请先登录抖音账号，再点「加载好友」。")
        friend_layout.addWidget(self.friend_stack, 1)
        self.tabs.addTab(self.friend_page, "好友")
        self.result = _ResultView("暂无执行记录，任务运行后可在这里查看进度。")
        self.tabs.addTab(self.result, "执行记录")
        self._update_task_selection()

    def on_activate(self):
        if not self._loaded:
            self._loaded = True
            self.refresh()

    def _show(self, data, refresh=False):
        self.result.show_data(data)
        value = _payload(data)
        if isinstance(value, str):
            self.status.setText(value)
        if refresh:
            self.refresh()

    def refresh(self):
        self.api.get("/spark/Api/Account/List", self._accounts_loaded)
        self.refresh_tasks()

    def _accounts_loaded(self, data):
        value = _payload(data)
        if not isinstance(value, dict):
            self._show(data)
            return
        selected = self.accounts.currentData() or value.get("active_id")
        active_id = value.get("active_id")
        account_changed = self._active_account_id is not None and active_id != self._active_account_id
        if account_changed:
            self.tasks.clearSelection()
            self.friends.clear()
            self.friend.clear()
            self.friend_stack.setCurrentIndex(1)
            self.friend_count.setText("账号已切换，请重新加载好友")
            self._update_friend_selection()
        self._active_account_id = active_id
        self.accounts.clear()
        for account in value.get("accounts", []):
            label = account.get("name") or account.get("id", "")
            if account.get("nickname"):
                label += " · " + account["nickname"]
            if account.get("is_active"):
                label += "（当前）"
            self.accounts.addItem(label, account.get("id"))
        index = self.accounts.findData(selected)
        if index >= 0:
            self.accounts.setCurrentIndex(index)
        self._login_pending = bool(value.get("login_pending"))
        self.accounts.setEnabled(self.accounts.count() > 0)
        if not self.accounts.count():
            self.status.setText("还没有账号，从「更多 → 新增账号」开始。")
        self.accounts.setToolTip(self.accounts.currentText())
        if account_changed:
            self.refresh_tasks()

    def _require_active_account(self):
        if self._active_account_id and self.accounts.currentData() != self._active_account_id:
            QMessageBox.information(self, "切换账号", "请先切换到所选账号，再操作它的好友和任务。")
            return False
        return True

    def _set_browser_busy(self, busy):
        self._browser_busy = busy
        busy = busy or self._batch_busy or self._send_running
        for button in (self.init_button, self.login_button, self.switch_button):
            button.setEnabled(not busy)
        self.accounts.setEnabled(not busy and self.accounts.count() > 0)

    def _browser_error(self, message):
        self._login_pending = False
        self.status.setText(str(message))
        self.result.show_data({"data": str(message)})

    def initialize_browser(self):
        account_id = self._account_id()
        if not account_id or self._browser_busy or self._batch_busy or self._send_running:
            return
        self._set_browser_busy(True)
        self.status.setText("正在初始化浏览器并恢复此账号的登录态，请稍候…")
        self.api.get("/spark/Api/Init", self._browser_ready, params={"account_id": account_id},
                     error=self._browser_error, done=lambda: self._set_browser_busy(False))

    def _browser_ready(self, data):
        self._show(data)
        self.refresh()

    def _login_started(self, data):
        self._show(data)
        self.check_login()
        self.api.get("/spark/Api/Account/List", self._accounts_loaded)

    def _account_id(self):
        account_id = self.accounts.currentData()
        if not account_id:
            QMessageBox.information(self, "账号", "请先新增或选择账号。")
        return account_id

    def add_account(self):
        name, accepted = QInputDialog.getText(self, "新增账号", "账号备注名")
        if accepted and name.strip():
            self.api.post("/spark/Api/Account/Add", lambda data: self._show(data, True), body={"name": name.strip()})

    def rename_account(self):
        account_id = self._account_id()
        if not account_id:
            return
        name, accepted = QInputDialog.getText(self, "修改账号备注", "新备注名")
        if accepted and name.strip():
            self.api.get("/spark/Api/Account/Rename", lambda data: self._show(data, True), params={"account_id": account_id, "name": name.strip()})

    def delete_account(self):
        account_id = self._account_id()
        if account_id and _confirm(self, "删除账号", "删除所选账号及其 Cookie、定时任务、分组、好友缓存和浏览器数据？此操作无法撤销。"):
            self.api.get("/spark/Api/Account/Remove", lambda data: self._show(data, True), params={"account_id": account_id})

    def switch_account(self):
        account_id = self._account_id()
        if account_id and not self._browser_busy and not self._batch_busy and not self._send_running:
            self._set_browser_busy(True)
            self.status.setText("正在切换账号并初始化浏览器，请稍候…")
            self.api.get("/spark/Api/Account/Switch", self._browser_ready, params={"account_id": account_id},
                         error=self._browser_error, done=lambda: self._set_browser_busy(False))

    def login(self):
        account_id = self._account_id()
        if account_id and not self._browser_busy and not self._batch_busy and not self._send_running:
            self._set_browser_busy(True)
            self._login_pending = True
            self.status.setText("正在打开登录浏览器，请在窗口中完成登录。")
            self.api.post("/spark/Api/Account/BrowserLogin", self._login_started, body={"account_id": account_id},
                          error=self._browser_error, done=lambda: self._set_browser_busy(False))

    def cancel_login(self):
        self._login_pending = False
        self.api.post("/spark/Api/Account/CancelLogin", self._show)

    def check_login(self):
        self.api.get("/spark/Api/Account/LoginState", self._login_loaded)

    def _login_loaded(self, data):
        value = _payload(data)
        if isinstance(value, dict):
            self._login_pending = value.get("status") == "waiting"
            if value.get("logged_in"):
                self.status.setText("抖音账号已登录，登录态已保存。")
            elif value.get("message"):
                self.status.setText(str(value["message"]))
            elif self._login_pending:
                self.status.setText("浏览器已打开，请在窗口中完成登录。")
        self.result.show_data(data)

    def refresh_tasks(self):
        self.api.get("/spark/Time/getlist", self._tasks_loaded)

    def _tasks_loaded(self, data):
        value = _payload(data)
        if not isinstance(value, dict):
            return
        selected_ids = set(self._selected_tasks(False))
        blocked = self.tasks.blockSignals(True)
        self.tasks.clear()
        for task in value.get("tasks", []):
            item = QTreeWidgetItem([str(task.get(key) or "") for key in
                                  ("name", "time", "msg", "send_type", "last_result", "next_run")])
            item.setText(3, {"text": "文字", "sticker": "表情包", "both": "文字+表情"}.get(task.get("send_type"), "文字"))
            item.setData(0, Qt.ItemDataRole.UserRole, task.get("task_id"))
            self.tasks.addTopLevelItem(item)
            for column in range(6):
                item.setToolTip(column, item.text(column))
            if task.get("task_id") in selected_ids:
                item.setSelected(True)
        self.tasks.blockSignals(blocked)
        count = self.tasks.topLevelItemCount()
        self.task_stack.setCurrentIndex(0 if count else 1)
        self.task_count.setText(f"共 {count} 个每日任务" if count else "暂无任务")
        self._update_task_selection()

    def _selected_tasks(self, notify=True):
        selected = [self.tasks.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole)
                    for index in range(self.tasks.topLevelItemCount())
                    if self.tasks.topLevelItem(index).isSelected()]
        selected = [task_id for task_id in selected if task_id]
        if not selected and notify:
            QMessageBox.information(self, "定时任务", "请先选择一个或多个任务。")
        return selected

    def _update_task_selection(self):
        count = len(self._selected_tasks(False))
        self.task_selected_count.setText(f"已选 {count} 项")
        self.run_button.setEnabled(bool(count) and not self._batch_busy and not self._send_running)

    def select_tasks(self, mode):
        for index in range(self.tasks.topLevelItemCount()):
            item = self.tasks.topLevelItem(index)
            item.setSelected(mode == "all" or (mode == "invert" and not item.isSelected()))
        self._update_task_selection()

    def select_friends(self, mode):
        self.friends.choose(mode)
        self._update_friend_selection()

    def _update_friend_selection(self, *_args):
        count = len(self.friends.checked_rows())
        self.friend_selected_count.setText(f"已选 {count} 位")
        self.friend_targets_summary.setText(f"已勾选 {count} 位好友" if count else "尚未勾选好友")
        self.friend.setEnabled(not self.use_selected_friends.isChecked())

    def use_friend_selection(self):
        if not self.friends.checked_rows():
            QMessageBox.information(self, "好友", "请先勾选一个或多个好友。")
            return
        self.use_selected_friends.setChecked(True)
        self.tabs.setCurrentWidget(self.add_page)

    def _mutate_each(self, path, entries, action):
        """Keep browser mutations sequential and report partial failures."""
        self._batch_busy = True
        self._set_browser_busy(self._browser_busy)
        self.add_button.setEnabled(False)
        self._update_task_selection()
        pending = list(entries)
        results = []

        def step():
            if not pending:
                self._batch_busy = False
                self._set_browser_busy(self._browser_busy)
                self.add_button.setEnabled(True)
                self._update_task_selection()
                ok = sum(result["ok"] for result in results)
                summary = f"{action}完成：成功 {ok} 项，失败 {len(results) - ok} 项"
                self.status.setText(summary)
                self.result.show_data({"data": {"message": summary, "results": results}})
                self.refresh_tasks()
                return
            label, params = pending.pop(0)
            self.status.setText(f"正在{action}：{label}（剩余 {len(pending)} 项）")

            def finish(ok, detail):
                results.append({"name": label, "ok": ok, "message": str(detail)})
                QTimer.singleShot(0, step)

            self.api.get(path, lambda data: finish(True, _payload(data)), params=params,
                         quiet=True, error=lambda message: finish(False, message))
        step()

    def run_task(self):
        if self._batch_busy or self._send_running or not self._require_active_account():
            return
        task_ids = self._selected_tasks()
        if task_ids and _confirm(self, "立即发送", f"立即执行所选 {len(task_ids)} 项任务并向好友发送消息？"):
            self._send_running = True
            self._set_browser_busy(self._browser_busy)
            self._update_task_selection()
            self.api.post("/spark/Time/runBatch", self._batch_started, body={"task_ids": task_ids}, error=self._send_error)

    def _send_error(self, message):
        self._send_running = False
        self._set_browser_busy(self._browser_busy)
        self._update_task_selection()
        self._show({"data": str(message)})

    def _batch_started(self, data):
        self._send_running = True
        self._set_browser_busy(self._browser_busy)
        self._update_task_selection()
        self._show(data)
        self.tabs.setCurrentWidget(self.result)

    def stop_sending(self):
        self.api.post("/spark/Time/batchCancel", self._show)

    def _progress_loaded(self, data):
        value = _payload(data)
        if isinstance(value, dict):
            was_running = self._send_running
            self._send_running = bool(value.get("running"))
            self._set_browser_busy(self._browser_busy)
            self._update_task_selection()
            if was_running and not self._send_running:
                self.refresh_tasks()
        self.result.show_data(data)

    def delete_task(self):
        if self._batch_busy or self._send_running or not self._require_active_account():
            return
        task_ids = self._selected_tasks()
        if task_ids and _confirm(self, "删除定时任务", f"删除所选 {len(task_ids)} 项定时消息任务？"):
            self._mutate_each("/spark/Time/del", [(task_id, {"task_id": task_id}) for task_id in task_ids], "删除任务")

    def add_task(self):
        if self._batch_busy or self._send_running or not self._require_active_account():
            return
        name = self.friend.currentText().strip()
        targets = [row["name"] for row in self.friends.checked_rows()] if self.use_selected_friends.isChecked() else [name]
        targets = list(dict.fromkeys(target for target in targets if target))
        message = self.message.toPlainText()
        kind = self.send_type.currentData()
        sticker = self.sticker.text().strip()
        if not targets or (kind in ("text", "both") and not message.strip()):
            QMessageBox.information(self, "添加任务", "请填写好友与消息正文。")
            return
        if kind in ("sticker", "both") and not sticker:
            QMessageBox.information(self, "添加任务", "请填写表情包地址。")
            return
        target_label = f"「{targets[0]}」" if len(targets) == 1 else f"所选 {len(targets)} 位好友"
        if not _confirm(self, "添加每日消息", f"每天 {self.task_time.time().toString('HH:mm')} 向{target_label}发送所填内容，确定添加？"):
            return
        params = {"name": name, "time": self.task_time.time().toString("HH:mm"), "text": message, "send_type": kind}
        if sticker:
            params["sticker_src"] = sticker
        self._mutate_each("/spark/Time/add", [(target, dict(params, name=target)) for target in targets], "添加任务")

    def refresh_friends(self, force=False):
        if not self._require_active_account():
            return
        self.api.get("/spark/Api/GetFriendsList", self._friends_loaded, params={"force": 1 if force else 0})

    def _friends_loaded(self, data):
        self.friends.show_data(data)
        count = self.friends.topLevelItemCount()
        self.friend_stack.setCurrentIndex(0 if count else 1)
        self.friend_count.setText(f"共 {count} 位好友" if count else "暂无好友，请确认已登录后重试。")
        value = _payload(data)
        friends = value.get("list", {}) if isinstance(value, dict) else {}
        current = self.friend.currentText()
        self.friend.clear()
        if isinstance(friends, dict):
            self.friend.addItems(list(friends))
        elif isinstance(friends, list):
            self.friend.addItems([str(item.get("name") or item.get("username") or "") for item in friends if isinstance(item, dict)])
        self.friend.setCurrentText(current)
        self._update_friend_selection()

    def poll(self):
        if not self._loaded or self._polling or time.monotonic() - self._poll_at < 4:
            return
        self._poll_at = time.monotonic()
        self._polling = True
        # 这里只读任务进度；不刷新好友，不启动浏览器，不触发发送。
        self.api.get("/spark/Time/progress", self._progress_loaded, quiet=True,
                     done=lambda: setattr(self, "_polling", False))
        if self._login_pending:
            self.api.get("/spark/Api/Account/LoginState", self._login_loaded, quiet=True)


class StarskyPage(QWidget):
    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.api = api
        self._loaded = False
        self._running = False
        self._polling = False
        self._poll_at = 0.0
        layout = _page_layout(self, 0)
        tabs = QTabWidget()
        layout.addWidget(tabs)
        overview = QWidget()
        overview_layout = _page_layout(overview, 12)
        status_card = _Card("签到状态")
        self.status = _label("尚未读取状态，点「刷新状态」检查。")
        status_card.box.addWidget(self.status)
        actions = QHBoxLayout()
        actions.addStretch()
        _menu_button(actions, [("停止签到", self.stop)])
        _button("刷新状态", self.refresh, actions)
        _button("立即签到", self.run, actions, "primary")
        status_card.box.addLayout(actions)
        overview_layout.addWidget(status_card)
        schedule_box = _Card("每日自动签到")
        self.schedule_status = _label("暂无计划任务，设置时间后点「保存时间」。")
        schedule_box.box.addWidget(self.schedule_status)
        schedule_row = QHBoxLayout()
        self.schedule_time = QTimeEdit(QTime(9, 30))
        self.schedule_time.setDisplayFormat("HH:mm")
        schedule_row.addWidget(self.schedule_time)
        schedule_row.addStretch()
        _menu_button(schedule_row, [("删除计划任务", self.delete_schedule)])
        _button("保存时间", self.create_schedule, schedule_row, "primary")
        schedule_box.box.addLayout(schedule_row)
        overview_layout.addWidget(schedule_box)
        self.result = _ResultView("暂无签到日志，执行签到后可在这里查看结果。")
        overview_layout.addWidget(self.result, 1)
        tabs.addTab(overview, "签到")

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings = QWidget()
        settings_layout = _page_layout(settings, 12)
        config_box = _Card("运行设置")
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setSpacing(8)
        self.executable = QLineEdit()
        self.executable.setPlaceholderText("StarSky.exe 完整路径，留空自动查找")
        form.addRow("软件路径", self.executable)
        self.auto_start = QCheckBox("未运行时自动打开 StarSky")
        self.auto_start.setChecked(True)
        form.addRow(self.auto_start)
        self.wait = QSpinBox()
        self.wait.setRange(5, 300)
        self.wait.setSuffix(" 秒")
        self.wait.setValue(30)
        form.addRow("等待时间", self.wait)
        self.close_after = QCheckBox("签到成功后关闭软件")
        self.close_after.setChecked(True)
        form.addRow(self.close_after)
        config_box.box.addLayout(form)
        save_row = QHBoxLayout()
        save_row.addStretch()
        _button("保存设置", self.save, save_row, "primary")
        config_box.box.addLayout(save_row)
        settings_layout.addWidget(config_box)
        settings_layout.addWidget(_label("签到时请保持桌面解锁，StarSky 窗口不要被遮挡。"))
        settings_layout.addStretch()
        settings_scroll.setWidget(settings)
        tabs.addTab(settings_scroll, "设置")

    def on_activate(self):
        if not self._loaded:
            self._loaded = True
            self.refresh()

    def refresh(self):
        self.api.get("/starsky/api/status", lambda data: self._status_loaded(data, True))

    def _status_loaded(self, data, load_config=False):
        state = data.get("state", {})
        self._running = bool(state.get("running"))
        task = data.get("task", {})
        label = "正在签到，请保持 StarSky 窗口可见。" if self._running else "当前没有执行中的签到。"
        if state.get("exit_code") is not None:
            label += " 上次已完成。" if state["exit_code"] == 0 else " 上次未能确认成功，请查看记录。"
        if task.get("legacy"):
            label += " 旧版计划任务需要重新保存时间。"
        if data.get("last_line"):
            label += "\n" + str(data["last_line"])
        self.status.setText(label)
        self.schedule_status.setText("已设置每日计划任务。" if task.get("exists") else "暂无计划任务，设置时间后点「保存时间」。")
        self.result.show_data(data)
        if load_config:
            config = data.get("config", {})
            self.executable.setText(str(config.get("软件路径", "")))
            self.auto_start.setChecked(str(config.get("自动启动", "1")) == "1")
            self.close_after.setChecked(str(config.get("签到后关闭软件", "1")) == "1")
            try:
                self.wait.setValue(int(config.get("等待超时", 30)))
            except (TypeError, ValueError):
                self.wait.setValue(30)

    def _done(self, data):
        self.result.show_data(data)
        self.api.get("/starsky/api/status", self._status_loaded)

    def save(self):
        config = {"软件路径": self.executable.text().strip(),
                  "自动启动": "1" if self.auto_start.isChecked() else "0",
                  "签到后关闭软件": "1" if self.close_after.isChecked() else "0",
                  "等待超时": str(self.wait.value())}
        self.api.post("/starsky/api/config", self._done, body=config)

    def run(self):
        if _confirm(self, "执行 StarSky 签到", "立即打开或切到 StarSky 并点击签到？执行期间会移动鼠标，系统可能请求管理员授权。"):
            self.api.post("/starsky/api/run", self._done)

    def stop(self):
        self.api.post("/starsky/api/stop", self._done)

    def create_schedule(self):
        self.api.post("/starsky/api/schedule", self._done,
                      body={"time": self.schedule_time.time().toString("HH:mm")})

    def delete_schedule(self):
        if _confirm(self, "删除计划任务", "删除 StarSky 的 Windows 每日签到计划任务？"):
            self.api.call("DELETE", "/starsky/api/schedule", self._done)

    def poll(self):
        if not self._loaded or not self._running or self._polling or time.monotonic() - self._poll_at < 4:
            return
        self._poll_at = time.monotonic()
        self._polling = True
        self.api.get("/starsky/api/status", self._status_loaded, quiet=True,
                     done=lambda: setattr(self, "_polling", False))


def _schema_type(schema):
    if not isinstance(schema, dict):
        return "string"
    if "anyOf" in schema or "oneOf" in schema:
        candidates = schema.get("anyOf", schema.get("oneOf", []))
        return next((_schema_type(candidate) for candidate in candidates if candidate.get("type") != "null"), "string")
    kind = schema.get("type", "object" if "properties" in schema else "string")
    return next((item for item in kind if item != "null"), "string") if isinstance(kind, list) else kind


def schema_example(schema, depth=0):
    """Build an editable starting value, without inventing optional settings."""
    if not isinstance(schema, dict) or depth > 12:
        return None
    for key in ("example", "default", "const"):
        if key in schema:
            return schema[key]
    if schema.get("examples"):
        return schema["examples"][0]
    if schema.get("enum"):
        return schema["enum"][0]
    for union in ("anyOf", "oneOf", "allOf"):
        if schema.get(union):
            return schema_example(next((part for part in schema[union] if part.get("type") != "null"), schema[union][0]), depth + 1)
    kind = _schema_type(schema)
    if kind == "object":
        required = set(schema.get("required", []))
        return {key: schema_example(value, depth + 1) for key, value in schema.get("properties", {}).items()
                if key in required or "default" in value}
    if kind == "array":
        count = min(int(schema.get("minItems", 0)), 5)
        return [schema_example(schema.get("items", {}), depth + 1) for _ in range(count)]
    if kind == "boolean":
        return False
    if kind in ("integer", "number"):
        return schema.get("minimum", 0)
    return ""


def operation_is_delete(operation):
    if operation.get("method", "").upper() == "DELETE":
        return True
    parts = operation.get("path", "").lower().split("/")
    return any(part in ("del", "delete", "remove", "clear", "purge", "reset") or
               part.startswith(("delete-", "remove-", "clear-")) for part in parts)


class OperationsPage(QWidget):
    """Schema-driven native UI retaining every mounted Python service operation."""
    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.api = api
        self.operations = []
        self.parameter_controls = []
        self.operation = None
        self._loaded = False
        self._busy = False
        layout = _page_layout(self)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索接口、模块或功能")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_operations)
        toolbar.addWidget(self.search, 1)
        self.auth_toggle = QPushButton("鉴权")
        self.auth_toggle.setCheckable(True)
        toolbar.addWidget(self.auth_toggle)
        _button("刷新目录", self.refresh, toolbar)
        layout.addLayout(toolbar)
        self.auth_card = _Card()
        token_row = QHBoxLayout()
        token_row.setSpacing(8)
        token_row.addWidget(_label("访问令牌"))
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("服务启用鉴权时填写")
        self.token.setText(str(api.headers.get("Authorization", "")).removeprefix("Bearer "))
        token_row.addWidget(self.token, 1)
        _button("应用令牌", self.apply_token, token_row)
        self.auth_card.box.addLayout(token_row)
        self.auth_card.hide()
        self.auth_toggle.toggled.connect(self.auth_card.setVisible)
        layout.addWidget(self.auth_card)
        splitter = QSplitter()
        self.operation_list = QListWidget()
        self.operation_list.setMinimumWidth(100)
        self.operation_list.setWordWrap(False)
        self.operation_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.operation_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.operation_list.currentItemChanged.connect(self._select_operation)
        self.catalog_stack = _empty_stack(self.operation_list, "接口目录尚未加载\n点「刷新目录」读取可用操作。")
        splitter.addWidget(self.catalog_stack)
        editor = _Card()
        editor_layout = editor.box
        self.heading = _label("选择一个接口", "sectionTitle")
        editor_layout.addWidget(self.heading)
        self.description = _label("从左侧选择操作，再填写参数。")
        self.description.setTextFormat(Qt.TextFormat.PlainText)
        editor_layout.addWidget(self.description)
        request_tabs = QTabWidget()
        request_scroll = QScrollArea()
        request_scroll.setWidgetResizable(True)
        request_widget = QWidget()
        request_layout = _page_layout(request_widget, 8)
        self.parameter_area = QScrollArea()
        self.parameter_area.setWidgetResizable(True)
        self.parameter_area.setMinimumHeight(48)
        request_layout.addWidget(self.parameter_area, 1)
        self.body_label = _label("请求正文", "sectionTitle")
        request_layout.addWidget(self.body_label)
        self.body = QPlainTextEdit()
        self.body.setPlaceholderText("需要正文时填写 JSON，无需正文则留空")
        self.body.setMinimumHeight(80)
        request_layout.addWidget(self.body, 1)
        request_scroll.setWidget(request_widget)
        request_tabs.addTab(request_scroll, "填写参数")
        self.result = JsonTree()
        self.result.show_data({"提示": "尚未执行操作，请先选择接口并填写参数。"})
        self.schema_tree = JsonTree()
        request_tabs.addTab(self.result, "执行结果")
        request_tabs.addTab(self.schema_tree, "参数说明")
        editor_layout.addWidget(request_tabs, 1)
        run_row = QHBoxLayout()
        run_row.addStretch()
        self.execute_button = _button("执行操作", self.execute, run_row, "primary")
        self.execute_button.setEnabled(False)
        editor_layout.addLayout(run_row)
        splitter.addWidget(editor)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 5)
        layout.addWidget(splitter, 1)

    def on_activate(self):
        if not self._loaded:
            self._loaded = True
            self.refresh()

    def poll(self):
        pass  # 全部操作由用户主动执行。

    def refresh(self):
        self.api.get("/api/desktop/catalog", self._catalog_loaded)

    def _catalog_loaded(self, data):
        self.operations = data.get("operations", []) if isinstance(data, dict) else []
        self.filter_operations()

    def filter_operations(self):
        query = self.search.text().strip().casefold()
        self.operation_list.clear()
        for operation in self.operations:
            method, path = operation.get("method", "GET").upper(), operation.get("path", "")
            summary = operation.get("summary", "")
            label = f"{method} {path}\n{summary}"
            if query and query not in label.casefold():
                continue
            item = QListWidgetItem(f"{summary or path}\n{method} {path}")
            item.setToolTip(label)
            item.setData(Qt.ItemDataRole.UserRole, operation)
            self.operation_list.addItem(item)
        self.catalog_stack.setCurrentIndex(0 if self.operation_list.count() else 1)

    def apply_token(self):
        value = self.token.text().strip()
        if value:
            self.api.headers["Authorization"] = value if value.lower().startswith("bearer ") else "Bearer " + value
        else:
            self.api.headers.pop("Authorization", None)

    def _select_operation(self, item, previous=None):
        self.operation = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.parameter_controls = []
        content = QWidget()
        form = QFormLayout(content)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        old = self.parameter_area.takeWidget()
        if old:
            old.deleteLater()
        self.parameter_area.setWidget(content)
        self.execute_button.setEnabled(bool(item) and not self._busy)
        if not item:
            self.heading.setText("选择一个接口")
            self.description.clear()
            self.body.clear()
            return
        operation = self.operation
        self.schema_tree.show_data({"parameters": operation.get("parameters", []),
                                   "requestBody": operation.get("requestBody", {})})
        self.heading.setText(f"{operation.get('method', 'GET').upper()} {operation.get('path', '')}")
        self.description.setText(str(operation.get("summary") or operation.get("description") or ""))
        for parameter in operation.get("parameters", []):
            location = parameter.get("in", "query")
            if location not in ("path", "query", "header", "cookie"):
                continue
            schema = parameter.get("schema", {})
            label = f"{parameter.get('name', '')} · {location}" + (" *" if parameter.get("required") else "")
            kind = _schema_type(schema)
            choices = schema.get("enum")
            if kind == "boolean" or choices:
                control = QComboBox()
                if not parameter.get("required"):
                    control.addItem("（不传）", None)
                for choice in choices or [True, False]:
                    control.addItem(json.dumps(choice, ensure_ascii=False), choice)
                if "default" in schema:
                    index = control.findData(schema["default"])
                    if index >= 0:
                        control.setCurrentIndex(index)
            else:
                control = QLineEdit()
                if "default" in schema and schema["default"] is not None:
                    value = schema["default"]
                    control.setText(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))
                if secret(parameter.get("name", "")):
                    control.setEchoMode(QLineEdit.EchoMode.Password)
                if kind in ("array", "object"):
                    control.setPlaceholderText("JSON，例如 [1, 2] 或 {\"key\": \"value\"}")
            control.setObjectName("parameter_" + location + "_" + parameter.get("name", ""))
            control.setToolTip(str(parameter.get("description") or schema.get("description") or ""))
            form.addRow(label, control)
            self.parameter_controls.append((parameter, control))
        self.parameter_area.setVisible(bool(self.parameter_controls))
        request_body = operation.get("requestBody", {}) or {}
        media_types = request_body.get("content", {})
        json_media = media_types.get("application/json") or next((value for key, value in media_types.items() if key.endswith("+json")), {})
        schema = json_media.get("schema", {})
        self.body.setEnabled(bool(json_media) or not media_types)
        if json_media:
            self.body.show()
            example = json_media.get("example", schema_example(schema))
            self.body.setPlainText(json.dumps(example, ensure_ascii=False, indent=2))
            self.body_label.setText("请求正文（JSON，可编辑）" + (" *" if request_body.get("required") else ""))
        else:
            self.body.clear()
            self.body.hide()
            self.body_label.setText("此接口无需 JSON 正文" if not media_types else "此接口要求 " + ", ".join(media_types) + "；请使用对应的导入/文件功能")
            if not media_types and not self.parameter_controls:
                self.body_label.setText("无需填写参数，可直接执行。")
        self.execute_button.setText("执行操作")

    def build_request(self):
        if not self.operation:
            raise ValueError("请先选择接口")
        path = self.operation.get("path", "")
        params, headers, cookies = {}, {}, {}
        for parameter, control in self.parameter_controls:
            schema = parameter.get("schema", {})
            kind = _schema_type(schema)
            if isinstance(control, QComboBox):
                value = control.currentData()
                missing = value is None
            else:
                value = control.text()
                missing = value == ""
                if not missing and kind in ("integer", "number", "array", "object"):
                    try:
                        value = json.loads(value)
                    except ValueError as exc:
                        raise ValueError(f"{parameter['name']} 需要有效的 {kind} 值") from exc
                    if kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
                        raise ValueError(f"{parameter['name']} 需要整数")
                    if kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
                        raise ValueError(f"{parameter['name']} 需要数字")
                    if kind == "array" and not isinstance(value, list):
                        raise ValueError(f"{parameter['name']} 需要 JSON 列表")
                    if kind == "object" and not isinstance(value, dict):
                        raise ValueError(f"{parameter['name']} 需要 JSON 对象")
            if missing:
                if parameter.get("required") or parameter.get("in") == "path":
                    raise ValueError(f"请填写必填参数 {parameter.get('name')}")
                continue
            location, name = parameter.get("in", "query"), parameter["name"]
            if location == "path":
                path = path.replace("{" + name + "}", quote(str(value), safe=""))
            elif location == "query":
                # httpx 支持列表型 query，object 按 JSON 提交。
                params[name] = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value
            elif location == "header":
                headers[name] = str(value)
            elif location == "cookie":
                cookies[name] = str(value)
        if re.search(r"\{[^}]+\}", path):
            raise ValueError("路径参数没有填写完整")
        if cookies:
            headers["Cookie"] = "; ".join(f"{name}={quote(value, safe='')}" for name, value in cookies.items())
        body = None
        text = self.body.toPlainText().strip()
        if text:
            try:
                body = json.loads(text)
            except ValueError as exc:
                raise ValueError("请求正文不是有效 JSON") from exc
        elif (self.operation.get("requestBody") or {}).get("required"):
            raise ValueError("请填写请求正文")
        return self.operation.get("method", "GET").upper(), path, params, body, headers

    def execute(self):
        try:
            method, path, params, body, headers = self.build_request()
        except ValueError as exc:
            QMessageBox.warning(self, "参数需要检查", str(exc))
            return
        if operation_is_delete(self.operation) and not _confirm(self, "确认删除操作", f"执行 {method} {path}？该操作可能删除数据，无法撤销。"):
            return
        self.apply_token()
        original_headers = dict(self.api.headers)
        self.api.headers.update(headers)
        self._busy = True
        self.execute_button.setEnabled(False)
        self.result.show_data({"状态": "请求执行中", "接口": f"{method} {path}"})
        try:
            # Api.call 在调用时复制 headers；自定义请求头不会残留到其他模块。
            self.api.call(method, path, self.result.show_data, params=params, body=body, done=self._finished)
        finally:
            self.api.headers.clear()
            self.api.headers.update(original_headers)

    def _finished(self):
        self._busy = False
        self.execute_button.setEnabled(self.operation is not None)
