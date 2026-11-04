"""打开一个受主程序管理的浏览器窗口。

使用独立 profile 启动 Chrome/Edge 的 app 模式窗口：
- 不会混入用户日常浏览器的标签页，退出时也不会误关用户自己的窗口；
- 用窗口内链接/window.open 打开的页面都属于同一个浏览器进程，
  主程序退出时随 Job 一起结束。

另外提供 ``watch_app_window``：应用窗口被关闭后让后端服务自动退出，
避免关掉页面后留下黑框 cmd/Python 进程。
"""

import ctypes
import os
import subprocess
import threading
import time
import webbrowser
from ctypes import wintypes
from pathlib import Path

from app.paths import DATA_ROOT

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

_proc = None


def find_browser() -> str | None:
    for path in _BROWSERS:
        if Path(path).is_file():
            return path
    return None


def open_app_window(url: str, profile_dir: Path | None = None) -> bool:
    """打开独立浏览器窗口；成功返回 True，找不到浏览器时回退到默认浏览器。"""
    global _proc
    browser = find_browser()
    if not browser:
        webbrowser.open(url)
        return False

    profile = profile_dir or (DATA_ROOT / "app_browser")
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-features=Translate",
        "--window-size=1280,900",
    ]
    try:
        _proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        return True
    except OSError:
        webbrowser.open(url)
        return False


# app 窗口标题来自页面 <title>：壳页面固定以“多平台工具”开头。
_APP_TITLE_PREFIXES = ("多平台工具", "抖音 · 聊天记录与火花")
_APP_WINDOW_CLASS = "Chrome_WidgetWin"

if os.name == "nt":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.IsWindow.restype = wintypes.BOOL
    _user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
else:
    _user32 = None
    _EnumWindowsProc = None


def _window_text(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, len(buf))
    return buf.value


def find_app_window() -> int | None:
    """返回可见 app 模式窗口的句柄；没有则返回 None。"""
    if os.name != "nt":
        return None
    found: list[int] = []

    def _callback(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        if not _window_class(hwnd).startswith(_APP_WINDOW_CLASS):
            return True
        if _window_text(hwnd).startswith(_APP_TITLE_PREFIXES):
            found.append(hwnd)
            return False
        return True

    callback = _EnumWindowsProc(_callback)
    _user32.EnumWindows(callback, 0)
    return found[0] if found else None


def watch_app_window(
    server,
    *,
    appear_timeout: float = 120.0,
    interval: float = 1.0,
    grace: float = 3.0,
) -> threading.Thread:
    """等 app 窗口出现后持续监视；窗口关闭超过 grace 秒就让 uvicorn 退出。

    用窗口句柄而不是浏览器进程判断：关掉最后一个窗口时 Chrome/Edge 的
    后台进程可能还会残留一两秒，进程存活不足以代表页面还在。
    """
    thread = threading.Thread(
        target=_watch_app_window,
        args=(server, appear_timeout, interval, grace),
        name="app-window-watch",
        daemon=True,
    )
    thread.start()
    return thread


def _watch_app_window(server, appear_timeout: float, interval: float, grace: float) -> None:
    if os.name != "nt":
        return

    deadline = time.monotonic() + appear_timeout
    hwnd = None
    while time.monotonic() < deadline:
        hwnd = find_app_window()
        if hwnd:
            break
        if getattr(server, "should_exit", False):
            return
        time.sleep(interval)

    if not hwnd:
        print("未检测到应用窗口，本次不启用“关闭窗口自动退出”。")
        return

    missing_since = None
    while not getattr(server, "should_exit", False):
        if hwnd and _user32.IsWindow(hwnd) and _user32.IsWindowVisible(hwnd):
            missing_since = None
        else:
            # 窗口可能被重建（例如重新打开），先尝试重新绑定再判断关闭。
            replacement = find_app_window()
            if replacement:
                hwnd = replacement
                missing_since = None
            else:
                now = time.monotonic()
                if missing_since is None:
                    missing_since = now
                elif now - missing_since >= grace:
                    print("\n检测到应用窗口已关闭，后端服务随之退出。")
                    server.should_exit = True
                    return
        time.sleep(interval)
