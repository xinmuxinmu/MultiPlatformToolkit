"""Control panel for managing scraper, viewer, and export."""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.request
from threading import Lock

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel

from dy_backend import database
from dy_common import accounts as _accts, config as _cfg, paths
from dy_backend.panel import notify as _notify
from dy_backend.panel.scheduler import parse_cron as _parse_cron, next_cron_run as _next_cron_run

control_router = APIRouter(prefix="/panel")

# The panel single-page app lives in backend/panel/static/panel.html and is
# loaded once at import; panel_page() serves it verbatim.
_PANEL_HTML_PATH = os.path.abspath(os.path.join(paths.REPO_ROOT, "..", "..", "web_ui", "douyin_panel.html"))
# Optional compatibility page; native API startup does not depend on archived UI files.
PANEL_HTML = "<html><meta charset=\"utf-8\"><body>请运行 python src/main.py 使用原生桌面界面。</body></html>"
if os.path.isfile(_PANEL_HTML_PATH):
    with open(_PANEL_HTML_PATH, encoding="utf-8") as _f:
        PANEL_HTML = _f.read()


# ── Persistent config (data/panel_config.json) — implemented in common.config ──
def _load_config():
    return _cfg.load_config()


def _save_config(cfg):
    _cfg.save_config(cfg)


def _utf8_subprocess_env() -> dict:
    """强制被重定向的 Python 子进程输出走 UTF-8（Windows 默认 GBK 会乱码）。"""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    try:
        from app.ui_settings import is_headless
        if is_headless():
            env["HEADLESS"] = "true"
    except Exception:
        pass
    return env


