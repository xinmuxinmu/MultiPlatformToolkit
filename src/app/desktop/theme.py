"""Semantic light/dark palette adapted from the user's D:/PythonApps/yd reference."""
from __future__ import annotations

import re
import os
from pathlib import Path
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QPalette, QFontDatabase, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QAbstractSpinBox, QComboBox, QSlider, QTabBar

SPEC = {
    "Window": ("#f3f5f9", "#151c29"), "WindowText": ("#263449", "#dce5f3"),
    "Base": ("#ffffff", "#1d2737"), "AlternateBase": ("#f0f3f8", "#243044"),
    "Text": ("#263449", "#dce5f3"), "Button": ("#ffffff", "#243044"),
    "ButtonText": ("#263449", "#dce5f3"), "Midlight": ("#e8effc", "#293d5c"),
    "Mid": ("#cbd5e3", "#3a4b63"), "Dark": ("#93a7c5", "#6c84a6"),
    "Shadow": ("#dce3ed", "#303e52"), "Highlight": ("#356bd6", "#3f76c9"),
    "HighlightedText": ("#ffffff", "#ffffff"), "Accent": ("#275bc1", "#568fe5"),
    "Link": ("#285ebc", "#93bcff"), "PlaceholderText": ("#5f7089", "#9caec5"),
    "ToolTipBase": ("#ffffff", "#243044"), "ToolTipText": ("#263449", "#dce5f3"),
    "BrightText": ("#b93842", "#ff969f"), "Light": ("#fff1f2", "#38252e"),
    "LinkVisited": ("#18754e", "#7cd6af"),
}
ROLES = {"window": "Window", "surface": "Base", "surface_alt": "AlternateBase",
         "text": "Text", "muted": "PlaceholderText", "border": "Shadow",
         "accent": "Highlight", "on_accent": "HighlightedText", "link": "Link",
         "danger": "BrightText", "danger_soft": "Light", "success": "LinkVisited"}
_dark = False
_fonts_ready = False

