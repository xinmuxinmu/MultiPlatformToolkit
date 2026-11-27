import threading
from pathlib import Path

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse, JSONResponse

from modules.kugou import core

UI_PATH = Path(__file__).resolve().parents[2] / "web_ui" / "modules" / "kugou" / "ui.html"

router = APIRouter(prefix="/kugou")

_refresh_lock = threading.Lock()


@router.get("/", include_in_schema=False)
def page():
    return FileResponse(UI_PATH, headers={"Cache-Control": "no-store"})


@router.get("/api/state")
def state():
    return {
        "config": core.load_config(),
        "cached": core.load_state(),
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
        data = core.KugouDaily(core.load_config(), lambda *_: None).refresh()
        return {"ok": True, "state": data}
    except core.KugouError as exc:
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


@router.post("/api/qr-start")
def qr_start(platform: str = "standard"):
    """扫码登录：拿二维码。比网页登录靠谱——网页 cookie 里的 token 调不了 App 接口。"""
    try:
        return {"ok": True, **core.qr_start(platform)}
    except core.KugouError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"拿二维码失败：{exc}"}, status_code=500)


@router.get("/api/qr-poll")
def qr_poll(key: str = "", platform: str = "standard"):
    if not key:
        return JSONResponse({"ok": False, "error": "缺少二维码 key"}, status_code=400)
    try:
        return {"ok": True, **core.qr_poll(key, platform)}
    except core.KugouError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"查询扫码状态失败：{exc}"}, status_code=500)
