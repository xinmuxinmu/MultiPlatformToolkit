"""Per-account chat storage layout and one-time migration.

    accounts/<id>/
      browser_profile/          # login state
      cookies_backup.json
      chat/                     # live data for this account only
        chat.db
        media/{images,emoji,voice,avatars,videos}
        conversations_list.json
        debug/                  # chat-page diagnostics
      exports/<YYYYmmdd_HHMMSS>/  # one dated snapshot per export/collection

Legacy installs kept chat.db, media/ and conversations_list.json at the
DATA_DIR root. ``ensure_data_migrated()`` moves them into the active account's
chat/ directory exactly once. It must run before the DB is opened.
"""
import os
import shutil
import time

from dy_common import accounts, paths

CHAT_DIRNAME = "chat"
EXPORTS_DIRNAME = "exports"

_migrated = False


def chat_dir(account_id: str) -> str:
    return os.path.join(paths.account_data_dir(account_id), CHAT_DIRNAME)


def exports_root(account_id: str) -> str:
    return os.path.join(paths.account_data_dir(account_id), EXPORTS_DIRNAME)


def active_chat_dir() -> str | None:
    account_id = paths.active_account_id()
    return chat_dir(account_id) if account_id else None


def active_exports_root() -> str | None:
    account_id = paths.active_account_id()
    return exports_root(account_id) if account_id else None


def export_snapshot_dir(now: float | None = None) -> str:
    """Create and return a dated export snapshot directory."""
    root = active_exports_root() or os.path.join(paths.DATA_DIR, "exports")
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
    target = os.path.join(root, stamp)
    index = 2
    while os.path.exists(target):
        target = os.path.join(root, f"{stamp}_{index}")
        index += 1
    os.makedirs(target, exist_ok=True)
    return target


def _move(src: str, dst: str) -> bool:
    try:
        os.replace(src, dst)
        return True
    except OSError:
        try:
            shutil.move(src, dst)
            return True
        except Exception as e:
            print(f"[!] 迁移失败 {src} -> {dst}: {e}")
            return False


def _merge_dir(src: str, dst: str) -> bool:
    """Move a directory, replacing an empty destination if necessary."""
    if not os.path.exists(dst):
        return _move(src, dst)
    try:
        if not os.listdir(dst):
            os.rmdir(dst)
            return _move(src, dst)
    except OSError:
        pass
    print(f"[!] 目标目录已存在且非空，保留原位: {src}")
    return False


def ensure_data_migrated() -> None:
    """Move legacy root-level chat data into the active account's chat dir.

    Safe to call repeatedly and from every process: each item is skipped once
    its destination exists. Run before opening the DB (server startup and
    extractor startup).
    """
    global _migrated
    if _migrated:
        return
    account_id = paths.active_account_id()
    if not account_id:
        return
    target = chat_dir(account_id)
    legacy_db = os.path.join(paths.DATA_DIR, "chat.db")
    target_db = os.path.join(target, "chat.db")
    moved = []

    if os.path.exists(legacy_db) and not os.path.exists(target_db):
        os.makedirs(target, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            src = legacy_db + suffix
            if os.path.exists(src) and _move(src, target_db + suffix):
                moved.append(f"chat.db{suffix}")
        if os.path.exists(legacy_db):  # move failed; don't half-migrate media
            return

    legacy_media = os.path.join(paths.DATA_DIR, "media")
    target_media = os.path.join(target, "media")
    if os.path.isdir(legacy_media) and _merge_dir(legacy_media, target_media):
        moved.append("media/")

    legacy_list = os.path.join(paths.DATA_DIR, "conversations_list.json")
    target_list = os.path.join(target, "conversations_list.json")
    if os.path.exists(legacy_list) and not os.path.exists(target_list):
        os.makedirs(target, exist_ok=True)
        if _move(legacy_list, target_list):
            moved.append("conversations_list.json")

    if moved:
        print(
            f"[i] 已把聊天数据迁移到账号目录 {target}"
            f"（{', '.join(moved)}）"
        )
    _migrated = True
