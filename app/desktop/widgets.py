"""Reusable native controls and lossless editors for module configuration."""
from __future__ import annotations

import json
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFormLayout, QGroupBox, QLineEdit, QPlainTextEdit,
    QScrollArea, QSpinBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

LABELS = {
    "cookie": "登录 Cookie", "web_cookie": "网页登录 Cookie", "token": "登录 Token",
    "sign_enable": "每日签到", "medal_enable": "勋章签到", "medal_tasks_enable": "勋章任务",
    "medal_singer_id": "勋章歌手 ID", "medal_task_ids": "勋章任务 ID（JSON 列表/null）",
    "auto_all_nonpaid": "自动识别免费任务", "medal_ids": "选择的勋章 ID（JSON 列表）",
    "medal_task_types": "选择的任务类型（JSON 列表）", "artist_name": "歌手名称",
    "group_id": "乐迷团 ID", "device_id": "设备 ID", "collect_enable": "收藏任务",
    "like_note_enable": "笔记点赞", "like_note_count": "点赞数量", "share_enable": "分享任务",
    "comment_enable": "评论任务", "comment_text": "评论内容", "publish_enable": "发布笔记",
    "publish_title": "笔记标题", "publish_text": "笔记正文", "play_enable": "听歌任务",
    "play_count": "听歌数量", "mission_ids": "任务 ID（JSON 列表/null）",
    "gap_min": "最短间隔（秒）", "gap_max": "最长间隔（秒）", "listen_enable": "听歌任务",
    "ad_enable": "广告任务", "ad_times": "广告次数", "ad_gap": "广告间隔（秒）",
    "platform": "客户端类型（standard/lite）", "userid": "用户 ID", "mid": "设备 MID",
    "dfid": "设备 DFID", "cookie_source": "登录态来源（bili_chat/manual）",
    "express_enable": "等级加速包", "bonus_enable": "每日福利", "view_enable": "浏览任务",
    "dress_enable": "装扮商城", "ogv_enable": "观看剧集", "view_wait": "浏览等待（秒）",
    "heybox_id": "小黑盒用户 ID", "share_post": "分享帖子", "share_game": "分享游戏",
    "share_comment": "分享评论", "weibo": "微博", "pojie": "吾爱破解", "tieba": "百度贴吧",
    "pojie_mode": "签到模式", "repost_enable": "转发动态", "repost_count": "转发数量",
    "repost_author": "转发作者", "repost_uid": "作者 UID", "repost_chaohua": "转发超话",
    "repost_texts": "转发文案", "repost_links": "转发链接", "comment_url": "评论链接",
    "comment_count": "评论数量", "comment_texts": "评论文案", "like_enable": "点赞任务",
    "like_chaohua": "点赞超话", "mtask_enable": "微博任务中心", "mtask_cookie": "任务中心 Cookie",
    "mtask_follow_enable": "任务中心关注", "mtask_hot_search_enable": "任务中心热搜",
    "fans_enable": "粉丝任务", "fans_blogger_uid": "博主 UID", "fans_fans_uid": "粉丝 UID",
    "group_enable": "群聊任务", "group_name": "群聊名称", "group_text": "群消息",
    "dm_enable": "私聊任务", "dm_target": "私聊对象", "dm_morning": "早间文案",
    "dm_noon": "午间文案", "dm_night": "晚间文案", "platforms": "启用的平台",
    "trae": "Trae CN", "qoder": "Qoder 国际版", "qoder_cn": "Qoder 国内版",
    "workbuddy": "WorkBuddy", "doubao": "豆包", "minimax": "MiniMax Code",
    "dumate": "百度搭子", "lobster": "LobsterAI", "schedule": "每日自动签到",
    "enabled": "启用", "time": "执行时间（HH:MM）", "keepalive": "自动续期临期登录态",
    "qoder_token": "Qoder 国际版 PAT/Token", "qoder_cn_token": "Qoder 国内版 PAT/Token",
    "qoder_cn_cookie": "Qoder 国内版 Cookie", "headless": "后台浏览器无头模式",
    "theme": "主题（dark/light）", "lang": "语言（zh/en）",
}
SECRET_WORDS = ("cookie", "token", "password", "authorization", "secret", "api_key")


