"""Multi-account cookie manager (registry + per-account browser profiles).

Layout under DATA_DIR:

    accounts/
      index.json                     # {"active": "<id>", "accounts": {<id>: meta}}
      <id>/
        browser_profile/             # Chromium persistent context = the login state
        cookies_backup.json          # JSON cookie jar snapshot (survives profile loss)
        chat/                        # this account's chat.db, media/, conversations_list.json
        spark_profile/               # Selenium profile for the spark tool (engine ≠ Chromium)
        exports/                     # dated export snapshots for this account
      .pending/browser_profile/      # staging dir for an in-progress browser login

Each account meta:
    id, name, uid, sec_uid, avatar_url,
    created_at, last_used_at, source ("qr"|"cookie"|"legacy"), status

The legacy single-account layout (data/browser_profile) is migrated into a
"default" account the first time this module runs with a non-empty legacy
profile and no registered accounts, so existing installs keep their login.

All paths are resolved from common.paths at call time (like common.config) so
tests can repoint them.
"""
import json
import os
import re
import secrets
import shutil
import threading
import time

from dy_common import paths

INDEX_NAME = "index.json"
PENDING_DIRNAME = ".pending"

# IM user-info API: POST sec_user_ids=<JSON array> → {data: [{uid, nickname,
# avatar_thumb}]}. Cookies only, no msToken/a_bogus needed.
IM_USER_INFO_API = "https://www.douyin.com/aweme/v1/web/im/user/info/"

# Self profile API (GET, cookies only): returns the logged-in user's
# uid / sec_uid / nickname / avatar.
SELF_PROFILE_API = (
    "https://www.douyin.com/aweme/v1/web/user/profile/self/"
    "?device_platform=webapp&aid=6383&channel=channel_pc_web"
    "&version_code=170400&version_name=17.4.0&cookie_enabled=true"
    "&platform=PC&cookie_base_url=https://www.douyin.com/"
)

# Re-entrant: mutation helpers hold the lock while calling ensure_migrated().
_lock = threading.RLock()


# ── paths ──────────────────────────────────────────────────────────────

def accounts_dir() -> str:
    return paths.ACCOUNTS_DIR


def index_path() -> str:
    return os.path.join(paths.ACCOUNTS_DIR, INDEX_NAME)


def account_dir(account_id: str) -> str:
    return os.path.join(paths.ACCOUNTS_DIR, account_id)


def profile_dir(account_id: str) -> str:
    return os.path.join(account_dir(account_id), "browser_profile")


def cookies_backup_path(account_id: str) -> str:
    return os.path.join(account_dir(account_id), "cookies_backup.json")


def pending_dir() -> str:
    return os.path.join(paths.ACCOUNTS_DIR, PENDING_DIRNAME)


def pending_profile_dir() -> str:
    return os.path.join(pending_dir(), "browser_profile")


def legacy_profile_dir() -> str:
    return paths.BROWSER_PROFILE


# ── index IO ───────────────────────────────────────────────────────────

def _default_index() -> dict:
    return {"active": None, "accounts": {}}


