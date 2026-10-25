import os
import re
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

HOME = Path(os.environ.get("STARSKY_HOME") or Path(__file__).resolve().parent / "data")
SCRIPTS = Path(__file__).resolve().parent / "scripts"
PS1 = SCRIPTS / "StarSky签到.ps1"
INI = HOME / "设置.ini"
LOG = HOME / "日志" / "签到日志.txt"
UI_PATH = Path(__file__).resolve().parent / "static" / "index.html"

TASK_NAME = "StarSky自动签到"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DECODE = "mbcs" if os.name == "nt" else "utf-8"

DEFAULT_INI = """[StarSky]
; StarSky.exe 的完整路径（留空自动识别）
软件路径=

; 软件没运行时，是否自动打开它（1=是，0=否）
自动启动=1

; 启动后等待界面加载、识别按钮的最长秒数
等待超时=30

; 签到结束后是否自动关闭 StarSky（1=是，0=否）
签到后关闭软件=1
"""

CONFIG_KEYS = ("软件路径", "自动启动", "等待超时", "签到后关闭软件")

router = APIRouter(prefix="/starsky")

_lock = threading.Lock()
_state = {
    "proc": None,
    "running": False,
    "via": "",
    "started_at": None,
    "exit_code": None,
}


def _ensure_ini() -> None:
    if not INI.exists():
        INI.parent.mkdir(parents=True, exist_ok=True)
        INI.write_text(DEFAULT_INI, encoding="utf-8")


def _read_ini() -> dict:
    cfg = {"软件路径": "", "自动启动": "1", "等待超时": "30", "签到后关闭软件": "1"}
    try:
        text = INI.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return cfg
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith((";", "#", "[")) or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        if k in cfg:
            cfg[k] = v.strip()
    return cfg


