"""Semantic, native components shared by the overview/settings task pages."""
from __future__ import annotations

import base64
from copy import deepcopy
import re

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QScrollArea,
    QSpinBox, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .widgets import ConfigEditor, LABELS, secret

NAMES = {"weibo": "微博超话", "pojie": "吾爱破解", "tieba": "百度贴吧",
         "trae": "Trae CN", "qoder": "Qoder 国际版", "qoder_cn": "Qoder 国内版",
         "workbuddy": "WorkBuddy", "doubao": "豆包", "minimax": "MiniMax Code",
         "dumate": "百度搭子", "lobster": "LobsterAI"}
FIELD_LABELS = {**LABELS, "card_pool_enable": "歌词卡卡池每日签到",
                "medal_selected_ids": "勋章选择", "bili_chat_account": "复用的 B站账号",
                "comment_texts": "评论文案", "repost_texts": "转发文案",
                "auto_all_nonpaid": "自动识别全部免费任务"}
RESERVED = {"medal_selected_ids", "medal_task_types", "mission_ids", "medal_task_ids"}
PRIMARY = {
    "/qq": {"sign_enable", "medal_enable", "medal_tasks_enable", "card_pool_enable"},
    "/kugou": {"platform", "listen_enable", "ad_enable", "medal_enable", "medal_tasks_enable", "auto_all_nonpaid"},
    "/netease-fans": {"artist_name", "collect_enable", "like_note_enable", "like_note_count", "share_enable",
                       "comment_enable", "comment_text", "publish_enable", "publish_title", "publish_text", "play_enable", "play_count"},
    "/bili-vip": {"cookie_source", "sign_enable", "express_enable", "bonus_enable", "view_enable", "dress_enable", "ogv_enable"},
    "/heybox": {"sign_enable", "share_enable", "share_post", "share_game", "share_comment"},
    "/ai-checkin": {"platforms", "schedule", "keepalive"},
}


def _num(value, default=0):
    try: return int(value)
    except (ValueError, TypeError): return default


def _mission(row):
    current = row.get("current", row.get("finish"))
    total = row.get("total", row.get("need"))
    done = row.get("done") is True or row.get("status") == "COMPLETED" or (
        total is not None and _num(total) > 0 and _num(current) >= _num(total))
    paid = row.get("paid") or row.get("capability") == "paid"
    status = ("待核对" if row.get("verified") is False else "已完成" if done else
              "付费跳过" if paid else "需手动" if row.get("capability") == "manual" else
              "未开放" if row.get("capability") == "unavailable" else "待完成")
    return {"name": row.get("subtitle") if row.get("accelerate") else
            row.get("name") or row.get("title") or row.get("subtitle") or "任务",
            "progress": f"{current or 0}/{total or '—'}" if current is not None else "—",
            "status": status, "reason": row.get("reason") or row.get("message") or row.get("msg") or "",
            "url": row.get("url") or ""}


