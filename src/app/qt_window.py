"""Compatibility import for the native Python desktop entry point."""
from __future__ import annotations

WINDOW_TITLE = "多平台工具箱 · Python"


def available() -> bool:
    """Only Qt Widgets is required; the application never loads WebEngine."""
    try:
        import PySide6.QtWidgets  # noqa: F401
    except Exception:  # noqa: BLE001 - 缺依赖/缺 DLL 都回退
        return False
    return True


def run_app_window(url: str, title: str = WINDOW_TITLE, *, on_close=None) -> int:
    from app.desktop.window import run_app_window as run_native
    return run_native(url, title, on_close=on_close)
