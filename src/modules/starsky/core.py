"""StarSky 签到：本地账户后台接口，或 Win32 + Pillow 窗口点击。

后台模式确认服务端返回结果；窗口模式仅在确认按钮变灰时成功，无法确认时保留截图。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

DATA_ROOT = Path(os.environ.get("APP_DATA_DIR") or Path(__file__).resolve().parents[3] / "data")
HOME = Path(os.environ.get("STARSKY_HOME") or DATA_ROOT / "starsky")
INI = HOME / "设置.ini"
LOG_DIR = HOME / "日志"
LOG = LOG_DIR / "签到日志.txt"
TASK_NAME = "StarSky自动签到"
SCRIPT = Path(__file__).resolve().parent / "scripts" / "checkin.py"


def log(message, quiet=False):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + message + "\n")
    if not quiet:
        print(message, flush=True)


def read_config():
    defaults = {"签到方式": "api", "软件路径": "", "自动启动": "1", "等待超时": "30", "签到后关闭软件": "1"}
    try:
        for line in INI.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith((";", "#")):
                key, value = line.split("=", 1)
                if key.strip() in defaults:
                    defaults[key.strip()] = value.strip()
    except OSError:
        pass
    return defaults


def is_admin():
    return os.name == "nt" and bool(ctypes.windll.shell32.IsUserAnAdmin())


def run_elevated(executable, args, timeout=180):
    """用 ShellExecuteEx 原生申请提权并等待退出，无 PowerShell 中间层。"""
    if os.name != "nt":
        return -1, "仅支持 Windows"
    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                    ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE)]
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40 | 0x100  # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = str(executable)
    info.lpParameters = subprocess.list2cmdline([str(arg) for arg in args])
    info.lpDirectory = str(SCRIPT.parent)
    info.nShow = 0
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error()
        return code or 1, "已取消管理员授权" if code == 1223 else f"提权失败（{code}）"
    try:
        if kernel.WaitForSingleObject(info.hProcess, int(timeout * 1000)) == 0x102:
            return -1, "等待管理员任务完成超时，请检查 StarSky 窗口"
        code = wintypes.DWORD()
        kernel.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value), ""
    finally:
        kernel.CloseHandle(info.hProcess)


def schedule_args(at):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
        raise ValueError("时间格式应为 HH:MM")
    command = subprocess.list2cmdline([sys.executable, str(SCRIPT), "--quiet", "--home", str(HOME)])
    return ["/Create", "/TN", TASK_NAME, "/TR", command, "/SC", "DAILY", "/ST", at, "/RL", "HIGHEST", "/F"]


def longest_band(counts, start, end, threshold):
    best_start, best_length, current_start, length = -1, 0, -1, 0
    for coordinate in range(start, end + 1, 2):
        if counts[coordinate] >= threshold:
            if not length:
                current_start = coordinate
            length += 1
            if length > best_length:
                best_start, best_length = current_start, length
        else:
            length = 0
    return best_start, best_length


def find_button(image):
    image = image.convert("RGB")
    width, height = image.size
    x0, x1 = int(width * .65), min(width - 1, int(width * .98))
    y0, y1 = int(height * .66), min(height - 1, int(height * .89))
    rows = [[0] * height for _ in range(2)]
    cols = [[0] * width for _ in range(2)]
    pixels = image.load()
    for y in range(y0, y1 + 1, 2):
        for x in range(x0, x1 + 1, 2):
            red, green, blue = pixels[x, y]
            kind = -1
            if blue >= 200 and 100 <= green <= 195 and red <= 125 and blue - red >= 90:
                kind = 0
            elif abs(red - 199) <= 8 and abs(green - 200) <= 8 and abs(blue - 202) <= 8:
                kind = 1
            if kind >= 0:
                rows[kind][y] += 1
                cols[kind][x] += 1
    for kind, state in enumerate(("Unsigned", "Signed")):
        by, vertical = longest_band(rows[kind], y0, y1, 22)
        bx, horizontal = longest_band(cols[kind], x0, x1, 12)
        button_width, button_height = (horizontal - 1) * 2 + 1, (vertical - 1) * 2 + 1
        if vertical >= 18 and horizontal >= 30 and button_width <= 500 and button_height <= 260:
            return {"state": state, "x": int(bx + button_width / 2), "y": int(by + button_height / 2),
                    "width": button_width, "height": button_height}
    return {"state": "Unknown"}


class Windows:
    """只初始化所需 Win32 API；导入模块不操作桌面。"""
    class Rect(ctypes.Structure):
        _fields_ = [(name, wintypes.LONG) for name in ("left", "top", "right", "bottom")]
    class Point(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.WindowFromPoint.argtypes = [self.Point]
        self.user.WindowFromPoint.restype = wintypes.HWND
        self.user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user.GetAncestor.restype = wintypes.HWND
        for name in ("ShowWindow", "IsIconic", "SetForegroundWindow", "BringWindowToTop", "SetFocus"):
            getattr(self.user, name).argtypes = [wintypes.HWND] + ([ctypes.c_int] if name == "ShowWindow" else [])
        self.user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(self.Rect)]
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.user.SetProcessDPIAware()

    def process_path(self, pid):
        handle = self.kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            return buffer.value if self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else ""
        finally:
            self.kernel.CloseHandle(handle)

    def find_window(self):
        found = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _):
            pid = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            path = self.process_path(pid.value)
            if Path(path).name.lower() == "starsky.exe":
                rect = self.Rect()
                if self.user.GetWindowRect(hwnd, ctypes.byref(rect)):
                    area = (rect.right - rect.left) * (rect.bottom - rect.top)
                    found.append((area, hwnd, path, pid.value))
            return True
        cb = callback_type(callback)
        self.user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        self.user.EnumWindows(cb, 0)
        return max(found, default=None, key=lambda item: item[0])

    def foreground(self, hwnd):
        if self.user.IsIconic(hwnd):
            self.user.ShowWindow(hwnd, 9)
        self.user.ShowWindow(hwnd, 5)
        self.user.BringWindowToTop(hwnd)
        self.user.SetForegroundWindow(hwnd)
        time.sleep(.4)
        if self.user.GetForegroundWindow() != hwnd:
            self.user.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x43)
            self.user.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x43)
            time.sleep(.3)

    def screenshot(self, hwnd):
        from PIL import ImageGrab
        rect = self.Rect()
        if not self.user.GetWindowRect(hwnd, ctypes.byref(rect)) or rect.right - rect.left < 200 or rect.bottom - rect.top < 200:
            return None
        return ImageGrab.grab((rect.left, rect.top, rect.right, rect.bottom), all_screens=True), rect

    def belongs_to(self, hwnd, x, y):
        return self.user.GetAncestor(self.user.WindowFromPoint(self.Point(x, y)), 2) == hwnd

    def click(self, hwnd, x, y):
        # 在移动前、移动后都确认目标窗口；遮挡时不点击其他应用。
        if not self.belongs_to(hwnd, x, y):
            return False
        old, now = self.Point(), self.Point()
        self.user.GetCursorPos(ctypes.byref(old))
        self.user.SetCursorPos(x, y)
        time.sleep(.18)
        self.user.GetCursorPos(ctypes.byref(now))
        if abs(now.x - x) > 2 or abs(now.y - y) > 2 or not self.belongs_to(hwnd, x, y):
            self.user.SetCursorPos(old.x, old.y)
            return False
        try:
            self.user.mouse_event(2, 0, 0, 0, 0)
            time.sleep(.09)
            self.user.mouse_event(4, 0, 0, 0, 0)
        finally:
            self.user.SetCursorPos(old.x, old.y)
        return True

    def close(self, hwnd):
        self.user.PostMessageW(hwnd, 0x10, 0, 0)  # WM_CLOSE，保留应用自己的退出处理。


def resolve_exe(configured, windows=None):
    path = Path(configured.strip().strip('"')) if configured else None
    if path and path.is_file():
        return path
    running = windows.find_window() if windows is not None else None
    if running:
        return Path(running[2])
    import winreg
    for hive, base in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                       (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                       (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")):
        try:
            with winreg.OpenKey(hive, base) as root:
                for index in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                            name = winreg.QueryValueEx(key, "DisplayName")[0]
                            if "starsky" not in str(name).lower():
                                continue
                            for value in ("InstallLocation", "DisplayIcon"):
                                try:
                                    candidate = str(winreg.QueryValueEx(key, value)[0]).split(",", 1)[0].strip().strip('"')
                                    target = Path(candidate) / "StarSky.exe" if value == "InstallLocation" else Path(candidate)
                                    if target.is_file():
                                        return target
                                except OSError:
                                    pass
                    except OSError:
                        continue
        except OSError:
            continue
    roots = [Path(value) for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA") if (value := os.environ.get(key))]
    roots += [Path(f"{drive}:\\") for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{drive}:\\").exists()]
    for root in roots:
        for sub in ("StarSky", "Program/StarSky", "Program Files/StarSky", "Program Files (x86)/StarSky", "Programs/StarSky", "App/StarSky"):
            candidate = root / sub / "StarSky.exe"
            if candidate.is_file():
                return candidate
    return None


def run_checkin(quiet=False):
    if os.name != "nt":
        log("失败：StarSky 签到仅支持 Windows", quiet)
        return 2
    config = read_config()
    if config["签到方式"] == "api":
        from .background import run_checkin as run_background

        executable = resolve_exe(config.get("软件路径", ""))
        if executable is None:
            log("失败：找不到 StarSky.exe，请在设置中填写已安装客户端的路径", quiet)
            return 2
        try:
            timeout = int(config.get("等待超时") or 30)
        except ValueError:
            timeout = 30
        log("正在使用本地账户后台签到", quiet)
        result = run_background(executable, timeout)
        log(result["message"], quiet)
        return 0 if result["ok"] else 1
    if config["签到方式"] != "desktop":
        log("失败：签到方式应为 api 或 desktop", quiet)
        return 2
    if not is_admin():
        code, error = run_elevated(sys.executable, [str(SCRIPT), "--quiet", "--home", str(HOME)], timeout=360)
        if error:
            log(error, quiet)
        return code
    windows = Windows()
    timeout = max(5, min(300, int(config.get("等待超时") or 30)))
    process = windows.find_window()
    if process is None:
        if config["自动启动"] != "1":
            log("失败：StarSky 未运行，已关闭自动启动", quiet)
            return 1
        executable = resolve_exe(config.get("软件路径", ""), windows)
        if executable is None:
            log("失败：找不到 StarSky.exe，请填写软件路径", quiet)
            return 2
        subprocess.Popen([str(executable)], cwd=str(executable.parent))
        deadline = time.monotonic() + timeout
        while process is None and time.monotonic() < deadline:
            time.sleep(.25)
            process = windows.find_window()
    if process is None:
        log("失败：没有找到 StarSky 主窗口，请先在托盘打开主界面", quiet)
        return 3
    hwnd = process[1]
    windows.foreground(hwnd)
    deadline = time.monotonic() + timeout
    button = {"state": "Unknown"}
    shot = None
    while time.monotonic() < deadline:
        shot = windows.screenshot(hwnd)
        if shot:
            button = find_button(shot[0])
            if button["state"] != "Unknown":
                break
        time.sleep(.25)
    if button["state"] == "Unknown":
        if shot:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            shot[0].save(LOG_DIR / "失败截图.png")
        log("失败：未识别到签到按钮，请检查登录、网络或窗口遮挡", quiet)
        return 1
    if button["state"] == "Signed":
        log("今日已签到", quiet)
        if config["签到后关闭软件"] == "1":
            windows.close(hwnd)
        return 0
    for attempt in range(3):
        shot = windows.screenshot(hwnd)
        if not shot:
            break
        button = find_button(shot[0])
        if button["state"] == "Signed":
            log("签到成功", quiet)
            if config["签到后关闭软件"] == "1":
                windows.close(hwnd)
            return 0
        if button["state"] != "Unsigned":
            break
        x, y = shot[1].left + button["x"], shot[1].top + button["y"]
        if not windows.click(hwnd, x, y):
            log("已中止：StarSky 被遮挡或系统阻止鼠标操作", quiet)
            return 4
        log(f"已点击签到按钮（第 {attempt + 1} 次）", quiet)
        deadline = time.monotonic() + 8
        unknown = 0
        while time.monotonic() < deadline:
            time.sleep(.2)
            shot = windows.screenshot(hwnd)
            if not shot:
                continue
            state = find_button(shot[0])["state"]
            if state == "Signed":
                log("签到成功", quiet)
                if config["签到后关闭软件"] == "1":
                    windows.close(hwnd)
                return 0
            if state == "Unknown":
                unknown += 1
                # 仅向目标窗口发 ESC，避免关闭其他应用的弹窗。
                if unknown == 5 and windows.user.GetForegroundWindow() == hwnd:
                    windows.user.PostMessageW(hwnd, 0x100, 0x1B, 0)
                    windows.user.PostMessageW(hwnd, 0x101, 0x1B, 0)
            else:
                unknown = 0
        if unknown:
            break
    if shot:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        shot[0].save(LOG_DIR / "签到后界面.png")
    log("结果未确认：已尝试点击，但未检测到已签到状态，请查看签到后界面截图", quiet)
    return 6