def overview_data(prefix, state, summary=None):
    """Translate actual module responses into readable cards without exposing credentials."""
    config = state.get("music" if prefix == "/qq" else "config") or {}
    cached = state.get("cached") or {}
    out = {"account": "本机账号", "login": "未配置登录态", "detail": "登录后刷新，读取平台返回的最新状态。",
           "metrics": [], "tasks": [], "notice": "状态以最近一次平台返回为准。"}
    configured = bool(config.get("cookie") or config.get("token"))
    out["login"] = "已保存登录态" if configured else "未配置登录态"
    out["account"] = str(cached.get("nickname") or "本机账号")
    if cached.get("nickname"):
        out["login"] = "已登录"
    if cached.get("warn") or cached.get("error"):
        out["notice"] = str(cached.get("warn") or cached.get("error"))
    if prefix == "/qq":
        if isinstance(summary, dict):
            cached = {**cached, **{key: value for key, value in summary.items()
                                   if key in ("music", "medal", "card_pool")}}
        out["account"] = "QQ 音乐账号"
        out["detail"] = "每日签到、乐迷勋章与歌词卡卡池"
        music, sign, medal = cached.get("music") or {}, cached.get("sign") or {}, cached.get("medal") or {}
        if _num(music.get("at")) > _num(sign.get("at")): sign = music
        out["tasks"].append({"name": "QQ 音乐每日签到", "status": "已签到" if sign.get("signed") else "待刷新" if not sign else "待签到",
                             "reason": sign.get("msg") or sign.get("error") or "", "progress": "—"})
        for row in medal.get("medals") or []:
            row_tasks = row.get("tasks") or []
            out["tasks"].append({"name": row.get("name") or "乐迷勋章", "status": "已签到" if row.get("signed") else "待签到",
                                 "progress": f"{row.get('task_done', sum(bool(t.get('done')) for t in row_tasks))}/{row.get('task_total') or len(row_tasks)}",
                                 "reason": row.get("msg") or row.get("error") or "",
                                 "children": [_mission(task) for task in row.get("tasks") or []]})
        all_tasks = [t for m in medal.get("medals") or [] for t in m.get("tasks") or []]
        medal_rows = medal.get("medals") or []
        signed_count = medal.get("signed") if medal.get("signed") is not None else sum(bool(m.get("signed")) for m in medal_rows)
        medal_total = medal.get("total") or len(medal_rows)
        out["metrics"] = [("勋章签到", f"{signed_count}/{medal_total}"),
                          ("勋章任务", f"{sum(bool(t.get('done')) for t in all_tasks)}/{len(all_tasks)}")]
        errors = " ".join(str(x.get("error") or x.get("msg") or "") for x in (music, sign, medal))
        if "失效" in errors or "重新登录" in errors:
            out["login"], out["notice"] = "登录态需更新", errors.strip()
        elif medal.get("ok") and configured:
            out["login"] = "已登录"
        pools = (cached.get("card_pool") or {}).get("pools") or []
        for pool in pools:
            signed = pool.get("signin") or {}
            out["tasks"].append({"name": "歌词卡卡池每日签到", "status": "已完成" if signed.get("ok") else "待处理",
                                 "reason": signed.get("msg") or signed.get("error") or "", "progress": "—"})
    elif prefix == "/netease-fans":
        group, me = cached.get("group") or {}, cached.get("me") or {}
        out["account"] = group.get("name") or config.get("artist_name") or "网易云乐迷团"
        out["detail"] = " · ".join(str(x) for x in (group.get("artist"), me.get("fan_title")) if x) or "乐迷团每日任务与亲密值"
        out["metrics"] = [("乐迷等级", me.get("level", "—")), ("亲密值", me.get("integral", "—")), ("距离升级", me.get("remaining", "—"))]
        out["tasks"] = [_mission(task) for task in cached.get("missions") or []]
    elif prefix == "/kugou":
        out["account"] = cached.get("nickname") or "酷狗音乐账号"
        out["detail"] = "App 扫码登录用于听歌 / 广告；网页登录用于勋章。"
        medal = cached.get("medal") or {}
        ad_done, ad_max = _num(cached.get("ad_done")), _num(cached.get("ad_max"), 8)
        out["metrics"] = [("广告次数", f"{ad_done}/{ad_max}"), ("会员到期", cached.get("vip_end", "—")), ("守护值", medal.get("integral", "—"))]
        out["tasks"] = [{"name": "听歌领 VIP", "status": "已完成" if cached.get("listen_done") else "待完成"},
                        {"name": "广告领 VIP", "status": "已领满" if ad_done >= ad_max else "待完成", "progress": f"{ad_done}/{ad_max}"},
                        {"name": "乐迷勋章任务", "progress": f"{medal.get('task_done', 0)}/{medal.get('task_total', 0)}", "status": "展开查看",
                         "children": [_mission(task) for task in medal.get("tasks") or []]}]
    elif prefix in ("/bili-vip", "/heybox"):
        out["account"] = cached.get("nickname") or ("B站账号" if prefix == "/bili-vip" else "小黑盒账号")
        out["tasks"] = [_mission(task) for task in cached.get("tasks") or []]
        out["detail"] = "大会员大积分与每日福利" if prefix == "/bili-vip" else "每日签到与分享任务"
        out["metrics"] = ([("当前积分", cached.get("point", "—")), ("本月获得", cached.get("score_month", "—")), ("即将到期", cached.get("expire_point", "—"))]
                          if prefix == "/bili-vip" else [("经验", cached.get("exp", "—")), ("盒币", cached.get("coin", "—")), ("等级", cached.get("level", "—"))])
        if prefix == "/bili-vip" and config.get("cookie_source") == "bili_chat":
            accounts = state.get("accounts") or {}
            values = accounts.get("accounts") or [] if isinstance(accounts, dict) else accounts
            out["login"] = "复用 B站聊天登录态" if values else "请先登录 B站聊天账号"
    elif prefix == "/signin":
        out["account"], out["detail"] = "平台签到账号", "微博超话 · 吾爱破解 · 百度贴吧"
        configured_count = sum(bool((config.get(key) or {}).get("cookie")) for key in NAMES if key in ("weibo", "pojie", "tieba"))
        out["login"] = f"{configured_count}/3 个平台已配置"
        results = summary if isinstance(summary, dict) else {}
        for key in ("weibo", "pojie", "tieba"):
            result = results.get(key) or {}
            total = _num(result.get("ok")) + _num(result.get("already"))
            out["tasks"].append({"name": NAMES[key], "status": "执行失败" if result.get("error") or result.get("fail") else "已完成" if total else "待执行",
                                 "progress": f"成功 {total}" if result else "—", "reason": result.get("error") or ""})
    elif prefix == "/ai-checkin":
        platforms = state.get("platforms") or [{"id": key, "name": NAMES.get(key, key)} for key in config.get("platforms", {})]
        results = (state.get("state") or {}).get("results") or {}
        out["account"], out["login"], out["detail"] = "桌面客户端登录态", "从本机客户端读取", "启用需要签到的平台；凭据会在运行时读取。"
        enabled = config.get("platforms") or {}
        out["metrics"] = [("启用平台", sum(bool(v) for v in enabled.values())), ("每日定时", (config.get("schedule") or {}).get("time") if (config.get("schedule") or {}).get("enabled") else "关闭")]
        states = {"success": "已签到", "already": "今日已签", "failed": "执行失败", "skipped": "已跳过"}
        for platform in platforms:
            pid = platform["id"]
            result = results.get(pid) or {}
            out["tasks"].append({"name": platform.get("name") or NAMES.get(pid, "AI 平台"),
                                 "status": states.get(result.get("status"), "待执行") if enabled.get(pid) else "未启用",
                                 "reason": " · ".join(str(x) for x in (result.get("at"), result.get("message") or platform.get("desc")) if x),
                                 "progress": "—"})
    return out


