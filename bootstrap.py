"""Prepare the Python environment and launch the native desktop without a console."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
IMPORTS = ("fastapi", "uvicorn", "PySide6.QtWidgets", "playwright.sync_api", "Crypto", "apscheduler",
           "cryptography", "httpx", "PIL", "pillow_heif", "truststore", "certifi", "selenium", "schedule", "requests")


def truthy(value):
    return str(value or "").strip().lower() not in ("", "0", "false", "no", "off")


def environment_python(root=ROOT):
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def dependencies_ready(python):
    probe = "import importlib; [importlib.import_module(name) for name in " + repr(IMPORTS) + "]"
    result = subprocess.run([str(python), "-X", "utf8", "-c", probe], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=40)
    return result.returncode == 0


def interpreter_usable(python):
    try:
        result = subprocess.run([str(python), "-c", "import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)"],
                                capture_output=True, timeout=15)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def prepare_environment(root=ROOT):
    environment, python = root / ".venv", environment_python(root)
    if not python.is_file() or not interpreter_usable(python):
        print("正在创建 / 修复 Python 虚拟环境…", flush=True)
        base = getattr(sys, "_base_executable", None) or sys.executable
        result = subprocess.run([base, "-m", "venv", str(environment)], cwd=root)
        if result.returncode or not python.is_file() or not interpreter_usable(python):
            raise RuntimeError("虚拟环境创建失败。请确认安装了完整的 Python 3.10+（包含 venv/pip），并查看上方错误。")
    requirements = root / "requirements.txt"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = environment / "toolkit-requirements.sha256"
    try:
        installed = marker.read_text(encoding="ascii").strip()
    except OSError:
        installed = ""
    if installed != digest or not dependencies_ready(python):
        print("首次使用、依赖清单更新或环境缺少依赖，正在安装 Python 组件…", flush=True)
        result = subprocess.run([str(python), "-m", "pip", "install", "-r", str(requirements)], cwd=root)
        if result.returncode:
            raise RuntimeError(f"依赖安装失败（退出码 {result.returncode}）。请检查上方网络/安装错误后重试；本次不会启动不完整的程序。")
        if not dependencies_ready(python):
            raise RuntimeError("依赖安装命令已结束，但组件仍无法导入。请在 .venv 中运行 main.py --check 查看详细错误。")
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(digest, encoding="ascii")
        os.replace(temporary, marker)
    return python


def foreground_mode(arguments, environment=None):
    environment = os.environ if environment is None else environment
    return os.name != "nt" or any(flag in arguments for flag in ("--verbose", "--headless", "--check", "--help", "-h")) or truthy(environment.get("APP_VERBOSE")) or truthy(environment.get("NO_BROWSER"))


def wait_for_startup(process, status_path, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
        if status.get("state") in ("ready", "activated"):
            return True, status.get("message") or "桌面已启动"
        if status.get("state") == "error":
            return False, status.get("message") or "桌面启动失败"
        code = process.poll()
        if code is not None:
            return False, f"程序在显示窗口前退出（退出码 {code}）。"
        time.sleep(.1)
    return False, "等待窗口就绪超时。程序可能仍在启动，请查看日志；再次启动会尝试唤起同一个窗口。"


def launch(python, arguments, root=ROOT):
    environment = os.environ.copy()
    environment["PYTHONUTF8"], environment["PYTHONIOENCODING"] = "1", "utf-8"
    if foreground_mode(arguments, environment):
        if "--verbose" in arguments or truthy(environment.get("APP_VERBOSE")):
            environment["APP_VERBOSE"], environment["APP_QUIET"] = "1", "0"
        else:
            environment["APP_QUIET"] = "0"
        return subprocess.call([str(python), "-X", "utf8", str(root / "main.py"), *arguments], cwd=root, env=environment)
    pythonw = python.with_name("pythonw.exe")
    if not pythonw.is_file():
        raise RuntimeError(f"找不到原生无控制台解释器：{pythonw}。请修复 Python 安装，或使用 启动.bat --verbose 诊断。")
    data = Path(environment.get("APP_DATA_DIR") or root / "data")
    logs = (data if data.is_absolute() else root / data) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    status_path = logs / ("startup-" + uuid.uuid4().hex + ".json")
    environment["APP_STARTUP_STATUS"] = str(status_path)
    environment["APP_QUIET"] = "1"
    log_path = logs / "app.log"
    try:
        with log_path.open("ab") as log:
            process = subprocess.Popen([str(pythonw), "-X", "utf8", str(root / "main.py"), *arguments],
                cwd=root, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ready, message = wait_for_startup(process, status_path)
        if not ready:
            print(message, flush=True)
            print(f"诊断日志：{log_path}\n也可运行：启动.bat --verbose", flush=True)
            return 1
        return 0
    finally:
        status_path.unlink(missing_ok=True)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if sys.version_info < (3, 10):
        print("需要 Python 3.10 或更新版本。", flush=True)
        return 1
    try:
        return launch(prepare_environment(), list(sys.argv[1:] if argv is None else argv))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"启动未完成：{exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
