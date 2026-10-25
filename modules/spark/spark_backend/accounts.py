"""火花账号模块：复用抖音聊天导出的统一账号体系。

聊天导出（Playwright）和火花助手（Selenium）用的是两个浏览器内核，profile
不能共用，但登录态可以共用：

    data/douyin_chat/accounts/<uid>/
      browser_profile/      # 聊天导出（Playwright Chromium）
      spark_profile/        # 火花助手（Selenium 驱动的 Edge/Chrome）
      cookies_backup.json   # 两边共用的一份 Cookie

所以账号列表、当前账号、Cookie 存档只有一个来源：``dy_common.accounts``。
火花这边的历史数据（tasks/groups/friends_cache/浏览器目录）在首次升级时
按账号 id 迁到统一账号下。
"""
import json
import os
import shutil
import sys
import time

# 统一仓库里 spark 和 douyin_chat 是兄弟目录；单独运行火花时也能找到。
_MODULES_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DOUYIN_CHAT_DIR = os.path.join(_MODULES_ROOT, "douyin_chat")
if os.path.isdir(_DOUYIN_CHAT_DIR) and _DOUYIN_CHAT_DIR not in sys.path:
    sys.path.insert(0, _DOUYIN_CHAT_DIR)

from dy_common import accounts as _chat_accounts  # noqa: E402

from spark_backend.config import (
    APP_HOME, DATA_DIR, config, save_config, normalize_browser_name,
)
from spark_backend import runtime


DEFAULT_ACCOUNT_ID = "default"
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
SPARK_PROFILE_DIRNAME = "spark_profile"

# 与聊天导出共用的自我信息接口（仅 Cookie，无需签名）。
SELF_PROFILE_API = _chat_accounts.SELF_PROFILE_API

# 兼容旧代码里的模块级列表；内容由 load_accounts() 原地刷新。
accounts = []


def _now_str(ts=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or time.time()))