def _write_ini(values: dict) -> None:
    _ensure_ini()
    lines = INI.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)([^;#=\s][^=]*?)(\s*=\s*)(.*)$", line)
        if not m:
            continue
        key = m.group(2).strip()
        if key in values:
            lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}{values[key]}"
            seen.add(key)
    for k, v in values.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    INI.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")


def _is_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _run_quiet(cmd: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        out = (p.stdout or b"") + (p.stderr or b"")
        return p.returncode, out.decode(DECODE, errors="replace").strip()
    except subprocess.TimeoutExpired:
        return -1, "命令超时"
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def _task_info() -> dict:
    if os.name != "nt":
        return {"exists": False, "info": {}, "error": "仅支持 Windows"}
    code, text = _run_quiet(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"], timeout=15
    )
    if code != 0:
        return {"exists": False, "info": {}, "error": text[-300:]}
    keep = ("任务名", "下次运行时间", "状态", "上次运行时间", "上次结果", "要运行的任务", "计划")
    info = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if k in keep:
            info[k] = v
    return {"exists": True, "info": info, "error": ""}


def _log_tail(limit: int = 200) -> list[str]:
    try:
        if not LOG.exists():
            return []
        with LOG.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            raw = f.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()
        return lines[-limit:]
    except OSError:
        return []


def _reader(proc: subprocess.Popen) -> None:
    code = proc.wait()
    with _lock:
        if _state["proc"] is proc:
            _state["proc"] = None
            _state["running"] = False
            _state["exit_code"] = code


def _monitor_task(started: float) -> None:
    time.sleep(4)
    while time.time() - started < 240:
        if not _state["running"]:
            return
        q = _task_info()
        status = (q.get("info") or {}).get("状态", "")
        if not q.get("exists") or "运行" not in status:
            break
        time.sleep(2)
    with _lock:
        if _state["via"] == "task":
            _state["running"] = False


def _start_direct() -> tuple[bool, str]:
    if not PS1.exists():
        return False, f"找不到脚本：{PS1}"
    env = os.environ.copy()
    env["STARSKY_HOME"] = str(HOME)
    try:
        proc = subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(PS1),
                "-Quiet",
            ],
            cwd=str(SCRIPTS),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"启动失败：{exc}"
    with _lock:
        _state.update(
            proc=proc, running=True, via="direct", started_at=time.time(), exit_code=None
        )
    threading.Thread(target=_reader, args=(proc,), daemon=True).start()
    return True, ""


@router.get("/", include_in_schema=False)
def page():
    return FileResponse(UI_PATH, headers={"Cache-Control": "no-store"})


@router.get("/api/status")
def status():
    cfg = _read_ini()
    exe = (cfg.get("软件路径") or "").strip().strip('"')
    with _lock:
        state = {
            "running": _state["running"],
            "via": _state["via"],
            "started_at": _state["started_at"],
            "exit_code": _state["exit_code"],
        }
    lines = _log_tail(150)
    return {
        "ok": True,
        "admin": _is_admin(),
        "config": cfg,
        "config_path": str(INI),
        "exe_exists": bool(exe) and Path(exe).is_file(),
        "log_path": str(LOG),
        "log_mtime": LOG.stat().st_mtime if LOG.exists() else None,
        "log_tail": lines,
        "last_line": lines[-1] if lines else "",
        "task": _task_info(),
        "task_name": TASK_NAME,
        "state": state,
    }


@router.post("/api/config")
def save_config(body: dict):
    values = {}
    for key in CONFIG_KEYS:
        if key in body:
            values[key] = str(body[key]).strip()
    if "自动启动" in values and values["自动启动"] not in ("0", "1"):
        return JSONResponse({"ok": False, "error": "自动启动只能是 0 或 1"}, status_code=400)
    if "签到后关闭软件" in values and values["签到后关闭软件"] not in ("0", "1"):
        return JSONResponse({"ok": False, "error": "签到后关闭软件只能是 0 或 1"}, status_code=400)
    if "等待超时" in values:
        try:
            wait = int(values["等待超时"])
        except ValueError:
            return JSONResponse({"ok": False, "error": "等待超时必须是整数秒"}, status_code=400)
        if not 5 <= wait <= 300:
            return JSONResponse({"ok": False, "error": "等待超时范围 5-300 秒"}, status_code=400)
        values["等待超时"] = str(wait)
    _write_ini(values)
    return {"ok": True, "config": _read_ini()}


@router.post("/api/run")
def run():
    with _lock:
        if _state["running"]:
            return JSONResponse({"ok": False, "error": "已有签到任务在进行中"}, status_code=409)
    info = _task_info()
    if info.get("exists"):
        code, out = _run_quiet(["schtasks", "/Run", "/TN", TASK_NAME], timeout=20)
        if code != 0:
            return JSONResponse({"ok": False, "error": out or "计划任务启动失败"}, status_code=500)
        with _lock:
            _state.update(
                proc=None, running=True, via="task", started_at=time.time(), exit_code=None
            )
        threading.Thread(target=_monitor_task, args=(time.time(),), daemon=True).start()
        return {
            "ok": True,
            "via": "task",
            "note": "已通过计划任务启动，签到期间会抢占鼠标和前台窗口",
        }
    if not _is_admin():
        ok, err = _start_direct()
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=500)
        return {
            "ok": True,
            "via": "uac",
            "note": "已发起提权，请在桌面确认 UAC 弹窗；签到期间会抢占鼠标和前台窗口",
        }
    ok, err = _start_direct()
    if not ok:
        return JSONResponse({"ok": False, "error": err}, status_code=500)
    return {"ok": True, "via": "direct", "note": "签到期间会抢占鼠标和前台窗口"}


@router.post("/api/stop")
def stop():
    with _lock:
        proc = _state["proc"]
        if not _state["running"]:
            return {"ok": False, "error": "当前没有运行中的签到"}
        if _state["via"] != "direct" or proc is None:
            return {"ok": False, "error": "计划任务启动的签到无法从此处停止"}
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )
    return {"ok": True}


@router.post("/api/schedule")
def create_schedule(body: dict):
    at = str(body.get("time") or "").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
        return JSONResponse({"ok": False, "error": "时间格式应为 HH:MM"}, status_code=400)
    if not PS1.exists():
        return JSONResponse({"ok": False, "error": f"找不到脚本：{PS1}"}, status_code=500)
    tr = (
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden "
        f'-File "{PS1}" -Quiet'
    )
    code, out = _run_quiet(
        [
            "schtasks",
            "/Create",
            "/TN",
            TASK_NAME,
            "/TR",
            tr,
            "/SC",
            "DAILY",
            "/ST",
            at,
            "/RL",
            "HIGHEST",
            "/F",
        ],
        timeout=30,
    )
    if code != 0:
        return JSONResponse({"ok": False, "error": out or "创建计划任务失败"}, status_code=500)
    return {"ok": True, "time": at, "note": out[-300:]}


@router.delete("/api/schedule")
def delete_schedule():
    code, out = _run_quiet(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], timeout=20)
    if code != 0:
        return JSONResponse({"ok": False, "error": out or "删除计划任务失败"}, status_code=500)
    return {"ok": True, "note": out[-300:]}


_ensure_ini()
