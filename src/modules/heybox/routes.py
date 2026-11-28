import threading
from pathlib import Path

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from modules.heybox import captcha_bridge, core

UI_PATH = Path(__file__).resolve().parents[2] / "web_ui" / "modules" / "heybox" / "ui.html"

router = APIRouter(prefix="/heybox")

_refresh_lock = threading.Lock()


@router.get("/", include_in_schema=False)
def page():
    return FileResponse(UI_PATH, headers={"Cache-Control": "no-store"})


@router.get("/api/state")
def state():
    cfg = core.load_config()
    # App pkey / device token are used by the task runner but never sent to the UI.
    safe_cfg = dict(cfg)
    safe_cfg.pop("app_session", None)
    return {
        "config": safe_cfg,
        "app_logged_in": core.app_login_ready(cfg),
        "app_login": core.app_login_status(),
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
        if key in core.DEFAULT_CONFIG and key != "app_session":
            cfg[key] = val
    core.save_config(cfg)
    return {"ok": True, "config_path": str(core.CONFIG_PATH)}


@router.post("/api/refresh")
def refresh():
    if not _refresh_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "正在刷新，稍等一下"}, status_code=409)
    try:
        data = core.HeyboxDaily(core.load_config(), lambda *_: None).refresh()
        return {"ok": True, "state": data}
    except core.HeyboxError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"刷新失败：{exc}"}, status_code=500)
    finally:
        _refresh_lock.release()


@router.post("/api/run")
def run():
    cfg = core.load_config()
    if not core.app_login_ready(cfg):
        return JSONResponse({"ok": False, "error": core.APP_LOGIN_REQUIRED,
                             "app_login_required": True}, status_code=400)
    started = core.TASK.start(cfg)
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


@router.get("/api/app-login/status")
def app_login_status():
    return core.app_login_status()


@router.post("/api/app-login/cancel")
def app_login_cancel():
    try:
        return {"ok": True, **core.app_login_cancel()}
    except core.HeyboxError as exc:
        return JSONResponse({"ok": False, "error": str(exc), **core.app_login_status()}, status_code=400)


@router.post("/api/app-login/send-code")
def app_login_send_code(body: dict = Body(...)):
    try:
        return {"ok": True, **core.app_login_send_code(
            str((body or {}).get("phone") or ""), (body or {}).get("captcha"))}
    except core.HeyboxError as exc:
        return JSONResponse({"ok": False, "error": str(exc), **core.app_login_status()}, status_code=400)
    except Exception:
        return JSONResponse({"ok": False, "error": "发送验证码失败，请稍后重试", **core.app_login_status()}, status_code=500)


@router.post("/api/app-login/verify")
def app_login_verify(body: dict = Body(...)):
    try:
        result = core.app_login_verify(
            str((body or {}).get("phone") or ""),
            str((body or {}).get("code") or ""),
            (body or {}).get("captcha"),
        )
        return {"ok": True, **result}
    except core.HeyboxError as exc:
        return JSONResponse({"ok": False, "error": str(exc), **core.app_login_status()}, status_code=400)
    except Exception:
        return JSONResponse({"ok": False, "error": "短信登录失败，原配置已保留", **core.app_login_status()}, status_code=500)


_CAPTCHA_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff"}


@router.post("/api/app-login/captcha/open")
def app_login_captcha_open(body: dict = Body(...)):
    try:
        result = captcha_bridge.open_bridge(
            str(body.get("captcha_id") or ""), str(body.get("captcha_for") or ""))
        return JSONResponse(result, headers=_CAPTCHA_HEADERS)
    except core.HeyboxError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400,
                            headers=_CAPTCHA_HEADERS)


@router.get("/api/app-login/captcha/result/{bridge_id}")
def app_login_captcha_result(bridge_id: str):
    return JSONResponse(captcha_bridge.take_result(bridge_id), headers=_CAPTCHA_HEADERS)


@router.get("/app-login/captcha/{bridge_id}", include_in_schema=False)
def app_login_captcha_page(bridge_id: str):
    html = captcha_bridge.bridge_page(bridge_id)
    if html is None:
        return HTMLResponse("<!doctype html><meta charset='utf-8'><title>验证已过期</title>"
                            "<p>人机验证已过期，请返回软件重新操作。</p>",
                            status_code=410, headers=_CAPTCHA_HEADERS)
    return HTMLResponse(html, headers=_CAPTCHA_HEADERS)


@router.post("/app-login/captcha/{bridge_id}", include_in_schema=False)
def app_login_captcha_submit(bridge_id: str, body: dict = Body(...)):
    try:
        return JSONResponse(captcha_bridge.submit_proof(bridge_id, body), headers=_CAPTCHA_HEADERS)
    except core.HeyboxError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400,
                            headers=_CAPTCHA_HEADERS)