def _as_ts(value):
    if isinstance(value, (int, float)):
        return value
    if not value:
        return 0
    try:
        return time.mktime(time.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return 0


# ── Cookie 格式转换 ────────────────────────────────────────────────────
# 统一存档使用 Playwright 的字段名（expires），Selenium 的 add_cookie 只认
# expiry，多余的键会让整条 Cookie 被拒。

_BROWSER_COOKIE_FIELDS = ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite")


def _to_browser_cookie(cookie: dict) -> dict | None:
    if not isinstance(cookie, dict) or not cookie.get("name"):
        return None
    out = {k: cookie[k] for k in _BROWSER_COOKIE_FIELDS if k in cookie}
    expiry = cookie.get("expiry", cookie.get("expires"))
    if isinstance(expiry, (int, float)) and expiry > 0:
        out["expiry"] = int(expiry)
    return out


def _to_storage_cookie(cookie: dict) -> dict | None:
    if not isinstance(cookie, dict) or not cookie.get("name"):
        return None
    out = {k: cookie[k] for k in _BROWSER_COOKIE_FIELDS if k in cookie}
    expiry = cookie.get("expiry", cookie.get("expires"))
    if isinstance(expiry, (int, float)) and expiry > 0:
        out["expires"] = float(expiry)
    return out


def _raw_cookies(account_id):
    try:
        with open(_chat_accounts.cookies_backup_path(account_id), encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [c for c in data if isinstance(c, dict) and c.get("name")]


def _cookies_for_browser(account_id):
    """可直接交给 Selenium add_cookie 的 Cookie；没有可用登录态时返回空。"""
    restorable = _chat_accounts.load_restorable_cookies(account_id)
    return [c for c in (_to_browser_cookie(x) for x in restorable) if c]


def _cookie_saved_at(account_id):
    try:
        ts = os.path.getmtime(_chat_accounts.cookies_backup_path(account_id))
    except OSError:
        return ""
    return _now_str(ts)


def _spark_view(meta):
    account_id = meta["id"]
    return {
        "id": account_id,
        "name": meta.get("name") or "",
        "nickname": meta.get("name") or "",
        "uid": meta.get("uid") or "",
        "avatar_url": meta.get("avatar_url") or "",
        # Selenium 可直接 add_cookie 的格式；过期/无 sessionid 时为空。
        "cookies": _cookies_for_browser(account_id),
        "cookie_saved_at": _cookie_saved_at(account_id),
        "created_at": _now_str(meta.get("created_at")) if meta.get("created_at") else "",
        "last_used_at": _now_str(meta.get("last_used_at")) if meta.get("last_used_at") else "",
    }


# ── 账号查询 ───────────────────────────────────────────────────────────

def load_accounts():
    accounts[:] = [_spark_view(m) for m in _chat_accounts.list_accounts()]
    return accounts


def save_accounts():
    """账号索引现在由 dy_common.accounts 落盘，这里保留为空操作。"""
    return True


def list_accounts():
    return [_spark_view(m) for m in _chat_accounts.list_accounts()]


def get_account(account_id):
    meta = _chat_accounts.get_account(account_id)
    return _spark_view(meta) if meta else None


def find_account_by_name(name):
    name = str(name or "").strip()
    if not name:
        return None
    for meta in _chat_accounts.list_accounts():
        if meta.get("name") == name:
            return _spark_view(meta)
    return None


def is_default_account(account_id):
    """最后一个账号视为默认账号（不可删除）。"""
    metas = _chat_accounts.list_accounts()
    return bool(metas) and len(metas) == 1 and metas[0]["id"] == account_id


def create_account(name):
    name = str(name or "").strip()[:40]
    if not name:
        return None, "账号备注名不能为空"
    if find_account_by_name(name):
        return None, f"账号「{name}」已存在，请换个备注名"
    meta = _chat_accounts.create_account(name=name, source="spark")
    return _spark_view(meta), None


def rename_account(account_id, name):
    name = str(name or "").strip()[:40]
    if not name:
        return None, "账号备注名不能为空"
    other = find_account_by_name(name)
    if other and other["id"] != account_id:
        return None, f"账号「{name}」已存在，请换个备注名"
    meta = _chat_accounts.update_account(account_id, name=name)
    if not meta:
        return None, "账号不存在"
    return _spark_view(meta), None


def delete_account(account_id):
    if is_default_account(account_id):
        return False, "默认账号不可删除（可以重命名）"
    if not _chat_accounts.get_account(account_id):
        return False, "账号不存在"
    purge_account_data(account_id)
    _chat_accounts.delete_account(account_id)
    return True, None


def active_account_id():
    aid = runtime.active_account_id
    if aid and _chat_accounts.get_account(aid):
        return aid
    active = _chat_accounts.get_active()
    return active["id"] if active else DEFAULT_ACCOUNT_ID


def active_account():
    account = get_account(active_account_id())
    if account:
        return account
    active = _chat_accounts.get_active()
    return _spark_view(active) if active else None


def set_active_account(account_id):
    if not _chat_accounts.set_active(account_id):
        return False
    runtime.active_account_id = account_id
    config["active_account_id"] = account_id
    save_config(config)
    return True


_applied_account_id = None


def apply_active_account():
    """启动时把火花的活动账号对齐到统一账号索引。

    统一应用里这个函数会被调用两次（spark_main 导入时 + 应用启动钩子），
    账号没变时直接跳过，避免重复卸载/恢复任务、日志里打两遍「已恢复 N 个定时任务」。
    """
    global _applied_account_id
    meta = _chat_accounts.get_active()
    saved = str(config.get("active_account_id") or "").strip()
    if not meta and saved:
        meta = _chat_accounts.set_active(saved) and _chat_accounts.get_active()
    account_id = meta["id"] if meta else DEFAULT_ACCOUNT_ID
    if meta and config.get("active_account_id") != account_id:
        config["active_account_id"] = account_id
        save_config(config)
    if _applied_account_id == account_id and runtime.active_account_id == account_id:
        return
    runtime.active_account_id = account_id
    try:
        from spark_backend import groups as group_store
        group_store.load_groups()
    except Exception as e:
        print(f"⚠️ 分组数据重载失败: {e}")
    try:
        # 账号 id 可能在启动迁移中变化（default → 抖音号），先卸掉旧账号的
        # 调度任务再按新账号恢复，避免任务带错 account_id 写回磁盘。
        from spark_backend.scheduler import unload_tasks, restore_tasks
        unload_tasks()
        restore_tasks()
    except Exception as e:
        print(f"⚠️ 定时任务恢复失败: {e}")
    _applied_account_id = account_id


# ── Cookie 存档（与聊天导出一份文件） ──────────────────────────────────

def save_account_cookies(account_id, cookies):
    cleaned = [c for c in (_to_storage_cookie(x) for x in cookies or []) if c]
    if not cleaned or not _chat_accounts.get_account(account_id):
        return False
    try:
        _chat_accounts.save_cookies_backup(account_id, cleaned)
        return True
    except OSError as e:
        print(f"⚠️ Cookie 存档失败: {e}")
        return False


def load_account_cookies(account_id):
    """给 Selenium 用的 Cookie 列表（已过滤过期项）。"""
    return _cookies_for_browser(account_id)


def clear_account_cookies(account_id):
    if not _chat_accounts.get_account(account_id):
        return
    try:
        _chat_accounts.save_cookies_backup(account_id, [])
    except OSError as e:
        print(f"⚠️ Cookie 清理失败: {e}")


def set_account_nickname(account_id, nickname):
    nickname = str(nickname or "").strip()[:40]
    if not nickname:
        return
    meta = _chat_accounts.get_account(account_id)
    if not meta or meta.get("name") == nickname:
        return
    _chat_accounts.update_account(account_id, name=nickname)


def set_account_identity(account_id, *, uid="", sec_uid="", avatar_url="", nickname=""):
    """登录后把抖音 uid/头像写回统一账号；目录改名留到下次启动做。"""
    fields = {}
    if uid:
        fields["uid"] = str(uid)
    if sec_uid:
        fields["sec_uid"] = str(sec_uid)
    if avatar_url:
        fields["avatar_url"] = str(avatar_url)
    if nickname:
        fields["name"] = str(nickname)[:40]
    if fields:
        _chat_accounts.update_account(account_id, **fields)


# ── 浏览器 profile ─────────────────────────────────────────────────────

def spark_profile_dir(account_id=None):
    aid = account_id or active_account_id()
    return os.path.join(_chat_accounts.account_dir(aid), SPARK_PROFILE_DIRNAME)


def account_profile_dir(account_id=None):
    return spark_profile_dir(account_id)


def _move_path(src, dst):
    if not os.path.exists(src) or os.path.exists(dst):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.replace(src, dst)
        return True
    except OSError:
        try:
            shutil.move(src, dst)
            return True
        except Exception as e:
            print(f"⚠️ 迁移浏览器目录失败 {src} -> {dst}: {e}")
            return False


def migrate_legacy_data():
    """把火花单账号时代的数据并到统一账号下（可重复执行）。"""
    meta = _chat_accounts.get_active()
    if not meta:
        return
    new_id = meta["id"]
    if new_id == DEFAULT_ACCOUNT_ID:
        return

    # 1) 定时任务 / 分组 / 好友缓存里的 default 归属改成统一账号 id
    for path in (
        os.path.join(DATA_DIR, "tasks.json"),
        os.path.join(DATA_DIR, "groups.json"),
    ):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                continue
            changed = False
            for item in data:
                if isinstance(item, dict) and not item.get("account_id"):
                    item["account_id"] = DEFAULT_ACCOUNT_ID
                if isinstance(item, dict) and item.get("account_id") == DEFAULT_ACCOUNT_ID:
                    item["account_id"] = new_id
                    changed = True
            if changed:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                print(f"[i] 火花账号数据已迁移: {os.path.basename(path)} → {new_id}")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue

    # 2) 好友缓存（默认账号用 friends_cache.json）
    legacy_friends = os.path.join(DATA_DIR, "friends_cache.json")
    target_friends = os.path.join(DATA_DIR, f"friends_cache_{new_id}.json")
    if _move_path(legacy_friends, target_friends):
        print(f"[i] 火花好友缓存已迁移 → friends_cache_{new_id}.json")

    # 3) 旧浏览器 profile（edge_user_data 等）并到 <账号>/spark_profile
    target_profile = spark_profile_dir(new_id)
    browser_name = normalize_browser_name(config.get("browser_name"))
    legacy_profile = os.path.join(APP_HOME, f"{browser_name}_user_data")
    if _move_path(legacy_profile, target_profile):
        print(f"[i] 火花浏览器目录已迁移 → {target_profile}")

    # 4) 旧 accounts.json 里的 Cookie 补到统一 Cookie 存档
    if not _raw_cookies(new_id):
        try:
            with open(ACCOUNTS_FILE, encoding="utf-8") as f:
                legacy_accounts = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            legacy_accounts = []
        for item in legacy_accounts if isinstance(legacy_accounts, list) else []:
            if isinstance(item, dict) and item.get("id") == DEFAULT_ACCOUNT_ID and item.get("cookies"):
                save_account_cookies(new_id, item["cookies"])
                print("[i] 火花旧 Cookie 已并入统一存档")
                break


def purge_account_data(account_id):
    """删除账号时清理火花的任务/分组/好友缓存（账号目录由统一模块删除）。"""
    try:
        from spark_backend.scheduler import purge_account_tasks
        purge_account_tasks(account_id)
    except Exception as e:
        print(f"⚠️ 清理账号任务失败: {e}")
    try:
        from spark_backend import groups as group_store
        group_store.purge_account_groups(account_id)
    except Exception as e:
        print(f"⚠️ 清理账号分组失败: {e}")
    try:
        from spark_backend.friends_store import purge_account_cache
        purge_account_cache(account_id)
    except Exception as e:
        print(f"⚠️ 清理账号好友缓存失败: {e}")


migrate_legacy_data()
