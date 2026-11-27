"""Native workbench, styled after the user's yd reference."""
from __future__ import annotations

import html
import sys
from PySide6.QtCore import Qt, QSize, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)
from app.paths import DATA_ROOT, SRC_ROOT
from app.ui_settings import load_settings, save_settings
from .api import Api
from .task_page import TaskPage
from . import theme
from .icons import line_icon
from .window_chrome import TitleBar, WindowControlBar, apply_round_corners, install_resize_handles
from .gui_lifecycle import install_gui_gc

WINDOW_TITLE = "多平台工具箱"


class Sidebar(QWidget):
    def __init__(self, specs, pick, toggle_theme, settings, parent=None):
        super().__init__(parent)
        self.setObjectName("workbenchSidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(228)
        self.pick_callback = pick
        self.buttons = []
        self.current = -1
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        brand = QWidget()
        brand_row = QHBoxLayout(brand)
        brand_row.setContentsMargins(16, 16, 12, 16)
        mark = QLabel()
        self.brand_mark = mark
        mark.setObjectName("workbenchBrandMark")
        mark.setFixedSize(30, 34)
        mark.setPixmap(line_icon("table", "Link", 28).pixmap(QSize(28, 28)))
        brand_row.addWidget(mark)
        text = QVBoxLayout()
        text.setSpacing(2)
        name = QPushButton("多平台工具箱")
        name.setObjectName("workbenchBrand")
        name.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        name.clicked.connect(lambda: pick(0))
        text.addWidget(name)
        sub = QLabel("聊天备份 · 每日任务")
        sub.setObjectName("workbenchBrandSub")
        text.addWidget(sub)
        brand_row.addLayout(text, 1)
        outer.addWidget(brand)
        search_box = QHBoxLayout()
        search_box.setContentsMargins(12, 0, 12, 10)
        self.search = QLineEdit()
        self.search.setObjectName("workbenchSearch")
        self.search.setPlaceholderText("搜索工具 · Ctrl+K")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter)
        self.search.returnPressed.connect(self.pick_first_visible)
        search_box.addWidget(self.search)
        outer.addLayout(search_box)
        scroll = QScrollArea()
        scroll.setObjectName("workbenchNavScroll")
        scroll.viewport().setObjectName("workbenchNavViewport")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName("workbenchNavItems")
        box = QVBoxLayout(holder)
        box.setContentsMargins(12, 0, 12, 8)
        box.setSpacing(2)
        self.groups = []
        for group in ("每日任务", "聊天记录", "自动维护"):
            label = QLabel(group)
            label.setObjectName("workbenchGroup")
            box.addWidget(label)
            group_buttons = []
            for index, (name, category, icon, factory) in enumerate(specs):
                if category != group: continue
                button = QPushButton(name)
                button.setObjectName("workbenchNavButton")
                button.setCheckable(True)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setIconSize(QSize(18, 18))
                button.setProperty("icon_name", icon)
                button.setIcon(line_icon(icon, "PlaceholderText", 18))
                button.clicked.connect(lambda checked=False, i=index: pick(i))
                box.addWidget(button)
                self.buttons.append((index, button))
                group_buttons.append(button)
            self.groups.append((label, group_buttons))
        box.addStretch(1)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)
        footer = QVBoxLayout()
        footer.setContentsMargins(12, 8, 12, 12)
        footer.setSpacing(4)
        self.theme_button = QPushButton()
        self.theme_button.setObjectName("workbenchFooter")
        self.theme_button.setToolTip("切换深色 / 浅色")
        self.theme_button.clicked.connect(toggle_theme)
        footer.addWidget(self.theme_button)
        settings_button = QPushButton("全局设置")
        settings_button.setObjectName("workbenchFooter")
        settings_button.clicked.connect(settings)
        footer.addWidget(settings_button)
        outer.addLayout(footer)
        self.refresh_icons()

    def refresh_icons(self):
        self.theme_button.setText("深色模式 · 切换浅色" if theme.is_dark() else "浅色模式 · 切换深色")
        self.brand_mark.setPixmap(line_icon("table", "Link", 28).pixmap(QSize(28, 28)))
        for index, button in self.buttons:
            button.setIcon(line_icon(button.property("icon_name"), "Link" if index == self.current else "PlaceholderText", 18))

    def select(self, index):
        self.current = index
        for i, button in self.buttons: button.setChecked(i == index)
        self.refresh_icons()

    def filter(self, value):
        query = value.strip().casefold()
        for label, buttons in self.groups:
            for button in buttons: button.setVisible(not query or query in button.text().casefold())
            label.setVisible(any(not button.isHidden() for button in buttons))

    def pick_first_visible(self):
        for index, button in self.buttons:
            if not button.isHidden():
                self.pick_callback(index)
                return


