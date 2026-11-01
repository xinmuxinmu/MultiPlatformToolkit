"""统一入口的界面设置（目前只有无头模式开关）。

存在 data/ui_settings.json，供各模块在启动浏览器前读取：
- 火花 Selenium 初始化 / 定时任务
- 抖音聊天导出（Playwright）
- 淘宝导出（Playwright）

登录流程（扫码/验证码）仍然必须有头，不受这个开关影响。
"""
import json
import os
import threading

from app.paths import DATA_ROOT

SETTINGS_FILE = DATA_ROOT / "ui_settings.json"
_SAVE_LOCK = threading.Lock()


THEMES = ("dark", "light", "ocean", "purple")
LANGS = ("zh", "en")


def load_settings() -> dict:
    data = {}
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    theme = str(data.get("theme") or "dark")
    lang = str(data.get("lang") or "zh")
    return {
        "headless": bool(data.get("headless", False)),
        "theme": theme if theme in THEMES else "dark",
        "lang": lang if lang in LANGS else "zh",
    }


def save_settings(settings: dict) -> dict:
    with _SAVE_LOCK:
        current = load_settings()
        if "headless" in settings:
            current["headless"] = bool(settings["headless"])
        if "theme" in settings and settings["theme"] in THEMES:
            current["theme"] = settings["theme"]
        if "lang" in settings and settings["lang"] in LANGS:
            current["lang"] = settings["lang"]
        try:
            DATA_ROOT.mkdir(parents=True, exist_ok=True)
            tmp = str(SETTINGS_FILE) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(current, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SETTINGS_FILE)
        except OSError:
            pass
        return current


def is_headless() -> bool:
    return load_settings()["headless"]
