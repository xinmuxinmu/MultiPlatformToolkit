import os
import threading
from pathlib import Path

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse, JSONResponse

from modules.ai_checkin import core

UI_PATH = Path(__file__).resolve().parents[2] / "web_ui" / "modules" / "ai_checkin" / "ui.html"

router = APIRouter(prefix="/ai-checkin")

_keepalive_lock = threading.Lock()

# 调度器由应用 lifespan 启停，导入 API 或离线测试不会执行真实签到。


def _client_presence() -> dict:
    """客户端登录态文件是否存在：给前端一句「装没装/登没登」的提示。"""
    appdata = os.environ.get("APPDATA", "")
    local = os.environ.get("LOCALAPPDATA", "")
    home = Path.home()
    checks = {
        "trae": [Path(appdata) / "TRAE SOLO CN" / "User" / "globalStorage" / "storage.json",
                 Path(appdata) / "Trae CN" / "User" / "globalStorage" / "storage.json"],
        "workbuddy": [Path(local) / "CodeBuddyExtension" / "Data" / "Public" / "auth" / "workbuddy-desktop.info",
                      Path(local) / "WorkBuddy" / "WorkBuddy.exe"],
        "workbuddy_ai": [Path(local) / "CodeBuddyExtension" / "Data" / "Public" / "auth" / "workbuddy-desktop-ai.info"],
        "qwen": [Path(appdata) / "QwenWorkCN" / "auth-v2.dat"],
        "zhipu": [Path(appdata) / "chatglm" / "Network" / "Cookies"],
        "doubao": [Path(local) / "Doubao"],
        "minimax": [home / ".minimax"],
        "dumate": [Path(appdata) / "qianfan-desktop-app"],
        "lobster": [Path(appdata) / "LobsterAI"],
        "qoder": [Path(appdata) / "com.qoder.app.stable", Path(appdata) / "com.qodercn.app.stable"],
    }
    return {key: any(p.exists() for p in paths) for key, paths in checks.items()}


@router.get("/", include_in_schema=False)
def page():
    return FileResponse(UI_PATH, headers={"Cache-Control": "no-store"})


@router.get("/api/state")
def state():
    return {
        "config": core.load_config(),
        "platforms": core.PLATFORMS,
        "clients": _client_presence(),
        "state": core.load_state(),
        "running": core.TASK.running,
        "done": core.TASK.done,
        "version": core.VERSION,
        "config_path": str(core.CONFIG_PATH),
        "started_at": core.STARTED_AT,
        "code_mtime": core.code_mtime(),
    }


@router.post("/api/save")
def save(body: dict = Body(...)):
    cfg = core.load_config()
    for key in ("qoder_token", "qoder_cn_token", "qoder_cn_cookie"):
        if key in body:
            cfg[key] = str(body.get(key) or "").strip()
    if isinstance(body.get("keepalive"), bool):
        cfg["keepalive"] = body["keepalive"]
    if isinstance(body.get("platforms"), dict):
        for pid, enabled in body["platforms"].items():
            if pid in cfg["platforms"]:
                cfg["platforms"][pid] = bool(enabled)
    if isinstance(body.get("schedule"), dict):
        sched = body["schedule"]
        if "enabled" in sched:
            cfg["schedule"]["enabled"] = bool(sched["enabled"])
        text = str(sched.get("time") or "").strip()
        if core._valid_time(text):
            cfg["schedule"]["time"] = text
    core.save_config(cfg)
    return {"ok": True, "config_path": str(core.CONFIG_PATH)}


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


@router.post("/api/run")
def run(body: dict = Body(...)):
    cfg = core.load_config()
    targets = [t for t in (body.get("targets") or []) if t in {p["id"] for p in core.PLATFORMS}]
    if not targets:
        return JSONResponse({"ok": False, "error": "没有选中任何平台"}, status_code=400)
    started = core.TASK.start(targets, cfg, dry=bool(body.get("dry")))
    return {"ok": started, "error": "" if started else "正在签到中，等它跑完"}


@router.post("/api/keepalive")
def refresh_keepalive():
    if not _keepalive_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "正在续期，稍等一下"}, status_code=409)
    try:
        return {"ok": True, "lines": core.keepalive()}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"续期失败：{exc}"}, status_code=500)
    finally:
        _keepalive_lock.release()