class Topbar(TitleBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("workbenchTopbar")
        self.setFixedHeight(54)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 8, 0)
        row.setSpacing(4)
        self.breadcrumb = QLabel()
        self.breadcrumb.setObjectName("workbenchBreadcrumb")
        row.addWidget(self.breadcrumb)
        row.addStretch(1)
        self.directory = QPushButton()
        self.directory.setObjectName("workbenchDirectory")
        self.directory.setFixedSize(32, 30)
        self.directory.setToolTip("打开数据目录")
        self.directory.setIconSize(QSize(16, 16))
        self.directory.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(DATA_ROOT))))
        row.addWidget(self.directory)
        divider = QFrame()
        divider.setObjectName("workbenchTopbarDivider")
        divider.setFixedSize(1, 18)
        row.addWidget(divider)
        self.controls = WindowControlBar(self)
        self.controls.minimize_requested.connect(lambda: self.window().showMinimized())
        self.controls.maximize_requested.connect(lambda: self.window().toggle_max_restore())
        self.controls.close_requested.connect(lambda: self.window().close())
        row.addWidget(self.controls)

    def refresh(self, group, name):
        self.breadcrumb.setText(f'<span style="color:{theme.color("muted")}">{html.escape(group)} / </span><span style="font-weight:600">{html.escape(name)}</span>')
        self.directory.setIcon(line_icon("folder", "PlaceholderText", 16))
        self.controls.refresh_icons()


class SettingsPage(QWidget):
    def __init__(self, api, window):
        super().__init__()
        self.api, self.window = api, window
        settings = load_settings()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        appearance = QFrame()
        appearance.setProperty("variant", "card")
        layout = QVBoxLayout(appearance)
        layout.setContentsMargins(12, 12, 12, 12)
        heading = QLabel("外观与运行")
        heading.setProperty("role", "sectionTitle")
        layout.addWidget(heading)
        row = QHBoxLayout()
        row.addWidget(QLabel("界面主题"))
        self.theme = QComboBox()
        self.theme.addItem("浅色", "light")
        self.theme.addItem("深色", "dark")
        self.theme.setCurrentIndex(1 if settings["theme"] == "dark" else 0)
        self.theme.setFixedWidth(160)
        row.addWidget(self.theme)
        row.addStretch()
        layout.addLayout(row)
        self.headless = QCheckBox("后台执行时隐藏浏览器（登录窗口仍会显示）")
        self.headless.setChecked(settings["headless"])
        layout.addWidget(self.headless)
        row = QHBoxLayout()
        row.addStretch()
        save = QPushButton("保存设置")
        save.setProperty("variant", "primary")
        save.clicked.connect(self.save)
        row.addWidget(save)
        layout.addLayout(row)
        outer.addWidget(appearance)
        storage = QFrame()
        storage.setProperty("variant", "card")
        box = QVBoxLayout(storage)
        box.setContentsMargins(12, 12, 12, 12)
        title = QLabel("本机数据")
        title.setProperty("role", "sectionTitle")
        box.addWidget(title)
        info = QLabel("账号、设置和聊天记录保存在本机。导出的离线页面可以直接打开。")
        info.setProperty("role", "muted")
        info.setWordWrap(True)
        box.addWidget(info)
        directory = QHBoxLayout()
        path = QLineEdit(str(DATA_ROOT))
        path.setReadOnly(True)
        path.setToolTip(str(DATA_ROOT))
        directory.addWidget(path, 1)
        browse = QPushButton("打开目录")
        browse.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(DATA_ROOT))))
        directory.addWidget(browse)
        box.addLayout(directory)
        outer.addWidget(storage)
        outer.addStretch()

    def save(self):
        save_settings({"headless": self.headless.isChecked(), "theme": self.theme.currentData()})
        self.window.apply_theme(self.theme.currentData())
        self.window.toast("设置已保存")


