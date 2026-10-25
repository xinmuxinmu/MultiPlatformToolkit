"""打开一个受主程序管理的浏览器窗口。

使用独立 profile 启动 Chrome/Edge 的 app 模式窗口：
- 不会混入用户日常浏览器的标签页，退出时也不会误关用户自己的窗口；
- 用窗口内链接/window.open 打开的页面都属于同一个浏览器进程，
  主程序退出时随 Job 一起结束。
"""

import os
import subprocess
import webbrowser
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
