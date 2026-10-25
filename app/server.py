import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse

from app.paths import DATA_ROOT, DOUYIN_CHAT_HOME, ROOT, SHELL_DIR, SIGNIN_HOME

sys.path.insert(0, str(ROOT / "modules" / "douyin_chat"))
sys.path.insert(0, str(ROOT / "modules" / "spark"))

from dy_backend.main import app as douyin_chat_app  # noqa: E402
from spark_main import app as spark_app  # noqa: E402
from modules.signin.routes import router as signin_router  # noqa: E402
from modules.weibo_chat.routes import router as weibo_chat_router  # noqa: E402
from modules.taobao_chat.routes import router as taobao_chat_router  # noqa: E402
from modules.starsky.routes import router as starsky_router  # noqa: E402


@asynccontextmanager
async def lifespan(_app: FastAPI):
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    SIGNIN_HOME.mkdir(parents=True, exist_ok=True)
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

    await restore_schedule_on_startup()
    try:
        yield
    finally:
        await shutdown_background_tasks()


app = FastAPI(title="多平台聊天记录导出及签到续火花", lifespan=lifespan)
app.include_router(signin_router)
app.include_router(weibo_chat_router)
app.include_router(starsky_router)
app.mount("/douyin-chat", douyin_chat_app)
app.mount("/spark", spark_app)
app.include_router(taobao_chat_router)


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


@app.get("/douyin", include_in_schema=False)
@app.get("/douyin/", include_in_schema=False)
def douyin_page():
    return FileResponse(SHELL_DIR / "douyin.html", headers=_NO_STORE)


@app.get("/", include_in_schema=False)
def shell():
    return FileResponse(SHELL_DIR / "index.html", headers=_NO_STORE)
