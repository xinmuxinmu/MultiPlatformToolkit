"""Native panel for the user-supplied Douyin circle task checklist."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QHBoxLayout, QHeaderView, QLineEdit, QPlainTextEdit,
    QMessageBox, QScrollArea, QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem, QWidget,
)

from .service_pages import _Card, _button, _empty_stack, _label, _menu_button, _page_layout
from .client_login import open_douyin_qr_login


class _FansDetails(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setPlainText("选择任务查看说明；需要提交内容的任务请在趣追中完成。")

    def show_data(self, data):
        lines = []
        if data.get("任务"):
            lines.append(str(data["任务"]))
            lines.append(str(data.get("状态") or "未同步"))
            if data.get("截图参考"):
                lines.append("截图参考：" + str(data["截图参考"]))
            lines.append(str(data.get("处理说明") or ""))
            if data.get("真实网页文字"):
                lines.append("网页显示：" + str(data["真实网页文字"]))
        else:
            entry = data.get("官方小程序入口") or {}
            if entry:
                lines.append(str(entry.get("app_name") or "趣追") + " · " + str(entry.get("circle_title") or "圈子"))
                lines.append("分享页用于确认小程序和圈子；任务是否可读取以 HTTP 实测结果为准。")
            else:
                lines.append("选择一项任务查看说明。")
                lines.append("当前任务来自截图，可先点「更多 → 解析入口」确认圈子。")
            for media in data.get("真实播放器观测", []):
                current = media.get("current_time")
                duration = media.get("duration")
                lines.append(f"播放器：{int(current or 0)} / {int(duration or 0)} 秒 · " + ("已暂停" if media.get("paused") else "播放中"))
            if data.get("真实播放器观测"):
                lines.append("播放器时间仅供查看，累计任务时长以官方显示为准。")
        result = data.get("最近操作") or {}
        if result.get("message"):
            lines.append("最近操作：" + str(result["message"]))
        probe = data.get("HTTP诊断") or {}
        if probe:
            lines.append("HTTP 检测：" + str(probe.get("checked_at") or "未记录时间"))
            lines.append("请求账号：" + str(probe.get("account_id") or "未选择已登录账号")
                         + (" · 已复用本机 Cookie" if probe.get("used_account_cookies") else " · 未使用账号 Cookie"))
            if probe.get("message"):
                lines.append(str(probe["message"]))
            for attempt in probe.get("attempts", []):
                endpoint = str(attempt.get("endpoint") or "").split("?", 1)[0].split("#", 1)[0]
                status = attempt.get("http_status")
                summary = str(attempt.get("method") or "GET") + " " + endpoint
                summary += "\n" + ("HTTP " + str(status) if status is not None else "未取得 HTTP 响应")
                if attempt.get("body_kind"):
                    summary += " · " + str(attempt["body_kind"])
                if attempt.get("business_code") is not None:
                    summary += " · 业务码 " + str(attempt["business_code"])
                if attempt.get("candidate"):
                    summary += " · 候选路径"
                if attempt.get("message"):
                    summary += "\n" + str(attempt["message"])
                lines.append(summary)
        self.setPlainText("\n\n".join(line for line in lines if line))


class FansPage(QWidget):
    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.api = api
        self._loaded = False
        self._running = False
        self._polling = False
        self._last_poll = 0.0
        self._tasks = {}
        self._http_probe = {}
        self._saved_config = {}
        layout = _page_layout(self, 0)
        tabs = QTabWidget()
        layout.addWidget(tabs)
        task_page = QWidget()
        task_layout = _page_layout(task_page)
        circle_card = _Card("封茗囧菌的圈子")
        self.status = _label("任务来自截图，尚未同步。")
        circle_card.box.addWidget(self.status)
        self.entry_note = _label("刷新任务和尝试签到会先发送 HTTP 请求，具体结果显示在下方诊断。", "caption")
        circle_card.box.addWidget(self.entry_note)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        toolbar.addStretch()
        _menu_button(toolbar, [("读取本地状态", self.refresh),
            ("解析入口", lambda: self.action("resolve")), ("登录账号", lambda: self.action("login"))])
        _button("扫码登录", self.qr_login, toolbar)
        self.checkin_button = _button("HTTP 免费签到", self.checkin, toolbar)
        _button("刷新任务", lambda: self.action("refresh"), toolbar)
        _button("打开圈子", lambda: self.action("open"), toolbar, "primary")
        circle_card.box.addLayout(toolbar)
        task_layout.addWidget(circle_card)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.tasks = QTreeWidget()
        self.tasks.setHeaderLabels(["任务", "来源与状态", "奖励 / 截图参考", "完成方式"])
        self.tasks.header().setStretchLastSection(False)
        self.tasks.header().setMinimumSectionSize(60)
        for column, width in enumerate((140, 160, 200, 120)):
            self.tasks.setColumnWidth(column, width)
        self.tasks.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tasks.currentItemChanged.connect(self._selected)
        self.task_stack = _empty_stack(self.tasks, "还没有任务记录\n点「刷新任务」读取圈子状态，或在手机端确认圈子入口。")
        splitter.addWidget(self.task_stack)
        self.details = _FansDetails()
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, 110])
        task_layout.addWidget(splitter, 1)
        manual_row = QHBoxLayout()
        self.limitations = _label("截图仅供参考；付费任务需本人在客户端处理。", "caption")
        manual_row.addWidget(self.limitations, 1)
        self.manual_button = _button("打开官方入口", self.open_task, manual_row)
        self.manual_button.setToolTip("只打开圈子官方入口，不会直达或自动完成任务；请本人在抖音客户端操作后刷新核对。")
        self.manual_button.setEnabled(False)
        task_layout.addLayout(manual_row)
        tabs.addTab(task_page, "粉丝任务")

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings = QWidget()
        settings_layout = _page_layout(settings)
        config_box = _Card("圈子设置")
        form = QFormLayout()
        form.setSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.artist = QLineEdit("封茗囧菌")
        self.circle_url = QLineEdit()
        self.circle_url.setPlaceholderText("粘贴官方圈子分享链接")
        self.accounts = QComboBox()
        self.accounts.addItem("当前抖音账号（HTTP复用）", "")
        self.accounts.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.accounts.setMinimumContentsLength(10)
        form.addRow("歌手", self.artist)
        form.addRow("圈子链接", self.circle_url)
        form.addRow("使用账号", self.accounts)
        config_box.box.addLayout(form)
        config_box.box.addWidget(_label("HTTP 默认复用当前抖音账号，也可指定账号。圈子浏览器保持独立登录，请在打开后核对账号。"))
        save_row = QHBoxLayout()
        save_row.addStretch()
        _button("保存设置", self.save, save_row, "primary")
        config_box.box.addLayout(save_row)
        settings_layout.addWidget(config_box)
        settings_layout.addStretch()
        settings_scroll.setWidget(settings)
        tabs.addTab(settings_scroll, "圈子设置")

    def on_activate(self):
        if not self._loaded:
            self._loaded = True
            self.refresh()

    def refresh(self):
        self.api.get("/douyin-fans/api/state", lambda value: self._state_loaded(value, True))

    def _state_loaded(self, value, load_config=False):
        self._running = bool(value.get("running"))
        self._http_probe = value.get("http_probe") or {}
        self._saved_config = dict(value.get("config") or {})
        if load_config:
            config = value.get("config", {})
            self.artist.setText(str(config.get("artist") or "封茗囧菌"))
            self.circle_url.setText(str(config.get("circle_url") or ""))
            self.accounts.clear()
            self.accounts.addItem("当前抖音账号（HTTP复用）", "")
            for account in value.get("accounts", {}).get("accounts", []):
                self.accounts.addItem(str(account.get("name") or account["id"]), account["id"])
            selected = self.accounts.findData(config.get("account_id", ""))
            self.accounts.setCurrentIndex(max(0, selected))
        previous = self.tasks.currentItem()
        selected_id = previous.data(0, Qt.ItemDataRole.UserRole) if previous else None
        self.tasks.clear()
        self._tasks = {task["id"]: task for task in value.get("tasks", [])}
        for task in value.get("tasks", []):
            source = {"user_screenshot": "截图参考", "official_http_snapshot": "接口记录"}.get(task.get("source"), "网页记录")
            mode = {"paid": "客户端付费", "manual": "本人提交", "playback": "实际播放", "checkin": "免费签到"}.get(task.get("kind"), "手动核对")
            item = QTreeWidgetItem([task.get("title", ""), source + " · " + task.get("status", "未同步"),
                                   task.get("reference", ""), mode])
            item.setData(0, Qt.ItemDataRole.UserRole, task["id"])
            item.setToolTip(1, task.get("observed_text") or "来自截图，尚未同步官方网页")
            for column in (0, 2, 3):
                item.setToolTip(column, item.text(column))
            self.tasks.addTopLevelItem(item)
            if selected_id == task["id"]:
                self.tasks.setCurrentItem(item)
        self.task_stack.setCurrentIndex(0 if self.tasks.topLevelItemCount() else 1)
        observed = value.get("last_observed_at")
        status = "正在读取圈子状态…" if self._running else ("最近同步：" + str(observed) if observed else "截图参考 · 任务尚未同步")
        result = value.get("last_result") or {}
        if result.get("message"):
            status += "\n" + str(result["message"])
        self.status.setText(status)
        if not selected_id:
            self.details.show_data({"官方小程序入口": value.get("share_entry", {}), "页面状态": value.get("page", {}), "真实播放器观测": value.get("playback", []),
                                    "最近操作": result, "HTTP诊断": self._http_probe, "限制": value.get("limitations", [])})
        is_share = "/share/microapp" in self.circle_url.text()
        if is_share:
            self.entry_note.setText("HTTP 已认证；签到将先检查今日状态，提交后重新读取官方资料和日历确认。"
                                    if self._http_probe.get("authenticated") else
                                    "刷新会检查趣追 HTTP 登录态、圈子资料及日历；需要已登录的抖音账号。")
        else:
            self.entry_note.setText("任务状态以官方接口或页面实测结果为准。")
        self.checkin_button.setEnabled(not self._running and bool(self.circle_url.text().strip()))
        self.checkin_button.setToolTip("仅对已加入圈子且今日明确未签到的账号提交免费签到；提交后复查资料和日历。" if is_share else "仅对可验证的官方免费签到操作生效")

    def _selected(self, item, previous=None):
        task = self._tasks.get(item.data(0, Qt.ItemDataRole.UserRole), {}) if item else {}
        can_open_entry = task.get("can_open_entry", task.get("can_open", True))
        self.manual_button.setEnabled(bool(task) and task.get("kind") != "paid"
                                      and can_open_entry and not self._running)
        if task:
            entry_note = ("此按钮只打开已配置的圈子官方入口，不会直达或自动执行任务；请本人在抖音客户端完成后刷新状态。"
                          if can_open_entry else "当前没有安全确认的任务入口链接。")
            self.details.show_data({"任务": task.get("title"), "来源": task.get("source"), "状态": task.get("status"),
                                    "截图参考": task.get("reference"), "真实网页文字": task.get("observed_text"),
                                    "网页进度": task.get("progress"), "处理说明": task.get("reason"),
                                    "官方入口": entry_note,
                                    "HTTP诊断": self._http_probe,
                                    "提醒": "截图奖励不是实时规则，网页播放器时间不是累计任务完成证明。"})

    def save(self):
        body = {"artist": self.artist.text().strip(), "circle_url": self.circle_url.text().strip(),
                "account_id": self.accounts.currentData() or ""}
        self.api.post("/douyin-fans/api/config", lambda _: self.refresh(), body=body)

    def qr_login(self):
        open_douyin_qr_login(self.api, self, self._login_saved)

    def _login_saved(self, account):
        self.status.setText("登录态已保存：" + str(account.get("name") or account.get("id")))
        if not self._running:
            self.api.post("/douyin-fans/api/config", lambda _: self.refresh(),
                          body={"account_id": account["id"]})
        else:
            self.status.setText("登录态已保存，当前圈子任务结束后可在设置中选择新账号。")

    def action(self, action, body=None):
        if action != "login" and not self.circle_url.text().strip():
            QMessageBox.information(self, "需要官方圈子链接", "请先从官方圈子分享入口复制链接，填写并保存。截图不能确定圈子地址。")
            return
        self._running = True
        self.status.setText("正在请求圈子操作…")
        self.api.post("/douyin-fans/api/" + action, lambda _: self.api.get("/douyin-fans/api/state", self._state_loaded),
                      body=body, done=lambda: self.api.get("/douyin-fans/api/state", self._state_loaded, quiet=True))

    def checkin(self):
        if self._running:
            return
        if (self.circle_url.text().strip() != self._saved_config.get("circle_url", "")
                or (self.accounts.currentData() or "") != self._saved_config.get("account_id", "")
                or self.artist.text().strip() != self._saved_config.get("artist", "")):
            QMessageBox.information(self, "请先保存设置", "圈子或账号选择有未保存修改，请先保存，再确认签到。")
            return
        if QMessageBox.question(self, "免费签到确认", "将为当前已保存的账号和圈子尝试官方免费签到，并重新读取状态确认。不会自动入圈。现在执行？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.action("checkin")

    def open_task(self):
        selected = self.tasks.currentItem()
        if selected:
            self.action("open-task", {"task_id": selected.data(0, Qt.ItemDataRole.UserRole)})

    def poll(self):
        if not self._loaded or not self._running or self._polling or time.monotonic() - self._last_poll < 2:
            return
        self._last_poll = time.monotonic()
        self._polling = True
        self.api.get("/douyin-fans/api/state", self._state_loaded, quiet=True,
                     done=lambda: setattr(self, "_polling", False))