class DesktopWindow(QMainWindow):
    def __init__(self, base_url, *, on_close=None):
        super().__init__()
        from .chat_pages import ExportPage, DouyinPage
        from .service_pages import SparkPage, StarskyPage, OperationsPage
        from .fans_page import FansPage
        from .ai_gateway_page import AIGatewayPage
        application = QApplication.instance()
        install_gui_gc(application)
        theme.apply_theme(load_settings()["theme"], application)
        self.on_close = on_close
        self.api = Api(base_url, self)
        self.api.error.connect(self.show_error)
        self.setWindowTitle(WINDOW_TITLE)
        icon = QIcon(str(SRC_ROOT / "launcher" / "assets" / "toolkit.ico"))
        self.setWindowIcon(icon)
        application.setWindowIcon(icon)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.resize(1240, 820)
        self.setMinimumSize(880, 560)
        self.specs = [
            ("QQ 音乐", "每日任务", "play", lambda: TaskPage(self.api, "/qq", "QQ 音乐")),
            ("一键签到", "每日任务", "table", lambda: TaskPage(self.api, "/signin", "一键签到")),
            ("网易云乐迷团", "每日任务", "play", lambda: TaskPage(self.api, "/netease-fans", "网易云乐迷团")),
            ("酷狗音乐", "每日任务", "play", lambda: TaskPage(self.api, "/kugou", "酷狗音乐")),
            ("B站大会员", "每日任务", "table", lambda: TaskPage(self.api, "/bili-vip", "B站大会员")),
            ("小黑盒", "每日任务", "table", lambda: TaskPage(self.api, "/heybox", "小黑盒")),
            ("AI 工具签到", "每日任务", "table", lambda: TaskPage(self.api, "/ai-checkin", "AI 工具签到")),
            ("抖音聊天", "聊天记录", "download", lambda: DouyinPage(self.api)),
            ("抖音粉丝团", "每日任务", "play", lambda: FansPage(self.api)),
            ("微博聊天", "聊天记录", "download", lambda: ExportPage(self.api, "/weibo-chat", "微博聊天记录")),
            ("B站聊天", "聊天记录", "download", lambda: ExportPage(self.api, "/bili-chat", "B站聊天记录")),
            ("淘宝聊天", "聊天记录", "download", lambda: ExportPage(self.api, "/taobao", "淘宝旺旺聊天记录")),
            ("火花续期", "自动维护", "refresh", lambda: SparkPage(self.api)),
            ("StarSky", "自动维护", "refresh", lambda: StarskyPage(self.api)),
            ("高级操作", "自动维护", "sliders", lambda: OperationsPage(self.api)),
            ("全局设置", "设置", "sliders", lambda: SettingsPage(self.api, self)),
            ("AI 账号与反代", "自动维护", "refresh", lambda: AIGatewayPage(self.api)),
        ]
        root = QWidget()
        root.setProperty("windowSurface", True)
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.side = Sidebar(self.specs, self.select_page, self.toggle_theme, lambda: self.select_page(15))
        row.addWidget(self.side)
        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        self.top = Topbar()
        content.addWidget(self.top)
        self.pages = QStackedWidget()
        content.addWidget(self.pages, 1)
        row.addLayout(content, 1)
        self.setCentralWidget(root)
        for name, group, icon, factory in self.specs:
            page = factory()
            page.setProperty("pageSurface", True)
            if page.layout():
                page.layout().setContentsMargins(16, 16, 16, 16)
                page.layout().setSpacing(12)
            for label in page.findChildren(QLabel, "pageTitle"): label.hide()
            self.pages.addWidget(page)
        self.toast_label = QLabel(self)
        self.toast_label.setObjectName("desktopToast")
        self.toast_label.setWordWrap(True)
        self.toast_label.hide()
        self.toast_timer = QTimer(self)
        self.toast_timer.setSingleShot(True)
        self.toast_timer.timeout.connect(self.toast_label.hide)
        self.select_page(0)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(2000)
        self.wheel_guard = theme.WheelGuard(self)
        application.installEventFilter(self.wheel_guard)
        self.search_shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        self.search_shortcut.activated.connect(self.side.search.setFocus)
        self.statusBar().showMessage("就绪 · 数据保存在本机")
        self._resize_handles = install_resize_handles(self)

    def select_page(self, index):
        if not hasattr(self, "pages") or not 0 <= index < self.pages.count(): return
        self.pages.setCurrentIndex(index)
        self.side.select(index)
        name, group, icon, factory = self.specs[index]
        self.top.refresh(group, name)
        activate = getattr(self.pages.currentWidget(), "on_activate", None)
        if activate: activate()

    def poll(self):
        function = getattr(self.pages.currentWidget(), "poll", None)
        if function: function()

    def apply_theme(self, name):
        theme.apply_theme(name)
        self.side.refresh_icons()
        page_name, group, icon, factory = self.specs[self.pages.currentIndex()]
        self.top.refresh(group, page_name)
        settings_page = self.pages.widget(15)
        settings_page.theme.setCurrentIndex(1 if name == "dark" else 0)

    def toggle_theme(self):
        name = "light" if theme.is_dark() else "dark"
        save_settings({"theme": name})
        self.apply_theme(name)
        self.toast("已切换深色" if name == "dark" else "已切换浅色")

    def toast(self, message, error=False):
        self.toast_label.setProperty("error", error)
        self.toast_label.style().unpolish(self.toast_label)
        self.toast_label.style().polish(self.toast_label)
        self.toast_label.setText(message)
        self.toast_label.setFixedWidth(min(580, max(280, self.width() - 260)))
        self.toast_label.adjustSize()
        self.toast_label.move((self.width() - self.toast_label.width()) // 2,
                              self.height() - self.toast_label.height() - 42)
        self.toast_label.show()
        self.toast_label.raise_()
        self.toast_timer.start(7000 if error else 2600)

    def show_error(self, message):
        self.statusBar().showMessage(message, 15000)
        self.toast(message, error=True)

    def toggle_max_restore(self):
        self.showNormal() if self.isMaximized() else self.showMaximized()
        self.top.controls.set_maximized(self.isMaximized())

    def showEvent(self, event):
        super().showEvent(event)
        apply_round_corners(self)

    def resizeEvent(self, event):
        if hasattr(self, "side"): self.side.setFixedWidth(200 if self.width() < 1040 else 228)
        super().resizeEvent(event)

    def closeEvent(self, event: QCloseEvent):
        self.timer.stop()
        QApplication.instance().removeEventFilter(self.wheel_guard)
        if self.on_close: self.on_close()
        super().closeEvent(event)


def run_app_window(url, title=WINDOW_TITLE, *, on_close=None):
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("MultiPlatformToolkit")
    application.setOrganizationName("MultiPlatformToolkit")
    application.setFont(QFont("Microsoft YaHei UI", 10))
    window = DesktopWindow(url, on_close=on_close)
    window.setWindowTitle(WINDOW_TITLE)
    window.show()
    return application.exec()
