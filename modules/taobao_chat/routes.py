"""淘宝旺旺聊天记录导出（买家侧）：登录、导出、离线查看。

复用微博聊天记录那套结构：FastAPI 控制台 + Node/Playwright 导出工具 + 本地 HTML 查看器。
淘宝网页版没有公开的买家会话列表接口，所以导出目标由用户在控制台粘贴聊天链接
（聊天窗口地址栏里的 .../app/im/chat/index.html?&uid=cntaobaoXXX&type=web）。
"""
import atexit
import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

HOME = Path(os.environ.get("TAOBAO_CHAT_HOME") or Path(__file__).resolve().parent / "data")
OUT = Path(os.environ.get("TAOBAO_CHAT_OUT") or HOME / "export")
UI_PATH = Path(__file__).resolve().parent / "static" / "index.html"
USERSCRIPT_PATH = Path(__file__).resolve().parent / "static" / "taobao-export.user.js"
TOOL_DIR = Path(__file__).resolve().parent / "tool"
RUN_LOG = HOME / "run.log"
INPUT_FILE = HOME / "input.json"
VIEWER_NAME = "淘宝聊天记录.html"

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


def _node() -> str | None:
    return shutil.which("node")


def _env() -> dict:
    env = os.environ.copy()
    env["TAOBAO_CHAT_HOME"] = str(HOME)
    env["TAOBAO_CHAT_OUT"] = str(OUT)
    env["TAOBAO_CHAT_PROFILE"] = str(HOME / "profile")
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
    if not (TOOL_DIR / "node_modules" / "playwright-core").exists():
        # 允许复用微博模块里装好的 playwright-core，避免重复安装
        shared = TOOL_DIR.parent.parent / "weibo_chat" / "tool" / "node_modules" / "playwright-core"
        if not shared.exists():
            return False, "缺少 playwright-core，请在 modules/taobao_chat/tool 下执行 npm install"

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
    info: dict = {
        "output_dir": str(OUT),
        "viewer": (OUT / VIEWER_NAME).exists(),
    }
    raw = OUT / "raw.json"
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
            "node": bool(_node()),
        }


@router.get("", include_in_schema=False)
def root_redirect():
    return RedirectResponse("/taobao/", status_code=307)


@router.get("/", include_in_schema=False)
def console():
    return FileResponse(UI_PATH, headers={"Cache-Control": "no-store"})


@router.get("/viewer", include_in_schema=False)
def viewer():
    page = OUT / VIEWER_NAME
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
    if not (HOME / "profile").exists():
        return {"ok": True, "logged_in": False, "name": "", "uid": "", "note": "还没有登录过淘宝"}

    with _account_lock:
        try:
            proc = subprocess.run(
                [node, "export.js", "--status"],
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
    _account_cache.update(ts=time.time(), data=result)
    return {"ok": True, **result, "cached": False}


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
    if not urls:
        return {"ok": False, "error": "请先粘贴至少一个淘宝聊天链接"}
    _write_input(urls)
    args = [f"--input={INPUT_FILE}", f"--mode={mode}"]
    ok, err = _start(args, "export")
    return {"ok": ok, "error": err}


@router.post("/api/login")
def login():
    ok, err = _start(["--login"], "login")
    return {"ok": ok, "error": err}


@router.post("/api/stop")
def stop():
    return {"ok": _stop(), "error": "" if _state["running"] else "当前没有运行中的任务"}


@router.get("/api/raw")
def raw_json():
    path = OUT / "raw.json"
    if not path.exists():
        return JSONResponse({"ok": False, "error": "还没有导出数据"}, status_code=404)
    return FileResponse(path, media_type="application/json; charset=utf-8",
                        filename="淘宝聊天记录_raw.json")


@router.get("/{asset:path}", include_in_schema=False)
def exported_asset(asset: str):
    base = OUT.resolve()
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
