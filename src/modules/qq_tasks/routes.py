from pathlib import Path

import time

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse, JSONResponse

from modules.qq_tasks import core, scheduler

UI_PATH = Path(__file__).resolve().parents[2] / "web_ui" / "modules" / "qq_tasks" / "ui.html"

router = APIRouter(prefix="/qq")

# 刷新与执行共享同一把锁，防止较早开始的刷新覆盖刚完成的任务进度。
_refresh_lock = core.OPERATION_LOCK


def _which(value: str) -> str:
    return "card" if value == "card" else "music"


@router.get("/", include_in_schema=False)
def page():
    return FileResponse(UI_PATH, headers={"Cache-Control": "no-store"})


@router.get("/api/ping")
def ping():
    return {"app": core.APP_ID, "version": core.VERSION}


@router.get("/api/state")
def state():
    return {
        "music": core.load_music(),
        "cached": core.load_state(),
        "running": core.TASK.running,
        "done": core.TASK.done,
        "version": core.VERSION,
        "scheduler": scheduler.snapshot(),
    }


@router.post("/api/save")
def save(body: dict = Body(...)):
    music = core.load_music()
    m = body.get("music") or {}
    if not isinstance(m, dict):
        return JSONResponse({"ok": False, "error": "music 必须是配置对象"}, status_code=400)
    for key in core.DEFAULT_MUSIC:
        if key in m:
            music[key] = m[key]
    if isinstance(music.get("cookie"), str):
        music["cookie"] = core.norm_cookie(music["cookie"])
    try:
        scheduler.validate_config(music)
        core.save_music(music)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True}


@router.post("/api/refresh")
def refresh():
    """只读刷新乐迷勋章状态，不签到。"""
    cfg = core.load_music()
    if not cfg.get("cookie"):
        return JSONResponse({"ok": False, "error": "QQ 音乐还没登录"},
                            status_code=400)
    if not _refresh_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "正在刷新或执行任务，稍等一下"},
                            status_code=409)
    try:
        medal = core.refresh_medals(cfg["cookie"])
        try:
            sign = core.MusicClient(cfg["cookie"]).sign_status()
        except core.QQError as exc:
            sign = {"error": str(exc)}
        sign["at"] = int(time.time())
        core.save_state({**core.load_state(), "medal": medal, "sign": sign,
                         "fetched_at": int(time.time())})
        return {"ok": True, "medal": medal, "sign": sign}
    except core.QQError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"刷新失败：{exc}"}, status_code=500)
    finally:
        _refresh_lock.release()


@router.post("/api/run")
def run():
    started = core.TASK.start()
    return {"ok": started, "error": "" if started else "正在跑，等它结束"}


@router.get("/api/logs")
def logs(since: int = 0):
    with core.TASK.lock:
        return {
            "logs": [x for x in core.TASK.logs if x["id"] > since],
            "running": core.TASK.running,
            "done": core.TASK.done,
            "summary": core.TASK.summary,
            "seq": core.TASK.seq,
        }


@router.post("/api/login")
def login(which: str = "music"):
    ok = core.login_start(_which(which))
    return {"ok": True, "note": "" if ok else "登录窗口已经开着，直接在里面登就行"}


@router.post("/api/login-grab")
def login_grab(which: str = "music"):
    try:
        return core.login_grab(_which(which))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"stage": "error", "msg": f"获取登录态失败：{exc}",
                             "qr": "", "cookie": "", "who": ""}, status_code=500)


@router.get("/api/login-status")
def login_status(which: str = "music"):
    return core.login_snapshot(_which(which))
