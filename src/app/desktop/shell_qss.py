# 副本：src/gui_qt/main_window.py 的 _SHELL_QSS，原样复制，供样张外壳使用。
SHELL_QSS = """
QWidget#workbenchShell { border: 1px solid palette(shadow); }
QWidget#workbenchShell[windowMaximized="true"] { border: none; }
QWidget#workbenchSidebar { background: palette(base); border-right: 1px solid palette(shadow); }
QWidget#workbenchNavItems, QWidget#workbenchNavViewport, QScrollArea#workbenchNavScroll,
QWidget#workbenchNavGroups, QWidget#workbenchSearchResults { background: transparent; border: none; }
QPushButton#workbenchBrand { background: transparent; color: palette(text); border: none; text-align: left; font-size: 16px; font-weight: bold; padding: 0; }
QLabel#workbenchBrandMark { background: transparent; border: none; }
QLabel#workbenchBrandSub { background: transparent; color: palette(placeholder-text); border: none; font-size: 10px; }
QLineEdit#workbenchSearch { background: palette(window); border: 1px solid palette(shadow); border-radius: 8px; padding: 8px 10px; font-size: 12px; }
QLineEdit#workbenchSearch:focus { border-color: palette(highlight); }
QLabel#workbenchGroup { background: transparent; color: palette(placeholder-text); border: none; font-size: 10px; font-weight: bold; padding: 15px 10px 5px; }
QPushButton#workbenchNavButton { background: transparent; color: palette(text); border: 1px solid transparent; border-radius: 8px; text-align: left; padding: 8px 11px; font-size: 12px; }
QPushButton#workbenchNavButton:hover { background: palette(alternate-base); color: palette(link); }
QPushButton#workbenchNavButton:checked { background: palette(midlight); color: palette(link); border-color: palette(midlight); font-weight: bold; }
QPushButton#workbenchNavButton:focus { border-color: palette(highlight); }
QFrame#workbenchTopbar { background: palette(base); border: none; border-bottom: 1px solid palette(shadow); }
QLabel#workbenchBreadcrumb { background: transparent; }
QPushButton#workbenchDirectory { background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 0; }
QPushButton#workbenchDirectory:hover { background: palette(alternate-base); border-color: palette(shadow); }
QFrame#workbenchTopbarDivider { background: palette(shadow); border: none; }
QPushButton#windowControl { background: transparent; border: none; border-radius: 8px; padding: 0; }
QPushButton#windowControl:hover { background: palette(alternate-base); }
QPushButton#windowControl:pressed { background: palette(midlight); }
QPushButton#windowControlClose { background: transparent; border: none; border-radius: 8px; padding: 0; }
QPushButton#windowControlClose:hover { background: #e81123; }
QPushButton#windowControlClose:pressed { background: #c42b1c; }
QWidget#workbenchCookie { background: palette(window); border: 1px solid palette(shadow); border-radius: 10px; }
QLabel#workbenchCookieDot { background: palette(mid); border: none; border-radius: 4px; }
QLabel#workbenchCookieTitle { background: transparent; color: palette(text); font-size: 12px; font-weight: bold; border: none; }
QLabel#workbenchCookieToggleLabel { background: transparent; color: palette(placeholder-text); border: none; font-size: 11px; }
QLabel#workbenchCookieStatus { background: transparent; color: palette(placeholder-text); border: none; font-size: 11px; }
QProgressBar#workbenchCookieMeter { background: palette(shadow); border: none; border-radius: 4px; }
QProgressBar#workbenchCookieMeter::chunk { background: palette(link); border-radius: 4px; }
QPushButton#workbenchCookieManage { background: palette(button); color: palette(link); border: 1px solid palette(shadow); border-radius: 8px; padding: 5px 8px; font-size: 11px; }
QPushButton#workbenchCookieManage:hover { background: palette(midlight); border-color: palette(highlight); }
QPushButton#workbenchCookieManage:pressed { background: palette(midlight); border-color: palette(highlight); }
QPushButton#workbenchCookieRefresh { background: palette(button); border: 1px solid palette(shadow); border-radius: 8px; padding: 0; }
QPushButton#workbenchCookieRefresh:hover { background: palette(midlight); border-color: palette(highlight); }
QPushButton#workbenchCookieRefresh:pressed { background: palette(midlight); }
QPushButton#workbenchFooter { background: transparent; color: palette(placeholder-text); border: 1px solid transparent; border-radius: 7px; padding: 7px 2px; font-size: 11px; }
QPushButton#workbenchFooter:hover, QPushButton#workbenchFooter:checked { background: palette(midlight); color: palette(link); }
QLabel#workbenchEmptySearch { background: transparent; color: palette(placeholder-text); padding: 18px 10px; font-size: 12px; }
"""
