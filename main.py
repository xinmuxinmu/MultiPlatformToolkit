import os
import socket
import sys
import threading

import uvicorn

from app.browser import open_app_window
from app.job import enable_kill_on_close
from app.paths import HOST, PORT
from app.server import app


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


def main() -> int:
    url = f"http://{HOST}:{PORT}/"
    if port_in_use(HOST, PORT):
        print(f"程序已经在运行：{url}")
        print("如果页面还是旧版本，请先关闭之前的程序窗口（或结束占用该端口的 Python 进程）再重新启动。")
        open_app_window(url)
        return 2

    # 主窗口关闭时由系统结束 Job 内所有子进程（浏览器、Node、PowerShell 等）。
    enable_kill_on_close()

    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="info"))
    app.state.uvicorn_server = server
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.5, lambda: open_app_window(url)).start()

    print(f"统一入口：{url}")
    print("关闭此窗口即退出程序，所有由它打开的窗口和子进程会一并关闭。")
    try:
        server.run()
    except KeyboardInterrupt:
        # Ctrl+C 是正常退出方式：uvicorn 已经优雅停服，这里吞掉它，
        # 不要再把 asyncio 的 KeyboardInterrupt 堆栈打到控制台吓人。
        print("\n已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