QSS = """
QWidget { background: transparent; color: palette(window-text); font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 13px; }
QMainWindow, QDialog, QMessageBox, QWidget[pageSurface="true"] { background: palette(window); }
QScrollArea { border: none; background: transparent; }
QPushButton { background: palette(button); color: palette(button-text); padding: 6px 12px; border: 1px solid palette(shadow); border-radius: 6px; min-height: 18px; }
QPushButton:hover { background: palette(midlight); color: palette(link); border-color: palette(highlight); }
QPushButton:pressed { background: palette(alternate-base); }
QPushButton:focus { border-color: palette(highlight); }
QPushButton:disabled { color: palette(placeholder-text); background: palette(alternate-base); border-color: palette(shadow); }
QPushButton[variant="primary"] { background: palette(highlight); color: palette(highlighted-text); border-color: palette(highlight); font-weight: 600; }
QPushButton[variant="primary"]:hover { background: palette(accent); border-color: palette(accent); }
QPushButton[variant="danger"] { background: palette(light); color: palette(bright-text); border-color: palette(mid); }
QPushButton[variant="ghost"] { background: transparent; border-color: transparent; color: palette(placeholder-text); }
QPushButton[variant="ghost"]:hover { background: palette(midlight); color: palette(link); }
QPushButton[menuButton="true"]:pressed, QPushButton[menuButton="true"]:open { background: palette(midlight); color: palette(link); }
QPushButton[menuButton="true"]::menu-indicator { subcontrol-position: right center; right: 6px; }
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QTimeEdit, QComboBox { background: palette(base); color: palette(text); border: 1px solid palette(mid); border-radius: 6px; padding: 6px; selection-background-color: palette(highlight); selection-color: palette(highlighted-text); }
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTimeEdit:focus, QComboBox:focus { border-color: palette(highlight); }
QLineEdit:disabled, QComboBox:disabled { background: palette(alternate-base); color: palette(placeholder-text); }
QTreeWidget, QTreeView, QTableWidget, QTableView, QListWidget { background: palette(base); alternate-background-color: palette(alternate-base); color: palette(text); border: 1px solid palette(mid); border-radius: 6px; outline: 0; gridline-color: palette(shadow); }
QTreeWidget::item, QTableWidget::item, QListWidget::item { padding: 5px 6px; border: none; }
QTreeWidget::item:selected, QTableWidget::item:selected, QListWidget::item:selected { background: palette(highlight); color: palette(highlighted-text); }
QTreeWidget::item:hover, QListWidget::item:hover { background: palette(midlight); }
QHeaderView::section { background: palette(alternate-base); color: palette(placeholder-text); border: none; border-bottom: 1px solid palette(shadow); padding: 6px 8px; font-size: 11px; }
QAbstractItemView::indicator { width: 16px; height: 16px; }
QCheckBox, QRadioButton { spacing: 6px; padding: 3px 0; }
QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; }
QCheckBox:disabled, QRadioButton:disabled { color: palette(placeholder-text); }
QTabWidget::pane { background: transparent; border: none; border-top: 1px solid palette(mid); top: -1px; }
QTabBar::tab { background: palette(alternate-base); color: palette(placeholder-text); border: 1px solid palette(mid); border-bottom: 2px solid transparent; border-top-left-radius: 6px; border-top-right-radius: 6px; padding: 8px 16px; margin-right: 4px; font-size: 12px; }
QTabBar::tab:selected { background: palette(base); color: palette(link); border-bottom-color: palette(highlight); }
QTabBar::tab:hover { color: palette(link); }
QGroupBox { background: palette(base); border: 1px solid palette(shadow); border-radius: 12px; margin-top: 14px; padding: 16px 12px 12px; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 12px; padding: 0 4px; font-weight: 600; font-size: 14px; }
QFrame[variant="card"] { background: palette(base); border: 1px solid palette(shadow); border-radius: 12px; }
QFrame[variant="subtle"] { background: palette(alternate-base); border: 1px solid palette(shadow); border-radius: 10px; }
QLabel[role="sectionTitle"] { font-weight: 600; font-size: 14px; }
QLabel[role="muted"], QLabel#muted, QLabel[role="caption"] { color: palette(placeholder-text); }
QLabel[role="caption"] { font-size: 11px; }
QLabel[role="stat"] { font-size: 27px; font-weight: 600; }
QLabel[role="success"] { color: palette(link-visited); }
QLabel[role="danger"] { color: palette(bright-text); }
QLabel#desktopToast { background: palette(base); color: palette(text); border: 1px solid palette(mid); border-radius: 8px; padding: 10px 16px; }
QLabel#desktopToast[error="true"] { background: palette(light); color: palette(bright-text); }
QMenu { background: palette(base); color: palette(text); border: 1px solid palette(mid); border-radius: 8px; padding: 4px; }
QMenu::item { padding: 6px 16px; border-radius: 4px; }
QMenu::item:selected { background: palette(midlight); color: palette(link); }
QStatusBar { background: palette(base); color: palette(placeholder-text); border-top: 1px solid palette(shadow); font-size: 11px; }
QStatusBar::item { border: none; }
QToolTip { background: palette(tool-tip-base); color: palette(tool-tip-text); border: 1px solid palette(mid); padding: 5px; }
QProgressBar { background: palette(alternate-base); border: none; border-radius: 4px; height: 6px; text-align: center; }
QProgressBar::chunk { background: palette(highlight); border-radius: 4px; }
QSplitter::handle { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 4px 0; }
QScrollBar::handle:vertical { background: palette(mid); border-radius: 4px; min-height: 24px; margin: 0 2px; }
QScrollBar::handle:vertical:hover { background: palette(dark); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""


def is_dark(): return _dark


def current_palette():
    app = QApplication.instance()
    return app.palette() if app else build_palette(_dark)


def color(name, dark=None):
    return SPEC[ROLES.get(name, name)][int(_dark if dark is None else dark)]


def build_palette(dark):
    palette = QPalette()
    for name, values in SPEC.items():
        role = getattr(QPalette.ColorRole, name, None)
        if role is not None: palette.setColor(role, QColor(values[int(dark)]))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(SPEC["PlaceholderText"][int(dark)]))
    return palette


def stylesheet(dark=False):
    from .shell_qss import SHELL_QSS
    roles = {re.sub(r"[^a-z]", "", name.lower()): values[int(dark)] for name, values in SPEC.items()}
    def replace(match):
        key = re.sub(r"[^a-z]", "", match.group(1).lower())
        return roles.get(key, SPEC["Text"][int(dark)])
    rendered = re.sub(r"palette\(([^)]+)\)", replace, QSS + "\n" + SHELL_QSS)
    if QApplication.instance():
        from app.paths import DATA_ROOT
        directory = DATA_ROOT / "cache" / "ui"
        directory.mkdir(parents=True, exist_ok=True)
        tick = directory / ("check-dark.png" if dark else "check-light.png")
        image = QPixmap(32, 32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(SPEC["HighlightedText"][int(dark)]), 3.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(8, 16, 13, 21)
        painter.drawLine(13, 21, 24, 10)
        painter.end()
        image.save(str(tick))
        styles = """
        QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid palette(mid); border-radius: 3px; background: palette(base); }
        QCheckBox::indicator:checked { background: palette(highlight); border-color: palette(highlight); image: url('%TICK%'); }
        QCheckBox::indicator:unchecked { background: palette(base); image: none; }
        QCheckBox::indicator:disabled { background: palette(alternate-base); border-color: palette(shadow); }
        """.replace("%TICK%", tick.as_posix())
        rendered += re.sub(r"palette\(([^)]+)\)", replace, styles)
    return rendered


def apply_theme(name="light", app=None):
    global _dark, _fonts_ready
    _dark = name == "dark"
    app = app or QApplication.instance()
    if app:
        if not _fonts_ready:
            # The Windows offscreen backend does not enumerate system fonts.
            # Register locally installed fonts for deterministic Chinese previews.
            if "Microsoft YaHei UI" not in QFontDatabase.families():
                font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
                for name in ("msyh.ttc", "msyhbd.ttc"):
                    if (font_dir / name).is_file(): QFontDatabase.addApplicationFont(str(font_dir / name))
            _fonts_ready = True
        app.setStyle("Fusion")
        app.setPalette(build_palette(_dark))
        app.setStyleSheet(stylesheet(_dark))


class WheelGuard(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel and isinstance(obj, (QComboBox, QAbstractSpinBox, QSlider, QTabBar)) and not obj.property("wheelOk"):
            return True
        return False