def load_index() -> dict:
    try:
        with open(index_path(), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _default_index()
        data.setdefault("active", None)
        data.setdefault("accounts", {})
        return data
    except FileNotFoundError:
        return _default_index()
    except (json.JSONDecodeError, UnicodeDecodeError):
        # A corrupt index must not brick login: start fresh rather than crash.
        return _default_index()


def save_index(index: dict) -> None:
    path = index_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _new_account_id() -> str:
    return "acc_" + secrets.token_hex(4)


def _safe_dir_id(value: str) -> str:
    """Douyin uid used as the account folder name; never used as a raw path."""
    cleaned = re.sub(r"[^0-9A-Za-z_-]", "_", (value or "").strip())[:64]
    return cleaned.strip("._-")


# ── migration ──────────────────────────────────────────────────────────

def _dir_non_empty(path: str) -> bool:
    try:
        return os.path.isdir(path) and bool(os.listdir(path))
    except OSError:
        return False


def ensure_migrated() -> dict:
    """One-time migration of the legacy single profile into a default account.

    Runs whenever no accounts are registered yet but a non-empty legacy
    data/browser_profile exists (also covers "CLI login happened after a fresh
    index was written"). Idempotent afterwards.
    """
    with _lock:
        index = load_index()
        if index["accounts"]:
            return index
        legacy = legacy_profile_dir()
        if not _dir_non_empty(legacy):
            return index

        account_id = "default"
        dest = profile_dir(account_id)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(legacy, dest)
        now = int(time.time())
        index["accounts"][account_id] = {
            "id": account_id,
            "name": "默认账号",
            "uid": "",
            "sec_uid": "",
            "avatar_url": "",
            "created_at": now,
            "last_used_at": now,
            "source": "legacy",
            "status": "logged_in",
        }
        index["active"] = account_id
        save_index(index)
        paths.invalidate_active_cache()
        print("[i] 已将旧登录态迁移为账号「默认账号」（data/browser_profile → data/accounts/default/）")
        return index


def normalize_account_ids() -> dict:
    """Rename account folders from legacy/random ids to the Douyin uid.

    Stable uid folders keep the login profile, chat DB and exports together
    when display names change. Only runs when the target folder does not
    already exist; otherwise the account keeps its current id.
    """
    with _lock:
        index = ensure_migrated()
        changed = False
        for old_id, meta in list(index["accounts"].items()):
            uid = (meta.get("uid") or "").strip()
            new_id = _safe_dir_id(uid)
            if not new_id or new_id == old_id or new_id in index["accounts"]:
                continue
            old_dir = account_dir(old_id)
            new_dir = account_dir(new_id)
            if os.path.exists(new_dir):
                continue
            if os.path.isdir(old_dir):
                try:
                    os.rename(old_dir, new_dir)
                except OSError as e:
                    print(f"[!] 账号目录改名失败 {old_id} -> {new_id}: {e}")
                    continue
            meta["id"] = new_id
            index["accounts"][new_id] = meta
            del index["accounts"][old_id]
            if index["active"] == old_id:
                index["active"] = new_id
            changed = True
            print(f"[i] 账号目录已改为抖音号: {old_id} -> {new_id}")
        if changed:
            save_index(index)
            paths.invalidate_active_cache()
        return index


# ── queries ────────────────────────────────────────────────────────────

def list_accounts() -> list:
    """All accounts, active first, then by last_used_at desc."""
    index = ensure_migrated()
    accounts = list(index["accounts"].values())
    for meta in accounts:
        meta["active"] = meta["id"] == index["active"]
    accounts.sort(key=lambda m: (not m.get("active"), -m.get("last_used_at", 0)))
    return accounts


def get_account(account_id: str) -> dict | None:
    index = ensure_migrated()
    meta = index["accounts"].get(account_id)
    if meta is None:
        return None
    meta = dict(meta)
    meta["active"] = account_id == index["active"]
    return meta


def get_active() -> dict | None:
    index = ensure_migrated()
    account_id = index["active"]
    if not account_id or account_id not in index["accounts"]:
        return None
    meta = dict(index["accounts"][account_id])
    meta["active"] = True
    return meta


def active_profile_dir() -> str | None:
    """Browser profile dir the scraper/login should use, or None if no account."""
    active = get_active()
    if active:
        return profile_dir(active["id"])
    return None


# ── mutations ──────────────────────────────────────────────────────────

def create_account(*, name: str = "", uid: str = "", sec_uid: str = "",
                   avatar_url: str = "", source: str = "qr",
                   status: str = "logged_in") -> dict:
    with _lock:
        index = ensure_migrated()
        # Prefer the stable Douyin uid as the folder name so login state,
        # chat DB, media and exports live under one predictable directory.
        account_id = _safe_dir_id(uid)
        if not account_id or account_id in index["accounts"]:
            account_id = _new_account_id()
            while account_id in index["accounts"]:
                account_id = _new_account_id()
        now = int(time.time())
        meta = {
            "id": account_id,
            "name": name or "新账号",
            "uid": uid,
            "sec_uid": sec_uid,
            "avatar_url": avatar_url,
            "created_at": now,
            "last_used_at": now,
            "source": source,
            "status": status,
        }
        index["accounts"][account_id] = meta
        index["active"] = account_id  # newly added account becomes active
        save_index(index)
        paths.invalidate_active_cache()
        return dict(meta)


def update_account(account_id: str, **fields) -> dict | None:
    with _lock:
        index = ensure_migrated()
        meta = index["accounts"].get(account_id)
        if meta is None:
            return None
        meta.update(fields)
        meta["id"] = account_id
        save_index(index)
        paths.invalidate_active_cache()
        return dict(meta)


def set_active(account_id: str) -> dict | None:
    with _lock:
        index = ensure_migrated()
        if account_id not in index["accounts"]:
            return None
        index["active"] = account_id
        index["accounts"][account_id]["last_used_at"] = int(time.time())
        save_index(index)
        paths.invalidate_active_cache()
        return dict(index["accounts"][account_id])


def delete_account(account_id: str) -> str | None:
    """Remove an account and its profile. Returns the new active id (or None)."""
    with _lock:
        index = ensure_migrated()
        if account_id not in index["accounts"]:
            return None
        del index["accounts"][account_id]
        if index["active"] == account_id:
            remaining = sorted(
                index["accounts"].values(),
                key=lambda m: -m.get("last_used_at", 0),
            )
            index["active"] = remaining[0]["id"] if remaining else None
        save_index(index)
        paths.invalidate_active_cache()
        try:
            shutil.rmtree(account_dir(account_id), ignore_errors=True)
        except Exception as e:
            print(f"[!] 删除账号目录失败: {e}")
        return index["active"]


def ensure_account_dirs(account_id: str) -> str:
    dest = profile_dir(account_id)
    os.makedirs(dest, exist_ok=True)
    return dest


def clear_pending() -> None:
    # Best-effort: a leftover staging dir must never fail a login flow.
    try:
        shutil.rmtree(pending_dir(), ignore_errors=True)
    except Exception as e:
        print(f"[!] 清理登录暂存目录失败: {e}")


def save_cookies_backup(account_id: str, cookies: list) -> str:
    path = cookies_backup_path(account_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def load_restorable_cookies(account_id: str) -> list:
    """Cookies from the backup that can still be injected into a profile.

    Expired entries are dropped. Session cookies (expires <= 0) get a 30-day
    expiry, matching the cookie-import behavior, so restoring survives the
    next browser restart.
    """
    try:
        with open(cookies_backup_path(account_id), encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(data, list):
        return []
    now = time.time()
    usable = []
    has_session = False
    for cookie in data:
        if not isinstance(cookie, dict) or not cookie.get("name"):
            continue
        expires = cookie.get("expires", cookie.get("expirationDate"))
        if isinstance(expires, (int, float)) and 0 < expires < now:
            continue
        entry = dict(cookie)
        if not isinstance(expires, (int, float)) or expires <= 0:
            entry["expires"] = now + 30 * 86400
        usable.append(entry)
        if entry.get("name") == "sessionid" and entry.get("value"):
            has_session = True
    return usable if has_session else []


def display_name(meta: dict) -> str:
    return meta.get("name") or ("账号 " + str(meta.get("id", ""))[-4:])


# ── identity resolution (Playwright page in, meta fields out) ───────────

async def fetch_self_info(page) -> dict:
    """Best-effort identity of the logged-in user.

    Primary path: the self-profile API (cookies only, no signature) returns
    uid / sec_uid / nickname / avatar. Fallback: /user/self redirects to
    /user/<sec_uid>, then the IM user-info API resolves that sec_uid.
    Returns {} on any failure so account naming degrades gracefully.
    `page` is a Playwright page with a live douyin.com session.
    """
    import asyncio

    info: dict = {}
    try:
        if "douyin.com" not in (page.url or ""):
            await page.goto("https://www.douyin.com/",
                            wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)
        data = await page.evaluate("""async (api) => {
            try {
                const r = await fetch(api, {credentials: 'include'});
                const j = await r.json();
                if (!j || j.status_code !== 0 || !j.user) return null;
                const u = j.user;
                return {
                    uid: String(u.uid || ''),
                    sec_uid: u.sec_uid || '',
                    nickname: u.nickname || '',
                    avatar_url: (u.avatar_thumb && u.avatar_thumb.url_list
                                 && u.avatar_thumb.url_list[0]) || '',
                };
            } catch (e) { return null; }
        }""", SELF_PROFILE_API)
        if data and (data.get("uid") or data.get("nickname")):
            return data
    except Exception:
        pass

    # Fallback: resolve sec_uid from the /user/self redirect, then ask the
    # IM user-info API. ("self" means no redirect happened — not a real id.)
    try:
        await page.goto("https://www.douyin.com/user/self",
                        wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)
        m = re.search(r"/user/([^/?#]+)", page.url or "")
        sec_uid = m.group(1) if m else ""
        if not sec_uid or sec_uid == "self":
            return info
        info["sec_uid"] = sec_uid
        users = await page.evaluate("""async (args) => {
            const [api, secs] = args;
            try {
                const r = await fetch(api, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: 'sec_user_ids=' + encodeURIComponent(JSON.stringify(secs)),
                });
                if (!r.ok) return [];
                const j = await r.json();
                return (j.data || []).map(u => ({
                    uid: String(u.uid || ''),
                    nickname: u.nickname || '',
                    avatar_url: (u.avatar_thumb && u.avatar_thumb.url_list
                                 && u.avatar_thumb.url_list[0]) || '',
                })).filter(u => u.uid);
            } catch (e) { return []; }
        }""", [IM_USER_INFO_API, [sec_uid]])
        if users:
            u = users[0]
            info["uid"] = u.get("uid") or ""
            info["nickname"] = u.get("nickname") or ""
            info["avatar_url"] = u.get("avatar_url") or ""
    except Exception:
        pass
    return info


def finalize_account_from_pending(*, info: dict, cookies: list, source: str) -> dict:
    """Move the staged browser profile into a new (or same-uid) account.

    Re-adding an account that is already registered (same uid) replaces its
    profile instead of creating a duplicate — used by both the browser-popup
    login and the cookie import.
    """
    uid = (info.get("uid") or "").strip()
    existing = None
    if uid:
        for meta in list_accounts():
            if meta.get("uid") and meta.get("uid") == uid:
                existing = meta
                break
    pending = pending_profile_dir()
    if existing:
        account_id = existing["id"]
        dest = profile_dir(account_id)
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(pending, dest)
        if cookies:
            save_cookies_backup(account_id, cookies)
        meta = update_account(
            account_id,
            name=info.get("nickname") or existing.get("name") or "新账号",
            sec_uid=info.get("sec_uid") or existing.get("sec_uid") or "",
            avatar_url=info.get("avatar_url") or existing.get("avatar_url") or "",
            status="logged_in",
        )
        set_active(account_id)
        clear_pending()
        normalize_account_ids()
        return get_account(account_id) or get_active() or meta
    account = create_account(
        name=info.get("nickname") or "",
        uid=uid,
        sec_uid=info.get("sec_uid") or "",
        avatar_url=info.get("avatar_url") or "",
        source=source,
        status="logged_in",
    )
    dest = profile_dir(account["id"])
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(pending, dest)
    if cookies:
        save_cookies_backup(account["id"], cookies)
    clear_pending()
    normalize_account_ids()
    return get_account(account["id"]) or get_active() or account