def label(text, *, muted=False, section=False):
    widget = QLabel(str(text))
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if muted:
        widget.setProperty("role", "muted")
    if section:
        widget.setProperty("role", "sectionTitle")
        font = QFont(widget.font())
        font.setPixelSize(14)
        font.setBold(True)
        widget.setFont(font)
    return widget


class Card(QFrame):
    def __init__(self, title="", parent=None):
        super().__init__(parent)
        self.setProperty("variant", "card")
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(12, 10, 12, 12)
        self.content.setSpacing(10)
        if title:
            self.content.addWidget(label(title, section=True))


class Fold(QWidget):
    def __init__(self, title, body, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.button = QToolButton()
        self.button.setText(title)
        self.button.setCheckable(True)
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.setArrowType(Qt.ArrowType.RightArrow)
        self.body = body
        body.hide()
        self.button.toggled.connect(self._toggle)
        layout.addWidget(self.button)
        layout.addWidget(body)
        body.hide()

    def _toggle(self, opened):
        self.button.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)
        self.body.setVisible(opened)


def _get(config, path):
    for key in path:
        config = config[key]
    return config


def _set(config, path, value):
    for key in path[:-1]:
        config = config[key]
    config[path[-1]] = value


class TaskSettingsEditor(QWidget):
    """Common controls first; account secrets and technical settings stay folded."""
    changed = Signal()

    def __init__(self, prefix, parent=None):
        super().__init__(parent)
        self.prefix = prefix
        self.config = {}
        self.readers = {}
        self._advanced_paths = []
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(12)

    def set_config(self, config):
        self.config = deepcopy(config or {})
        self.readers = {}
        while self.layout.count():
            item = self.layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        common = Card("任务选项")
        account = Card()
        account_body = QWidget()
        account_form = QFormLayout(account_body)
        account_form.setContentsMargins(0, 0, 0, 0)
        account_form.setSpacing(8)
        self.credentials = Fold("手动填写登录凭据", account_body)
        account.content.addWidget(self.credentials)
        account_controls = []
        advanced = {}

        def control(form, key, value, path, title):
            if isinstance(value, bool):
                widget = QCheckBox(title)
                widget.setChecked(value)
                reader = widget.isChecked
                widget.toggled.connect(lambda *_args: self.changed.emit())
                form.addRow(widget)
            elif key in ("platform", "cookie_source", "pojie_mode"):
                widget = QComboBox()
                choices = {"platform": [("酷狗音乐", "standard"), ("酷狗概念版", "lite")],
                           "cookie_source": [("复用 B站聊天账号", "bili_chat"), ("手动填写 Cookie", "manual")],
                           "pojie_mode": [("浏览器签到", "browser"), ("接口签到", "http")]}
                for name, data in choices[key]: widget.addItem(name, data)
                index = widget.findData(value)
                if index < 0:
                    widget.addItem(str(value), value)
                    index = widget.count() - 1
                widget.setCurrentIndex(index)
                reader = widget.currentData
                widget.currentIndexChanged.connect(lambda *_args: self.changed.emit())
                form.addRow(title, widget)
            elif isinstance(value, int):
                widget = QSpinBox()
                widget.setRange(-2147483647, 2147483647)
                widget.setValue(value)
                reader = widget.value
                widget.valueChanged.connect(lambda *_args: self.changed.emit())
                form.addRow(title, widget)
            elif isinstance(value, float):
                widget = QDoubleSpinBox()
                widget.setRange(-1e8, 1e8)
                widget.setValue(value)
                reader = widget.value
                widget.valueChanged.connect(lambda *_args: self.changed.emit())
                form.addRow(title, widget)
            else:
                widget = QLineEdit(str(value or ""))
                if secret(key):
                    widget.setEchoMode(QLineEdit.EchoMode.Password)
                    account_controls.append(widget)
                widget.setClearButtonEnabled(True)
                reader = widget.text
                widget.textChanged.connect(lambda *_args: self.changed.emit())
                form.addRow(title, widget)
            widget.setObjectName("config_" + "_".join(path))
            self.readers[path] = reader

        def build(obj, destination, path=()):
            form = QFormLayout()
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setContentsMargins(0, 0, 0, 0)
            form.setSpacing(8)
            destination.addLayout(form)
            hidden = {}
            for key, value in obj.items():
                here = (*path, key)
                if key in RESERVED:
                    continue
                title = FIELD_LABELS.get(key) or NAMES.get(key)
                if secret(key) and isinstance(value, str):
                    prefix = (NAMES.get(path[0], "") + " · ") if path else ""
                    control(account_form, key, value, here, prefix + (title or "登录凭据"))
                elif isinstance(value, dict):
                    if self.prefix == "/signin" or key in PRIMARY.get(self.prefix, set()):
                        title_widget = label(title or "其他选项", section=True)
                        form.addRow(title_widget)
                        nested = QVBoxLayout()
                        form.addRow(nested)
                        child_hidden = build(value, nested, here)
                        if child_hidden: hidden[key] = child_hidden
                    else:
                        hidden[key] = deepcopy(value)
                else:
                    allowed = key in PRIMARY.get(self.prefix, set()) or (
                        self.prefix == "/signin" and title is not None and not key.endswith(("_uid", "_id")))
                    if path and self.prefix == "/ai-checkin":
                        allowed = path[0] in ("platforms", "schedule")
                    if allowed and title:
                        control(form, key, value, here, title)
                    else:
                        hidden[key] = deepcopy(value)
            return hidden

        advanced = build(self.config, common.content)
        if self.readers:
            self.layout.addWidget(common)
        else:
            common.deleteLater()
        if account_controls:
            reveal = QCheckBox("显示凭据")
            reveal.toggled.connect(lambda shown: [w.setEchoMode(QLineEdit.EchoMode.Normal if shown
                                                               else QLineEdit.EchoMode.Password)
                                                  for w in account_controls])
            account_form.addRow(reveal)
            self.layout.addWidget(account)
        else:
            account.deleteLater()
        self.advanced = ConfigEditor()
        self.advanced.set_config(advanced)
        self.advanced.changed.connect(lambda *_args: self.changed.emit())
        self.advanced.setMinimumHeight(180)
        self.advanced_fold = Fold("高级设置", self.advanced)
        if advanced:
            extra = Card()
            extra.content.addWidget(self.advanced_fold)
            self.layout.addWidget(extra)
        else:
            self.advanced_fold.hide()

    def value(self):
        result = deepcopy(self.config)
        for path, reader in self.readers.items():
            _set(result, path, reader())
        def merge(target, values):
            for key, value in values.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict): merge(target[key], value)
                else: target[key] = value
        merge(result, self.advanced.value())
        return result

    def merge_login(self, server):
        """Refresh credentials in place; unrelated drafts may include unfinished JSON."""
        def update(values, path=()):
            for key, value in values.items():
                here = (*path, key)
                if isinstance(value, dict):
                    update(value, here)
                elif secret(key) or key in ("userid", "heybox_id", "platform"):
                    try: _set(self.config, here, value)
                    except KeyError: continue
                    try:
                        _get(self.advanced.config, here)
                        _set(self.advanced.config, here, value)
                    except KeyError:
                        pass
                    widget = self.findChild(QWidget, "config_" + "_".join(here))
                    if widget:
                        blocked = widget.blockSignals(True)
                        if isinstance(widget, QLineEdit): widget.setText(str(value or ""))
                        elif isinstance(widget, QComboBox):
                            index = widget.findData(value)
                            if index >= 0: widget.setCurrentIndex(index)
                        elif isinstance(widget, (QSpinBox, QDoubleSpinBox)): widget.setValue(value)
                        widget.blockSignals(blocked)
        update(server)


