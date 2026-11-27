"""在隐藏的 32 位 .NET 进程中复用 StarSky 的本地账户和接口。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


BRIDGE = Path(__file__).resolve().parent / "scripts" / "background_checkin.ps1"
ERROR_MESSAGES = {
    "unsupported_client": "客户端版本暂不支持后台签到，请改用窗口点击",
    "local_account_missing": "未找到可用的本地登录态，请先打开 StarSky 登录一次",
    "request_failed": "后台签到请求失败，请检查网络；登录失效时请在客户端重新登录",
    "invalid_response": "后台签到返回无法识别的状态，结果未确认",
    "server_error": "后台签到失败",
    "client_error": "后台签到调用失败，请检查客户端文件及版本，或改用窗口点击",
}


def run_checkin(executable: Path, timeout: int = 30) -> dict:
    """桥接只返回签到结果；凭据不经命令行、不回传 Python、不落盘。"""
    windows_dir = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = windows_dir / "SysWOW64" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file() or not BRIDGE.is_file():
        return {"ok": False, "message": "后台签到不可用：缺少 Windows 32 位 .NET 桥接环境"}
    try:
        proc = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", str(BRIDGE),
             "-ExePath", str(executable)],
            cwd=str(executable.parent), capture_output=True,
            timeout=max(5, min(300, timeout)) + 10,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "后台签到请求超时，结果未确认，请稍后重试"}
    except OSError:
        return {"ok": False, "message": "后台签到启动失败，请检查 StarSky 路径及 .NET 环境"}
    try:
        result = json.loads(proc.stdout.decode("utf-8-sig").strip())
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ValueError("invalid bridge result")
        message = result.get("message", "")
        if not isinstance(message, str):
            raise ValueError("invalid bridge message")
        if not isinstance(result.get("error", ""), str):
            raise ValueError("invalid bridge error")
    except (UnicodeError, ValueError):
        # 不将子进程原始输出/异常写入日志，以免暴露登录态。
        return {"ok": False, "message": "后台签到返回无效结果，请检查客户端版本或改用窗口点击"}
    if proc.returncode != 0:
        result["ok"] = False
    if result["ok"]:
        prefix = "今日已签到（后台确认）" if result.get("already_signed") is True else "后台签到成功"
    else:
        prefix = ERROR_MESSAGES.get(result.get("error"), "后台签到结果未确认")
    return {"ok": result["ok"], "message": prefix + ("：" + message[:400] if message else "")}
