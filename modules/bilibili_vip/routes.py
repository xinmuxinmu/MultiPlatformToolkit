import threading
from pathlib import Path

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse, JSONResponse

from modules.bilibili_vip import core

UI_PATH = Path(__file__).resolve().parent / "ui.html"

router = APIRouter(prefix="/bili-vip")

_refresh_lock = threading.Lock()


@router.get("/", include_in_schema=False)
def page():
    return FileResponse(UI_PATH, headers={"Cache-Control": "no-store"})


@router.get("/api/state")
def state():
    return {
        "config": core.load_config(),
        "cached": core.load_state(),
        "accounts": core.bili_chat_accounts(),
        "running": core.TASK.running,
        "done": core.TASK.done,
        "version": core.VERSION,
        "config_path": str(core.CONFIG_PATH),
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
        return JSONResponse({"ok": False, "error": "正在刷新，稍等一下"}, status_code=409)
    try:
        data = core.VipBigPoint(core.load_config(), lambda *_: None).refresh()
        return {"ok": True, "state": data}
    except core.BiliError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"刷新失败：{exc}"}, status_code=500)
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