class TaskPicker(Card):
    changed = Signal()

    def __init__(self, prefix, parent=None):
        super().__init__("选择要执行的任务", parent)
        self.prefix, self._dirty, self._loading = prefix, False, False
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["任务", "进度", "说明"])
        self.tree.setRootIsDecorated(False)
        self.tree.setMinimumHeight(180)
        self.tree.setWordWrap(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.content.addWidget(self.tree)
        self.empty = label("登录并刷新状态后，可在这里勾选具体任务。", muted=True)
        self.content.addWidget(self.empty)
        self.tree.itemChanged.connect(self._changed)
        self.key = "mission_ids" if prefix == "/netease-fans" else "medal_task_ids"

    def _changed(self, item, column):
        if not self._loading and column == 0:
            self._dirty = True
            self.changed.emit()

    def set_state(self, state, config):
        if self._dirty: return
        cached = state.get("cached") or {}
        rows = cached.get("missions") or [] if self.prefix == "/netease-fans" else (cached.get("medal") or {}).get("tasks") or []
        selected = config.get(self.key)
        selected = None if selected is None else {str(x) for x in selected}
        automatic = self.prefix == "/kugou" and config.get("auto_all_nonpaid", True)
        self._loading = True
        self.tree.clear()
        try:
            for task in rows:
                identifier = str(task.get("id" if self.prefix == "/netease-fans" else "task_id") or "")
                if not identifier or identifier == "sign_in": continue
                name = str(task.get("subtitle") if task.get("accelerate") else task.get("name") or task.get("title") or task.get("subtitle") or "任务")
                current, total = task.get("current", task.get("finish", 0)), task.get("total", task.get("need", 0))
                done = task.get("status") == "COMPLETED" or (_num(total) > 0 and _num(current) >= _num(total))
                paid = bool(re.search(r"purchase|pay_|buy_|coin_|battle_pass|svip|vip", identifier, re.I)
                            or re.search(r"购买|付费|开通|金币|超会|会员", name))
                kind = task.get("kind") or next((key for word, key in (("评论", "comment"), ("播放", "play"), ("听歌", "play"),
                    ("发布", "publish"), ("分享", "share"), ("点赞", "like_note"), ("收藏", "collect")) if word in name), "")
                supported = self.prefix == "/kugou" or bool(kind)
                checked = not paid and not done and (identifier in selected if selected is not None else
                    bool(config.get(kind + "_enable")) if self.prefix == "/netease-fans" else True)
                reason = "已完成" if done else "付费跳过" if paid else ("自动识别免费任务已启用" if automatic else
                         "发布公开内容" if kind in ("comment", "publish") else "按勾选执行" if supported else "未识别任务")
                item = QTreeWidgetItem([name, f"{current}/{total or '—'}", reason])
                item.setData(0, Qt.ItemDataRole.UserRole, identifier)
                item.setData(0, Qt.ItemDataRole.UserRole + 1, not paid and supported and not done)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                if paid or automatic or not supported or done: item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                self.tree.addTopLevelItem(item)
            self.empty.setVisible(self.tree.topLevelItemCount() == 0)
            self.tree.setVisible(self.tree.topLevelItemCount() > 0)
        finally:
            self._loading = False

    def update_config(self, config):
        result = deepcopy(config)
        if self._dirty:
            result[self.key] = [self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
                                for i in range(self.tree.topLevelItemCount())
                                if self.tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked]
        return result

    def update_mode(self, config):
        if self.prefix != "/kugou": return
        automatic = config.get("auto_all_nonpaid", True)
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            allowed = item.data(0, Qt.ItemDataRole.UserRole + 1)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled if allowed and not automatic
                          else item.flags() & ~Qt.ItemFlag.ItemIsEnabled)

    def mark_saved(self):
        self._dirty = False


