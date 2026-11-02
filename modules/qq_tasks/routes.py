from pathlib import Path

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse, JSONResponse

from modules.qq_tasks import core

UI_PATH = Path(__file__).resolve().parent / "ui.html"

router = APIRouter(prefix="/qq")


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
    music = core.load_music()
    card = core.load_card()
    return {
        "music": music,
        "card": card,
        "cached": core.load_state(),
        "running": core.TASK.running,
        "done": core.TASK.done,
        "version": core.VERSION,
    }


@router.post("/api/save")
def save(body: dict = Body(...)):
    music = core.load_music()
    card = core.load_card()
    m = body.get("music") or {}
    c = body.get("card") or {}
    for key in core.DEFAULT_MUSIC:
        if key in m:
            music[key] = m[key]
    for key in core.DEFAULT_CARD:
        if key in c:
            card[key] = c[key]
    if isinstance(music.get("cookie"), str):
        music["cookie"] = core.norm_cookie(music["cookie"])
    if isinstance(card.get("cookie"), str):
        card["cookie"] = core.norm_cookie(card["cookie"])
    core.save_music(music)
    core.save_card(card)
    return {"ok": True}


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
