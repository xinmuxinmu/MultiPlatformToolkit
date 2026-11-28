"""Qoder desktop-session importer.

The desktop token and account identity endpoint are stable and can be imported
without launching Qoder.  Qoder's model request still depends on the bundled
Rust/WASM signing context, so this adapter deliberately exposes import only
until that request path can be reproduced and tested end to end.
"""
from __future__ import annotations

import copy

import requests

from modules.ai_gateway.base import GatewayError
from modules.ai_checkin.vendor import qoder_cred


BASES = {
    "qoder": "https://openapi.qoder.sh",
    "qoder_cn": "https://openapi.qoder.com.cn",
}


def _text(value):
    return isinstance(value, str) and value.strip() and not any(c in value for c in "\r\n")


class _QoderBaseProvider:
    capabilities = dict(
        login=False, local_import=True, chat=False, checkin=False,
        reason="支持 Qoder 桌面登录态导入；模型请求依赖桌面端 WASM 签名，聊天协议尚未闭环",
    )

    def __init__(self, http=None, base=None):
        self.http = http or requests.Session()
        self.base = base or BASES[self.id]

    def _userinfo(self, token, device):
        headers = {"Accept": "application/json", "Authorization": "Bearer " + token,
                   "User-Agent": "Qoder"}
        try:
            headers.update(qoder_cred.device_headers(device))
            response = self.http.get(self.base + "/api/v1/userinfo", headers=headers,
                                     timeout=20, allow_redirects=False)
        except requests.RequestException:
            raise GatewayError("Qoder 账号信息接口连接失败", 502, "upstream_unavailable") from None
        if response.status_code in (401, 403):
            raise GatewayError("Qoder 桌面登录态已失效，请重新登录", 401, "credential_expired")
        if response.status_code >= 400:
            raise GatewayError(f"Qoder 账号信息接口返回 HTTP {response.status_code}", 502,
                               "upstream_error")
        try:
            value = response.json()
        except (TypeError, ValueError):
            raise GatewayError("Qoder 账号信息响应无效", 502, "invalid_upstream_response") from None
        if not isinstance(value, dict):
            raise GatewayError("Qoder 账号信息响应无效", 502, "invalid_upstream_response")
        external_id = next((value.get(key) for key in ("id", "user_id", "uid")
                            if _text(value.get(key))), "")
        if not external_id:
            raise GatewayError("Qoder 账号信息缺少用户标识", 502, "invalid_identity")
        label = next((value.get(key) for key in ("name", "username", "user_name", "email")
                      if _text(value.get(key))), self.name + "账号")
        return str(external_id), str(label), value

    def import_local(self):
        try:
            device, _ = qoder_cred.device(self.base)
            token, error = qoder_cred.get_token(self.base)
        except Exception:
            raise GatewayError("无法读取 Qoder 桌面登录态，请使用登录客户端的 Windows 用户运行",
                               400, "local_credentials_unavailable") from None
        if not _text(token):
            raise GatewayError(error or "未找到 Qoder 桌面登录态", 401, "login_required")
        external_id, label, user = self._userinfo(token, device)
        cached = qoder_cred.read_cache().get(qoder_cred._cache_key(self.base))
        credential = {"access_token": token, "token": token, "base_url": self.base,
                      "device": copy.deepcopy(device), "user": user}
        if isinstance(cached, dict):
            for key in ("refreshToken", "expiresAt", "refreshTokenExpiresAt"):
                if cached.get(key):
                    credential[key] = cached[key]
        return [{"credential": credential, "external_id": external_id, "label": label}]

    def models(self, credential):
        raise GatewayError(self.capabilities["reason"], 501, "unsupported_capability")

    def chat(self, credential, payload):
        raise GatewayError(self.capabilities["reason"], 501, "unsupported_capability")


class QoderProvider(_QoderBaseProvider):
    id = "qoder"
    name = "Qoder 国际版"


class QoderCNProvider(_QoderBaseProvider):
    id = "qoder_cn"
    name = "Qoder 国内版"