def secret(key):
    return any(word in key.lower() for word in SECRET_WORDS)


def safe_value(value, key=""):
    if secret(key) and isinstance(value, str) and value:
        return "••••••（已保存）"
    if isinstance(value, dict):
        return {str(k): safe_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_value(v) for v in value]
    return value


class JsonTree(QTreeWidget):
    def __init__(self):
        super().__init__()
        self.setHeaderLabels(["项目", "值 / 状态"])
        self.setColumnWidth(0, 260)
        self.setAlternatingRowColors(True)

    def show_data(self, data):
        self.clear()
        def add(parent, key, value, depth=0):
            complex_value = isinstance(value, (dict, list))
            text = f"{len(value)} 项" if complex_value else str(value if value is not None else "—")
            item = QTreeWidgetItem([LABELS.get(str(key), str(key)), text])
            item.setToolTip(1, text)
            parent.addChild(item) if isinstance(parent, QTreeWidgetItem) else parent.addTopLevelItem(item)
            if complex_value:
                values = value.items() if isinstance(value, dict) else enumerate(value)
                for k, v in values:
                    add(item, k, v, depth + 1)
                item.setExpanded(depth < 1)
        cleaned = safe_value(data)
        if isinstance(cleaned, dict):
            for k, v in cleaned.items(): add(self, k, v)
        else:
            add(self, "结果", cleaned)


class ConfigEditor(QScrollArea):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.readers = {}
        self.config = {}
        self.setFrameShape(QScrollArea.Shape.NoFrame)

    def set_config(self, config):
        self.config = config
        self.readers = {}
        widget = QWidget()
        layout = QVBoxLayout(widget)
        def build(obj, dest, path=()):
            form = QFormLayout()
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            dest.addLayout(form)
            for key, value in obj.items():
                label = LABELS.get(key, key)
                field_path = (*path, key)
                if isinstance(value, dict):
                    group = QGroupBox(label)
                    child = QVBoxLayout(group)
                    form.addRow(group)
                    build(value, child, field_path)
                    continue
                if isinstance(value, bool):
                    control = QCheckBox(label)
                    control.setChecked(value)
                    form.addRow(control)
                    reader = control.isChecked
                elif isinstance(value, int):
                    control = QSpinBox()
                    control.setRange(-2147483647, 2147483647)
                    control.setValue(value)
                    form.addRow(label, control)
                    reader = control.value
                elif isinstance(value, float):
                    control = QDoubleSpinBox()
                    control.setRange(-1e9, 1e9)
                    control.setDecimals(3)
                    control.setValue(value)
                    form.addRow(label, control)
                    reader = control.value
                elif isinstance(value, list) or value is None:
                    control = QPlainTextEdit(json.dumps(value, ensure_ascii=False))
                    control.setMaximumHeight(85)
                    control.setToolTip("填写 JSON，例如 [1, 2]；null 表示自动选择。")
                    form.addRow(label, control)
                    def reader(c=control, name=label):
                        try: return json.loads(c.toPlainText())
                        except ValueError as exc: raise ValueError(f"{name}：JSON 格式不正确") from exc
                else:
                    control = QLineEdit(str(value))
                    control.setClearButtonEnabled(True)
                    if secret(key): control.setEchoMode(QLineEdit.EchoMode.Password)
                    form.addRow(label, control)
                    reader = control.text
                control.setObjectName("config_" + "_".join(field_path))
                if isinstance(control, QCheckBox): control.toggled.connect(lambda *_args: self.changed.emit())
                elif isinstance(control, (QSpinBox, QDoubleSpinBox)): control.valueChanged.connect(lambda *_args: self.changed.emit())
                elif isinstance(control, QPlainTextEdit): control.textChanged.connect(lambda *_args: self.changed.emit())
                else: control.textChanged.connect(lambda *_args: self.changed.emit())
                self.readers[field_path] = reader
        build(config, layout)
        layout.addStretch()
        old = self.takeWidget()
        if old:
            old.hide()
            old.deleteLater()
        self.setWidget(widget)

    def value(self):
        result = json.loads(json.dumps(self.config))
        for path, reader in self.readers.items():
            target = result
            for key in path[:-1]: target = target[key]
            target[path[-1]] = reader()
        return result
