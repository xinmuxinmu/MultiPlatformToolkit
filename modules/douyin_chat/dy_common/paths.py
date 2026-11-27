"""Single source of truth for every filesystem path in the project.

All state lives under DATA_DIR. Account-scoped data (chat DB, media,
conversation list, exports) lives under ACCOUNTS_DIR/<account_id>/ so each
Douyin account is fully isolated. Paths that depend on the active account are
resolved lazily via module ``__getattr__``; mutating account state calls
``invalidate_active_cache()`` so the next lookup sees the new account.

The legacy single-account layout at the DATA_DIR root is migrated into the
active account's directory by ``dy_common.storage.ensure_data_migrated()``.
"""
import os
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 统一应用通过环境变量把数据目录指向 data/douyin_chat；独立运行时沿用 <repo>/data。
DATA_DIR = os.path.abspath(
    os.environ.get("DOUYIN_CHAT_DATA_DIR") or os.path.join(REPO_ROOT, "data")
)

# Config / logs stay global: they describe the app, not one account.
CONFIG_PATH = os.path.join(DATA_DIR, "panel_config.json")
SCRAPE_LOG = os.path.join(DATA_DIR, "scrape.log")
VOICE_TRANSCRIPTION_LOG = os.path.join(DATA_DIR, "voice_transcription.log")
DISCOVER_LOG = os.path.join(DATA_DIR, "discover.log")

# Legacy single-account browser profile; migrated into ACCOUNTS_DIR on first use.
BROWSER_PROFILE = os.path.join(DATA_DIR, "browser_profile")

# 共享浏览器的 CDP 调试端口。火花「初始化」的浏览器和聊天导出自动拉起的
# 浏览器都带这个端口，导出（Playwright）与火花（Selenium debuggerAddress）
# 通过它接入同一个浏览器窗口，不再各开各的。使用方：
# modules/douyin_chat/dy_extractor/web_scraper.py、dy_backend/control_panel.py、
# modules/spark/spark_backend/browser_manager.py。
SHARED_BROWSER_CDP_PORT = 9341

# Multi-account root: ACCOUNTS_DIR/<id>/{browser_profile, cookies_backup.json,
# chat/, exports/}. The registry (active pointer + per-account meta) lives in
# ACCOUNTS_DIR/index.json.
ACCOUNTS_DIR = os.path.join(DATA_DIR, "accounts")

# Optional archived browser compatibility build, not used by the native desktop.
FRONTEND_DIST = os.path.abspath(os.path.join(REPO_ROOT, "..", "..", "legacy", "web_ui", "douyin", "dist"))

_ACTIVE_CACHE = {"ts": 0.0, "id": None}
_ACTIVE_CACHE_TTL = 1.0


def invalidate_active_cache() -> None:
    _ACTIVE_CACHE["ts"] = 0.0
    _ACTIVE_CACHE["id"] = None


def active_account_id() -> str | None:
    """Active account id, cached briefly so per-request DB connects stay cheap.

    The TTL also picks up account changes made by another process (CLI login).
    """
    now = time.monotonic()
    if _ACTIVE_CACHE["ts"] and now - _ACTIVE_CACHE["ts"] < _ACTIVE_CACHE_TTL:
        return _ACTIVE_CACHE["id"]
    from dy_common import accounts

    active = accounts.get_active()
    _ACTIVE_CACHE["id"] = active["id"] if active else None
    _ACTIVE_CACHE["ts"] = now
    return _ACTIVE_CACHE["id"]


def account_data_dir(account_id: str) -> str:
    return os.path.join(ACCOUNTS_DIR, account_id)


def chat_data_dir() -> str:
    """Live chat data root for the active account (legacy DATA_DIR if none)."""
    account_id = active_account_id()
    if account_id:
        return os.path.join(account_data_dir(account_id), "chat")
    return DATA_DIR


def exports_dir() -> str:
    account_id = active_account_id()
    if account_id:
        return os.path.join(account_data_dir(account_id), "exports")
    return os.path.join(DATA_DIR, "exports")


def __getattr__(name):
    """Resolve account-scoped paths at access time.

    ``from dy_common.paths import DB_PATH`` still binds the value at import
    time; new code should use ``paths.DB_PATH`` (attribute access) so account
    switches take effect. Every live call site does.
    """
    if name == "DB_PATH":
        return os.path.join(chat_data_dir(), "chat.db")
    if name == "CONVERSATIONS_LIST":
        return os.path.join(chat_data_dir(), "conversations_list.json")
    if name == "MEDIA_DIR":
        return os.path.join(chat_data_dir(), "media")
    if name == "IMAGES_DIR":
        return os.path.join(chat_data_dir(), "media", "images")
    if name == "EMOJI_DIR":
        return os.path.join(chat_data_dir(), "media", "emoji")
    if name == "VOICE_DIR":
        return os.path.join(chat_data_dir(), "media", "voice")
    if name == "AVATARS_DIR":
        return os.path.join(chat_data_dir(), "media", "avatars")
    if name == "VIDEOS_DIR":
        return os.path.join(chat_data_dir(), "media", "videos")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
