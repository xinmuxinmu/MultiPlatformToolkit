import ctypes
import os
import socket
import sys
import threading
import time

import uvicorn

from app.browser import open_app_window, watch_app_window
from app.job import enable_kill_on_close
from app.paths import DATA_ROOT, HOST, PORT

_LOG_MAX_BYTES = 2 * 1024 * 1024


class _Coord(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _SmallRect(ctypes.Structure):
    _fields_ = [
        ("Left", ctypes.c_short),
        ("Top", ctypes.c_short),
        ("Right", ctypes.c_short),
        ("Bottom", ctypes.c_short),
    ]


class _ScreenBufferInfo(ctypes.Structure):
    _fields_ = [
        ("dwSize", _Coord),
        ("dwCursorPosition", _Coord),
        ("wAttributes", ctypes.c_ushort),
        ("srWindow", _SmallRect),
        ("dwMaximumWindowSize", _Coord),
    ]


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_LAUNCHER_NAMES = {"多平台工具.exe", "multi-tool.exe"}


def _console_title() -> str:
    if os.name != "nt":
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.kernel32.GetConsoleTitleW(buf, len(buf))
        return buf.value
    except OSError:
        return ""


def _launched_by_desktop_launcher() -> bool:
    """桌面启动器没传环境变量，只能从控制台标题或父进程链认出来。"""
    if os.name != "nt":
        return False
    title = _console_title().lower()
    if "multi-tool" in title or "多平台工具" in title:
        return True

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        kernel32.Process32FirstW.restype = ctypes.c_int
        kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32W)]
        kernel32.Process32NextW.restype = ctypes.c_int
        kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32W)]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return False
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        parents: dict[int, tuple[int, str]] = {}
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                parents[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
        kernel32.CloseHandle(snapshot)

        pid = os.getpid()
        for _ in range(16):
            ppid, name = parents.get(pid, (0, ""))
            if name.lower() in _LAUNCHER_NAMES:
                return True
            if not ppid or ppid == pid:
                return False
            pid = ppid
    except OSError:
        pass
    return False


def quiet_mode() -> bool:
    if os.environ.get("APP_VERBOSE"):
        return False
    if os.environ.get("APP_QUIET") == "1" or os.environ.get("NO_BROWSER"):
        return True
    return _launched_by_desktop_launcher()


def clear_console() -> None:
    """清掉启动器已打印的提示，控制台只留一个空黑框。"""
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetStdHandle.argtypes = [ctypes.c_uint32]
        handle = kernel32.GetStdHandle(0xFFFFFFF5)  # STD_OUTPUT_HANDLE
        if not handle or handle == ctypes.c_void_p(-1).value:
            return
        info = _ScreenBufferInfo()
        kernel32.GetConsoleScreenBufferInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ScreenBufferInfo),
        ]
        if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return
        cells = info.dwSize.X * info.dwSize.Y
        origin = _Coord(0, 0)
        written = ctypes.c_uint32(0)
        kernel32.FillConsoleOutputCharacterW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar,
            ctypes.c_uint32,
            _Coord,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        kernel32.FillConsoleOutputCharacterW(handle, " ", cells, origin, ctypes.byref(written))
        kernel32.FillConsoleOutputAttribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ushort,
            ctypes.c_uint32,
            _Coord,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        kernel32.FillConsoleOutputAttribute(
            handle, info.wAttributes, cells, origin, ctypes.byref(written)
        )
        kernel32.SetConsoleCursorPosition.argtypes = [ctypes.c_void_p, _Coord]
        kernel32.SetConsoleCursorPosition(handle, origin)
    except OSError:
        pass


def redirect_output_to_log() -> bool:
    """控制台保持空白，所有输出改写到 data/logs/app.log。"""
    log_dir = DATA_ROOT / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "app.log"
    except OSError:
        return False
    try:
        if log_path.exists() and log_path.stat().st_size > _LOG_MAX_BYTES:
            os.replace(log_path, log_dir / "app.log.1")
    except OSError:
        pass  # 旧日志可能正被另一个实例占用，继续追加即可
    try:
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
    except OSError:
        return False
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)
    sys.stdout = open(1, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)
    sys.stderr = open(2, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)
    print(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====")
    return True


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


def main() -> int:
    quiet = quiet_mode()
    if quiet:
        clear_console()
        quiet = redirect_output_to_log()

    # 各业务模块导入时可能打印日志，安静模式下要先完成清屏和重定向。
    from app.server import app

    url = f"http://{HOST}:{PORT}/"
    if port_in_use(HOST, PORT):
        print(f"程序已经在运行：{url}")
        print("如果页面还是旧版本，请先关闭之前的程序窗口（或结束占用该端口的 Python 进程）再重新启动。")
        open_app_window(url)
        return 2

    # 主窗口关闭时由系统结束 Job 内所有子进程（浏览器、Node、PowerShell 等）。
    enable_kill_on_close()

    server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=PORT, log_level="info", access_log=not quiet)
    )
    app.state.uvicorn_server = server
    # 前端 app 窗口关闭后也让后端退出：桌面启动器会用 NO_BROWSER 自己开窗口，
    # 所以这里不跟着 NO_BROWSER 开关走。
    watch_app_window(server)
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.5, lambda: open_app_window(url)).start()

    print(f"统一入口：{url}")
    print("关闭应用窗口或此控制台窗口即退出程序，所有由它打开的窗口和子进程会一并关闭。")
    try:
        server.run()
    except KeyboardInterrupt:
        # Ctrl+C 是正常退出方式：uvicorn 已经优雅停服，这里吞掉它，
        # 不要再把 asyncio 的 KeyboardInterrupt 堆栈打到控制台吓人。
        print("\n已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
