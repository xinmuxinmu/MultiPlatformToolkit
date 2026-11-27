"""Kimi desktop's first-party QR login and local token store (no app launch).

The REST QR flow is copied from the installed 3.2.28 LoginModal bundle.
Desktop bearer tokens are deliberately not treated as Moonshot API keys.
"""
from __future__ import annotations

import base64
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlencode, quote

import requests

from modules.ai_gateway.base import GatewayError

ORIGIN = "https://www.kimi.com"


def _read_token_store(profile: Path) -> dict:
    source = profile / "bridge-store" / "token-store.json"
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
        if value.get("encryption") == "safeStorage.v1":
            from modules.ai_checkin.vendor.qoder_cred import _dpapi_unprotect, _decrypt_gcm

            blob = base64.b64decode(value["data"], validate=True)
            if blob[:3] in (b"v10", b"v11"):
                state = json.loads((profile / "Local State").read_text(encoding="utf-8"))
                wrapped = base64.b64decode(state["os_crypt"]["encrypted_key"], validate=True)
                if not wrapped.startswith(b"DPAPI"):
                    raise ValueError("unsupported key wrapper")
                plain = _decrypt_gcm(_dpapi_unprotect(wrapped[5:]), blob[3:15], blob[15:])
            elif blob[:3] == b"v20":
                raise ValueError("unsupported app-bound encryption")
            else:
                plain = _dpapi_unprotect(blob)
            value = json.loads(plain.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("invalid store")
        if value.get("origin", ORIGIN) != ORIGIN:
            raise GatewayError("Kimi 本地登录态属于其他站点，未导入", code="credential_origin_mismatch")
        tokens = value.get("tokens", value)
        if not isinstance(tokens, dict):
            raise ValueError("invalid tokens")
        return {key: tokens[key] for key in ("access_token", "refresh_token", "msh_user_id")
                if isinstance(tokens.get(key), str) and tokens[key]}
    except GatewayError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise GatewayError("无法读取或解密 Kimi 本地登录态，请使用扫码添加账号",
                           code="local_credential_unavailable") from None


class KimiProvider:
    id = "kimi"
    name = "Kimi"
    capabilities = dict(login=True, local_import=True, chat=False, checkin=False,
                        reason="已核实微信扫码与本地凭据导入；聊天流及每日签到协议尚未完整验证")

    def __init__(self, http=None):
        self.http = http or requests.Session()

    def _json(self, method, path, *, device_id="", **kwargs):
        headers = {"Origin": ORIGIN, "Referer": ORIGIN + "/",
                   "x-msh-platform": "web", "x-msh-device-id": device_id,
                   "User-Agent": "Mozilla/5.0", "Accept": "application/json",
                   # Session cookies are not part of the confirmed QR API. Keep one account's
                   # cookies out of another login or device-poll request.
                   "Cookie": ""}
        try:
            response = self.http.request(method, ORIGIN + "/api" + path,
                                         headers=headers, timeout=20, **kwargs)
            if response.status_code >= 400:
                raise GatewayError(f"Kimi 接口返回 HTTP {response.status_code}",
                                   status_code=502, code="upstream_error")
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("invalid response")
            return result
        except GatewayError:
            raise
        except (requests.RequestException, ValueError):
            raise GatewayError("Kimi 接口请求失败或响应无效", status_code=502,
                               code="upstream_error") from None

    def start_login(self, options=None):
        device_id = str(uuid.uuid4())
        result = self._json("POST", "/user/wx/register_login", device_id=device_id, json={})
        login_id = result.get("id")
        if not isinstance(login_id, str) or not login_id:
            raise GatewayError("Kimi 未返回扫码会话，无法生成二维码", status_code=502)
        auth_url = ORIGIN + "/wechat/mp/auth?" + urlencode({"id": login_id, "device_id": device_id})
        return {"auth_url": auth_url, "expires_in": 180,
                "state": {"login_id": login_id, "device_id": device_id}}

    def poll_login(self, session):
        state = session.get("state", session)
        login_id = str(state.get("login_id") or "")
        if not login_id:
            raise GatewayError("Kimi 扫码会话不存在", code="invalid_login_session")
        result = self._json("GET", "/user/wx/register_login/" + quote(login_id, safe=""),
                            device_id=state.get("device_id", ""))
        status = result.get("status")
        if status in ("pending", "scanned", "expired"):
            return {"status": status}
        if status == "login":
            user = result.get("user") or {}
            token, refresh = result.get("access_token"), result.get("refresh_token")
            external_id = str(user.get("id") or user.get("_id") or "")
            if not token or not refresh or not external_id:
                raise GatewayError("Kimi 已确认扫码，但未返回完整登录凭据和身份", status_code=502)
            return {"status": "authorized", "external_id": external_id,
                    "label": str(user.get("nickname") or user.get("name") or "Kimi 账号"),
                    "credential": {"access_token": token, "refresh_token": refresh,
                                   "msh_user_id": external_id, "device_id": state.get("device_id", "")}}
        if status in ("failed", "phone_already_bound"):
            raise GatewayError("Kimi 扫码授权失败或需要绑定手机，请重新添加", code="login_rejected")
        raise GatewayError("Kimi 返回未识别的扫码状态", status_code=502)

    def import_local(self):
        override = os.environ.get("KIMI_USER_DATA_DIR", "").strip()
        profiles = [Path(override)] if override else [Path(os.environ.get("APPDATA", "")) / "kimi-desktop"]
        accounts = []
        for profile in profiles:
            try:
                if not (profile / "bridge-store" / "token-store.json").is_file():
                    continue
                credential = _read_token_store(profile)
            except PermissionError:
                raise GatewayError("无法访问 Kimi 本地登录态，请检查当前 Windows 用户权限或使用扫码添加账号",
                                   code="local_credential_access_denied") from None
            if not credential.get("access_token") or not credential.get("msh_user_id"):
                continue
            accounts.append({"credential": credential, "external_id": credential["msh_user_id"],
                             "label": "Kimi 本地账号"})
        if not accounts:
            raise GatewayError("未找到 Kimi 本地登录态，可直接扫码添加账号", code="local_credential_unavailable")
        return accounts

    def models(self, credential):
        return []

    def chat(self, credential, payload):
        raise GatewayError("Kimi 桌面聊天流协议尚未验证，暂不可用于反代", code="unsupported_capability")
