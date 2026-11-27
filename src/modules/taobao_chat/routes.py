"""淘宝旺旺聊天记录导出（买家侧）：登录、导出、离线查看。

复用微博聊天记录那套结构：FastAPI 控制台 + Python/Playwright 导出工具 + 本地 HTML 查看器。
淘宝网页版没有公开的买家会话列表接口，所以导出目标由用户在控制台粘贴聊天链接
（聊天窗口地址栏里的 .../app/im/chat/index.html?&uid=cntaobaoXXX&type=web）。
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import importlib.util
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

HOME = Path(os.environ.get("TAOBAO_CHAT_HOME") or Path(__file__).resolve().parents[3] / "modules" / "taobao_chat" / "data")
UI_PATH = Path(__file__).resolve().parents[2] / "web_ui" / "modules" / "taobao_chat" / "ui.html"
USERSCRIPT_PATH = Path(__file__).resolve().parent / "static" / "taobao-export.user.js"
TOOL_DIR = Path(__file__).resolve().parent / "tool"
RUN_LOG = HOME / "run.log"
INPUT_FILE = HOME / "input.json"
VIEWER_NAME = "淘宝聊天记录.html"

# 多账号：每个账号独立 profile / export
ACCOUNTS_DIR = HOME / "accounts"
INDEX_FILE = ACCOUNTS_DIR / "index.json"
LEGACY_OUT = Path(os.environ.get("TAOBAO_CHAT_OUT") or HOME / "export")
LEGACY_PROFILE = Path(os.environ.get("TAOBAO_CHAT_PROFILE") or HOME / "profile")
_meta_lock = threading.RLock()


def _save_index(index: dict) -> None:
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    os.replace(tmp, INDEX_FILE)


def _load_index() -> dict:
    try:
        with open(INDEX_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("active", "")
    if not isinstance(data.get("accounts"), dict):
        data["accounts"] = {}
    return data


def _ensure_accounts() -> None:
    with _meta_lock:
        if INDEX_FILE.exists():
            return
        ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        aid = "default"
        adir = ACCOUNTS_DIR / aid
        adir.mkdir(parents=True, exist_ok=True)
        for src, dst in (
            (LEGACY_PROFILE, adir / "profile"),
            (LEGACY_OUT, adir / "export"),
        ):
            if not src.exists() or dst.exists():
                continue
            try:
                shutil.move(str(src), str(dst))
            except OSError:
                try:
                    if src.is_dir():
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)
                except OSError:
                    pass
        _save_index({
            "active": aid,
            "accounts": {
                aid: {
                    "id": aid, "name": "默认账号", "uid": "",
                    "created_at": int(time.time()),
                }
            },
        })


_ensure_accounts()


def _active_id() -> str:
    index = _load_index()
    aid = str(index.get("active") or "")
    accounts = index.get("accounts") or {}
    if aid not in accounts:
        aid = next(iter(accounts), "")
    return aid


def _account_dir(account_id: str = "") -> Path:
    return ACCOUNTS_DIR / (account_id or _active_id())


def _out(account_id: str = "") -> Path:
    return _account_dir(account_id) / "export"


def _profile_dir(account_id: str = "") -> Path:
    return _account_dir(account_id) / "profile"


def _account_meta(account_id: str = "") -> dict:
    return (_load_index().get("accounts") or {}).get(account_id or _active_id()) or {}


def _update_account_meta(account_id: str, **fields) -> None:
    if not account_id:
        return
    with _meta_lock:
        index = _load_index()
        meta = (index.get("accounts") or {}).get(account_id)
        if not meta:
            return
        meta.update(fields)
        meta["id"] = account_id
        _save_index(index)


def _account_items() -> list[dict]:
    index = _load_index()
    active = _active_id()
    items = []
    for aid, meta in (index.get("accounts") or {}).items():
        items.append({
            "id": aid,
            "name": meta.get("name") or aid,
            "uid": meta.get("uid") or "",
            "logged_in": (_profile_dir(aid) / "Default").is_dir(),
            "active": aid == active,
        })
    return items

router = APIRouter(prefix="/taobao")

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_lock = threading.Lock()
_account_lock = threading.Lock()
_logs = deque(maxlen=4000)
_seq = 0
_account_cache = {"ts": 0.0, "data": None}
_state = {
    "proc": None,
    "kind": "",
    "running": False,
    "started_at": None,
    "exit_code": None,
    "stop_requested": False,
}


def _append(line: str) -> None:
    global _seq
    with _lock:
        _seq += 1
        _logs.append({"seq": _seq, "line": line})
        try:
            RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
            with RUN_LOG.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _rotate_log() -> None:
    try:
        if RUN_LOG.exists() and RUN_LOG.stat().st_size > 5 * 1024 * 1024:
            RUN_LOG.replace(RUN_LOG.with_suffix(".log.1"))
    except OSError:
        pass


def _python() -> str:
    return sys.executable


def _playwright_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def _env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["TAOBAO_CHAT_HOME"] = str(HOME)
    env["TAOBAO_CHAT_OUT"] = str(_out())
    env["TAOBAO_CHAT_PROFILE"] = str(_profile_dir())
    try:
        from app.ui_settings import is_headless
        if is_headless():
            env["TAOBAO_HEADLESS"] = "1"
    except Exception:
        pass
    return env


def _reader(proc: subprocess.Popen) -> None:
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            _append(raw.rstrip("\r\n"))
    except Exception as exc:  # noqa: BLE001 - 子进程输出不可控，失败也要复位状态
        _append(f"[wrapper] 读取输出失败：{exc}")
    code = proc.wait()
    with _lock:
        if _state["proc"] is proc:
            _state["proc"] = None
            _state["running"] = False
            _state["exit_code"] = code
    _append(f"[wrapper] 进程结束，退出码 {code}")


def _start(args: list[str], kind: str) -> tuple[bool, str]:
    if not _playwright_available():
        return False, "缺少 Python Playwright，请执行 python -m pip install -r requirements.txt"

    _account_cache.update(ts=0.0, data=None)
    with _lock:
        if _state["running"]:
            return False, "已有任务在运行，请等待结束或先停止"
        _state["running"] = True
        _state["kind"] = kind
        _state["started_at"] = time.time()
        _state["exit_code"] = None
        _state["stop_requested"] = False

    cmd = [_python(), "-u", "export.py", *args]
    _append(f"$ {' '.join(cmd[1:])}")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(TOOL_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=_env(),
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _state.update(running=False, proc=None, exit_code=-1)
        _append(f"[wrapper] 启动失败：{exc}")
        return False, f"启动失败：{exc}"

    with _lock:
        _state["proc"] = proc
    threading.Thread(target=_reader, args=(proc,), daemon=True).start()
    return True, ""


def _stop() -> bool:
    with _lock:
        proc = _state["proc"]
        if not _state["running"] or proc is None:
            return False
        _state["stop_requested"] = True
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
        )
    else:
        proc.terminate()
    _append("[wrapper] 已请求停止")
    return True


def _summary() -> dict:
    info: dict = {
        "output_dir": str(_out()),
        "viewer": (_out() / VIEWER_NAME).exists(),
    }
    raw = _out() / "raw.json"
    if raw.exists():
        st = raw.stat()
        info["raw_mtime"] = st.st_mtime
        info["raw_size"] = st.st_size
        try:
            data = json.loads(raw.read_text(encoding="utf-8"))
            convs = data.get("convs") or []
            info.update(
                exported_at=data.get("exportedAt"),
                me=(data.get("me") or {}).get("name") or "",
                conversations=len(convs),
                messages=data.get("total") or sum(len(c.get("messages") or []) for c in convs),
            )
        except (OSError, ValueError):
            info["raw_error"] = "raw.json 解析失败"
    return info


def _task() -> dict:
    with _lock:
        return {
            "running": _state["running"],
            "kind": _state["kind"],
            "started_at": _state["started_at"],
            "exit_code": _state["exit_code"],
            "stop_requested": _state["stop_requested"],
            "seq": _seq,
            "python": True,
            "playwright": _playwright_available(),
        }


@router.get("", include_in_schema=False)
def root_redirect():
    return RedirectResponse("/taobao/", status_code=307)


@router.get("/", include_in_schema=False)
def console():
    return FileResponse(UI_PATH, headers={"Cache-Control": "no-store"})


VIEWER_TEMPLATE = TOOL_DIR / "template.html"


def _render_viewer(data: dict) -> None:
    tpl = VIEWER_TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    page = _out() / VIEWER_NAME
    tmp = page.with_name(page.name + ".tmp")
    tmp.write_text(tpl.replace("__DATA__", payload), encoding="utf-8")
    os.replace(tmp, page)


@router.get("/viewer", include_in_schema=False)
def viewer():
    page = _out() / VIEWER_NAME
    raw = _out() / "raw.json"
    if raw.exists() and not _state["running"]:
        try:
            stale = not page.exists() or VIEWER_TEMPLATE.stat().st_mtime > page.stat().st_mtime
            if stale:
                _render_viewer(json.loads(raw.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    if not page.exists():
        return JSONResponse({"ok": False, "error": "还没有导出数据，先运行一次导出"}, status_code=404)
    return FileResponse(page, headers={"Cache-Control": "no-store"})


@router.get("/userscript", include_in_schema=False)
def userscript():
    return FileResponse(
        USERSCRIPT_PATH,
        media_type="application/javascript; charset=utf-8",
        filename="淘宝旺旺聊天记录导出.user.js",
    )


# 兼容旧地址：以前首页直接链到油猴脚本文件
@router.get("/taobao-export.user.js", include_in_schema=False)
def legacy_userscript():
    return userscript()


@router.get("/api/task")
def task():
    return _task()


@router.get("/api/logs")
def logs(since: int = 0):
    with _lock:
        return {
            "logs": [x for x in _logs if x["seq"] > since],
            "task": {
                "running": _state["running"],
                "kind": _state["kind"],
                "exit_code": _state["exit_code"],
                "seq": _seq,
                "python": True,
            "playwright": _playwright_available(),
            },
        }


@router.get("/api/summary")
def summary():
    return {"ok": True, "data": _summary(), "task": _task()}


@router.get("/api/account")
def account():
    now = time.time()
    if _account_cache["data"] and now - _account_cache["ts"] < 15:
        return {"ok": True, **_account_cache["data"], "cached": True}

    if not _playwright_available():
        return {"ok": False, "error": "缺少 Python Playwright，请先安装 requirements.txt"}
    if not _profile_dir().exists():
        return {"ok": True, "logged_in": False, "name": "", "uid": "", "note": "还没有登录过淘宝"}

    with _account_lock:
        try:
            proc = subprocess.run(
                [_python(), "-u", "export.py", "--status"],
                cwd=str(TOOL_DIR),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
                env=_env(),
                creationflags=CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "检测超时，稍后重试"}
        lines = [x for x in (proc.stdout or "").splitlines() if x.strip()]
        try:
            data = json.loads(lines[-1]) if lines else {}
        except ValueError:
            return {"ok": False, "error": "登录态检测返回异常", "output": (proc.stdout or "")[-500:]}
    result = {
        "logged_in": bool(data.get("ok")),
        "name": data.get("name") or "",
        "uid": data.get("uid") or "",
    }
    if result["logged_in"] and result["uid"]:
        _update_account_meta(_active_id(), uid=result["uid"])
    _account_cache.update(ts=time.time(), data=result)
    return {"ok": True, **result, "cached": False, "account_id": _active_id()}


class AccountSwitch(BaseModel):
    id: str


class AccountCreate(BaseModel):
    name: str = ""


class AccountRename(BaseModel):
    id: str
    name: str


class AccountDelete(BaseModel):
    id: str


class RecordDelete(BaseModel):
    id: str


@router.post("/api/records/delete")
def records_delete(req: RecordDelete):
    with _lock:
        if _state["running"]:
            return JSONResponse({"error": "任务进行中，请先停止再删除记录"}, status_code=409)
        raw_path = _out() / "raw.json"
        if not raw_path.exists():
            return JSONResponse({"error": "还没有导出记录"}, status_code=404)
        try:
            data = json.loads(raw_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return JSONResponse({"error": "读取导出数据失败"}, status_code=500)
        convs = data.get("convs") or []
        idx = next(
            (i for i, c in enumerate(convs) if str(c.get("id") or c.get("name") or "") == req.id),
            -1,
        )
        if idx < 0:
            return JSONResponse({"error": "会话不存在，可能已被删除"}, status_code=404)
        removed = convs.pop(idx)
        data["convs"] = convs
        data["total"] = sum(int(c.get("count") or len(c.get("messages") or [])) for c in convs)
        try:
            tmp = raw_path.with_name(raw_path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, raw_path)
            _render_viewer(data)
        except OSError:
            return JSONResponse({"error": "写入导出数据失败"}, status_code=500)
    return {
        "ok": True,
        "id": req.id,
        "name": removed.get("name") or "",
        "conversations": len(convs),
        "total": data["total"],
    }


@router.get("/api/accounts")
def accounts():
    return {"accounts": _account_items(), "active": _active_id()}


@router.post("/api/accounts/switch")
def accounts_switch(req: AccountSwitch):
    if _state["running"]:
        return JSONResponse({"error": "任务进行中，请先停止再切换账号"}, status_code=409)
    with _meta_lock:
        index = _load_index()
        if req.id not in (index.get("accounts") or {}):
            return JSONResponse({"error": "账号不存在"}, status_code=404)
        index["active"] = req.id
        _save_index(index)
    _account_cache.update(ts=0.0, data=None)
    return {"ok": True, "active": req.id}


@router.post("/api/accounts/create")
def accounts_create(req: AccountCreate):
    if _state["running"]:
        return JSONResponse({"error": "任务进行中，请先停止再新建账号"}, status_code=409)
    aid = uuid.uuid4().hex[:10]
    adir = ACCOUNTS_DIR / aid
    (adir / "profile").mkdir(parents=True, exist_ok=True)
    (adir / "export").mkdir(parents=True, exist_ok=True)
    with _meta_lock:
        index = _load_index()
        name = req.name.strip() or f"账号 {len(index.get('accounts') or {}) + 1}"
        index.setdefault("accounts", {})[aid] = {
            "id": aid, "name": name, "uid": "", "created_at": int(time.time()),
        }
        index["active"] = aid
        _save_index(index)
    _account_cache.update(ts=0.0, data=None)
    return {"ok": True, "account": _account_meta(aid)}


@router.post("/api/accounts/rename")
def accounts_rename(req: AccountRename):
    name = req.name.strip()[:40]
    if not name:
        return JSONResponse({"error": "名称不能为空"}, status_code=400)
    with _meta_lock:
        index = _load_index()
        meta = (index.get("accounts") or {}).get(req.id)
        if not meta:
            return JSONResponse({"error": "账号不存在"}, status_code=404)
        meta["name"] = name
        _save_index(index)
    return {"ok": True, "account": _account_meta(req.id)}


@router.post("/api/accounts/delete")
def accounts_delete(req: AccountDelete):
    if _state["running"]:
        return JSONResponse({"error": "任务进行中，请先停止再删除账号"}, status_code=409)
    with _meta_lock:
        index = _load_index()
        accounts_map = index.get("accounts") or {}
        if req.id not in accounts_map:
            return JSONResponse({"error": "账号不存在"}, status_code=404)
        del accounts_map[req.id]
        if not accounts_map:
            aid = "default"
            ACCOUNTS_DIR.joinpath(aid, "profile").mkdir(parents=True, exist_ok=True)
            ACCOUNTS_DIR.joinpath(aid, "export").mkdir(parents=True, exist_ok=True)
            accounts_map[aid] = {
                "id": aid, "name": "默认账号", "uid": "", "created_at": int(time.time()),
            }
            index["active"] = aid
        elif index.get("active") == req.id:
            index["active"] = next(iter(accounts_map))
        _save_index(index)
    shutil.rmtree(ACCOUNTS_DIR / req.id, ignore_errors=True)
    _account_cache.update(ts=0.0, data=None)
    return {"ok": True, "active": _active_id()}


def _write_input(urls: list[str]) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = INPUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"urls": urls}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, INPUT_FILE)


@router.post("/api/run")
async def run(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - 空 body 也允许，按默认入口尝试
        body = {}
    raw_urls = body.get("urls") or []
    if isinstance(raw_urls, str):
        raw_urls = raw_urls.splitlines()
    urls = [str(u).strip() for u in raw_urls if str(u).strip()]
    seen = set()
    urls = [u for u in urls if not (u in seen or seen.add(u))]
    mode = "full" if str(body.get("mode") or "").lower() == "full" else "incremental"
    only = str(body.get("only") or "").strip()
    if only:
        args = [f"--only={only}", f"--mode={mode}"]
        auto = False
    else:
        auto = bool(body.get("auto")) or not urls
        if auto:
            args = ["--auto", f"--mode={mode}"]
        else:
            _write_input(urls)
            args = [f"--input={INPUT_FILE}", f"--mode={mode}"]
    ok, err = _start(args, "export")
    return {"ok": ok, "error": err, "auto": auto}


@router.post("/api/login")
def login():
    ok, err = _start(["--login"], "login")
    return {"ok": ok, "error": err}


@router.post("/api/stop")
def stop():
    return {"ok": _stop(), "error": "" if _state["running"] else "当前没有运行中的任务"}


@router.get("/api/raw")
def raw_json():
    path = _out() / "raw.json"
    if not path.exists():
        return JSONResponse({"ok": False, "error": "还没有导出数据"}, status_code=404)
    return FileResponse(path, media_type="application/json; charset=utf-8",
                        filename="淘宝聊天记录_raw.json")


@router.get("/{asset:path}", include_in_schema=False)
def exported_asset(asset: str):
    base = _out().resolve()
    target = (base / asset).resolve()
    if target != base and base not in target.parents:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    if not target.is_file():
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return FileResponse(target)


def _shutdown() -> None:
    with _lock:
        proc = _state["proc"]
    if proc is not None and proc.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
            )
        else:
            proc.terminate()


atexit.register(_shutdown)

_rotate_log()