def _scraper_command(*extra_args: str) -> list:
    """Command that runs extract.py, frozen-aware.

    - Source run: [sys.executable, "-u", "extract.py", ...]
    - PyInstaller frozen: [DouyinChatScraper.exe, "-u", ...] next to the main exe.
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        return [os.path.join(base, "DouyinChatScraper.exe"), "-u", *extra_args]
    return [sys.executable, "-u", "extract.py", *extra_args]


def _read_utf8_or_gbk(path: str) -> str:
    """读日志：优先 UTF-8，回退到旧的 Windows GBK/GB18030 日志。"""
    with open(path, "rb") as file:
        raw = file.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("gb18030", errors="replace")


# Cooperative stop flag: the scraper watches this file and closes its browser
# gracefully instead of being hard-killed mid-write.
STOP_FLAG = os.path.join(paths.DATA_DIR, ".stop_scrape")

# ── Scrape job state ──
_scrape_state = {
    "status": "idle",  # idle | running | completed | failed
    "kind": "scrape",  # scrape | voice_backfill
    "started_at": None,
    "finished_at": None,
    "message": "",
    "process": None,
}

# Last real login probe. Returned instead of launching a second Chromium when
# a scrape/login already owns the profile.
_last_probe_state = {"status": "unknown", "has_cookies": False, "message": ""}


def _clear_stop_flag() -> None:
    try:
        os.remove(STOP_FLAG)
    except FileNotFoundError:
        pass


def _request_stop() -> None:
    try:
        os.makedirs(os.path.dirname(STOP_FLAG), exist_ok=True)
        with open(STOP_FLAG, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass

# ── Export state ──
_export_state = {
    "status": "idle",
    "file_path": None,
    "message": "",
}

# Database export/import replaces a single SQLite file. Keep the file swap
# serialized with snapshots and retain a rollback copy of the previous file.
_DATABASE_FILE_LOCK = Lock()
_DATABASE_IMPORT_MAX_BYTES = 2 * 1024 * 1024 * 1024

# ── Scheduler state ──
_scheduler_state = {
    "enabled": False,
    "schedule": "",
    "task": None,
    "next_run": None,
}

# ── Conversation discovery (refresh conv list) state ──
_discover_state = {
    "status": "idle",  # idle | running | completed | failed
    "message": "",
    "process": None,
    "started_at": None,
    "finished_at": None,
}

# ── Media backfill state ──
_backfill_state = {
    "status": "idle",  # idle | running | completed | failed
    "total": 0,
    "done": 0,
    "ok": 0,
    "failed": 0,
    "message": "",
    "started_at": None,
    "finished_at": None,
}

_video_backfill_state = {
    "status": "idle",
    "total": 0,
    "done": 0,
    "ok": 0,
    "failed": 0,
    "skipped": 0,
    "message": "",
    "started_at": None,
    "finished_at": None,
}

LOG_PATH = paths.SCRAPE_LOG
VOICE_LOG_PATH = paths.VOICE_TRANSCRIPTION_LOG
DISCOVER_LOG_PATH = paths.DISCOVER_LOG


async def restore_schedule_on_startup():
    """从 panel_config.json 恢复定时任务（容器重启后自动恢复）。"""
    cfg = _load_config()
    cron = cfg.get("schedule", "").strip()
    if not cron:
        return
    parsed = _parse_cron(cron)
    if not parsed:
        print(f"[scheduler] 配置中的 cron 表达式无效: {cron}", flush=True)
        return
    next_run = _next_cron_run(parsed)
    _scheduler_state["enabled"] = True
    _scheduler_state["schedule"] = cron
    _scheduler_state["next_run"] = next_run
    _scheduler_state["task"] = asyncio.create_task(
        _cron_loop(parsed, incremental=True)
    )
    from datetime import datetime
    next_str = datetime.fromtimestamp(next_run).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[scheduler] 已恢复定时任务: {cron}, 下次执行: {next_str}", flush=True)


class ScrapeRequest(BaseModel):
    incremental: bool = True
    filter: str = ""
    conversations: list[str] | None = None  # selected nicknames; overrides filter
    persist: bool = True  # False = 临时任务，不覆盖面板里保存的会话勾选


class VoiceBackfillRequest(BaseModel):
    conversations: list[str] | None = None  # selected nicknames; empty = all DB candidates


class ExportRequest(BaseModel):
    format: str = "jsonl"
    filter: str = ""
    conversations: list[str] | None = None  # selected nicknames; overrides filter


class ScheduleRequest(BaseModel):
    enabled: bool
    cron: str = ""  # cron expression: "0 0 * * *" or shorthand
    incremental: bool = True
    conversations: list[str] | None = None  # selected nicknames for scheduled scrape


class CustomFilterAction(BaseModel):
    action: str  # "add" | "remove"
    value: str


class CookieImportRequest(BaseModel):
    cookies: str  # JSON array from DevTools or "key=value; key=value" string


class PasswordRequest(BaseModel):
    password: str = ""  # empty = remove password


class SelectedUpdate(BaseModel):
    section: str  # "scraper" | "export" | "schedule"
    conversations: list[str]


@control_router.post("/api/password")
async def set_password(req: PasswordRequest):
    import hashlib
    cfg = _load_config()
    if req.password:
        cfg["password_hash"] = hashlib.sha256(req.password.encode()).hexdigest()
        _save_config(cfg)
        return {"status": "ok", "message": "密码已设置"}
    else:
        cfg.pop("password_hash", None)
        _save_config(cfg)
        return {"status": "ok", "message": "密码已清除"}


@control_router.get("/api/password/status")
async def password_status():
    cfg = _load_config()
    return {"has_password": bool(cfg.get("password_hash"))}


# ── Notifications (Server酱 / sct.ftqq.com) ──

class NotifyKeyRequest(BaseModel):
    sendkey: str = ""  # empty = remove


# Server酱 notification helpers live in backend/panel/notify.py.
_send_serverchan_sync = _notify.send_serverchan_sync
_build_failure_desp = _notify.build_failure_desp
_notify_on_failure = _notify.notify_on_failure


@control_router.post("/api/notify/serverchan")
async def set_notify_key(req: NotifyKeyRequest):
    cfg = _load_config()
    key = req.sendkey.strip()
    if key:
        cfg["notify_serverchan_key"] = key
        _save_config(cfg)
        return {"status": "ok", "message": "SendKey 已保存"}
    cfg.pop("notify_serverchan_key", None)
    _save_config(cfg)
    return {"status": "ok", "message": "SendKey 已清除"}


@control_router.get("/api/notify/serverchan/status")
async def notify_status():
    cfg = _load_config()
    return {"has_key": bool(cfg.get("notify_serverchan_key"))}


@control_router.post("/api/notify/test")
async def notify_test():
    cfg = _load_config()
    sendkey = (cfg.get("notify_serverchan_key") or "").strip()
    if not sendkey:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "未配置 SendKey"},
        )
    ok, msg = await asyncio.to_thread(
        _send_serverchan_sync, sendkey,
        "抖音聊天导出 · 测试通知",
        "如果你收到这条消息，说明 Server酱 配置正常。",
    )
    return {"status": "ok" if ok else "error", "message": msg}


# ── Media download toggle + backfill ──

class DownloadImagesToggle(BaseModel):
    enabled: bool


@control_router.get("/api/config/download-images")
async def get_download_images():
    return {"enabled": bool(_load_config().get("download_images"))}


@control_router.post("/api/config/download-images")
async def set_download_images(req: DownloadImagesToggle):
    cfg = _load_config()
    cfg["download_images"] = bool(req.enabled)
    _save_config(cfg)
    return {"status": "ok", "enabled": cfg["download_images"]}


@control_router.get("/api/media/backfill/status")
async def backfill_status():
    return {
        "status": _backfill_state["status"],
        "total": _backfill_state["total"],
        "done": _backfill_state["done"],
        "ok": _backfill_state["ok"],
        "failed": _backfill_state["failed"],
        "message": _backfill_state["message"],
        "started_at": _backfill_state["started_at"],
        "finished_at": _backfill_state["finished_at"],
    }


@control_router.post("/api/media/backfill")
async def backfill_start():
    if _backfill_state["status"] == "running":
        return JSONResponse({"error": "Backfill already running"}, status_code=409)
    # Mark running synchronously before spawning so two rapid POSTs can't both
    # pass the 409 check (the coroutine sets it too, but that races).
    _backfill_state["status"] = "running"
    asyncio.create_task(_run_backfill())
    return {"status": "started"}


async def _run_backfill():
    """Download all historical image/emoji media that has a URL but no local file.

    - 表情 (msg_type=2): 直接下载 media_url
    - 图片 (msg_type=3): 从 raw_data 取 origin_url + skey，AES-GCM 解密后保存
    """
    _backfill_state.update({
        "status": "running", "total": 0, "done": 0, "ok": 0, "failed": 0,
        "message": "扫描数据库...", "started_at": time.time(), "finished_at": None,
    })
    try:
        # Imports inside try: a failed import (e.g. playwright missing) must set
        # status='failed', not leave it stuck at 'running' (409 on every retry).
        from dy_extractor.web_scraper import _save_emoji, _save_image
        from dy_backend.database import get_db

        media_root = paths.MEDIA_DIR
        img_dir = os.path.join(media_root, "images")
        emoji_dir = os.path.join(media_root, "emoji")
        os.makedirs(img_dir, exist_ok=True)
        os.makedirs(emoji_dir, exist_ok=True)

        conn = get_db()
        rows = conn.execute(
            "SELECT msg_id, msg_type, media_url, raw_data FROM messages "
            "WHERE msg_type IN (2, 3) "
            "AND (media_local_path IS NULL OR media_local_path = '')"
        ).fetchall()
        _backfill_state["total"] = len(rows)
        _backfill_state["message"] = f"待下载 {len(rows)} 条"

        loop = asyncio.get_event_loop()
        for msg_id, msg_type, url, raw in rows:
            rel = None
            try:
                if msg_type == 2:
                    if url:
                        rel = await loop.run_in_executor(None, _save_emoji, url, emoji_dir)
                elif msg_type == 3:
                    try:
                        data = json.loads(raw) if raw else {}
                        cj = json.loads(data.get("content_json") or "{}")
                    except Exception:
                        cj = {}
                    ru = cj.get("resource_url") or {}
                    skey = ru.get("skey")
                    origin = (ru.get("origin_url_list") or [None])[0]
                    if skey and origin:
                        rel = await loop.run_in_executor(
                            None, _save_image, origin, skey, str(msg_id), img_dir,
                        )
                if rel:
                    conn.execute(
                        "UPDATE messages SET media_local_path = ? WHERE msg_id = ?",
                        (rel, msg_id),
                    )
                    conn.commit()
                    _backfill_state["ok"] += 1
                else:
                    _backfill_state["failed"] += 1
            except Exception:
                _backfill_state["failed"] += 1
            _backfill_state["done"] += 1
        conn.close()

        _backfill_state["status"] = "completed"
        _backfill_state["message"] = f"完成: 成功 {_backfill_state['ok']}，失败 {_backfill_state['failed']}"
    except Exception as e:
        _backfill_state["status"] = "failed"
        _backfill_state["message"] = f"错误: {e}"
    finally:
        _backfill_state["finished_at"] = time.time()


# ── 视频回填：调 batch_play_info 解析签名 URL 后落地 mp4 ──

@control_router.get("/api/media/videos/status")
async def video_backfill_status():
    return {
        "status": _video_backfill_state["status"],
        "total": _video_backfill_state["total"],
        "done": _video_backfill_state["done"],
        "ok": _video_backfill_state["ok"],
        "failed": _video_backfill_state["failed"],
        "skipped": _video_backfill_state["skipped"],
        "message": _video_backfill_state["message"],
        "started_at": _video_backfill_state["started_at"],
        "finished_at": _video_backfill_state["finished_at"],
    }


@control_router.get("/api/media/videos/pending")
async def video_backfill_pending():
    # Reuse the same Python filter as the backfill itself so the count matches
    # what will actually be processed (excludes text replies that quote a video).
    from dy_extractor.video_downloader import pending_videos
    from dy_backend.database import get_db
    conn = get_db()
    rows = pending_videos(conn)
    conn.close()
    return {"pending": len(rows)}


@control_router.post("/api/media/videos/backfill")
async def video_backfill_start():
    if _video_backfill_state["status"] == "running":
        return JSONResponse({"error": "video backfill already running"}, status_code=409)
    # Mark running synchronously before spawning to avoid the check-then-act race.
    _video_backfill_state["status"] = "running"
    asyncio.create_task(_run_video_backfill())
    return {"status": "started"}


async def _run_video_backfill():
    _video_backfill_state.update({
        "status": "running", "total": 0, "done": 0, "ok": 0, "failed": 0,
        "skipped": 0, "message": "启动浏览器解析视频 URL...",
        "started_at": time.time(), "finished_at": None,
    })

    def _cb(p):
        _video_backfill_state["total"] = p.get("total", _video_backfill_state["total"])
        _video_backfill_state["ok"] = p.get("ok", 0)
        _video_backfill_state["failed"] = p.get("fail", 0)
        _video_backfill_state["skipped"] = p.get("skipped", 0)
        _video_backfill_state["done"] = (
            _video_backfill_state["ok"] + _video_backfill_state["failed"] + _video_backfill_state["skipped"]
        )
        cur = p.get("current", "")
        _video_backfill_state["message"] = (
            f"已下载 {_video_backfill_state['ok']}，失败 {_video_backfill_state['failed']}，"
            f"跳过 {_video_backfill_state['skipped']} / {_video_backfill_state['total']}（{cur[-12:] if cur else ''}）"
        )

    try:
        # Import inside try so a failed import sets status='failed', not stuck 'running'.
        from dy_extractor.video_downloader import backfill as run_backfill
        result = await run_backfill(progress_cb=_cb)
        _video_backfill_state["status"] = "completed"
        _video_backfill_state["message"] = (
            f"完成：成功 {result['ok']}，失败 {result['fail']}，跳过 {result['skipped']} / {result['total']}"
        )
    except Exception as e:
        _video_backfill_state["status"] = "failed"
        _video_backfill_state["message"] = f"错误: {e}"
    finally:
        _video_backfill_state["finished_at"] = time.time()


@control_router.get("", response_class=HTMLResponse)
@control_router.get("/", response_class=HTMLResponse)
async def panel_page(request: Request):
    html = PANEL_HTML
    # 统一入口的子路径是运行期才知道的，给面板里的绝对路径补前缀。
    root_path = request.scope.get("root_path", "").rstrip("/")
    if root_path:
        shim = (
            "<script>(function(){var B=" + json.dumps(root_path) + ";"
            "var f=window.fetch;window.fetch=function(u,o){"
            "if(typeof u==='string'&&u.charAt(0)==='/')u=B+u;"
            "return f.call(this,u,o);};"
            "var w=window.open;window.open=function(u){"
            "if(typeof u==='string'&&u.charAt(0)==='/')u=B+u;"
            "return w.call(window,u);};"
            "document.addEventListener('DOMContentLoaded',function(){"
            "document.querySelectorAll('a[href^=\"/\"],img[src^=\"/\"]').forEach(function(el){"
            "var k=el.hasAttribute('href')?'href':'src';var a=el.getAttribute(k);"
            "if(a&&a.charAt(0)==='/'&&a.indexOf(B+'/')!==0)el.setAttribute(k,B+a);});});"
            "})();</script>"
        )
        html = html.replace("<head>", "<head>" + shim, 1)
    return HTMLResponse(
        content=html,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@control_router.get("/api/status")
async def panel_status():
    stats = database.get_stats()
    from dy_backend.database import get_db
    conn = get_db()
    row = conn.execute("SELECT MAX(last_message_time) FROM conversations").fetchone()
    last_time = row[0] if row and row[0] else 0
    convs = conn.execute("SELECT name FROM conversations ORDER BY last_message_time DESC").fetchall()
    conn.close()

    cfg = _load_config()

    return {
        "conversations": stats["conversations"],
        "messages": stats["messages"],
        "users": stats["users"],
        "last_message_time": last_time,
        "conversation_names": [c[0] for c in convs if c[0]],
        "custom_filters": cfg.get("custom_filters", []),
        "scrape": {
            "status": _scrape_state["status"],
            "kind": _scrape_state.get("kind", "scrape"),
            "started_at": _scrape_state["started_at"],
            "finished_at": _scrape_state["finished_at"],
            "message": _scrape_state["message"],
        },
        "export": {
            "status": _export_state["status"],
            "file_path": _export_state["file_path"],
            "message": _export_state["message"],
        },
        "scheduler": {
            "enabled": _scheduler_state["enabled"],
            "schedule": _scheduler_state["schedule"],
            "next_run": _scheduler_state["next_run"],
        },
    }


@control_router.post("/api/scrape")
async def start_scrape(req: ScrapeRequest):
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "Scrape already running"}, status_code=409)

    _clear_stop_flag()
    probe = await _probe_login_state()
    if not probe["has_cookies"]:
        return JSONResponse(
            {"error": "未检测到登录态，请先扫码登录或导入 Cookie"},
            status_code=400,
        )

    # Selected conversations (checkbox list) take precedence over free-text filter
    effective_filter = ",".join(req.conversations) if req.conversations else req.filter

    cmd = _scraper_command()
    if req.incremental:
        cmd.append("--incremental")
    if effective_filter:
        cmd.extend(["--filter", effective_filter])
    if _load_config().get("download_images"):
        cmd.append("--download-images")

    _scrape_state["status"] = "running"
    _scrape_state["kind"] = "scrape"
    _scrape_state["started_at"] = time.time()
    _scrape_state["finished_at"] = None
    _scrape_state["message"] = f"{'增量' if req.incremental else '全量'}采集"
    if req.conversations:
        _scrape_state["message"] += f" ({len(req.conversations)} 个会话)"
    elif req.filter:
        _scrape_state["message"] += f" (过滤: {req.filter})"

    # Persist selection so it's remembered next time（查看器里的单会话更新不覆盖）
    if req.conversations is not None and req.persist:
        cfg = _load_config()
        cfg["scraper_selected"] = list(req.conversations)
        _save_config(cfg)

    asyncio.create_task(_run_scrape(cmd))
    return {"status": "started", "message": _scrape_state["message"]}


@control_router.post("/api/voice-transcriptions/backfill")
async def start_voice_backfill(req: VoiceBackfillRequest | None = None):
    """Start a local-DB voice backfill without fetching chat history again."""
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "Scrape already running"}, status_code=409)

    _clear_stop_flag()
    conversations = list((req.conversations if req else None) or [])
    cmd = _scraper_command("--transcribe-voices")
    if conversations:
        cmd.extend(["--filter", ",".join(conversations)])

    _scrape_state["status"] = "running"
    _scrape_state["kind"] = "voice_backfill"
    _scrape_state["started_at"] = time.time()
    _scrape_state["finished_at"] = None
    _scrape_state["message"] = "补充历史语音转写"
    if conversations:
        _scrape_state["message"] += f" ({len(conversations)} 个会话)"
    asyncio.create_task(
        _run_scrape(cmd, log_path=VOICE_LOG_PATH, job_kind="voice_backfill")
    )
    return {"status": "started", "message": _scrape_state["message"]}


async def _run_scrape(cmd, *, log_path=None, job_kind="scrape"):
    # Reset here (not in start_scrape) so BOTH the manual and cron paths clear a
    # prior manual-stop flag; otherwise a scheduled scrape after a manual Stop
    # would be mislabeled '已停止' and its failure notification suppressed.
    _scrape_state["stopped"] = False
    try:
        log_path = log_path or LOG_PATH
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w", encoding="utf-8", newline="") as log_file:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=paths.REPO_ROOT,
                env=_utf8_subprocess_env(),
            )
            _scrape_state["process"] = proc
            await proc.wait()

        if _scrape_state.get("stopped"):
            # User-initiated stop: SIGTERM makes returncode nonzero, but this is
            # not a failure — don't report failed or push a WeChat notification.
            _scrape_state["status"] = "idle"
            _scrape_state["message"] = "已停止"
        elif proc.returncode == 0:
            _scrape_state["status"] = "completed"
            _scrape_state["message"] = (
                "语音转写补充完成" if job_kind == "voice_backfill" else "采集完成"
            )
        else:
            _scrape_state["status"] = "failed"
            label = "语音转写补充" if job_kind == "voice_backfill" else "采集"
            _scrape_state["message"] = f"{label}失败 (exit code {proc.returncode})"
    except Exception as e:
        _scrape_state["status"] = "failed"
        label = "语音转写补充" if job_kind == "voice_backfill" else "采集"
        _scrape_state["message"] = f"{label}错误: {e}"
    finally:
        _scrape_state["finished_at"] = time.time()
        _scrape_state["process"] = None
        if _scrape_state["status"] == "failed" and not _scrape_state.get("stopped"):
            label = "语音转写补充" if job_kind == "voice_backfill" else "采集"
            asyncio.create_task(_notify_on_failure(
                f"抖音聊天导出 · {label}失败",
                _build_failure_desp(_scrape_state["message"], log_path),
            ))


@control_router.get("/api/scrape/log")
async def scrape_log(lines: int = 50):
    if not os.path.exists(LOG_PATH):
        return {"log": ""}
    try:
        all_lines = _read_utf8_or_gbk(LOG_PATH).splitlines(keepends=True)
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"log": "".join(tail)}
    except Exception:
        return {"log": ""}


@control_router.get("/api/voice-transcriptions/log")
async def voice_transcription_log(lines: int = 80):
    if not os.path.exists(VOICE_LOG_PATH):
        return {"log": ""}
    try:
        all_lines = _read_utf8_or_gbk(VOICE_LOG_PATH).splitlines(keepends=True)
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"log": "".join(tail)}
    except Exception:
        return {"log": ""}


@control_router.get("/api/conversations/refresh/log")
async def discover_log(lines: int = 80):
    if not os.path.exists(DISCOVER_LOG_PATH):
        return {"log": ""}
    try:
        all_lines = _read_utf8_or_gbk(DISCOVER_LOG_PATH).splitlines(keepends=True)
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"log": "".join(tail)}
    except Exception:
        return {"log": ""}


@control_router.post("/api/scrape/stop")
async def stop_scrape():
    proc = _scrape_state.get("process")
    if proc and proc.returncode is None:
        _scrape_state["stopped"] = True  # tell _run_scrape this was intentional
        _request_stop()
        # Give the scraper time to finish its current step, close Chromium and
        # flush cookies; hard-kill only if it ignores the flag.
        for _ in range(80):
            if proc.returncode is not None:
                break
            await asyncio.sleep(0.1)
        if proc.returncode is None:
            proc.terminate()
        _scrape_state["status"] = "idle"
        _scrape_state["message"] = "已停止"
        return {"status": "stopped"}
    return {"status": "not_running"}


async def shutdown_background_tasks():
    """Best-effort graceful shutdown for app exit (Ctrl+C / server stop)."""
    procs = [
        p for p in (_scrape_state.get("process"), _discover_state.get("process"))
        if p is not None and p.returncode is None
    ]
    if procs:
        _request_stop()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all(p.returncode is not None for p in procs):
                break
            await asyncio.sleep(0.1)
        for p in procs:
            if p.returncode is None:
                p.terminate()
    for cleanup in (_login_cleanup, _browser_login_cleanup):
        try:
            await cleanup()
        except Exception:
            pass


@control_router.post("/api/custom-filter")
async def manage_custom_filter(req: CustomFilterAction):
    cfg = _load_config()
    filters = cfg.get("custom_filters", [])
    if req.action == "add" and req.value and req.value not in filters:
        filters.append(req.value)
    elif req.action == "remove" and req.value in filters:
        filters.remove(req.value)
    cfg["custom_filters"] = filters
    _save_config(cfg)
    return {"custom_filters": filters}


# ── Conversation discovery / selection ────────────────────────────

def _read_conv_list():
    path = paths.CONVERSATIONS_LIST
    if not os.path.exists(path):
        return {"discovered_at": 0, "items": []}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"discovered_at": 0, "items": []}


_login_probe_lock = asyncio.Lock()


def _profile_busy() -> bool:
    """True when another job already owns a Chromium profile."""
    return (
        _scrape_state["status"] == "running"
        or _discover_state["status"] == "running"
        or _login_state["status"] in ("starting", "waiting_scan")
        or _browser_login_state["status"] in ("starting", "waiting_scan", "finalizing")
    )


async def _restore_cookies_to_context(ctx, page) -> bool:
    active = _accts.get_active()
    if not active:
        return False
    cookies = _accts.load_restorable_cookies(active["id"])
    if not cookies:
        return False
    print(f"[*] 登录态缺失，尝试从备份恢复 {len(cookies)} 个 Cookie...")
    try:
        await ctx.add_cookies(cookies)
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        await asyncio.sleep(1)
        current = await ctx.cookies("https://www.douyin.com")
        if any(c["name"] == "sessionid" and c["value"] for c in current):
            print("[+] 已从备份恢复登录态")
            return True
    except Exception as e:
        print(f"[!] Cookie 备份恢复失败: {e}")
    return False


async def _probe_shared_browser_login() -> dict | None:
    """共享浏览器（火花初始化 / 自动拉起的那个）在线时直接读它的登录态。

    返回 None 表示不在线（没有共享浏览器），走老的 profile 探测路径。
    在线时探测不再另起 Chromium——刮取子进程会接入同一个浏览器。
    """
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{paths.SHARED_BROWSER_CDP_PORT}/json/version",
                timeout=1) as r:
            r.read()
    except Exception:
        return None
    try:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{paths.SHARED_BROWSER_CDP_PORT}", timeout=5000)
            ctx = browser.contexts[0] if browser.contexts else None
            if not ctx:
                return None
            cookies = await ctx.cookies("https://www.douyin.com")
            has_login = any(c["name"] == "sessionid" and c["value"] for c in cookies)
            if has_login:
                # 登录态轮换后同步刷新账号备份，profile 探测的恢复路径不落后
                active = _accts.get_active()
                if active:
                    _accts.save_cookies_backup(active["id"], cookies)
            return {"status": "logged_in" if has_login else "expired",
                    "has_cookies": has_login}
        finally:
            await pw.stop()
    except Exception:
        return None


async def _probe_login_state(profile_dir: str | None = None) -> dict:
    """Single source of truth for whether a persistent profile is logged in.

    Always launches Chromium against the given profile (default: the active
    account's) and reads cookies via Playwright. This intentionally goes
    through the same code path Chromium uses internally so we never disagree
    with what the actual scraper sees (whatever path the cookies DB lives at,
    WAL checkpoints, format migrations — all handled by Chromium itself).

    Returns one of:
        {"status": "logged_in",  "has_cookies": True}
        {"status": "expired",    "has_cookies": False}
        {"status": "no_profile", "has_cookies": False}
        {"status": "error",      "has_cookies": False, "message": "..."}

    Serialized via a module-level lock so the badge poll and the
    refresh/scrape preconditions can't race to launch two Chromium
    instances on the same profile (which would lock-conflict).
    """
    async with _login_probe_lock:
        if profile_dir is None:
            shared = await _probe_shared_browser_login()
            if shared is not None:
                _last_probe_state.update(shared)
                return shared
        if profile_dir is None and _profile_busy():
            # Never launch a second Chromium against a profile a scrape/login
            # is using. The cached result is the last real probe.
            return {**_last_probe_state, "busy": True}

        target = profile_dir or _active_profile_dir()
        has_profile = os.path.isdir(target) and os.listdir(target)
        if not has_profile:
            result = {"status": "no_profile", "has_cookies": False}
            if profile_dir is None:
                _last_probe_state.update(result)
            return result
        try:
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            try:
                ctx = await pw.chromium.launch_persistent_context(
                    target, headless=True,
                    viewport={"width": 1400, "height": 900}, locale="zh-CN",
                    args=["--disable-blink-features=AutomationControlled"],
                )
                try:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
                    await asyncio.sleep(2)
                    cookies = await ctx.cookies("https://www.douyin.com")
                    has_login = any(c["name"] == "sessionid" and c["value"] for c in cookies)
                    if not has_login and profile_dir is None:
                        if await _restore_cookies_to_context(ctx, page):
                            cookies = await ctx.cookies("https://www.douyin.com")
                            has_login = any(
                                c["name"] == "sessionid" and c["value"] for c in cookies)
                    if has_login and profile_dir is None:
                        # Keep the fallback snapshot aligned with the live
                        # session (Douyin rotates session cookies over time).
                        try:
                            active = _accts.get_active()
                            if active:
                                _accts.save_cookies_backup(
                                    active["id"], await ctx.cookies())
                        except Exception:
                            pass
                    result = {
                        "status": "logged_in" if has_login else "expired",
                        "has_cookies": has_login,
                    }
                    if profile_dir is None:
                        _last_probe_state.update(result)
                    return result
                finally:
                    await ctx.close()
            finally:
                await pw.stop()
        except Exception as e:
            result = {"status": "error", "has_cookies": False, "message": str(e)}
            if profile_dir is None:
                _last_probe_state.update(result)
            return result


@control_router.post("/api/conversations/refresh")
async def refresh_conversations():
    """Run a lightweight scrape that only enumerates the conversation list."""
    if _discover_state["status"] == "running":
        return JSONResponse({"error": "Refresh already running"}, status_code=409)
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "Scraper is running — stop it first"}, status_code=409)

    _clear_stop_flag()

    # Pre-check: don't spawn the 3-minute browser wait if we already know
    # there's no usable session. Uses the same Playwright probe as the
    # login badge so the two never disagree.
    probe = await _probe_login_state()
    if not probe["has_cookies"]:
        return JSONResponse(
            {"error": "未检测到登录态，请先扫码登录或导入 Cookie"},
            status_code=400,
        )

    _discover_state["status"] = "running"
    _discover_state["message"] = "正在加载会话列表..."
    _discover_state["started_at"] = time.time()
    _discover_state["finished_at"] = None

    cmd = _scraper_command("--list-conversations")
    asyncio.create_task(_run_discover(cmd))
    return {"status": "started"}


async def _run_discover(cmd):
    proc = None
    try:
        os.makedirs(os.path.dirname(DISCOVER_LOG_PATH), exist_ok=True)
        with open(DISCOVER_LOG_PATH, "w", encoding="utf-8", newline="") as log_file:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=paths.REPO_ROOT,
                env=_utf8_subprocess_env(),
            )
            _discover_state["process"] = proc
            await proc.wait()

        if proc.returncode == 0:
            data = _read_conv_list()
            count = len(data.get("items", []))
            _discover_state["status"] = "completed"
            _discover_state["message"] = f"发现 {count} 个会话"
        elif proc.returncode == 2:
            _discover_state["status"] = "failed"
            _discover_state["message"] = "未检测到登录态，请先扫码或导入 Cookie"
        else:
            _discover_state["status"] = "failed"
            _discover_state["message"] = f"刷新失败 (exit {proc.returncode})"
    except Exception as e:
        _discover_state["status"] = "failed"
        _discover_state["message"] = f"刷新错误: {e}"
        # Best-effort: kill any lingering subprocess so it doesn't pin the state
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
    finally:
        _discover_state["finished_at"] = time.time()
        _discover_state["process"] = None
        # Defensive: ensure status is never left at "running" when this coroutine exits
        if _discover_state["status"] == "running":
            _discover_state["status"] = "failed"
            _discover_state["message"] = _discover_state["message"] or "刷新中断"


@control_router.get("/api/conversations/refresh/status")
async def refresh_status():
    data = _read_conv_list()
    return {
        "status": _discover_state["status"],
        "message": _discover_state["message"],
        "started_at": _discover_state["started_at"],
        "finished_at": _discover_state["finished_at"],
        "discovered_at": data.get("discovered_at", 0),
        "items": data.get("items", []),
    }


@control_router.post("/api/conversations/refresh/stop")
async def refresh_stop():
    proc = _discover_state.get("process")
    if proc and proc.returncode is None:
        proc.terminate()
        _discover_state["status"] = "idle"
        _discover_state["message"] = "已停止"
        return {"status": "stopped"}
    # No live process — if state is still "running", force-reset (was stuck)
    if _discover_state["status"] == "running":
        _discover_state["status"] = "idle"
        _discover_state["message"] = "已重置"
        _discover_state["finished_at"] = time.time()
        return {"status": "reset"}
    return {"status": "not_running"}


@control_router.get("/api/spark-friends")
async def spark_friends():
    """火花（续火花）任务里配置的好友，供采集面板一键同步会话勾选。

    两个抖音功能共用一套会话设置：火花侧维护「昵称 + sec_uid」，导出侧
    用同一份名单定位会话，改名/标题带火花天数也不会失配。优先用定时任务
    名单（用户实际配置的目标）；还没建任务时退回火花好友缓存。
    """
    from dy_common.spark_bridge import (
        load_spark_friends, load_spark_task_friends, spark_data_dir,
    )

    tasks = load_spark_task_friends()
    if tasks:
        friends, scope = tasks, "tasks"
    else:
        friends, scope = load_spark_friends(), "friends"
    items = [{"name": name, "sec_uid": sec_uid}
             for name, sec_uid in sorted(friends.items())]
    return {"friends": items, "scope": scope, "source": spark_data_dir()}


@control_router.get("/api/conversations/selected")
async def get_selected():
    cfg = _load_config()
    return {
        "scraper": cfg.get("scraper_selected", []),
        "export": cfg.get("export_selected", []),
        "schedule": cfg.get("schedule_selected", []),
    }


@control_router.post("/api/conversations/selected")
async def set_selected(req: SelectedUpdate):
    if req.section not in ("scraper", "export", "schedule"):
        return JSONResponse({"error": "invalid section"}, status_code=400)
    cfg = _load_config()
    cfg[f"{req.section}_selected"] = list(req.conversations)
    _save_config(cfg)
    return {"status": "ok", "selected": cfg[f"{req.section}_selected"]}


@control_router.post("/api/schedule")
async def set_schedule(req: ScheduleRequest):
    # Cancel existing scheduled task
    if _scheduler_state["task"] and not _scheduler_state["task"].done():
        _scheduler_state["task"].cancel()
        _scheduler_state["task"] = None

    _scheduler_state["enabled"] = req.enabled
    _scheduler_state["schedule"] = req.cron if req.enabled else ""
    _scheduler_state["next_run"] = None

    # Always persist the schedule selection so the cron loop + UI stay in sync
    cfg = _load_config()
    if req.conversations is not None:
        cfg["schedule_selected"] = list(req.conversations)

    if req.enabled and req.cron:
        parsed = _parse_cron(req.cron)
        if not parsed:
            return JSONResponse({"error": "无效的 cron 表达式（分 时 日 月 周）"}, status_code=400)

        next_run = _next_cron_run(parsed)
        _scheduler_state["next_run"] = next_run
        _scheduler_state["task"] = asyncio.create_task(
            _cron_loop(parsed, req.incremental)
        )
        cfg["schedule"] = req.cron
        _save_config(cfg)
        return {"status": "enabled", "cron": req.cron, "next_run": next_run}

    cfg["schedule"] = ""
    _save_config(cfg)
    return {"status": "disabled"}


# Cron parsing (_parse_cron / _next_cron_run) lives in backend/panel/scheduler.py.


async def _cron_loop(parsed: list, incremental: bool):
    """Run scrape on cron schedule."""
    try:
        while True:
            next_run = _next_cron_run(parsed)
            _scheduler_state["next_run"] = next_run
            wait_secs = next_run - time.time()
            if wait_secs > 0:
                await asyncio.sleep(wait_secs)
            if _scrape_state["status"] != "running":
                cmd = _scraper_command()
                if incremental:
                    cmd.append("--incremental")
                cfg = _load_config()
                if cfg.get("download_images"):
                    cmd.append("--download-images")
                # Preferred: schedule_selected (checkbox picks).
                # Fallback: custom_filters (legacy).
                # Fallback: all DB conversations (scrape everything we know).
                filters = cfg.get("schedule_selected") or cfg.get("custom_filters") or []
                if not filters:
                    from dy_backend.database import get_db
                    conn = get_db()
                    convs = conn.execute("SELECT name FROM conversations WHERE name IS NOT NULL AND name != ''").fetchall()
                    conn.close()
                    filters = [c[0] for c in convs]
                if filters:
                    cmd.extend(["--filter", ",".join(filters)])
                _scrape_state["status"] = "running"
                _scrape_state["kind"] = "scrape"
                _scrape_state["started_at"] = time.time()
                _scrape_state["finished_at"] = None
                filter_desc = f" (过滤: {','.join(filters[:5])}{'...' if len(filters) > 5 else ''})" if filters else " (全部会话)"
                _scrape_state["message"] = f"定时{'增量' if incremental else '全量'}采集{filter_desc}"
                await _run_scrape(cmd)
            # Wait at least 61 seconds to avoid re-trigger in same minute
            await asyncio.sleep(61)
    except asyncio.CancelledError:
        pass


@control_router.post("/api/export")
async def start_export(req: ExportRequest):
    if _export_state["status"] == "running":
        return JSONResponse({"error": "Export already running"}, status_code=409)

    _export_state["status"] = "running"
    _export_state["file_path"] = None
    _export_state["message"] = "正在导出..."

    # Persist selection
    if req.conversations is not None:
        cfg = _load_config()
        cfg["export_selected"] = list(req.conversations)
        _save_config(cfg)

    convs = list(req.conversations) if req.conversations else None
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _do_export, req.format, req.filter, convs)
    return {
        "status": _export_state["status"],
        "message": _export_state["message"],
        "file_path": _export_state["file_path"],
    }


@control_router.post("/api/database/import")
async def import_database(request: Request, account_id: str | None = None,
                          mode: str = "merge"):
    """Import a SQLite export into a chosen account, merging by default.

    merge（默认）：把文件里的会话/消息并进目标账号现有数据库，相同会话只补
    新消息（按 msg_id 去重），不删除任何现有记录。不同账号导出的同一个群聊
    conv_id 相同，所以能合并成一份完整记录。
    replace：保留原有的整库覆盖语义。
    """
    conflict = _database_job_conflict()
    if conflict:
        return JSONResponse({"error": conflict}, status_code=409)

    mode = (mode or "merge").strip().lower()
    if mode not in ("merge", "replace"):
        return JSONResponse({"error": "导入方式只能是 merge 或 replace"}, status_code=400)

    if not account_id:
        active = _accts.get_active()
        if not active:
            return JSONResponse({"error": "还没有任何账号，请先扫码登录"}, status_code=400)
        account_id = active["id"]
    account = _accts.get_account(account_id)
    if not account:
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    target_db = _account_database_path(account_id)

    content_length = request.headers.get("content-length")
    try:
        if content_length and int(content_length) > _DATABASE_IMPORT_MAX_BYTES:
            return JSONResponse({"error": "导入文件过大"}, status_code=413)
    except ValueError:
        pass

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    allowed_types = {"", "application/octet-stream", "application/x-sqlite3", "application/vnd.sqlite3"}
    if content_type not in allowed_types:
        return JSONResponse({"error": "请上传 SQLite 数据库文件"}, status_code=415)

    os.makedirs(paths.DATA_DIR, exist_ok=True)
    fd, staged_path = tempfile.mkstemp(
        prefix=".chat_database_import_", suffix=".db", dir=paths.DATA_DIR
    )
    size = 0
    try:
        with os.fdopen(fd, "wb") as staged:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > _DATABASE_IMPORT_MAX_BYTES:
                    return JSONResponse({"error": "导入文件过大"}, status_code=413)
                staged.write(chunk)

        if size == 0:
            return JSONResponse({"error": "导入文件为空"}, status_code=400)

        try:
            _validate_database_file(staged_path)
        except (OSError, sqlite3.Error, ValueError) as exc:
            return JSONResponse({"error": f"数据库校验失败: {exc}"}, status_code=400)

        account_name = account.get("name") or account_id
        try:
            if mode == "merge":
                work_path, stats = _prepare_merged_database(staged_path, target_db)
                try:
                    backup_name = _install_database(
                        work_path, db_path=target_db, account_id=account_id)
                finally:
                    try:
                        os.remove(work_path)
                    except FileNotFoundError:
                        pass
                message = (
                    f"已增量合并到账号「{account_name}」："
                    f"新增 {stats['messages_added']} 条消息、"
                    f"{stats['conversations_added']} 个会话"
                )
                if stats["messages_skipped"]:
                    message += f"（{stats['messages_skipped']} 条重复消息已跳过）"
            else:
                backup_name = _install_database(
                    staged_path, db_path=target_db, account_id=account_id)
                stats = {}
                message = f"已用导入文件覆盖账号「{account_name}」的数据库"
        except (OSError, sqlite3.Error, ValueError) as exc:
            return JSONResponse({"error": f"数据库导入失败: {exc}"}, status_code=500)

        if backup_name:
            message += f"；旧数据库已备份为 {backup_name}"
        return {
            "status": "ok",
            "message": message,
            "backup_file": backup_name,
            "account_id": account_id,
            "mode": mode,
            "stats": stats,
        }
    finally:
        try:
            os.remove(staged_path)
        except FileNotFoundError:
            pass


def _account_database_path(account_id: str) -> str:
    return os.path.join(paths.account_data_dir(account_id), "chat", "chat.db")


# 合并时按这个顺序写，保证外键（messages→conversations、voice→messages）成立
_MERGE_TABLES = {
    "users": ("uid", "nickname", "avatar_url", "unique_id"),
    "conversations": ("conv_id", "conv_type", "name", "participant_uids",
                      "last_message_time", "message_count", "avatar_url"),
    "messages": ("msg_id", "conv_id", "sender_uid", "sender_name", "content",
                 "msg_type", "media_url", "media_local_path", "timestamp", "seq",
                 "raw_data", "ref_msg"),
    "voice_transcriptions": ("msg_id", "message_id", "text_result", "status",
                             "error", "updated_at"),
}


def _prepare_merged_database(source_path: str, target_path: str):
    """把 source 合并进 target 的一份副本，返回 (合并后的临时库路径, 统计)。

    先快照目标库再合并，失败不会动线上数据库；合并完成后由 _install_database
    原子替换。消息按 msg_id 去重，现有记录一律保留。
    """
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    from dy_common.db import init_db
    if not os.path.exists(target_path):
        init_db(db_path=target_path)

    work_dir = os.path.dirname(target_path)
    fd, work_path = tempfile.mkstemp(
        prefix=".chat_database_merge_", suffix=".db", dir=work_dir)
    os.close(fd)
    try:
        # 用 SQLite backup 复制目标库（含 WAL 中的最新数据）
        src = sqlite3.connect(target_path)
        dst = sqlite3.connect(work_path)
        try:
            src.execute("PRAGMA busy_timeout=10000")
            src.backup(dst, pages=1000, sleep=0.05)
        finally:
            dst.close()
            src.close()

        # 补齐旧库可能缺的列/索引（ref_msg、avatar_url 等）
        init_db(db_path=work_path)

        def counts(conn, prefix):
            return {
                t: conn.execute(f"SELECT COUNT(*) FROM {prefix}{t}").fetchone()[0]
                for t in _MERGE_TABLES
            }

        conn = sqlite3.connect(work_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            before = counts(conn, "main.")
            conn.execute("ATTACH DATABASE ? AS src", (source_path,))
            try:
                src_message_count = conn.execute(
                    "SELECT COUNT(*) FROM src.messages").fetchone()[0]
                for table, wanted in _MERGE_TABLES.items():
                    main_cols = {r[1] for r in conn.execute(
                        f"PRAGMA main.table_info({table})")}
                    src_cols = {r[1] for r in conn.execute(
                        f"PRAGMA src.table_info({table})")}
                    cols = [c for c in wanted if c in main_cols and c in src_cols]
                    if not cols:
                        continue
                    col_sql = ", ".join(cols)
                    conn.execute(
                        f"INSERT OR IGNORE INTO main.{table} ({col_sql}) "
                        f"SELECT {col_sql} FROM src.{table}")

                # 已有行只补空字段，不覆盖目标账号的内容（昵称/头像等可能更新）
                conn.execute("""
                    UPDATE main.users SET
                        nickname = COALESCE(NULLIF(nickname, ''),
                            (SELECT nickname FROM src.users s WHERE s.uid = users.uid)),
                        avatar_url = COALESCE(NULLIF(avatar_url, ''),
                            (SELECT avatar_url FROM src.users s WHERE s.uid = users.uid)),
                        unique_id = COALESCE(NULLIF(unique_id, ''),
                            (SELECT unique_id FROM src.users s WHERE s.uid = users.uid))
                    WHERE EXISTS (SELECT 1 FROM src.users s WHERE s.uid = users.uid)
                """)
                conn.execute("""
                    UPDATE main.conversations SET
                        name = COALESCE(NULLIF(name, ''),
                            (SELECT name FROM src.conversations s
                             WHERE s.conv_id = conversations.conv_id)),
                        avatar_url = COALESCE(NULLIF(avatar_url, ''),
                            (SELECT avatar_url FROM src.conversations s
                             WHERE s.conv_id = conversations.conv_id))
                    WHERE EXISTS (SELECT 1 FROM src.conversations s
                                  WHERE s.conv_id = conversations.conv_id)
                """)

                # 批量并完后统一重算会话统计（消息数/最后消息时间）
                conn.execute("""
                    UPDATE main.conversations SET
                        message_count = (SELECT COUNT(*) FROM main.messages m
                                         WHERE m.conv_id = conversations.conv_id),
                        last_message_time = COALESCE(
                            (SELECT MAX(timestamp) FROM main.messages m
                             WHERE m.conv_id = conversations.conv_id), 0)
                """)

                check = conn.execute("PRAGMA main.quick_check").fetchone()
                if not check or check[0] != "ok":
                    raise ValueError("合并后的数据库完整性检查未通过")
                fk_errors = conn.execute("PRAGMA main.foreign_key_check").fetchall()
                if fk_errors:
                    raise ValueError("合并后的数据库外键检查未通过")
                conn.commit()
            finally:
                conn.execute("DETACH DATABASE src")
            after = counts(conn, "main.")
            # 把 WAL 合并回主文件，便于整体替换
            conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            conn.close()

        stats = {
            "messages_added": after["messages"] - before["messages"],
            "messages_skipped": max(0, src_message_count - (after["messages"] - before["messages"])),
            "conversations_added": after["conversations"] - before["conversations"],
            "source_messages": src_message_count,
        }
        return work_path, stats
    except Exception:
        try:
            os.remove(work_path)
        except OSError:
            pass
        raise


def _database_job_conflict() -> str | None:
    """Return a user-facing reason why replacing the DB is unsafe now."""
    if _scrape_state["status"] == "running":
        return "采集任务正在运行，请完成后再导入数据库"
    if _backfill_state["status"] == "running":
        return "历史图片下载正在运行，请完成后再导入数据库"
    if _video_backfill_state["status"] == "running":
        return "历史视频下载正在运行，请完成后再导入数据库"
    if _export_state["status"] == "running":
        return "导出任务正在运行，请完成后再导入数据库"
    return None


def _database_snapshot(output_path: str) -> None:
    """Create a consistent standalone SQLite snapshot, including WAL data."""
    temp_path = f"{output_path}.tmp"
    try:
        with _DATABASE_FILE_LOCK:
            source = sqlite3.connect(paths.DB_PATH)
            target = sqlite3.connect(temp_path)
            try:
                source.execute("PRAGMA busy_timeout=10000")
                source.backup(target, pages=1000, sleep=0.05)
            finally:
                target.close()
                source.close()
            os.replace(temp_path, output_path)
    except Exception:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass
        raise


def _export_relpath(path: str) -> str:
    return os.path.relpath(path, paths.DATA_DIR)


def _do_database_export() -> str:
    from dy_common.storage import export_snapshot_dir

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"chat_database_{timestamp}.db"
    snapshot_dir = export_snapshot_dir()
    output_path = os.path.join(snapshot_dir, filename)
    collision_index = 2
    while os.path.exists(output_path):
        filename = f"chat_database_{timestamp}_{collision_index}.db"
        output_path = os.path.join(snapshot_dir, filename)
        collision_index += 1
    _database_snapshot(output_path)
    return output_path


def _validate_database_file(path: str) -> None:
    """Validate an uploaded SQLite file before it can replace chat.db."""
    required_columns = {
        "users": {"uid"},
        "conversations": {"conv_id", "name"},
        "messages": {"msg_id", "conv_id", "raw_data"},
    }
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        check = conn.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise ValueError("数据库完整性检查未通过")
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise ValueError("数据库外键完整性检查未通过")
        for table, columns in required_columns.items():
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not row:
                raise ValueError(f"数据库缺少表: {table}")
            actual = {item[1] for item in conn.execute(f"PRAGMA table_info({table})")}
            missing = columns - actual
            if missing:
                raise ValueError(f"数据库表 {table} 缺少字段: {', '.join(sorted(missing))}")
    finally:
        conn.close()


def _move_if_exists(source: str, target: str) -> bool:
    if not os.path.exists(source):
        return False
    os.replace(source, target)
    return True


def _install_database(staged_path: str, db_path: str | None = None,
                      account_id: str | None = None) -> str | None:
    """Atomically install a validated DB into the target account and keep the
    previous one recoverable."""
    db_path = db_path or paths.DB_PATH
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    suffix = f"_{account_id}" if account_id else ""
    backup_path = os.path.join(
        paths.DATA_DIR, f"chat_database_before_import{suffix}_{timestamp}.db")
    suffixes = ("-wal", "-shm")
    moved_sidecars: list[tuple[str, str]] = []
    moved_db = False
    installed = False

    if os.path.exists(backup_path):
        index = 2
        while os.path.exists(backup_path):
            backup_path = os.path.join(
                paths.DATA_DIR,
                f"chat_database_before_import{suffix}_{timestamp}_{index}.db",
            )
            index += 1

    with _DATABASE_FILE_LOCK:
        try:
            if os.path.exists(db_path):
                os.replace(db_path, backup_path)
                moved_db = True
            for suffix in suffixes:
                old_sidecar = db_path + suffix
                backup_sidecar = backup_path + suffix
                if _move_if_exists(old_sidecar, backup_sidecar):
                    moved_sidecars.append((old_sidecar, backup_sidecar))
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            os.replace(staged_path, db_path)
            installed = True

            from dy_common.db import init_db
            init_db(db_path=db_path)
        except Exception:
            try:
                if os.path.exists(db_path) and installed:
                    os.remove(db_path)
                if moved_db and os.path.exists(backup_path):
                    os.replace(backup_path, db_path)
                for original, backup in reversed(moved_sidecars):
                    if os.path.exists(backup):
                        os.replace(backup, original)
            finally:
                raise

    return os.path.basename(backup_path) if moved_db else None


def _do_export(fmt: str, filter_name: str, conversations: list | None):
    try:
        from dy_extractor.exporter import ChatLabExporter, build_export_filename
        from dy_common.storage import export_snapshot_dir
        import zipfile

        snapshot_dir = export_snapshot_dir()

        if fmt == "database":
            output_path = _do_database_export()
            _export_state["file_path"] = _export_relpath(output_path)
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            _export_state["message"] = f"数据库导出完成 ({size_mb:.1f} MB)"
            _export_state["status"] = "completed"
            return

        # Decide targets
        if conversations:
            targets = conversations
        elif filter_name:
            targets = [filter_name]
        else:
            targets = [None]  # None = exporter picks latest

        if len(targets) <= 1:
            # Single file
            exporter = ChatLabExporter(
                conv_name=targets[0] or None,
                output_format=fmt,
                output_dir=snapshot_dir,
            )
            output_path = exporter.export()
            if not output_path or not os.path.exists(output_path):
                raise RuntimeError(f"未找到会话: {targets[0] or '(any)'}")
            _export_state["file_path"] = _export_relpath(output_path)
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            _export_state["message"] = f"导出完成 ({size_mb:.1f} MB)"
        else:
            # Multiple → bundle into a zip inside the dated snapshot
            tmp_dir = os.path.join(snapshot_dir, "_tmp")
            os.makedirs(tmp_dir, exist_ok=True)
            # Clear old tmp files
            for fn in os.listdir(tmp_dir):
                try:
                    os.remove(os.path.join(tmp_dir, fn))
                except Exception:
                    pass

            produced = []
            used_filenames = set()
            exported_at = int(time.time())
            for name in targets:
                filename = build_export_filename(name, fmt, exported_at)
                collision_index = 2
                while filename in used_filenames:
                    filename = build_export_filename(
                        name, fmt, exported_at, collision_index=collision_index
                    )
                    collision_index += 1
                used_filenames.add(filename)
                path = os.path.join(tmp_dir, filename)
                try:
                    ChatLabExporter(conv_name=name, output_format=fmt).export(path)
                    if os.path.exists(path):
                        produced.append((name, path))
                except Exception as e:
                    print(f"[-] 导出 {name} 失败: {e}")

            if not produced:
                raise RuntimeError("没有成功导出的会话")

            zip_path = os.path.join(snapshot_dir, "export.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for _, path in produced:
                    zf.write(path, arcname=os.path.basename(path))
            shutil.rmtree(tmp_dir, ignore_errors=True)

            _export_state["file_path"] = _export_relpath(zip_path)
            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            _export_state["message"] = f"导出完成 ({len(produced)} 个会话, {size_mb:.1f} MB)"

        _export_state["status"] = "completed"
    except Exception as e:
        _export_state["status"] = "failed"
        _export_state["message"] = f"导出失败: {e}"


@control_router.get("/api/export/download")
async def download_export():
    if not _export_state["file_path"]:
        return JSONResponse({"error": "No export file"}, status_code=404)
    root = os.path.realpath(paths.DATA_DIR)
    path = os.path.realpath(os.path.join(root, _export_state["file_path"]))
    if path != root and not path.startswith(root + os.sep):
        return JSONResponse({"error": "Invalid export path"}, status_code=400)
    if not os.path.isfile(path):
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(path, filename=os.path.basename(path))


# ── Login & multi-account cookie manager ──

import base64
import shutil


def _active_profile_dir() -> str:
    """Browser profile of the active account; legacy path when none exists."""
    profile = _accts.active_profile_dir()
    return profile if profile else paths.BROWSER_PROFILE


def _in_docker() -> bool:
    return os.path.exists("/.dockerenv") or bool(os.environ.get("DOUYIN_IN_DOCKER"))


async def _fetch_self_info(page) -> dict:
    """Delegate to common.accounts (shared with login.py)."""
    return await _accts.fetch_self_info(page)


def _finalize_account_from_pending(*, info: dict, cookies: list, source: str) -> dict:
    """Delegate to common.accounts (shared with login.py)."""
    return _accts.finalize_account_from_pending(
        info=info, cookies=cookies, source=source)


# ── Accounts API ──

class AccountSwitchRequest(BaseModel):
    id: str


class AccountRenameRequest(BaseModel):
    name: str


@control_router.get("/api/accounts")
async def accounts_list():
    """All registered accounts + which one is active."""
    accounts = _accts.list_accounts()
    active = _accts.get_active()
    return {
        "accounts": accounts,
        "active": active["id"] if active else None,
        "browser_login_supported": not _in_docker(),
        "docker": _in_docker(),
    }


@control_router.post("/api/accounts/switch")
async def accounts_switch(req: AccountSwitchRequest):
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "采集进行中，请先停止再切换账号"}, status_code=409)
    # 登录/采集会占用账号 profile，同时再开一个 Chromium 会互相踩登录态。
    if _profile_busy():
        return JSONResponse({"error": "登录或采集进行中，请稍后再切换账号"}, status_code=409)
    meta = _accts.set_active(req.id)
    if not meta:
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    probe = await _probe_login_state()
    return {"account": meta, "probe": probe}


@control_router.post("/api/accounts/{account_id}/rename")
async def accounts_rename(account_id: str, req: AccountRenameRequest):
    """统一账号中心里改备注名（账号数据和登录态不变）。"""
    if not _accts.get_account(account_id):
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    name = (req.name or "").strip()[:40]
    if not name:
        return JSONResponse({"error": "备注名不能为空"}, status_code=400)
    for meta in _accts.list_accounts():
        if meta["id"] != account_id and (meta.get("name") or "") == name:
            return JSONResponse({"error": "该备注名已被使用"}, status_code=409)
    updated = _accts.update_account(account_id, name=name)
    return {"account": updated}


@control_router.post("/api/accounts/browser-login/start")
async def accounts_browser_login_start():
    """Pop up a real browser window on the host for QR login."""
    if _browser_login_state["status"] in ("starting", "waiting_scan", "finalizing"):
        return JSONResponse({"error": "登录流程已在进行中"}, status_code=409)
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "请先停止采集再登录"}, status_code=409)
    if _in_docker():
        return JSONResponse(
            {"error": "Docker 环境无法弹出浏览器，请使用「远程扫码」或「导入 Cookie」"},
            status_code=400)
    _browser_login_state["status"] = "starting"
    _browser_login_state["message"] = "正在启动浏览器..."
    _browser_login_state["account"] = None
    asyncio.create_task(_browser_login_flow())
    return {"status": "started"}


@control_router.get("/api/accounts/browser-login/status")
async def accounts_browser_login_status():
    return {
        "status": _browser_login_state["status"],
        "message": _browser_login_state["message"],
        "account": _browser_login_state["account"],
    }


@control_router.post("/api/accounts/browser-login/cancel")
async def accounts_browser_login_cancel():
    if _browser_login_state["status"] not in ("starting", "waiting_scan", "finalizing"):
        return {"status": "idle"}
    _browser_login_state["status"] = "cancelled"
    _browser_login_state["message"] = "已取消"
    # The flow notices the status change, closes the browser and cleans up.
    for _ in range(50):
        if _browser_login_state["status"] != "cancelled":
            break
        await asyncio.sleep(0.1)
    return {"status": "cancelled"}


@control_router.post("/api/accounts/{account_id}/delete")
async def accounts_delete(account_id: str):
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "采集进行中，请先停止"}, status_code=409)
    meta = _accts.get_account(account_id)
    if not meta:
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    # 火花与聊天共用账号：聊天侧删除时顺带清掉火花的任务/分组/好友缓存。
    try:
        from spark_backend import accounts as spark_accounts
        spark_accounts.purge_account_data(account_id)
    except ImportError:
        pass  # 独立运行 douyin_chat 时 spark 模块不存在
    except Exception as e:
        print(f"⚠️ 清理火花账号数据失败: {e}")
    new_active = _accts.delete_account(account_id)
    return {"status": "deleted", "active": new_active}


@control_router.post("/api/accounts/{account_id}/refresh")
async def accounts_refresh(account_id: str):
    """Probe login state and refresh nickname/avatar for one account."""
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "采集进行中，请先停止"}, status_code=409)
    meta = _accts.get_account(account_id)
    if not meta:
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    probe = await _probe_login_state(_accts.profile_dir(account_id))
    fields = {"status": probe.get("status", "unknown")}
    if probe.get("status") == "logged_in":
        info = await _probe_account_identity(_accts.profile_dir(account_id))
        if info.get("nickname"):
            fields["name"] = info["nickname"]
        if info.get("avatar_url"):
            fields["avatar_url"] = info["avatar_url"]
        if info.get("uid"):
            fields["uid"] = info["uid"]
        if info.get("sec_uid"):
            fields["sec_uid"] = info["sec_uid"]
    meta = _accts.update_account(account_id, **fields) or meta
    return {"account": meta, "probe": probe}


async def _probe_account_identity(profile: str) -> dict:
    """Headless launch on one account's profile to re-read its identity."""
    from playwright.async_api import async_playwright
    try:
        pw = await async_playwright().start()
        try:
            ctx = await pw.chromium.launch_persistent_context(
                profile, headless=True,
                viewport={"width": 1400, "height": 900}, locale="zh-CN",
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                return await _accts.fetch_self_info(page)
            finally:
                await ctx.close()
        finally:
            await pw.stop()
    except Exception:
        return {}


# Browser-popup login (headful Chromium on the host machine).
_browser_login_state = {
    "status": "idle",  # idle | starting | waiting_scan | finalizing | done | failed | cancelled
    "message": "",
    "account": None,
    "_context": None,
    "_pw": None,
}


async def _browser_login_cleanup():
    try:
        if _browser_login_state["_context"]:
            await _browser_login_state["_context"].close()
    except Exception:
        pass
    try:
        if _browser_login_state["_pw"]:
            await _browser_login_state["_pw"].stop()
    except Exception:
        pass
    _browser_login_state["_context"] = None
    _browser_login_state["_pw"] = None


async def _browser_login_flow():
    """Headful QR login staged in .pending, finalized into an account."""
    from playwright.async_api import async_playwright

    _accts.clear_pending()
    os.makedirs(_accts.pending_profile_dir(), exist_ok=True)
    ctx = None
    try:
        pw = await async_playwright().start()
        _browser_login_state["_pw"] = pw
        ctx = await pw.chromium.launch_persistent_context(
            _accts.pending_profile_dir(),
            headless=False,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        _browser_login_state["_context"] = ctx
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        # Douyin's heavy JS can stall "domcontentloaded" past 30s even though
        # the page is already usable for scanning — wait only for commit and
        # rely on the cookie poll below (same tolerance as login.py).
        try:
            await page.goto("https://www.douyin.com/", wait_until="commit", timeout=60000)
        except Exception:
            pass

        _browser_login_state["status"] = "waiting_scan"
        _browser_login_state["message"] = "请在弹出的浏览器窗口中扫码登录（5 分钟内有效）"
        logged_in = False
        for _ in range(300):
            if _browser_login_state["status"] != "waiting_scan":
                break  # cancelled
            try:
                cookies = await ctx.cookies("https://www.douyin.com")
            except Exception:
                await asyncio.sleep(1)
                continue
            if any(c.get("name") == "sessionid" and c.get("value") for c in cookies):
                logged_in = True
                break
            await asyncio.sleep(1)

        if not logged_in:
            if _browser_login_state["status"] == "waiting_scan":
                _browser_login_state["status"] = "failed"
                _browser_login_state["message"] = "登录超时（5 分钟）"
            return

        # Logged in: flush cookies to disk, grab identity, finalize.
        _browser_login_state["status"] = "finalizing"
        _browser_login_state["message"] = "登录成功，正在保存账号..."
        await asyncio.sleep(4)
        info = await _fetch_self_info(page)
        try:
            cookies = await ctx.cookies()
        except Exception:
            cookies = []
        await ctx.close()
        _browser_login_state["_context"] = None
        ctx = None

        account = _finalize_account_from_pending(
            info=info, cookies=cookies, source="qr")
        _accts.clear_pending()
        _browser_login_state["status"] = "done"
        _browser_login_state["message"] = "已添加账号：" + _accts.display_name(account)
        _browser_login_state["account"] = account
    except Exception as e:
        _browser_login_state["status"] = "failed"
        _browser_login_state["message"] = f"登录错误: {e}"
    finally:
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass
        await _browser_login_cleanup()
        _accts.clear_pending()


_login_state = {
    "status": "idle",  # idle | starting | waiting_scan | logged_in | failed
    "screenshot": None,  # base64 png
    "message": "",
    "countdown": 0,
    "_context": None,
    "_pw": None,
}


@control_router.get("/api/login/check")
async def login_check():
    """Check login by actually opening browser and reading cookies."""
    return await _probe_login_state()


@control_router.post("/api/login/start")
async def login_start():
    if _login_state["status"] in ("starting", "waiting_scan"):
        return JSONResponse({"error": "已在登录流程中"}, status_code=409)
    # If scraper is running, reject
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "请先停止采集再登录"}, status_code=409)

    _login_state["status"] = "starting"
    _login_state["screenshot"] = None
    _login_state["message"] = "正在启动浏览器..."
    asyncio.create_task(_login_flow())
    return {"status": "started"}


@control_router.get("/api/login/status")
async def login_status():
    return {
        "status": _login_state["status"],
        "screenshot": _login_state["screenshot"],
        "message": _login_state["message"],
        "countdown": _login_state["countdown"],
    }


class MouseAction(BaseModel):
    action: str  # click, mousedown, mousemove, mouseup
    x: float
    y: float


class KeyAction(BaseModel):
    action: str  # press, type
    key: str = ""
    text: str = ""


@control_router.post("/api/login/mouse")
async def login_mouse(req: MouseAction):
    """Forward mouse events to the headless browser page."""
    ctx = _login_state.get("_context")
    if not ctx or _login_state["status"] not in ("waiting_scan",):
        return JSONResponse({"error": "No active login session"}, status_code=400)

    try:
        page = ctx.pages[0] if ctx.pages else None
        if not page:
            return JSONResponse({"error": "No page"}, status_code=400)

        mouse = page.mouse
        if req.action == "click":
            await mouse.click(req.x, req.y)
        elif req.action == "mousedown":
            await mouse.move(req.x, req.y)
            await mouse.down()
        elif req.action == "mousemove":
            await mouse.move(req.x, req.y)
        elif req.action == "mouseup":
            await mouse.up()
        else:
            return JSONResponse({"error": f"Unknown action: {req.action}"}, status_code=400)

        # Take a fresh screenshot after interaction
        await asyncio.sleep(0.15)
        png = await page.screenshot(type="png")
        _login_state["screenshot"] = base64.b64encode(png).decode()

        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@control_router.post("/api/login/keyboard")
async def login_keyboard(req: KeyAction):
    """Forward keyboard events to the headless browser page."""
    ctx = _login_state.get("_context")
    if not ctx or _login_state["status"] not in ("waiting_scan",):
        return JSONResponse({"error": "No active login session"}, status_code=400)

    try:
        page = ctx.pages[0] if ctx.pages else None
        if not page:
            return JSONResponse({"error": "No page"}, status_code=400)

        kb = page.keyboard
        if req.action == "type" and req.text:
            await kb.type(req.text)
        elif req.action == "press" and req.key:
            await kb.press(req.key)
        else:
            return JSONResponse({"error": "Invalid keyboard action"}, status_code=400)

        await asyncio.sleep(0.15)
        png = await page.screenshot(type="png")
        _login_state["screenshot"] = base64.b64encode(png).decode()
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@control_router.post("/api/login/cancel")
async def login_cancel():
    await _login_cleanup()
    _login_state["status"] = "idle"
    _login_state["message"] = "已取消"
    _login_state["screenshot"] = None
    return {"status": "cancelled"}


@control_router.post("/api/login/clear")
async def login_clear():
    """Remove the active account (profile + registry entry).

    Falls back to wiping the legacy profile dir when no account exists.
    """
    active = _accts.get_active()
    if active:
        new_active = _accts.delete_account(active["id"])
        return {"status": "cleared", "active": new_active}
    try:
        if os.path.isdir(paths.BROWSER_PROFILE):
            shutil.rmtree(paths.BROWSER_PROFILE, ignore_errors=True)
    except Exception as e:
        return JSONResponse({"error": f"清除失败: {e}"}, status_code=500)
    return {"status": "cleared", "active": None}


def _validate_cookie_entries(parsed: list[dict]) -> tuple[list[str], list[str]]:
    """Pre-flight check on parsed cookies. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    sids = [c for c in parsed if c["name"] == "sessionid"]
    if not sids:
        errors.append("Cookie 中未包含 sessionid，请确保已登录后再导出（cookie-editor 需全选导出）")
        return errors, warnings

    sid = sids[0]
    value = (sid.get("value") or "").strip()
    if not value:
        errors.append("sessionid 的值为空")
    elif len(value) < 16:
        warnings.append(f"sessionid 长度异常 ({len(value)} 字节)，可能被截断")

    domain = (sid.get("domain") or "").lstrip(".")
    if domain and domain != "douyin.com" and not domain.endswith(".douyin.com"):
        errors.append(
            f"sessionid 的 domain 是 .{domain}（应为 .douyin.com）"
            "—— 可能在子站点（iesdouyin.com 等）导出了，请回到 www.douyin.com 重导"
        )

    exp = sid.get("expires")
    if exp and exp > 0 and exp < time.time():
        errors.append("sessionid 已过期（expirationDate 在过去），请重新登录后再导出")

    if len(parsed) < 3:
        warnings.append(
            f"只解析出 {len(parsed)} 个 cookie，抖音通常需要 10+ 个才能完整工作，"
            "建议在 cookie-editor 里全选后再导出"
        )
    return errors, warnings


@control_router.post("/api/login/cookie-import")
async def login_cookie_import(req: CookieImportRequest):
    """Import cookies from browser DevTools or document.cookie string.

    The imported cookies are staged, verified, then saved as a new account
    (or replace the profile of an already-registered account with the same
    uid) and made active.
    """
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "采集进行中，请先停止"}, status_code=409)
    if _login_state["status"] in ("starting", "waiting_scan"):
        return JSONResponse({"error": "登录流程进行中，请先取消"}, status_code=409)
    if _browser_login_state["status"] in ("starting", "waiting_scan", "finalizing"):
        return JSONResponse({"error": "浏览器登录流程进行中，请先取消"}, status_code=409)

    raw = req.cookies.strip()
    if not raw:
        return JSONResponse({"error": "Cookie 数据为空"}, status_code=400)

    parsed: list[dict] = []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            for c in data:
                if not isinstance(c, dict) or not c.get("name"):
                    continue
                entry: dict = {
                    "name": c["name"],
                    "value": str(c.get("value", "")),
                    "domain": c.get("domain", ".douyin.com"),
                    "path": c.get("path", "/"),
                }
                exp = c.get("expirationDate") or c.get("expires")
                if exp:
                    entry["expires"] = float(exp)
                if c.get("httpOnly") is not None:
                    entry["httpOnly"] = bool(c["httpOnly"])
                if c.get("secure") is not None:
                    entry["secure"] = bool(c["secure"])
                # cookie-editor exports sameSite as lowercase enum.
                # Map "no_restriction" → "None" (cross-site allowed) — must NOT downgrade to Lax,
                # since some Douyin auth cookies require cross-site delivery for IM API calls.
                ss = (c.get("sameSite") or "").strip().lower()
                ss_map = {"no_restriction": "None", "none": "None",
                          "lax": "Lax", "strict": "Strict"}
                if ss in ss_map:
                    entry["sameSite"] = ss_map[ss]
                    # Playwright requires Secure=true when SameSite=None
                    if entry["sameSite"] == "None":
                        entry["secure"] = True
                parsed.append(entry)
        else:
            return JSONResponse({"error": "JSON 格式需为数组"}, status_code=400)
    except (json.JSONDecodeError, ValueError):
        for pair in raw.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            parsed.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".douyin.com",
                "path": "/",
            })

    if not parsed:
        return JSONResponse({"error": "未能解析出任何 Cookie"}, status_code=400)

    errors, warnings = _validate_cookie_entries(parsed)
    if errors:
        return JSONResponse({"error": "；".join(errors)}, status_code=400)

    # Session cookies (no expirationDate) get dropped on browser restart,
    # so the next login probe wouldn't see them. Pin a 30-day default.
    default_exp = time.time() + 30 * 86400
    for c in parsed:
        if "expires" not in c:
            c["expires"] = default_exp

    try:
        from playwright.async_api import async_playwright
        # Stage into .pending first: only finalize into an account after the
        # sessionid verifies, so a bad import never touches existing accounts.
        _accts.clear_pending()
        os.makedirs(_accts.pending_profile_dir(), exist_ok=True)
        pw = await async_playwright().start()
        ctx = await pw.chromium.launch_persistent_context(
            _accts.pending_profile_dir(),
            headless=True,
            viewport={"width": 1400, "height": 900},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        await asyncio.sleep(1)
        await ctx.add_cookies(parsed)
        cookies = await ctx.cookies("https://www.douyin.com")
        ok = "sessionid" in {c["name"] for c in cookies}
        all_cookies = await ctx.cookies()  # everything regardless of url, for diagnostics
        info = await _fetch_self_info(page) if ok else {}
        await ctx.close()
        await pw.stop()
        if ok:
            account = _finalize_account_from_pending(
                info=info, cookies=all_cookies, source="cookie")
            _accts.clear_pending()
            msg = f"成功导入 {len(parsed)} 个 Cookie，已保存为账号「{_accts.display_name(account)}」"
            if warnings:
                msg += "（注意：" + "；".join(warnings) + "）"
            return {"status": "ok", "message": msg, "count": len(parsed),
                    "warnings": warnings, "account": account}
        # Verification failed — diagnose why so the user knows what to fix.
        sid_other = [c for c in all_cookies if c["name"] == "sessionid"]
        if sid_other:
            wrong_domain = sid_other[0].get("domain", "?")
            return JSONResponse(
                {"error": f"sessionid 被加载到 domain={wrong_domain}，"
                          f"对 www.douyin.com 不生效。请确认 cookie 的 domain 是 .douyin.com"},
                status_code=400,
            )
        return JSONResponse(
            {"error": "sessionid 导入后无法在 douyin.com 读取到，"
                      "可能已被服务端注销，请重新登录后再导出"},
            status_code=400,
        )
    except Exception as e:
        return JSONResponse({"error": f"导入失败: {e}"}, status_code=500)


async def _login_cleanup():
    try:
        if _login_state["_context"]:
            await _login_state["_context"].close()
    except Exception:
        pass
    try:
        if _login_state["_pw"]:
            await _login_state["_pw"].stop()
    except Exception:
        pass
    _login_state["_context"] = None
    _login_state["_pw"] = None


async def _login_flow():
    """In-container: open headless browser, screenshot the page for QR scanning.

    Operates on the active account's profile (legacy path when no account is
    registered yet — it migrates to the default account on next startup).
    """
    target = _active_profile_dir()
    try:
        from playwright.async_api import async_playwright

        os.makedirs(target, exist_ok=True)
        pw = await async_playwright().start()
        _login_state["_pw"] = pw

        ctx = await pw.chromium.launch_persistent_context(
            target,
            headless=True,
            viewport={"width": 1400, "height": 900},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        _login_state["_context"] = ctx
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Navigate to Douyin
        _login_state["message"] = "正在打开抖音..."
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        await asyncio.sleep(2)

        # Check if already logged in
        cookies = await ctx.cookies("https://www.douyin.com")
        if any(c["name"] == "sessionid" and c["value"] for c in cookies):
            _login_state["status"] = "logged_in"
            _login_state["message"] = "已登录，无需扫码"
            await _login_cleanup()
            return

        # Try to click login button
        _login_state["status"] = "waiting_scan"
        _login_state["message"] = "正在获取二维码..."
        try:
            login_btn = await page.wait_for_selector(
                'button:has-text("登录")', timeout=5000
            )
            if login_btn:
                await login_btn.click()
                await asyncio.sleep(2)
        except Exception:
            pass

        # Poll: take screenshots and check cookies
        timeout_secs = 180
        for i in range(timeout_secs):
            if _login_state["status"] != "waiting_scan":
                break  # cancelled

            _login_state["countdown"] = timeout_secs - i

            # Screenshot
            png = await page.screenshot(type="png")
            _login_state["screenshot"] = base64.b64encode(png).decode()
            _login_state["message"] = f"请用抖音 APP 扫码 ({timeout_secs - i}s)"

            # Check login
            cookies = await ctx.cookies("https://www.douyin.com")
            if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                _login_state["status"] = "logged_in"
                _login_state["message"] = "登录成功！"
                _login_state["screenshot"] = None
                await _login_cleanup()
                return

            await asyncio.sleep(1)

        if _login_state["status"] == "waiting_scan":
            _login_state["status"] = "failed"
            _login_state["message"] = "扫码超时（3 分钟）"

    except Exception as e:
        _login_state["status"] = "failed"
        _login_state["message"] = f"登录错误: {e}"
    finally:
        await _login_cleanup()
