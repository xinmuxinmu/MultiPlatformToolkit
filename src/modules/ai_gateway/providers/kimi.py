"""Kimi desktop login and its separately provisioned Work model credentials.

The REST QR flow is copied from the installed 3.2.28 LoginModal bundle.
Work uses the native runtime's OpenAI protocol at a fixed first-party origin.
Desktop bearer tokens never substitute for Work keys; source stores are read only.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlencode, quote

import requests

from modules.ai_gateway.base import GatewayError

ORIGIN = "https://www.kimi.com"
WORK_BASE = "https://agent-gw.kimi.com/coding/v1"
MAX_EVENT_BYTES = 4 * 1024 * 1024


def _valid_token(value):
    return isinstance(value, str) and bool(value.strip()) and not any(c in value for c in "\r\n")


def _profiles():
    override = os.environ.get("KIMI_USER_DATA_DIR", "").strip()
    return [Path(override)] if override else [Path(os.environ.get("APPDATA", "")) / "kimi-desktop"]


def _read_work_key(profile: Path, user_id: str) -> dict:
    """Read only the native v2 key-store entry bound to the requested account."""
    if not _valid_token(user_id):
        return {}
    share = profile / "daimon-share"
    try:
        pointer = json.loads((profile / "daimon-storage.json").read_text(encoding="utf-8"))
        candidate = pointer.get("shareDir") if isinstance(pointer, dict) else None
        if isinstance(candidate, str) and candidate.strip() and Path(candidate).is_absolute():
            share = Path(candidate)
    except (OSError, ValueError):
        pass
    try:
        store = json.loads((share / "daimon" / "kimi-code-key.json").read_text(encoding="utf-8"))
        if not isinstance(store, dict) or store.get("v") != 2 or not isinstance(store.get("keys"), list):
            return {}
        for entry in store["keys"]:
            if (isinstance(entry, dict) and entry.get("userId") == user_id and
                    _valid_token(entry.get("apiKey"))):
                result = {"work_api_key": entry["apiKey"], "work_key_user_id": user_id}
                if _valid_token(entry.get("keyId")):
                    result["work_key_id"] = entry["keyId"]
                return result
    except (OSError, ValueError):
        pass
    return {}


def _events(response):
    event, lines, size = "", [], 0
    for raw in response.iter_lines():
        try:
            line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        except UnicodeError:
            raise GatewayError("Kimi Work 返回无效流编码", 502, "invalid_stream") from None
        if not isinstance(line, str):
            raise GatewayError("Kimi Work 返回无效流事件", 502, "invalid_stream")
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            value = line[5:].lstrip(" ")
            size += len(value.encode("utf-8"))
            if size > MAX_EVENT_BYTES:
                raise GatewayError("Kimi Work 流事件过大", 502, "invalid_stream")
            lines.append(value)
        elif not line:
            if lines or event == "error":
                yield event, "\n".join(lines)
            event, lines, size = "", [], 0
    if lines or event == "error":
        yield event, "\n".join(lines)


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
    capabilities = dict(login=True, local_import=True, chat=True, checkin=False,
                        reason="聊天依赖官方桌面端已初始化的对应账号 Kimi Work 模型凭据；扫码登录令牌不能替代模型密钥，签到尚未核实")

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
        accounts = []
        for profile in _profiles():
            try:
                if not (profile / "bridge-store" / "token-store.json").is_file():
                    continue
                credential = _read_token_store(profile)
            except PermissionError:
                raise GatewayError("无法访问 Kimi 本地登录态，请检查当前 Windows 用户权限或使用扫码添加账号",
                                   code="local_credential_access_denied") from None
            if not credential.get("access_token") or not credential.get("msh_user_id"):
                continue
            credential.update(_read_work_key(profile, credential["msh_user_id"]))
            accounts.append({"credential": credential, "external_id": credential["msh_user_id"],
                             "label": "Kimi 本地账号"})
        if not accounts:
            raise GatewayError("未找到 Kimi 本地登录态，可直接扫码添加账号", code="local_credential_unavailable")
        return accounts

    def _work_key(self, credential):
        user_id = credential.get("msh_user_id")
        if not _valid_token(user_id):
            raise GatewayError("Kimi 账号身份缺失，请重新扫码或导入", 409, "invalid_credential")
        if credential.get("work_api_key"):
            if credential.get("work_key_user_id") != user_id:
                raise GatewayError("Kimi Work 模型凭据与当前账号不符，未发送请求", 409,
                                   "credential_identity_mismatch")
        else:
            for profile in _profiles():
                found = _read_work_key(profile, user_id)
                if found:
                    credential.update(found)
                    break
        key = credential.get("work_api_key")
        if (not _valid_token(key) or key in (credential.get("access_token"), credential.get("refresh_token"))):
            raise GatewayError("未找到该账号的 Kimi Work 模型凭据，请先在官方桌面端初始化 Work，再导入本机登录态",
                               409, "work_credential_unavailable")
        return key

    def _work_request(self, method, path, credential, **kwargs):
        headers = {"Authorization": "Bearer " + self._work_key(credential),
                   "User-Agent": "Desktop Kimi Work", "X-Msh-Version": "3.2.28",
                   "R-Timezone": "Asia/Shanghai", "Cookie": "",
                   "Accept": "text/event-stream" if kwargs.get("stream") else "application/json"}
        response = self.http.request(method, WORK_BASE + path, headers=headers,
                                     timeout=(20, 180), allow_redirects=False, **kwargs)
        if not 200 <= response.status_code < 300:
            response.close()
            if response.status_code in (401, 403):
                raise GatewayError("Kimi Work 模型凭据已失效或无权限，请在官方桌面端确认后重新导入",
                                   response.status_code, "credential_expired")
            status = response.status_code if response.status_code in (400, 402, 404, 429) else 502
            raise GatewayError(f"Kimi Work 接口返回 HTTP {response.status_code}", status, "upstream_error")
        return response

    def models(self, credential):
        response = None
        try:
            response = self._work_request("GET", "/models", credential)
            result = response.json()
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise ValueError("invalid catalog")
            return [{"id": row["id"]} for row in result["data"]
                    if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]]
        except ValueError:
            raise GatewayError("Kimi Work 模型目录响应无效", 502, "invalid_upstream_response") from None
        except requests.RequestException:
            raise GatewayError("Kimi Work 模型目录连接失败", 502, "upstream_unavailable") from None
        finally:
            if response is not None:
                response.close()

    def chat(self, credential, payload):
        request = copy.deepcopy(payload)
        if "n" in request and (isinstance(request["n"], bool) or request["n"] != 1):
            raise GatewayError("Kimi Work 当前仅支持 n=1", 400, "unsupported_parameter")
        request["stream"] = True
        options = request.get("stream_options")
        if options is not None and not isinstance(options, dict):
            raise GatewayError("stream_options 必须是对象", 400, "invalid_request")
        request["stream_options"] = {**(options or {}), "include_usage": True}
        if "max_tokens" in request:
            request.setdefault("max_completion_tokens", request.pop("max_tokens"))
        request.setdefault("thinking", {"type": "disabled"})
        response = None
        finished = False
        try:
            response = self._work_request("POST", "/chat/completions", credential,
                                          json=request, stream=True)
            if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                raise GatewayError("Kimi Work 未返回 SSE 聊天流", 502, "invalid_stream")
            for event, raw in _events(response):
                if event == "error":
                    raise GatewayError("Kimi Work 聊天流返回错误", 502, "upstream_error")
                if raw == "[DONE]":
                    if not finished:
                        raise GatewayError("Kimi Work 聊天流缺少结束状态", 502, "incomplete_stream")
                    return
                try:
                    chunk = json.loads(raw)
                except ValueError:
                    raise GatewayError("Kimi Work 返回非 JSON 流事件", 502, "invalid_stream") from None
                if not isinstance(chunk, dict):
                    raise GatewayError("Kimi Work 返回无效聊天事件", 502, "invalid_stream")
                if chunk.get("error") is not None or chunk.get("type") == "error":
                    raise GatewayError("Kimi Work 聊天流返回错误", 502, "upstream_error")
                choices = chunk.get("choices")
                if not isinstance(choices, list) or any(not isinstance(c, dict) for c in choices):
                    raise GatewayError("Kimi Work 聊天事件缺少 choices", 502, "invalid_stream")
                if len(choices) > 1 or any(c.get("index", 0) != 0 or
                                           isinstance(c.get("index"), bool) for c in choices):
                    raise GatewayError("Kimi Work 返回未请求的多条回复", 502, "invalid_stream")
                for choice in choices:
                    if not isinstance(choice.get("delta", {}), dict):
                        raise GatewayError("Kimi Work 返回无效聊天增量", 502, "invalid_stream")
                    if choice.get("finish_reason") is not None:
                        if choice["finish_reason"] not in ("stop", "length", "tool_calls",
                                                           "content_filter", "function_call"):
                            raise GatewayError("Kimi Work 返回无效结束状态", 502, "invalid_stream")
                        finished = True
                    # The native Kimi OpenAI protocol also accepts usage on a choice.
                    if not chunk.get("usage") and isinstance(choice.get("usage"), dict):
                        chunk["usage"] = choice["usage"]
                yield chunk
            raise GatewayError("Kimi Work 聊天流中断，未收到完整结束标记", 502, "incomplete_stream")
        except requests.RequestException:
            raise GatewayError("Kimi Work 聊天连接中断", 502, "upstream_unavailable") from None
        finally:
            if response is not None:
                response.close()
