import threading
from pathlib import Path

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse, JSONResponse

from modules.netease_fans import core

UI_PATH = Path(__file__).resolve().parents[2] / "legacy" / "web_ui" / "modules" / "netease_fans" / "ui.html"

router = APIRouter(prefix="/netease-fans")

# 状态刷新是同步的只读请求，加把锁避免连点把接口打出一串并发。
_refresh_lock = threading.Lock()


@router.get("/", include_in_schema=False)
def page():
    return FileResponse(UI_PATH, headers={"Cache-Control": "no-store"})


@router.get("/api/ping")
def ping():
    return {"app": core.APP_ID, "version": core.VERSION}


@router.get("/api/state")
def state():
    return {
        "config": core.load_config(),
        "cached": core.load_state(),
        "running": core.TASK.running,
        "done": core.TASK.done,
        "version": core.VERSION,
        "config_path": str(core.CONFIG_PATH),
        "code_mtime": core.code_mtime(),
    }


@router.post("/api/save")
def save(body: dict = Body(...)):
    cfg = core.load_config()
    for key, val in (body or {}).items():
        if key in core.DEFAULT_CONFIG:
            cfg[key] = val
    core.save_config(cfg)
    return {"ok": True, "config_path": str(core.CONFIG_PATH)}


@router.post("/api/refresh")
def refresh():
    if not _refresh_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "正在刷新，稍等一下"},
                            status_code=409)
    try:
        data = core.NeteaseFans(core.load_config(), lambda *_: None).refresh()
        return {"ok": True, "state": data}
    except core.FansError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"刷新失败：{exc}"},
                            status_code=500)
    finally:
        _refresh_lock.release()


@router.post("/api/run")
def run():
    started = core.TASK.start(core.load_config())
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
def login():
    ok = core.login_start()
    return {"ok": True, "note": "" if ok else "登录窗口已经开着，直接在里面登就行"}


@router.post("/api/login-grab")
def login_grab():
    """用户在浏览器里登完后主动点一下，再读取并保存 Cookie。"""
    return core.login_grab()


@router.get("/api/login-status")
def login_status():
    return core.login_snapshot()