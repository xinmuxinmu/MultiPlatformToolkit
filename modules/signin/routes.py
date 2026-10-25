import os
import signal
import threading
from pathlib import Path

from fastapi import APIRouter, Body, Request
from fastapi.responses import FileResponse, JSONResponse

from modules.signin import core

UI_PATH = Path(__file__).resolve().parent / "ui.html"

router = APIRouter(prefix="/signin")


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
        "running": core.TASK.running,
        "done": core.TASK.done,
        "version": core.VERSION,
        "config_path": str(core.CONFIG_PATH),
        "started_at": core.STARTED_AT,
        "code_mtime": core.code_mtime(),
    }


@router.get("/api/weibo-qr-status")
def weibo_qr_status():
    return core.QR.snapshot()


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


@router.post("/api/save")
def save(body: dict = Body(...)):
    cfg = core.load_config()
    for key in core.SIGNERS:
        if isinstance(body.get(key), dict):
            for k, v in body[key].items():
                if k in core.DEFAULT_CONFIG[key]:
                    cfg[key][k] = v
    core.save_config(cfg)
    return {"ok": True, "config_path": str(core.CONFIG_PATH)}


@router.post("/api/run")
def run(body: dict = Body(...)):
    targets = [t for t in (body.get("targets") or list(core.SIGNERS)) if t in core.SIGNERS]
    if not targets:
        return JSONResponse({"ok": False, "error": "没有选中任何平台"}, status_code=400)
    started = core.TASK.start(targets, core.load_config())
    return {"ok": started, "error": "" if started else "正在签到中，等它跑完"}


@router.post("/api/pojie-login")
def pojie_login():
    browser = core.find_browser()
    if not browser:
        return {"ok": False, "error": "没找到 Chrome 或 Edge"}
    try:
        core._http_json("http://127.0.0.1:9333/json/version", timeout=1)
        return {"ok": True, "error": "", "note": "浏览器已经开着，直接在里面登录就行"}
    except Exception:
        pass
    url = "https://www.52pojie.cn/member.php?mod=logging&action=login"
    threading.Thread(
        target=lambda: core.launch_browser(
            browser, core.POJIE_PROFILE, url, keep_open=True),
        daemon=True).start()
    return {"ok": True, "error": "", "note": ""}


@router.post("/api/weibo-qr-login")
def weibo_qr_login():
    ok = core.QR.start()
    return {"ok": True, "error": "" if ok else "扫码登录已在进行中，去弹窗里等着就行"}


@router.post("/api/quit")
def quit_app(request: Request):
    server = getattr(request.app.state, "uvicorn_server", None)

    def stop():
        if server is not None:
            server.should_exit = True
        else:
            os.kill(os.getpid(), signal.SIGINT)

    threading.Timer(0.4, stop).start()
    return {"ok": True}
