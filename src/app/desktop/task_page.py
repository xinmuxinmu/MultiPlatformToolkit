"""Native task pages preserving the original overview → settings workflow."""
from __future__ import annotations

from copy import deepcopy
import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from .widgets import JsonTree, secret
from .task_components import (
    Card, Fold, KugouLoginDialog, MissionView, NAMES, TaskPicker,
    TaskSettingsEditor, label, overview_data,
)


class TaskPage(QWidget):
    def __init__(self, api, prefix, title, parent=None):
        super().__init__(parent)
        self.api, self.prefix, self.title = api, prefix, title
        self.initialized = self.busy = self.config_loaded = False
        self.seq, self._revision = 0, 0
        self.state, self._summary = {}, {}
        self._dirty = self._was_running = False
        self._login_pending = False
        self._dialogs = []
        self.setProperty("pageSurface", True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.status = label("读取本地设置后即可使用。", muted=True)
        toolbar.addWidget(self.status, 1)
        self.refresh_button = self.add_button(toolbar, "刷新", self.refresh_platform)
        self.save_button = self.add_button(toolbar, "保存", self.save)
        self.run_button = self.add_button(toolbar, "执行任务", self.run)
        self.run_button.setProperty("variant", "primary")
        self.run_button.setEnabled(False)
        layout.addLayout(toolbar)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("taskTabs")
        layout.addWidget(self.tabs, 1)

        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        overview_layout.setContentsMargins(0, 12, 0, 0)
        overview_layout.setSpacing(12)
        self.account_card = Card()
        self.account_name = label("账号状态", section=True)
        self.account_login = label("读取中…", muted=True)
        self.account_detail = label("", muted=True)
        self.metrics = label("")
        self.account_card.content.addWidget(self.account_name)
        self.account_card.content.addWidget(self.account_login)
        self.account_card.content.addWidget(self.account_detail)
        self.account_card.content.addWidget(self.metrics)
        overview_layout.addWidget(self.account_card)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(12)
        self.mission_card = Card("今日任务")
        self.missions = MissionView()
        self.mission_card.content.addWidget(self.missions, 1)
        self.overview_notice = label("状态以最近一次平台返回为准。", muted=True)
        self.mission_card.content.addWidget(self.overview_notice)
        log_card = Card("运行日志")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("执行任务后，过程和结果会显示在这里。")
        self.log.document().setMaximumBlockCount(5000)
        self.log.setMinimumWidth(230)
        log_card.content.addWidget(self.log, 1)
        split.addWidget(self.mission_card)
        split.addWidget(log_card)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([580, 360])
        overview_layout.addWidget(split, 1)
        self.tabs.addTab(overview, "概览")

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        setting_page = QWidget()
        settings_layout = QVBoxLayout(setting_page)
        settings_layout.setContentsMargins(0, 12, 0, 0)
        settings_layout.setSpacing(12)
        self.account_settings = Card("账号与登录")
        self.account_settings.content.addWidget(label(self._login_hint(), muted=True))
        self.heybox_login_form = None
        if prefix == "/heybox":
            from .heybox_login import HeyboxLoginWidget
            self.heybox_login_form = HeyboxLoginWidget(api, self)
            self.heybox_login_form.message.connect(self.status.setText)
            self.heybox_login_form.saved.connect(lambda: self.refresh(merge_login=True))
            self.account_settings.content.addWidget(self.heybox_login_form)
        login_actions = QHBoxLayout()
        login_actions.setSpacing(8)
        if prefix in ("/qq", "/netease-fans", "/kugou"):
            if prefix == "/kugou":
                self.add_button(login_actions, "App 扫码登录", self.kugou_login)
                self.add_button(login_actions, "勋章网页登录", lambda: self.action("login"))
            else:
                self.add_button(login_actions, "快捷登录", lambda: self.action("login"))
            self.add_button(login_actions, "获取登录态", lambda: self.action("login-grab", reload=True))
        elif prefix == "/signin":
            self.add_button(login_actions, "微博扫码登录", lambda: self.action("weibo-qr-login"))
            self.add_button(login_actions, "吾爱网页登录", lambda: self.action("pojie-login"))
        elif prefix == "/ai-checkin":
            self.add_button(login_actions, "预览状态", lambda: self.run(dry=True))
            self.add_button(login_actions, "续期登录态", lambda: self.action("keepalive"))
        login_actions.addStretch()
        if prefix != "/heybox":
            self.account_settings.content.addLayout(login_actions)
        self.target_checks = {}
        if prefix == "/signin":
            choices = QHBoxLayout()
            for key in ("weibo", "pojie", "tieba"):
                check = QCheckBox(NAMES[key])
                check.setChecked(True)
                self.target_checks[key] = check
                choices.addWidget(check)
            choices.addStretch()
            self.account_settings.content.addLayout(choices)
        settings_layout.addWidget(self.account_settings)
        self.settings = TaskSettingsEditor(prefix)
        self.settings.changed.connect(self._mark_dirty)
        settings_layout.addWidget(self.settings)
        self.selection = None
        if prefix == "/qq":
            from .qq_selection import QQSelection
            box = Card("要处理的勋章与任务")
            self.selection = QQSelection()
            self.selection.changed.connect(self._mark_dirty)
            box.content.addWidget(self.selection)
            settings_layout.addWidget(box)
        self.task_picker = TaskPicker(prefix) if prefix in ("/netease-fans", "/kugou") else None
        if self.task_picker:
            self.task_picker.changed.connect(self._mark_dirty)
            settings_layout.addWidget(self.task_picker)
        self.result = JsonTree()
        self.result.setMinimumHeight(180)
        details = Card()
        details.content.addWidget(Fold("查看详细响应", self.result))
        settings_layout.addWidget(details)
        settings_layout.addStretch()
        scroll.setWidget(setting_page)
        self.tabs.addTab(scroll, "设置")
        self.tabs.setCurrentIndex(0)

    @staticmethod
    def add_button(layout, text, callback):
        button = QPushButton(text)
        button.setProperty("variant", "secondary")
        button.clicked.connect(lambda checked=False: callback())
        layout.addWidget(button)
        return button

    def _login_hint(self):
        if self.prefix == "/ai-checkin": return "先在对应桌面客户端登录；程序读取本机登录态。可选凭据在下方收起的账号设置中。"
        if self.prefix == "/bili-vip": return "默认复用 B站聊天账号。请先在 B站聊天页登录，或切换为手动 Cookie。"
        if self.prefix == "/kugou": return "App 扫码用于听歌与广告任务；勋章需另存网页登录态。两种登录态分别保留。"
        if self.prefix == "/signin": return "选择本次要签到的平台。微博和吾爱支持浏览器登录，贴吧可在下方手动填写 Cookie。"
        if self.prefix == "/heybox": return "直接输入手机号接收短信并登录。仅遇到人机验证时打开验证页，通过后自动继续。"
        return "先在浏览器完成登录，再点“获取登录态”。手动 Cookie 输入默认收起。"

    def _mark_dirty(self, *args):
        self._dirty = True
        self._revision += 1
        self.status.setText("有未保存的设置。")
        if self.task_picker:
            try:
                current = self.settings.value()
                self.task_picker.update_mode(current)
                if not self.task_picker._dirty: self.task_picker.set_state(self.state, current)
            except (ValueError, KeyError): pass

    def on_activate(self):
        self.initialized = True
        self.refresh()

    def refresh(self, *, merge_login=False):
        self.api.get(self.prefix + "/api/state", lambda state: self.loaded(state, merge_login=merge_login))

    def refresh_platform(self):
        if not self.config_loaded or self.prefix in ("/signin", "/ai-checkin"):
            self.refresh()
            return
        self.status.setText("正在刷新平台状态…")
        succeeded = [False]
        def refreshed(data):
            succeeded[0] = True
            self._platform_refreshed(data)
        def finished():
            if not succeeded[0]: self.status.setText("刷新未完成，设置草稿已保留。")
        self.api.post(self.prefix + "/api/refresh", refreshed, body={}, done=finished)

    def _platform_refreshed(self, data):
        self.result.show_data(data)
        self.refresh()

    @staticmethod
    def _merge_credentials(draft, server):
        result = deepcopy(draft)
        for key, value in server.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = TaskPage._merge_credentials(result[key], value)
            elif secret(key) or key in ("userid", "heybox_id", "platform"):
                result[key] = value
        return result

    def loaded(self, state, *, merge_login=False):
        self.state = deepcopy(state)
        config = state.get("music" if self.prefix == "/qq" else "config") or {}
        if not self.config_loaded or not self._dirty:
            self.settings.set_config(config)
        elif merge_login:
            self.settings.merge_login(config)
        self.config_loaded = True
        if self.heybox_login_form:
            self.heybox_login_form.update_state(state)
        if self.selection: self.selection.set_state(state)
        if self.task_picker:
            try: self.task_picker.set_state(state, self.settings.value())
            except ValueError: pass
        self.result.show_data({k: v for k, v in state.items() if k not in ("config", "music")})
        self._render_overview()
        self.status.setText("任务运行中…" if state.get("running") else
                            "状态已更新，未保存的设置已保留。" if self._dirty else "状态已更新。")
        self.run_button.setEnabled(not state.get("running", False))

    def _render_overview(self):
        data = overview_data(self.prefix, self.state, self._summary)
        self.account_name.setText(str(data["account"]))
        self.account_login.setText(str(data["login"]))
        self.account_detail.setText(str(data["detail"]))
        self.metrics.setText("    ·    ".join(f"{name}  {value}" for name, value in data["metrics"]))
        self.metrics.setVisible(bool(data["metrics"]))
        self.overview_notice.setText(str(data["notice"]))
        self.missions.show_tasks(data["tasks"])

    def current_config(self):
        if not self.config_loaded:
            raise ValueError("设置仍在读取，请稍候。")
        config = self.settings.value()
        if self.selection: config = self.selection.update_config(config)
        if self.task_picker: config = self.task_picker.update_config(config)
        return config

    def save(self, after=None):
        try:
            config = self.current_config()
        except ValueError as exc:
            QMessageBox.warning(self, "设置需要检查", str(exc))
            return
        revision = self._revision
        body = {"music": config} if self.prefix == "/qq" else config
        succeeded = [False]
        self.status.setText("正在保存…")
        self.save_button.setEnabled(False)
        def saved(result):
            succeeded[0] = True
            self.state["music" if self.prefix == "/qq" else "config"] = deepcopy(config)
            if self._revision == revision:
                self._dirty = False
                if self.selection: self.selection.mark_saved()
                if self.task_picker: self.task_picker.mark_saved()
            self.status.setText("设置已保存。" if not self._dirty else "已保存提交时的设置，后续改动仍待保存。")
            if callable(after): after(config)
        def finished():
            self.save_button.setEnabled(True)
            if not succeeded[0]: self.status.setText("保存未完成，改动已保留，请重试。")
        self.api.post(self.prefix + "/api/save", saved, body=body, done=finished)

    def run(self, dry=False):
        if self.prefix == "/heybox" and not self.state.get("app_logged_in"):
            self.tabs.setCurrentIndex(1)
            self.heybox_login_form.phone.setFocus()
            self.status.setText("请先在下方输入手机号和短信验证码，登录并验证任务后再执行。")
            return
        def saved(config):
            body = {}
            if self.prefix == "/ai-checkin":
                body = {"targets": [key for key, enabled in config.get("platforms", {}).items() if enabled], "dry": dry}
                if not body["targets"]:
                    QMessageBox.information(self, "未选择平台", "请在设置中启用至少一个平台。")
                    return
            elif self.prefix == "/signin":
                body = {"targets": [key for key, check in self.target_checks.items() if check.isChecked()]}
                if not body["targets"]:
                    QMessageBox.information(self, "未选择平台", "请在设置中选择本次要签到的平台。")
                    return
            self.run_button.setEnabled(False)
            started = [False]
            def accepted(data):
                started[0] = True
                self._run_started(data)
            def finished():
                if not started[0]:
                    self.run_button.setEnabled(self.config_loaded)
                    self.status.setText("任务未启动，请检查登录状态后重试。")
            self.api.post(self.prefix + "/api/run", accepted, body=body, done=finished)
        self.save(after=saved)

    def _run_started(self, data):
        self.seq = 0
        self.log.clear()
        self._was_running = True
        self._summary = {}
        self.status.setText("任务已启动，执行过程显示在右侧日志。")
        self.tabs.setCurrentIndex(0)
        self.poll()

    def action(self, name, reload=False):
        self.status.setText("正在处理…")
        def finished(data):
            self.action_done(data)
            if name == "login":
                self._login_pending = True
                self.status.setText("登录窗口已打开；完成后请点“获取登录态”。")
            elif name == "weibo-qr-login":
                self._login_pending = True
                self.status.setText("请在浏览器中扫码，成功后登录态会自动保存。")
            elif name == "refresh" or (reload and data.get("stage") in (None, "success")):
                self.refresh(merge_login=reload)
        if name == "refresh" and self.prefix in ("/ai-checkin", "/signin"):
            self.refresh()
        else:
            self.api.post(self.prefix + "/api/" + name, finished, body={})

    def action_done(self, data):
        self.result.show_data(data)
        self.status.setText(str(data.get("note") or data.get("msg") or "操作已处理。"))
        self.poll()

    def kugou_login(self):
        platform = self.settings.value().get("platform", "standard") if self.config_loaded else "standard"
        dialog = KugouLoginDialog(self.api, platform, self)
        dialog.saved.connect(lambda: self.refresh(merge_login=True))
        self._dialogs.append(dialog)
        dialog.show()
        dialog.start()

    def poll(self):
        if self.busy: return
        self.busy = True
        self.api.get(self.prefix + "/api/logs", self.logs_loaded,
                     params={"since": self.seq}, quiet=True,
                     done=lambda: setattr(self, "busy", False))
        if self._login_pending:
            path = "/signin/api/weibo-qr-status" if self.prefix == "/signin" else self.prefix + "/api/login-status"
            self.api.get(path, self._login_loaded, quiet=True)

    def _login_loaded(self, data):
        stage = data.get("stage")
        if data.get("msg"): self.status.setText(str(data["msg"]))
        if stage in ("success", "error", "expired"):
            self._login_pending = False
            if stage == "success": self.refresh(merge_login=True)

    def logs_loaded(self, data):
        new_seq = int(data.get("seq") or 0)
        restarted = new_seq < self.seq
        if restarted:
            self.seq = 0
            self.log.clear()
        for line in data.get("logs", []):
            if isinstance(line, str):
                self.log.appendPlainText(line)
            else:
                stamp = line.get("time") or line.get("at") or line.get("t") or ""
                message = line.get("text") or line.get("message") or line.get("msg") or line.get("m") or json.dumps(line, ensure_ascii=False)
                self.log.appendPlainText(f"{stamp} {message}".strip())
                self.seq = max(self.seq, int(line.get("id") or line.get("seq") or 0))
        self.seq = 0 if restarted else max(self.seq, new_seq)
        running = bool(data.get("running", False))
        self.run_button.setEnabled(not running and self.config_loaded)
        if running:
            self._was_running = True
            self.status.setText("任务运行中…")
        elif data.get("done") and not (self.heybox_login_form and self.heybox_login_form.in_progress):
            summary = data.get("summary") or {}
            self.status.setText("任务执行失败，请查看日志并处理后重试。" if summary.get("error") else
                                "本次执行仍有失败项，请查看日志。" if summary.get("fail") else
                                "任务已结束，详细结果见任务卡片和日志。")
        if data.get("summary"):
            self._summary = data["summary"]
            self.result.show_data(self._summary)
            self._render_overview()
        if not running and self._was_running:
            self._was_running = False
            self.refresh()