class MissionView(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["任务", "进度", "状态"])
        self.setAlternatingRowColors(False)
        self.setWordWrap(True)
        self.setMinimumHeight(220)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.itemDoubleClicked.connect(self._open)

    def _open(self, item, column):
        url = item.data(0, Qt.ItemDataRole.UserRole)
        if url and QUrl(url).scheme() in ("http", "https"):
            QDesktopServices.openUrl(QUrl(url))

    def show_tasks(self, tasks):
        self.clear()
        def add(task, parent=None):
            item = QTreeWidgetItem([str(task.get("name") or "任务"), str(task.get("progress") or "—"), str(task.get("status") or "待刷新")])
            item.setToolTip(0, str(task.get("reason") or task.get("name") or ""))
            item.setToolTip(2, str(task.get("reason") or ""))
            item.setData(0, Qt.ItemDataRole.UserRole, task.get("url") or "")
            parent.addChild(item) if parent else self.addTopLevelItem(item)
            for child in task.get("children") or []: add(child, item)
        for task in tasks: add(task)
        if sum(len(task.get("children") or []) for task in tasks) <= 16:
            self.expandAll()
        if not tasks:
            self.addTopLevelItem(QTreeWidgetItem(["暂无任务数据，请登录后刷新状态", "—", "待刷新"]))


