"""千问办公本地凭据与 HTTP 签到（不启动客户端）。

协议依据 QwenWorkCN 2.1.0-26111906 内置主进程代码。生产签到
接口实测曾返回 404；这种情况明确跳过，绝不推断已经签到。
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .base import BaseCheckin

try:
    import qoder_cred as _crypto
except ImportError:
    from modules.ai_checkin.vendor import qoder_cred as _crypto

API_ORIGIN = "https://gateway.qwenwork.cn"
# constants 模块 Gi=N=On，生产设备授权入口同样位于 gateway。
AUTH_ORIGIN = API_ORIGIN
STATUS_PATH = "/sash/api/v1/me/daily-check-in/status"
CLAIM_PATH = "/sash/api/v1/me/daily-check-in/claim"
CLIENT_ID = "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb"
REDIRECT_URI = "qwenwork-cn://"


def client_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "QoderWork",
        "X-QwenWork-Version": "2.1.0",
        "X-QwenWork-Release-Version": "2.1.0-26111906",
        "X-QwenWork-Build": "26111906",
        "X-QwenWork-Platform": "win32" if os.name == "nt" else os.sys.platform,
        "X-QwenWork-Arch": "x64",
        "X-QwenWork-Channel": "stable",
        "X-QwenWork-App-Mode": "standard",
    }


def profile_dir() -> Path:
    override = os.environ.get("QWENWORK_AUTH_DIR")
    if override:
        return Path(override)
    roaming = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    return roaming / "QwenWorkCN"


def read_local_session(root: Path | None = None) -> dict:
    """只读 auth-v2.dat，复用 Windows DPAPI/AES-GCM；不修改原登录态。"""
    root = Path(root) if root is not None else profile_dir()
    encrypted = root / "auth-v2.dat"
    if encrypted.exists():
        raw = encrypted.read_bytes()
        if raw[:3] != b"v10" or len(raw) < 31:
            raise RuntimeError("千问办公登录态加密格式无法识别")
        state = json.loads((root / "Local State").read_text(encoding="utf-8"))
        wrapped = base64.b64decode(state["os_crypt"]["encrypted_key"])
        if not wrapped.startswith(b"DPAPI"):
            raise RuntimeError("千问办公本地密钥格式无法识别")
        value = json.loads(_crypto._decrypt_gcm(
            _crypto._dpapi_unprotect(wrapped[5:]), raw[3:15], raw[15:]
        ).decode("utf-8"))
    else:
        fallback = root / "auth-v2.dat.json"
        if not fallback.is_file():
            raise RuntimeError("未找到千问办公本地登录态，可通过账号页扫码添加")
        value = json.loads(fallback.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("schemaVersion") == 3:
            raise RuntimeError("千问办公新版登录态必须从加密存储读取")
    if not isinstance(value, dict) or value.get("schemaVersion") not in (2, 3):
        raise RuntimeError("千问办公登录态版本无法识别")
    user = value.get("user") or {}
    if not isinstance(user, dict) or not isinstance(user.get("id"), str) or not user["id"]:
        raise RuntimeError("千问办公登录态缺少账号标识")
    api = (value.get("endpoints") or {}).get("apiOrigin")
    if api not in (None, API_ORIGIN):
        raise RuntimeError("千问办公登录态属于其他部署，未发送任何请求")
    if value.get("schemaVersion") == 3 and (value.get("webIdentitySession") or {}).get("phase") != "active":
        raise RuntimeError("千问办公登录态需要重新授权")
    return value


def access_token(session: dict) -> str:
    token = session.get("token")
    if not isinstance(token, str) or not token.strip() or any(c in token for c in "\r\n"):
        raise RuntimeError("千问办公登录态缺少有效 token")
    expires = _crypto.coerce_exp(session.get("expiresAt"))
    if not expires or expires <= time.time():
        raise RuntimeError("千问办公登录态已过期，请在账号页重新扫码授权")
    return token


def request_json(path: str, *, method: str = "GET", token: str = "", body=None, origin=API_ORIGIN) -> dict:
    if origin not in (API_ORIGIN, AUTH_ORIGIN) or not path.startswith("/") or path.startswith("//"):
        raise RuntimeError("千问办公请求地址不在已确认范围内")
    headers = client_headers()
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(origin + path, data=data, headers=headers, method=method)
    # 认证头不能经 HTTP 重定向转发到另一个主机。
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as response:
        value = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("千问办公接口响应格式异常")
    return value


def unwrap(value: dict) -> dict:
    inner = value.get("data")
    return inner if isinstance(inner, dict) else value


class QwenWorkCheckin(BaseCheckin):
    PLATFORM_NAME = "千问办公"

    def __init__(self, dry=False, account: dict | None = None):
        super().__init__(dry=dry)
        self.account = account

    def checkin(self) -> dict:
        def result(status, message, credits=0):
            return {"platform": self.PLATFORM_NAME, "success": status in ("success", "already"),
                    "status": status, "message": message, "credits": credits}
        session = self.account if self.account is not None else read_local_session()
        token = access_token(session)
        if self.dry:
            return result("skipped", "已读取千问办公本地登录态，预演未发送 HTTP 请求")
        try:
            state = unwrap(request_json(STATUS_PATH, token=token))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return result("skipped", "千问办公签到状态接口返回 HTTP 404，未提交领取，无法确认今日是否已签")
            return result("failed", f"千问办公签到状态查询失败（HTTP {exc.code}）")
        status = state.get("status")
        if status == "CLAIMED_TODAY":
            return result("already", "千问办公接口确认今日已签到")
        if status in ("DISABLED", "NOT_STARTED", "ENDED"):
            return result("skipped", f"千问办公当前签到活动状态：{status}")
        if status != "CLAIMABLE":
            return result("failed", "千问办公未返回可识别的签到状态，未提交领取")
        try:
            claimed = unwrap(request_json(CLAIM_PATH, method="POST", token=token))
        except urllib.error.HTTPError as exc:
            return result("failed", f"千问办公签到领取失败（HTTP {exc.code}）")
        if claimed.get("result") == "ALREADY_CLAIMED":
            return result("already", "千问办公接口确认今日已经领取")
        if claimed.get("result") != "CLAIMED":
            return result("failed", "千问办公领取接口未确认成功")
        credits = claimed.get("rewardCredits", 0)
        if isinstance(credits, bool) or not isinstance(credits, (int, float)) or credits < 0:
            credits = 0
        return result("success", "千问办公签到成功", credits)
