"""QQ 音乐原生勋章/任务选择器；只展示状态，不在此控件中执行网络任务。"""
from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)


class QQSelectionWidget(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False
        self._dirty = False
        self._medals_dirty = False
        self._tasks_dirty = False
        self._loaded_medals = False
        self._loaded_tasks = False
        self._state = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel("勾选歌手和任务类型。可自动项会执行真实浏览/点赞并核对进度；"
                      "广告、收听、分享及游戏需在官方客户端完成。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.medals = QTreeWidget()
        self.medals.setHeaderLabels(["要处理的歌手 / 勋章", "今日状态", "任务进度"])
        self.medals.setRootIsDecorated(False)
        self.medals.setMinimumHeight(135)
        layout.addWidget(self.medals)
        buttons = QHBoxLayout()
        for title, value in (("全选歌手", True), ("清空歌手", False)):
            button = QPushButton(title)
            button.clicked.connect(lambda _checked=False, v=value: self._check_all(self.medals, v))
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.tasks = QTreeWidget()
        self.tasks.setHeaderLabels(["任务类型（同类型会一并处理）", "处理方式", "说明"])
        self.tasks.setRootIsDecorated(False)
        self.tasks.setMinimumHeight(160)
        self.tasks.setWordWrap(True)
        layout.addWidget(self.tasks)
        task_buttons = QHBoxLayout()
        auto = QPushButton("全选可自动任务")
        auto.clicked.connect(self._select_auto)
        clear = QPushButton("清空任务")
        clear.clicked.connect(lambda: self._check_all(self.tasks, False))
        task_buttons.addWidget(auto)
        task_buttons.addWidget(clear)
        task_buttons.addStretch()
        layout.addLayout(task_buttons)
        self.hint = QLabel("先刷新 QQ 音乐状态，读取歌手和任务列表。")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.medals.itemChanged.connect(self._on_changed)
        self.tasks.itemChanged.connect(self._on_changed)

    def _on_changed(self, item, column):
        if self._loading or column != 0:
            return
        self._dirty = True
        if item.treeWidget() is self.medals:
            self._medals_dirty = True
        else:
            self._tasks_dirty = True
        self.changed.emit()

    def _check_all(self, tree, checked):
        for i in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(i)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable and item.flags() & Qt.ItemFlag.ItemIsEnabled:
                item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _select_auto(self):
        for i in range(self.tasks.topLevelItemCount()):
            item = self.tasks.topLevelItem(i)
            if item.flags() & Qt.ItemFlag.ItemIsEnabled:
                item.setCheckState(0, Qt.CheckState.Checked if item.data(0, Qt.ItemDataRole.UserRole + 1)
                                   else Qt.CheckState.Unchecked)

    def set_state(self, state: dict):
        """接受 /qq/api/state 的整体结构；轮询时保留尚未保存的勾选。"""
        if self._dirty:
            return
        self._state = deepcopy(state or {})
        config = self._state.get("music") or {}
        medals = (((self._state.get("cached") or {}).get("medal") or {}).get("medals") or [])
        self._loading = True
        try:
            self.medals.clear()
            self.tasks.clear()
            self._loaded_medals = bool(medals)
            selected_medals = config.get("medal_selected_ids")
            selected_medals = None if selected_medals is None else {str(x) for x in selected_medals}
            task_types = config.get("medal_task_types")
            selected_types = None if task_types is None else {str(x) for x in task_types}
            grouped = {}
            for medal in medals:
                mid = str(medal.get("id") or "")
                if not mid:
                    continue
                status = "已签到" if medal.get("signed") else "未签到"
                if medal.get("signed") is None:
                    status = "待刷新"
                row = QTreeWidgetItem([medal.get("name") or f"勋章 {mid}", status,
                                       f"{medal.get('task_done', 0)}/{medal.get('task_total', 0)}"])
                row.setData(0, Qt.ItemDataRole.UserRole, mid)
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                row.setCheckState(0, Qt.CheckState.Checked if selected_medals is None or mid in selected_medals
                                  else Qt.CheckState.Unchecked)
                self.medals.addTopLevelItem(row)
                for task in medal.get("tasks") or []:
                    typ = str(task.get("type") or "")
                    if not typ or typ == "sign_in":
                        continue
                    # 老缓存的 auto 标志可能来自旧版本，缺 capability 时不擅自执行。
                    cap = task.get("capability") or "manual"
                    group = grouped.setdefault(typ, {"name": task.get("name") or typ,
                                                      "caps": set(), "reasons": []})
                    group["caps"].add(cap)
                    reason = task.get("reason") or "请刷新任务列表以识别当前支持能力"
                    if reason not in group["reasons"]:
                        group["reasons"].append(reason)
            self._loaded_tasks = bool(grouped)
            for typ, group in grouped.items():
                caps = group["caps"]
                can_auto = "auto" in caps
                blocked = caps <= {"paid", "unavailable"}
                mode = "可自动" if can_auto else ("付费 / 未开放" if blocked else "需手动")
                reason = "；".join(group["reasons"])
                row = QTreeWidgetItem([group["name"], mode, reason])
                row.setToolTip(0, typ)
                row.setToolTip(2, reason)
                row.setData(0, Qt.ItemDataRole.UserRole, typ)
                row.setData(0, Qt.ItemDataRole.UserRole + 1, can_auto)
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                if blocked:
                    row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                checked = can_auto if selected_types is None else typ in selected_types
                row.setCheckState(0, Qt.CheckState.Checked if checked and not blocked
                                  else Qt.CheckState.Unchecked)
                self.tasks.addTopLevelItem(row)
            self.medals.setColumnWidth(0, 230)
            self.tasks.setColumnWidth(0, 270)
            self.tasks.setColumnWidth(1, 105)
            self.hint.setText(f"已读取 {self.medals.topLevelItemCount()} 枚勋章、"
                              f"{self.tasks.topLevelItemCount()} 种任务；勾选后请保存配置。"
                              if medals else "先刷新 QQ 音乐状态，读取歌手和任务列表。")
        finally:
            self._loading = False

    def update_config(self, config: dict) -> dict:
        """接收 music 配置，返回副本；空勾选保留 []，未读到列表时保留原值。"""
        result = deepcopy(config or {})
        if self._loaded_medals and self._medals_dirty:
            result["medal_selected_ids"] = self._checked(self.medals)
        if self._loaded_tasks and self._tasks_dirty:
            result["medal_task_types"] = self._checked(self.tasks)
        return result

    def mark_saved(self):
        """仅在保存接口成功回调中调用，失败时继续保留未提交的勾选。"""
        self._dirty = False
        self._medals_dirty = False
        self._tasks_dirty = False

    @staticmethod
    def _checked(tree) -> list[str]:
        return [str(tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole))
                for i in range(tree.topLevelItemCount())
                if tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked
                and tree.topLevelItem(i).flags() & Qt.ItemFlag.ItemIsEnabled]


# 便于外层页面使用简短名称。
QQSelection = QQSelectionWidget
