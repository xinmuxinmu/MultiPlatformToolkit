"""Native-client API; GET state never opens or refreshes a browser."""
from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from . import core

router = APIRouter(prefix="/douyin-fans", tags=["抖音圈子粉丝任务"])


@router.get("/api/state", summary="读取本地设置、截图参考任务与最近网页快照")
def state():
    return core.state()


@router.post("/api/config", summary="保存官方圈子链接与共享账号选择")
def config(body: dict = Body(...)):
    try:
        return {"ok": True, "config": core.save_config(body)}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


def _start(action, task_id=""):
    try:
        return core.start(action, task_id)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@router.post("/api/login", summary="打开专用浏览器供本人登录抖音")
def login():
    return _start("login")


@router.post("/api/open", summary="打开已保存的官方圈子页面，不点击任务")
def open_circle():
    return _start("open")


@router.post("/api/refresh", summary="从真实官方网页重新读取可见任务状态")
def refresh():
    return _start("refresh")


@router.post("/api/resolve", summary="只读解析官方小程序分享链接，不登录或执行任务")
def resolve():
    return _start("resolve")


@router.post("/api/checkin", summary="只尝试明确免费的圈子签到并刷新复查")
def checkin():
    return _start("checkin")


@router.post("/api/open-task", summary="打开圈子官方入口，由本人处理所选免费任务")
def open_task(body: dict = Body(...)):
    return _start("open-task", str(body.get("task_id") or ""))
