import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.paths import BILI_CHAT_HOME, DATA_ROOT, DOUYIN_CHAT_HOME, SRC_ROOT, SHELL_DIR, SIGNIN_HOME, WEB_DIR
from app.ui_settings import load_settings, save_settings

sys.path.insert(0, str(SRC_ROOT / "modules" / "douyin_chat"))
sys.path.insert(0, str(SRC_ROOT / "modules" / "spark"))

from dy_backend.main import app as douyin_chat_app  # noqa: E402
from spark_main import app as spark_app  # noqa: E402
from modules.signin.routes import router as signin_router  # noqa: E402
from modules.weibo_chat.routes import router as weibo_chat_router  # noqa: E402
from modules.taobao_chat.routes import router as taobao_chat_router  # noqa: E402
from modules.starsky.routes import router as starsky_router  # noqa: E402
from modules.bilibili_chat.routes import router as bili_chat_router  # noqa: E402
from modules.netease_fans.routes import router as netease_fans_router  # noqa: E402
from modules.kugou.routes import router as kugou_router  # noqa: E402
from modules.qq_tasks.routes import router as qq_tasks_router  # noqa: E402
from modules.bilibili_vip.routes import router as bili_vip_router  # noqa: E402
from modules.ai_checkin.routes import router as ai_checkin_router  # noqa: E402
from modules.ai_gateway.routes import router as ai_gateway_router, proxy_router, error_response  # noqa: E402
from modules.ai_gateway.base import GatewayError  # noqa: E402
from modules.heybox.routes import router as heybox_router  # noqa: E402
from modules.douyin_fans.routes import router as douyin_fans_router  # noqa: E402


@asynccontextmanager
async def lifespan(_app: FastAPI):
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    SIGNIN_HOME.mkdir(parents=True, exist_ok=True)
    BILI_CHAT_HOME.mkdir(parents=True, exist_ok=True)
    DOUYIN_CHAT_HOME.mkdir(parents=True, exist_ok=True)

    # 挂载的子应用不会执行自己的 startup 钩子，这里复刻 dy_backend.main 的初始化。
    from dy_backend.control_panel import restore_schedule_on_startup, shutdown_background_tasks
    from dy_common import accounts, config as dy_config
    from dy_common.db import init_db
    from dy_common.storage import ensure_data_migrated

    # 顺序很重要：先确定账号目录，再把旧版根目录的聊天数据迁进去，最后才打开数据库。
    accounts.ensure_migrated()
    accounts.normalize_account_ids()
    ensure_data_migrated()
    init_db()
    dy_config.ensure_api_token()

    # 火花与聊天共用同一套账号：把火花单账号时代的数据并到 uid 目录后再恢复任务。
    from spark_backend import accounts as spark_accounts
    spark_accounts.migrate_legacy_data()
    spark_accounts.apply_active_account()

    import os
    from modules.ai_checkin import core as ai_core
    from modules.qq_tasks import scheduler as qq_scheduler
    schedules_enabled = os.environ.get("APP_DISABLE_SCHEDULES") != "1"
    if schedules_enabled:
        await restore_schedule_on_startup()
        ai_core.start_scheduler()
        qq_scheduler.start_scheduler()
    try:
        yield
    finally:
        ai_core.stop_scheduler()
        qq_scheduler.stop_scheduler()
        await shutdown_background_tasks()


app = FastAPI(title="MultiPlatformToolkit · Python Service", lifespan=lifespan)
app.add_exception_handler(GatewayError, error_response)
app.include_router(signin_router)
app.include_router(netease_fans_router)
app.include_router(kugou_router)
app.include_router(qq_tasks_router)
app.include_router(bili_vip_router)
app.include_router(ai_checkin_router)
app.include_router(ai_gateway_router)
app.include_router(proxy_router)
app.include_router(heybox_router)
app.include_router(douyin_fans_router)
app.include_router(weibo_chat_router)
app.include_router(starsky_router)
app.mount("/douyin-chat", douyin_chat_app)
app.mount("/spark", spark_app)
app.include_router(taobao_chat_router)
app.include_router(bili_chat_router)
app.mount("/shared", StaticFiles(directory=str(WEB_DIR / "shared")), name="shared")


@app.get("/api/desktop/health")
def desktop_health():
    return {"app": "MultiPlatformToolkit", "architecture": "python-native", "version": "2.0.0"}


@app.get("/api/desktop/catalog")
def desktop_catalog():
    from app.desktop.catalog import api_catalog
    return api_catalog(("", app), ("/douyin-chat", douyin_chat_app), ("/spark", spark_app))


@app.get("/api/ui/settings")
def ui_settings_get():
    return load_settings()


@app.post("/api/ui/settings")
async def ui_settings_set(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return save_settings(body or {})


@app.get("/panel", include_in_schema=False)
def douyin_panel_shortcut():
    return RedirectResponse("/douyin-chat/panel", status_code=307)


@app.get("/panel/{rest:path}", include_in_schema=False)
def douyin_panel_subpath(rest: str):
    return RedirectResponse(f"/douyin-chat/panel/{rest}", status_code=307)


@app.get("/back.js", include_in_schema=False)
def back_button_script():
    return FileResponse(SHELL_DIR / "back.js", media_type="application/javascript")


_NO_STORE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}


@app.middleware("http")
async def shared_no_store(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/shared/"):
        response.headers.update(_NO_STORE)
    return response


@app.get("/douyin", include_in_schema=False)
@app.get("/douyin/", include_in_schema=False)
def douyin_page():
    return FileResponse(SHELL_DIR / "douyin.html", headers=_NO_STORE)


@app.get("/", include_in_schema=False)
def shell():
    return {**desktop_health(), "message": "请运行 python src/main.py 打开原生桌面界面", "api_docs": "/docs"}
