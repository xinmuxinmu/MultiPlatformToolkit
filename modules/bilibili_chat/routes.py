import atexit
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

HOME = Path(os.environ.get("BILI_CHAT_HOME") or Path(__file__).resolve().parent / "data")
UI_PATH = Path(__file__).resolve().parent / "static" / "index.html"
TOOL_DIR = Path(__file__).resolve().parent / "tool"
RUN_LOG = HOME / "run.log"
EXPORT_HTML = "B站聊天记录.html"

# 多账号：每个账号独立 profile / state.json / export
ACCOUNTS_DIR = HOME / "accounts"
INDEX_FILE = ACCOUNTS_DIR / "index.json"
LEGACY_OUT = Path(os.environ.get("BILI_CHAT_OUT") or HOME / "export")
LEGACY_STATE = Path(os.environ.get("BILI_CHAT_STATE") or HOME / "state.json")
LEGACY_PROFILE = Path(os.environ.get("BILI_CHAT_PROFILE") or HOME / "profile")

# playwright-core 允许复用 weibo_chat 工具目录里已安装的副本
_SHARED_PW = Path(__file__).resolve().parent.parent / "weibo_chat" / "tool" / "node_modules" / "playwright-core"

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
            (LEGACY_STATE, adir / "state.json"),
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
                    "id": aid, "name": "默认账号", "nickname": "", "uid": "",
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


def _state_file(account_id: str = "") -> Path:
    return _account_dir(account_id) / "state.json"


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
    accounts = index.get("accounts") or {}
    active = _active_id()
    items = []
    for aid, meta in accounts.items():
        items.append({
            "id": aid,
            "name": meta.get("name") or aid,
            "nickname": meta.get("nickname") or "",
            "uid": meta.get("uid") or "",
            "logged_in": _state_file(aid).exists(),
            "active": aid == active,
        })
    return items


router = APIRouter(prefix="/bili-chat")

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


def _node() -> str | None:
    return shutil.which("node")


def _playwright_ready() -> bool:
    return (TOOL_DIR / "node_modules" / "playwright-core").exists() or _SHARED_PW.exists()


def _env() -> dict:
    env = os.environ.copy()
    env["BILI_CHAT_HOME"] = str(HOME)
    env["BILI_CHAT_OUT"] = str(_out())
    env["BILI_CHAT_STATE"] = str(_state_file())
    env["BILI_CHAT_PROFILE"] = str(_profile_dir())
    try:
        from app.ui_settings import is_headless
        env["BILI_HEADLESS"] = "1" if is_headless() else "0"
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
    node = _node()
    if not node:
        return False, "未找到 Node.js，请先安装 Node 16 以上版本"
    if not _playwright_ready():
        return False, "缺少 playwright-core，请在 modules/bilibili_chat/tool 下执行 npm install"

    _account_cache.update(ts=0.0, data=None)
    with _lock:
        if _state["running"]:
            return False, "已有任务在运行，请等待结束或先停止"
        _state["running"] = True
        _state["kind"] = kind
        _state["started_at"] = time.time()
        _state["exit_code"] = None
        _state["stop_requested"] = False

    cmd = [node, "export.js", *args]
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
    out = _out()
    state = _state_file()
    info: dict = {
        "output_dir": str(out),
        "export_html": (out / EXPORT_HTML).exists(),
        "state_exists": state.exists(),
        "state_mtime": state.stat().st_mtime if state.exists() else None,
    }
    raw = out / "raw.json"
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
                incremental=data.get("fixed") is True,
            )
        except (OSError, ValueError):
            info["raw_error"] = "raw.json 解析失败"
    for sub in ("images", "avatars"):
        d = out / sub
        info[sub] = sum(1 for p in d.iterdir() if p.is_file()) if d.is_dir() else 0
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
            "node": bool(_node()),
        }


@router.get("", include_in_schema=False)
def root_redirect():
    return RedirectResponse("/bili-chat/", status_code=307)


@router.get("/", include_in_schema=False)
def console():
    return FileResponse(UI_PATH, headers={"Cache-Control": "no-store"})


VIEWER_TEMPLATE = TOOL_DIR / "template.html"


def _render_viewer(data: dict) -> None:
    tpl = VIEWER_TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    page = _out() / EXPORT_HTML
    tmp = page.with_name(page.name + ".tmp")
    tmp.write_text(tpl.replace("__DATA__", payload), encoding="utf-8")
    os.replace(tmp, page)


@router.get("/viewer", include_in_schema=False)
def viewer():
    page = _out() / EXPORT_HTML
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
                "node": bool(_node()),
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

    node = _node()
    if not node:
        return {"ok": False, "error": "未找到 Node.js"}
    if not _state_file().exists():
        return {"ok": True, "logged_in": False, "name": "", "uid": "", "note": "还没有保存登录态"}

    with _account_lock:
        try:
            proc = subprocess.run(
                [node, "export.js", "--status"],
                cwd=str(TOOL_DIR),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
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
    if result["logged_in"]:
        _update_account_meta(_active_id(), nickname=result["name"], uid=result["uid"])
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
            (i for i, c in enumerate(convs) if str(c.get("uid") or c.get("name") or "") == req.id),
            -1,
        )
        if idx < 0:
            return JSONResponse({"error": "会话不存在，可能已被删除"}, status_code=404)
        removed = convs.pop(idx)
        data["convs"] = convs
        data["total"] = sum(int(c.get("count") or len(c.get("messages") or [])) for c in convs)
        try:
            tmp = raw_path.with_name(raw_path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
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
            "id": aid, "name": name, "nickname": "", "uid": "",
            "created_at": int(time.time()),
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
                "id": aid, "name": "默认账号", "nickname": "", "uid": "",
                "created_at": int(time.time()),
            }
            index["active"] = aid
        elif index.get("active") == req.id:
            index["active"] = next(iter(accounts_map))
        _save_index(index)
    shutil.rmtree(ACCOUNTS_DIR / req.id, ignore_errors=True)
    _account_cache.update(ts=0.0, data=None)
    return {"ok": True, "active": _active_id()}


@router.post("/api/run")
def run(mode: str = "incremental", max_group: int = 0, max_dm: int = 0,
        only_uid: str = ""):
    args: list[str] = []
    if mode == "full":
        args.append("--mode=full")
    if max_group > 0:
        args.append(f"--max-group={max_group}")
    if max_dm > 0:
        args.append(f"--max-dm={max_dm}")
    if only_uid.strip():
        args.append(f"--only-uid={only_uid.strip()}")
    ok, err = _start(args, "full" if mode == "full" else "export")
    return {"ok": ok, "error": err}


@router.post("/api/login")
def login():
    ok, err = _start(["--login"], "login")
    return {"ok": ok, "error": err}


@router.post("/api/stop")
def stop():
    return {"ok": _stop(), "error": "" if _state["running"] else "当前没有运行中的任务"}


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


_rotate_log()
atexit.register(_shutdown)
