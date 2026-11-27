# -*- coding: utf-8 -*-
"""线形图标（与 src/gui_qt/navigation.line_icon 同一绘制协议）。

样张本地副本：颜色改为走 theme_real.color()。
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

from . import theme


_ICON_ROLES = {
    "Link": "link", "ButtonText": "text", "HighlightedText": "on_accent",
    "PlaceholderText": "muted", "BrightText": "danger",
}


def _icon_color(value):
    """主程序传的是颜色字符串（'#9caec5'），样张内部也支持传角色名。"""
    if isinstance(value, str) and value.startswith("#"):
        return QColor(value)
    return QColor(theme.color(_ICON_ROLES.get(value, "text")))


# ---------------------------------------------------------------- 线形图标


def _icon_painter(name, painter):
    """24×24 逻辑坐标系内绘制，描边 1.65，不填充。"""
    if name == "search":
        painter.drawEllipse(4, 4, 11, 11)
        painter.drawLine(14, 14, 20, 20)
    elif name == "download":
        painter.drawLine(12, 4, 12, 15)
        painter.drawLine(7, 10, 12, 15)
        painter.drawLine(17, 10, 12, 15)
        painter.drawLine(5, 19, 19, 19)
    elif name == "refresh":
        painter.drawArc(4, 4, 16, 16, 40 * 16, 280 * 16)
        painter.drawLine(17, 4, 17, 9)
        painter.drawLine(17, 9, 12, 9)
    elif name == "folder":
        painter.drawLine(3, 7, 10, 7)
        painter.drawLine(10, 7, 12, 10)
        painter.drawRoundedRect(3, 10, 18, 10, 2, 2)
    elif name == "play":
        painter.drawLine(7, 5, 18, 12)
        painter.drawLine(18, 12, 7, 19)
        painter.drawLine(7, 19, 7, 5)
    elif name == "stop":
        painter.drawRoundedRect(6, 6, 12, 12, 2, 2)
    elif name == "trash":
        painter.drawLine(4, 7, 20, 7)
        painter.drawLine(9, 7, 9, 4)
        painter.drawLine(15, 7, 15, 4)
        painter.drawRoundedRect(6, 7, 12, 13, 2, 2)
    elif name == "sliders":
        for y, x in ((7, 15), (12, 9), (17, 13)):
            painter.drawLine(4, y, 20, y)
            painter.drawEllipse(x - 2, y - 2, 4, 4)
    elif name == "table":
        painter.drawRoundedRect(3, 5, 18, 14, 2, 2)
        painter.drawLine(3, 10, 21, 10)
        painter.drawLine(9, 10, 9, 19)
    elif name == "win_min":
        painter.drawLine(6, 12, 18, 12)
    elif name == "win_max":
        painter.drawRect(6, 6, 12, 12)
    elif name == "win_restore":
        painter.drawRect(6, 9, 9, 9)
        painter.drawLine(9, 6, 18, 6)
        painter.drawLine(18, 6, 18, 15)
    elif name == "win_close":
        painter.drawLine(7, 7, 17, 17)
        painter.drawLine(17, 7, 7, 17)


def line_icon(name, role, size=18):
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(size * 2 / 24.0, size * 2 / 24.0)
    pen = QPen(_icon_color(role), 1.65)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    _icon_painter(name, p)
    p.end()
    pm.setDevicePixelRatio(2)
    return QIcon(pm)
