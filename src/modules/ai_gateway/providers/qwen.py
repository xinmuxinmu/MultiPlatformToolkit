"""QwenWorkCN device-flow QR authorization and read-only desktop import."""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
import urllib.error
import urllib.parse
import uuid
from datetime import datetime, timezone

from modules.ai_gateway.base import GatewayError
from modules.ai_checkin.vendor.platforms import qwenwork as protocol
from .qwen_maas import QwenMaasTransport


class QwenProvider:
    id = "qwen"
    name = "千问办公"
    capabilities = {
        "login": True, "local_import": True, "chat": True, "checkin": True,
        "reason": "支持独立设备授权与本机只读导入、MaaS HTTP 聊天；签到状态接口 HTTP 404 时明确跳过",
    }

    def __init__(self, client_factory=None):
        self.transport = QwenMaasTransport(client_factory)

    def start_login(self, options=None):
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        nonce = str(uuid.uuid4())
        query = urllib.parse.urlencode({
            "challenge": challenge, "challenge_method": "S256", "nonce": nonce,
            "machine_id": str(uuid.uuid4()), "client_id": protocol.CLIENT_ID,
            "redirect_uri": protocol.REDIRECT_URI,
        })
        return {
            "auth_url": protocol.AUTH_ORIGIN + "/device/selectAccounts?" + query,
            "expires_in": 300, "nonce": nonce, "verifier": verifier,
            "challenge": challenge, "created_at": time.time(),
        }

    def poll_login(self, session):
        if time.time() - session.get("created_at", 0) >= 300:
            return {"status": "expired"}
        query = urllib.parse.urlencode({
            "nonce": session["nonce"], "verifier": session["verifier"], "challenge_method": "S256",
        })
        try:
            result = protocol.request_json("/api/v1/deviceToken/poll?" + query)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"status": "pending"}
            if exc.code in (408, 410):
                return {"status": "expired"}
            raise GatewayError(f"千问办公授权轮询失败（HTTP {exc.code}）", 502, "upstream_error") from None
        except (OSError, ValueError, RuntimeError):
            raise GatewayError("千问办公授权轮询暂时无法连接", 502, "upstream_error") from None
        if not result.get("token"):
            return {"status": "pending"}
        if (result.get("nonce") != session["nonce"] or
                result.get("code_challenge") != session["challenge"] or
                result.get("code_challenge_method") != "S256"):
            raise GatewayError("千问办公授权响应校验失败，请重新扫码", 502, "invalid_login_binding")
        external_id = str(result.get("user_id") or "").strip()
        if not external_id:
            raise GatewayError("千问办公授权响应缺少账号标识", 502, "invalid_upstream_response")
        expires = protocol._crypto.coerce_exp(result.get("expires_at"))
        if not expires and isinstance(result.get("expires_in"), (int, float)):
            expires = int(time.time() + result["expires_in"])
        credential = {
            "schemaVersion": 2, "token": result["token"],
            "refreshToken": result.get("refresh_token") or "",
            "expiresAt": datetime.fromtimestamp(expires, timezone.utc).isoformat() if expires else "",
            "authRealm": "standard", "loginMethod": "browser", "refreshStrategy": "device_token",
            "endpoints": {"apiOrigin": protocol.API_ORIGIN, "authOrigin": protocol.AUTH_ORIGIN},
            "user": {"id": external_id, "name": str(result.get("user_name") or "千问账号")},
        }
        try:
            protocol.access_token(credential)
        except RuntimeError:
            raise GatewayError("千问办公授权响应没有有效期限，请重新授权", 502, "invalid_upstream_response") from None
        return {"status": "authorized", "credential": credential,
                "external_id": external_id, "label": credential["user"]["name"]}

    def import_local(self):
        try:
            credential = protocol.read_local_session()
            protocol.access_token(credential)
        except PermissionError:
            raise GatewayError("无权读取千问办公本地登录态，请用原 Windows 账号运行服务或在账号页重新扫码", 400, "local_credentials_permission_denied") from None
        except (OSError, KeyError, ValueError, RuntimeError):
            raise GatewayError("千问办公本地登录态不可读取、已过期或无法解密，可重新扫码添加", 400, "local_credentials_unavailable") from None
        user = credential["user"]
        return [{"credential": credential, "external_id": user["id"],
                 "label": user.get("name") or user.get("username") or "千问账号"}]

    def models(self, credential):
        return self.transport.models(credential)

    def chat(self, credential, payload):
        yield from self.transport.chat(credential, payload)

    def checkin(self, credential, dry=False):
        return protocol.QwenWorkCheckin(dry=dry, account=credential).run()