class KugouLoginDialog(QDialog):
    saved = Signal()

    def __init__(self, api, platform="standard", parent=None):
        super().__init__(parent)
        self.api, self.platform, self.key = api, platform, ""
        self._closed, self._polling = False, False
        self.setWindowTitle("酷狗 App 扫码登录")
        self.setMinimumWidth(330)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        self.message = label("用手机酷狗 App 扫码，确认后会自动保存登录态。")
        layout.addWidget(self.message)
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setMinimumSize(240, 240)
        layout.addWidget(self.picture)
        actions = QHBoxLayout()
        self.reload = QPushButton("换一张")
        self.reload.clicked.connect(self.start)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        actions.addStretch()
        actions.addWidget(self.reload)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.poll)
        self.finished.connect(lambda _result: self.timer.stop())

    def start(self):
        self._closed = False
        self.key = ""
        self.timer.stop()
        self.message.setText("正在获取二维码…")
        self.reload.setEnabled(False)
        received = [False]
        def done():
            if self._closed: return
            self.reload.setEnabled(True)
            if not received[0]: self.message.setText("二维码获取失败，请重试。")
        def loaded(data):
            if self._closed: return
            received[0] = True
            self.key = str(data.get("key") or "")
            encoded = str(data.get("img") or "").split(",")[-1]
            try:
                picture = QPixmap()
                if not picture.loadFromData(base64.b64decode(encoded, validate=True)):
                    raise ValueError("图片格式无效")
                self.picture.setPixmap(picture.scaled(240, 240, Qt.AspectRatioMode.KeepAspectRatio,
                                                      Qt.TransformationMode.SmoothTransformation))
            except (ValueError, TypeError):
                self.key = ""
                self.message.setText("二维码图片无效，请换一张。")
                return
            self.message.setText("用手机酷狗 App 扫码，再在手机上确认。")
            if self.key: self.timer.start()
        self.api.post("/kugou/api/qr-start", loaded, params={"platform": self.platform}, body={}, done=done)

    def poll(self):
        if self._closed or self._polling or not self.key: return
        self._polling = True
        requested_key = self.key
        def loaded(data):
            if self._closed or requested_key != self.key: return
            self.message.setText(str(data.get("msg") or "等待扫码…"))
            stage = data.get("stage")
            if stage in ("expired", "error", "success"): self.timer.stop()
            if stage == "success":
                self.saved.emit()
                self.key = ""
        self.api.get("/kugou/api/qr-poll", loaded, params={"key": self.key, "platform": self.platform},
                     quiet=True, done=lambda: setattr(self, "_polling", False))

    def closeEvent(self, event):
        self._closed = True
        self.key = ""
        self.timer.stop()
        super().closeEvent(event)
