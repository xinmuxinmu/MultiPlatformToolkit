"""Python-native desktop/service entry point with single-window activation."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parent
_LOG_STREAM = None


def truthy(value):
    return str(value or "").strip().lower() not in ("", "0", "false", "no", "off")


def report_startup(state, message=""):
    value = os.environ.get("APP_STARTUP_STATUS")
    if not value:
        return
    try:
        target = Path(value)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"state": state, "message": message, "pid": os.getpid()}, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, target)
    except OSError:
        pass


def redirect_output_to_log():
    global _LOG_STREAM
    directory = Path(os.environ.get("APP_DATA_DIR") or ROOT / "data") / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "app.log"
    try:
        if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
            os.replace(path, directory / "app.log.1")
    except OSError:
        pass
    _LOG_STREAM = path.open("a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = sys.stderr = _LOG_STREAM
    print(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 pid={os.getpid()} =====")


def port_in_use(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(.4)
        return connection.connect_ex((host, port)) == 0


def instance_name(host, port, root=ROOT):
    # Junctions and the real renamed folder must address the same desktop.
    identity = f"{root.resolve()}|{host}:{port}|{getpass.getuser()}"
    return "MultiPlatformToolkit-" + hashlib.sha256(identity.casefold().encode("utf-8")).hexdigest()[:24]


def acquire_desktop(host, port):
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication, QMainWindow
    app = QApplication.instance() or QApplication(sys.argv[:1])
    name = instance_name(host, port)

    def activate_existing():
        peer = QLocalSocket()
        peer.connectToServer(name)
        if not peer.waitForConnected(700):
            return False
        peer.write(b"ACTIVATE\n")
        peer.waitForBytesWritten(700)
        peer.disconnectFromServer()
        return True

    if activate_existing():
        return app, None, True
    server = QLocalServer(app)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(name):
        if activate_existing():
            server.deleteLater()
            return app, None, True
        raise RuntimeError("无法建立桌面单实例通道；已有程序可能仍在启动或退出，请稍后再试。")

    def activate():
        while server.hasPendingConnections():
            peer = server.nextPendingConnection()
            peer.close()
            peer.deleteLater()
        for window in app.topLevelWidgets():
            if isinstance(window, QMainWindow) and window.isVisible():
                if window.isMinimized():
                    window.showNormal()
                window.raise_()
                window.activateWindow()
    server.newConnection.connect(activate)
    return app, server, False


def read_runtime(url):
    import httpx
    with httpx.Client(timeout=3, trust_env=False) as client:
        health = client.get(url + "/api/desktop/health")
        runtime = client.get(url + "/api/desktop/runtime")
        health.raise_for_status()
        runtime.raise_for_status()
        health, runtime = health.json(), runtime.json()
    if health.get("app") != "MultiPlatformToolkit" or health.get("architecture") != "python-native":
        raise ValueError("端口由其他程序使用")
    if Path(runtime.get("root") or "").resolve() != ROOT.resolve():
        raise ValueError("端口由另一个项目副本使用")
    return runtime


def wait_ready(url, token, thread, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not thread.is_alive():
            return False
        try:
            if read_runtime(url).get("token") == token:
                return True
        except Exception:
            pass
        time.sleep(.15)
    return False


def main(argv=None):
    if sys.stdout is None or sys.stderr is None:
        redirect_output_to_log()
    parser = argparse.ArgumentParser(description="MultiPlatformToolkit · Python 原生桌面工具")
    parser.add_argument("--headless", action="store_true", help="仅运行 Python 服务，保留控制台")
    parser.add_argument("--no-schedule", action="store_true", help="本次启动不恢复自动任务")
    parser.add_argument("--check", action="store_true", help="检查 Python 服务和原生界面依赖后退出")
    parser.add_argument("--verbose", action="store_true", help="在控制台显示完整启动和运行日志")
    args = parser.parse_args(argv)
    if args.no_schedule or args.check:
        os.environ["APP_DISABLE_SCHEDULES"] = "1"
    verbose = args.verbose or truthy(os.environ.get("APP_VERBOSE"))
    if verbose:
        os.environ["APP_VERBOSE"] = "1"
    headless = args.headless or truthy(os.environ.get("NO_BROWSER"))
    quiet = not (verbose or headless or args.check) and truthy(os.environ.get("APP_QUIET"))
    if quiet and _LOG_STREAM is None:
        redirect_output_to_log()
    from app.paths import HOST, PORT
    from app.qt_window import available, run_app_window
    if args.check:
        from app.server import desktop_health, desktop_catalog
        if not available():
            raise RuntimeError("缺少 PySide6：请运行 python -m pip install -r requirements.txt")
        from app.desktop.window import DesktopWindow  # noqa: F401
        print(json.dumps({**desktop_health(), "operations": len(desktop_catalog()["operations"])}, ensure_ascii=False))
        return 0
    guard = qt_app = None
    if not headless:
        if not available():
            raise RuntimeError("缺少 PySide6 原生界面依赖，请运行 python -m pip install -r requirements.txt")
        qt_app, guard, existing = acquire_desktop(HOST, PORT)
        if existing:
            report_startup("activated", "已唤起运行中的桌面窗口")
            return 0
    url = f"http://{HOST}:{PORT}"
    server = server_thread = None
    try:
        if port_in_use(HOST, PORT):
            try:
                runtime = read_runtime(url)
            except Exception as exc:
                raise RuntimeError(f"端口 {PORT} 已被旧版、其他项目或其他程序占用。请先关闭对应程序或设置 APP_PORT；不会结束其他进程。") from exc
            if headless:
                print(f"Python 服务已在运行：{url}")
                report_startup("ready", "Python 服务已运行")
                return 0
            if runtime.get("mode") != "headless":
                raise RuntimeError("已有桌面进程正在启动或退出，请稍后重新运行启动器以唤起窗口。")
        else:
            import uvicorn
            from app.job import enable_kill_on_close
            from app.server import app
            enable_kill_on_close()
            token = uuid.uuid4().hex
            app.state.toolkit_runtime = {"root": str(ROOT.resolve()), "pid": os.getpid(),
                                         "mode": "headless" if headless else "desktop", "token": token}
            if not any(getattr(route, "path", "") == "/api/desktop/runtime" for route in app.routes):
                @app.get("/api/desktop/runtime", include_in_schema=False)
                def runtime_info():
                    return dict(app.state.toolkit_runtime)
            server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="info", access_log=not quiet))
            app.state.uvicorn_server = server
            if headless:
                try:
                    server.run()
                except KeyboardInterrupt:
                    pass
                return 0
            server_thread = threading.Thread(target=server.run, name="python-service", daemon=True)
            server_thread.start()
            if not wait_ready(url, token, server_thread):
                raise RuntimeError("Python 服务未能启动，请查看 data/logs/app.log，或使用 启动.bat --verbose 诊断。")
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: report_startup("ready", "原生桌面已就绪"))
        return run_app_window(url, on_close=lambda: setattr(server, "should_exit", True) if server else None)
    finally:
        if server:
            server.should_exit = True
        if server_thread:
            server_thread.join(timeout=10)
        if guard:
            guard.close()


def entry(argv=None):
    try:
        return main(argv)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        report_startup("error", str(exc))
        if sys.stderr is None:
            redirect_output_to_log()
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(entry())
