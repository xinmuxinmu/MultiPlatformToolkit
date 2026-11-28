"""ChatGLM's first-party OAuth, local cookies, score API and text chat stream.

Protocol evidence: desktop auth-headers.js and chatglm.cn's production bundles.
No application or embedded browser is started; local files are read only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import requests
from PIL import Image
import io

from modules.ai_gateway.base import GatewayError

ORIGIN = "https://chatglm.cn"
SIGN_SECRET = "8a1317a7468aa3ad86e997d08f3f31cb"  # Published first-party client constant.
TOKEN_NAMES = ("chatglm_token", "chatglm_refresh_token")
MAX_QR_IMAGE_BYTES = 2 * 1024 * 1024
CHAT_ASSISTANT_ID = "65940acff94777010aa6b796"
CHAT_PATH = "/chatglm/backend-api/assistant/stream"
MODEL_PATH = "/chatglm/agent-api/operation/detail"
MAX_STREAM_EVENT_BYTES = 4 * 1024 * 1024


def _chat_events(response):
    event, lines, size = "", [], 0
    for raw in response.iter_lines():
        try:
            line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        except UnicodeError:
            raise GatewayError("智谱聊天流编码无效", 502, "invalid_stream") from None
        if not isinstance(line, str):
            raise GatewayError("智谱聊天流事件无效", 502, "invalid_stream")
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            value = line[5:].lstrip(" ")
            size += len(value.encode("utf-8"))
            if size > MAX_STREAM_EVENT_BYTES:
                raise GatewayError("智谱聊天流事件过大", 502, "invalid_stream")
            lines.append(value)
        elif not line:
            if lines or event in ("FatalError", "error"):
                yield event, "\n".join(lines)
            event, lines, size = "", [], 0
    if lines or event in ("FatalError", "error"):
        yield event, "\n".join(lines)


def _chat_text(payload):
    """Only the single-user text request emitted by the official UI is verified."""
    allowed = {"model", "messages", "stream", "n", "stream_options"}
    if any(key not in allowed and value is not None for key, value in payload.items()):
        raise GatewayError("智谱普通聊天暂不支持所请求的可选参数", 400, "unsupported_parameter")
    if "n" in payload and (isinstance(payload["n"], bool) or payload["n"] != 1):
        raise GatewayError("智谱普通聊天仅支持 n=1", 400, "unsupported_parameter")
    options = payload.get("stream_options")
    if options is not None and (not isinstance(options, dict) or
                                any(k != "include_usage" for k in options) or
                                any(not isinstance(v, bool) for v in options.values())):
        raise GatewayError("智谱不支持所请求的 stream_options", 400, "unsupported_parameter")
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        raise GatewayError("智谱普通聊天当前仅支持单条 user 文本；多轮历史尚未核实",
                           400, "unsupported_messages")
    message = messages[0]
    if (not isinstance(message, dict) or message.get("role") != "user" or
            any(k not in ("role", "content") and v is not None for k, v in message.items())):
        raise GatewayError("智谱普通聊天当前仅支持 user 文本，不支持 system、工具或多轮历史",
                           400, "unsupported_messages")
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list) and all(isinstance(b, dict) and b.get("type") == "text" and
                                          isinstance(b.get("text"), str) and
                                          set(b) <= {"type", "text"} for b in content):
        text = "\n".join(b["text"] for b in content)
    else:
        raise GatewayError("智谱普通聊天当前仅支持纯文本内容", 400, "unsupported_content")
    if not text.strip():
        raise GatewayError("智谱聊天文本不能为空", 400, "invalid_messages")
    return text


def _chat_chunks(response, model):
    parts = {}
    text = reasoning = ""
    chunk_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())

    def chunk(delta, finish=None):
        return {"id": chunk_id, "object": "chat.completion.chunk", "created": created,
                "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    yield chunk({"role": "assistant", "content": ""})
    for event, raw in _chat_events(response):
        if event in ("FatalError", "error"):
            raise GatewayError("智谱聊天服务返回错误", 502, "upstream_error")
        if raw.strip() == "HEARTBEAT":
            continue
        try:
            frame = json.loads(raw)
        except ValueError:
            raise GatewayError("智谱返回非 JSON 聊天事件", 502, "invalid_stream") from None
        if not isinstance(frame, dict):
            raise GatewayError("智谱返回无效聊天事件", 502, "invalid_stream")
        status = frame.get("status")
        last_error = frame.get("last_error")
        if (frame.get("error") is not None or status in ("error", "intervene", "waiting") or
                isinstance(last_error, dict) and last_error.get("error_code") not in (None, 0)):
            raise GatewayError("智谱聊天服务拒绝或中断了请求", 502, "upstream_error")
        if status not in ("init", "pending", "in_progress", "finish"):
            raise GatewayError("智谱返回未识别的聊天状态", 502, "invalid_stream")
        metadata = frame.get("meta_data", {})
        if not isinstance(metadata, dict):
            raise GatewayError("智谱聊天元数据无效", 502, "invalid_stream")
        incremental = metadata.get("if_increase_push", not metadata.get("full", False))
        if not isinstance(incremental, bool):
            raise GatewayError("智谱聊天增量标记无效", 502, "invalid_stream")
        incoming = frame.get("parts", [])
        if not isinstance(incoming, list):
            raise GatewayError("智谱聊天内容结构无效", 502, "invalid_stream")
        for part in incoming:
            if not isinstance(part, dict) or part.get("role", "assistant") != "assistant":
                raise GatewayError("智谱聊天片段无效", 502, "invalid_stream")
            if part.get("status") in ("error", "intervene") or part.get("error"):
                raise GatewayError("智谱聊天片段返回错误", 502, "upstream_error")
            key = part.get("logic_id") or part.get("id") or "default"
            if not isinstance(key, str):
                raise GatewayError("智谱聊天片段标识无效", 502, "invalid_stream")
            values = part.get("content", [])
            if not isinstance(values, list):
                raise GatewayError("智谱聊天内容块无效", 502, "invalid_stream")
            value = {"content": "", "reasoning_content": ""}
            for block in values:
                kind = block.get("type") if isinstance(block, dict) else None
                field = {"text": "content", "think": "reasoning_content"}.get(kind) if isinstance(kind, str) else None
                if field is None:
                    raise GatewayError("智谱返回未支持的非文本内容", 502, "unsupported_upstream_content")
                segment = block.get("text" if kind == "text" else "think", "")
                if not isinstance(segment, str):
                    raise GatewayError("智谱返回无效文本内容", 502, "invalid_stream")
                value[field] += segment
            previous = parts.setdefault(key, {"content": "", "reasoning_content": ""})
            for field in value:
                previous[field] = previous[field] + value[field] if incremental else value[field]
        current = "".join(p["content"] for p in parts.values())
        current_reasoning = "".join(p["reasoning_content"] for p in parts.values())
        if not current.startswith(text) or not current_reasoning.startswith(reasoning):
            raise GatewayError("智谱修改了已输出的文本，无法转换为追加式聊天流", 502, "invalid_stream")
        delta = {}
        if len(current) > len(text):
            delta["content"] = current[len(text):]
        if len(current_reasoning) > len(reasoning):
            delta["reasoning_content"] = current_reasoning[len(reasoning):]
        text, reasoning = current, current_reasoning
        if delta:
            yield chunk(delta)
        if status == "finish":
            if not text:
                raise GatewayError("智谱未返回文本回复", 502, "empty_response")
            yield chunk({}, "stop")
            return
    raise GatewayError("智谱聊天流中断，未收到完整结束状态", 502, "incomplete_stream")


def signed_headers(credential=None, *, milliseconds=None, nonce=None):
    credential = credential or {}
    digits = str(int(time.time() * 1000) if milliseconds is None else int(milliseconds))
    stamp = digits[:-2] + str((sum(map(int, digits)) - int(digits[-2])) % 10) + digits[-1]
    nonce = nonce or uuid.uuid4().hex
    headers = {"Content-Type": "application/json;charset=utf-8", "App-Name": "chatglm",
               "X-Device-Id": credential.get("device_id") or uuid.uuid4().hex,
               "X-Request-Id": uuid.uuid4().hex, "X-App-Platform": "pc",
               "X-App-Version": "0.0.1", "X-Timestamp": stamp, "X-Nonce": nonce,
               "X-Sign": hashlib.md5(f"{stamp}-{nonce}-{SIGN_SECRET}".encode()).hexdigest(),
               "Origin": ORIGIN, "Referer": ORIGIN + "/", "User-Agent": "Mozilla/5.0"}
    token = credential.get("access_token")
    if token:
        if not isinstance(token, str) or any(c in token for c in "\r\n"):
            raise GatewayError("智谱登录凭据无效", code="invalid_credential")
        headers["Authorization"] = "Bearer " + token
    return headers


def _read_qr_image(response):
    chunks, size = [], 0
    iterator = getattr(response, "iter_content", None)
    if iterator:
        for chunk in iterator(64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_QR_IMAGE_BYTES:
                raise ValueError("QR image is too large")
            chunks.append(chunk)
    else:
        content = response.content
        if len(content) > MAX_QR_IMAGE_BYTES:
            raise ValueError("QR image is too large")
        chunks.append(content)
    return b"".join(chunks)


def _read_local_cookies(profile):
    from modules.ai_checkin.vendor.qoder_cred import _dpapi_unprotect, _decrypt_gcm

    try:
        state = json.loads((profile / "Local State").read_text(encoding="utf-8"))
        wrapped = base64.b64decode(state["os_crypt"]["encrypted_key"], validate=True)
        if not wrapped.startswith(b"DPAPI"):
            raise ValueError("unsupported key wrapper")
        key = _dpapi_unprotect(wrapped[5:])
        database = profile / "Network" / "Cookies"
        uri = database.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            version = int(conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0])
            rows = conn.execute(
                "SELECT host_key,name,value,encrypted_value,last_access_utc FROM cookies "
                "WHERE name IN ('chatglm_token','chatglm_refresh_token') "
                "ORDER BY last_access_utc DESC").fetchall()
        finally:
            conn.close()
        tokens = {}
        for host, name, value, encrypted, _ in rows:
            # Only cookies usable for the confirmed chatglm.cn production origin.
            if host.lower().lstrip(".") != "chatglm.cn" or name in tokens:
                continue
            if encrypted:
                blob = bytes(encrypted)
                if blob[:3] in (b"v10", b"v11"):
                    plain = _decrypt_gcm(key, blob[3:15], blob[15:])
                elif blob[:3] == b"v20":
                    raise ValueError("unsupported app-bound encryption")
                else:
                    plain = _dpapi_unprotect(blob)
                if version >= 24:
                    if plain[:32] != hashlib.sha256(host.encode()).digest():
                        raise ValueError("cookie host binding mismatch")
                    plain = plain[32:]
                value = plain.decode("utf-8")
            if isinstance(value, str) and value and not any(c in value for c in "\r\n"):
                tokens[name] = value
        if not tokens.get("chatglm_token"):
            raise ValueError("access token missing")
        return {"access_token": tokens["chatglm_token"],
                "refresh_token": tokens.get("chatglm_refresh_token", ""), "device_id": uuid.uuid4().hex}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        raise GatewayError("无法读取或解密智谱清言本地登录态，可使用微信扫码添加",
                           code="local_credential_unavailable") from None


class ZhipuProvider:
    id = "zhipu"
    name = "智谱清言"
    capabilities = dict(login=True, local_import=True, chat=True, checkin=True,
                        reason="支持官网扫码、本地导入、每日积分和单条 user 纯文本聊天；system、多轮历史、工具及图片尚未核实")

    def __init__(self, http=None):
        self.http = http or requests.Session()

    def _json(self, method, path, credential=None, **kwargs):
        response = None
        try:
            # An injected requests.Session is shared by provider calls. An explicit empty Cookie
            # header prevents its mutable cookie jar from carrying one account into another.
            headers = {**signed_headers(credential), "Cookie": ""}
            response = self.http.request(method, ORIGIN + path, headers=headers,
                                         timeout=20, allow_redirects=False, **kwargs)
            if response.status_code in (401, 403):
                raise GatewayError("智谱登录态失效或被拒绝，请重新扫码", 401, "credential_expired")
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("invalid response")
            if response.status_code >= 300 and not (path.endswith("/daily_login_score") and
                                                      result.get("status") == 10001):
                raise GatewayError(f"智谱接口返回 HTTP {response.status_code}", 502, "upstream_error")
            return result
        except GatewayError:
            raise
        except (requests.RequestException, ValueError):
            raise GatewayError("智谱接口请求失败或响应无效", 502, "upstream_error") from None
        finally:
            if response is not None:
                response.close()

    def _refresh(self, credential):
        """Refresh one expired web token using the first-party refresh endpoint."""
        refresh_token = credential.get("refresh_token") if isinstance(credential, dict) else ""
        if not isinstance(refresh_token, str) or not refresh_token or any(c in refresh_token for c in "\r\n"):
            raise GatewayError("智谱登录态失效或被拒绝，请重新扫码", 401, "credential_expired")
        headers = {**signed_headers({"access_token": refresh_token,
                                     "device_id": credential.get("device_id")}),
                   "Cookie": ""}
        response = None
        try:
            response = self.http.request(
                "POST", ORIGIN + "/chatglm/user-api/user/refresh",
                headers=headers, json={}, timeout=20, allow_redirects=False,
            )
            if response.status_code in (401, 403):
                raise GatewayError("智谱登录态失效或被拒绝，请重新扫码", 401, "credential_expired")
            result = response.json()
            data = result.get("result") if isinstance(result, dict) else None
            access_token = data.get("access_token") if isinstance(data, dict) else None
            new_refresh_token = data.get("refresh_token") if isinstance(data, dict) else None
            if (response.status_code >= 300 or not isinstance(result, dict) or
                    result.get("status") not in (0, 200) or
                    not isinstance(access_token, str) or not access_token or
                    not isinstance(new_refresh_token, str) or not new_refresh_token or
                    any(c in access_token for c in "\r\n") or
                    any(c in new_refresh_token for c in "\r\n")):
                raise GatewayError("智谱登录态失效或被拒绝，请重新扫码", 401, "credential_expired")
        except GatewayError:
            raise
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            raise GatewayError("智谱登录态失效或被拒绝，请重新扫码", 401, "credential_expired") from None
        finally:
            if response is not None:
                response.close()
        credential.update(access_token=access_token, refresh_token=new_refresh_token)
        return credential

    def _identity(self, credential):
        try:
            result = self._json("GET", "/chatglm/user-api/user/info", credential)
        except GatewayError as exc:
            if exc.code != "credential_expired":
                raise
            self._refresh(credential)
            result = self._json("GET", "/chatglm/user-api/user/info", credential)
        if result.get("status") not in (0, 200):
            raise GatewayError("智谱未确认登录身份，请重新扫码", 401, "credential_expired")
        user = result.get("result") or {}
        if not isinstance(user, dict) or user.get("is_guest") is True:
            raise GatewayError("智谱当前登录态为游客，无法添加或签到", code="guest_credential")
        external_id = str(user.get("user_id") or user.get("id") or user.get("_id") or "")
        if not external_id:
            raise GatewayError("智谱未返回账号标识", 502, "invalid_upstream_response")
        return {"credential": credential, "external_id": external_id,
                "label": str(user.get("nickname") or user.get("name") or "智谱账号")}

    def start_login(self, options=None):
        state = secrets.token_urlsafe(24)
        auth_url = "https://open.weixin.qq.com/connect/qrconnect?" + urlencode({
            "appid": "wx7b19c27985d99d60", "redirect_uri": ORIGIN + "/WeChatLoginTransit",
            "response_type": "code", "scope": "snsapi_login", "state": state})
        try:
            response = self.http.get(auth_url, headers={"Cookie": ""}, timeout=20, allow_redirects=False)
            match = re.search(r"/connect/qrcode/([A-Za-z0-9_-]+)", response.text)
            if response.status_code != 200 or not match:
                raise ValueError("no QR session")
            image = self.http.get("https://open.weixin.qq.com/connect/qrcode/" + match[1],
                                  headers={"Cookie": ""}, timeout=20, allow_redirects=False, stream=True)
            image_bytes = _read_qr_image(image)
            if image.status_code != 200 or not image_bytes or not image.headers.get("Content-Type", "").startswith("image/"):
                raise ValueError("no QR image")
            with Image.open(io.BytesIO(image_bytes)) as source:
                output = io.BytesIO()
                source.convert("RGB").save(output, format="PNG")
        except (requests.RequestException, ValueError, OSError):
            raise GatewayError("智谱微信二维码暂时无法获取，请重试", 502, "upstream_error") from None
        return {"auth_url": auth_url, "qr_url": "https://open.weixin.qq.com/connect/qrcode/" + match[1],
                "qr_image": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii"),
                "expires_in": 300, "state": {"uuid": match[1], "oauth_state": state,
                    "created_at": time.time(), "device_id": uuid.uuid4().hex}}

    def poll_login(self, session):
        state = session.get("state", session)
        if time.time() - state.get("created_at", 0) >= 300:
            return {"status": "expired"}
        if not re.fullmatch(r"[A-Za-z0-9_-]+", state.get("uuid", "")):
            raise GatewayError("智谱扫码会话不存在", code="invalid_login_session")
        try:
            response = self.http.get("https://long.open.weixin.qq.com/connect/l/qrconnect",
                params={"uuid": state["uuid"]}, headers={"Cookie": ""},
                timeout=32, allow_redirects=False)
        except requests.Timeout:
            return {"status": "pending"}
        except requests.RequestException:
            raise GatewayError("智谱扫码状态查询失败", 502, "upstream_error") from None
        match = re.search(r"wx_errcode\s*=\s*(\d+)", response.text)
        if response.status_code != 200 or not match:
            raise GatewayError("智谱扫码状态响应无效", 502, "upstream_error")
        code = int(match[1])
        if code == 408:
            return {"status": "pending"}
        if code == 404:
            return {"status": "scanned"}
        if code in (402, 403):
            return {"status": "expired"}
        login_code = re.search(r"wx_code\s*=\s*[\"']([^\"']+)[\"']", response.text)
        if code != 405 or not login_code:
            raise GatewayError("智谱微信扫码授权失败，请刷新重试", code="login_rejected")
        result = self._json("POST", "/chatglm/user-api/user/wechat_qr_login",
                            {"device_id": state["device_id"]}, json={"login_code": login_code[1]})
        data = result.get("result") or {}
        if result.get("status") != 0 or not isinstance(data, dict):
            raise GatewayError("智谱微信登录未获确认，请重试", code="login_rejected")
        if data.get("has_login") is not True:
            raise GatewayError("此智谱微信账号需要先在官网绑定手机，再重新扫码", code="phone_binding_required")
        if not data.get("access_token") or not data.get("refresh_token"):
            raise GatewayError("智谱扫码成功但登录凭据不完整", 502, "invalid_upstream_response")
        credential = {"access_token": data["access_token"], "refresh_token": data["refresh_token"],
                      "device_id": state["device_id"]}
        return {"status": "authorized", **self._identity(credential)}

    def import_local(self):
        profile = Path(os.environ.get("ZHIPU_USER_DATA_DIR") or
                       (Path(os.environ.get("APPDATA", "")) / "chatglm"))
        return [self._identity(_read_local_cookies(profile))]

    def models(self, credential):
        try:
            result = self._json("GET", MODEL_PATH, credential, params={"tag": "available_models_v2"})
        except GatewayError as exc:
            if exc.code != "credential_expired":
                raise
            self._refresh(credential)
            result = self._json("GET", MODEL_PATH, credential, params={"tag": "available_models_v2"})
        main = result.get("result")
        main = main.get("main_chat") if isinstance(main, dict) else None
        rows = main.get("models") if isinstance(main, dict) else None
        if result.get("status") not in (0, 200) or not isinstance(rows, list):
            raise GatewayError("智谱未返回普通聊天模型目录", 502, "invalid_upstream_response")
        models = []
        for row in rows:
            model = row.get("selected_model") if isinstance(row, dict) else None
            if isinstance(model, str) and model.strip() and not any(c in model for c in "\r\n") and model not in models:
                models.append(model)
        if not models:
            raise GatewayError("智谱普通聊天模型目录为空", 502, "models_unavailable")
        credential["_zhipu_chat_models"] = {"ids": models, "expires_at": time.time() + 300}
        return [{"id": model} for model in models]

    def chat(self, credential, payload):
        text = _chat_text(payload)
        model = payload.get("model")
        catalog = credential.get("_zhipu_chat_models")
        if (not isinstance(catalog, dict) or not isinstance(catalog.get("expires_at"), (int, float)) or
                catalog["expires_at"] <= time.time() or not isinstance(catalog.get("ids"), list)):
            self.models(credential)
            catalog = credential["_zhipu_chat_models"]
        if not isinstance(model, str) or model not in catalog["ids"]:
            raise GatewayError("智谱模型不属于已确认的普通聊天目录", 400, "invalid_model")
        body = {"assistant_id": CHAT_ASSISTANT_ID, "conversation_id": "", "chat_type": "user_chat",
                "meta_data": {"cogview": {"rm_label_watermark": True}, "is_test": False,
                              "input_question_type": "xxxx", "channel": "", "chat_mode": "",
                              "reasoning_effort": "high", "selected_model": model,
                              "is_networking": False, "platform": "pc", "mcp_list": []},
                "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}
        response = None
        try:
            for attempt in range(2):
                headers = {**signed_headers(credential), "Accept": "text/event-stream", "Cookie": ""}
                if "Authorization" not in headers:
                    raise GatewayError("智谱登录凭据缺失", 401, "invalid_credential")
                response = self.http.request("POST", ORIGIN + CHAT_PATH, headers=headers,
                                             json=body, stream=True, timeout=(20, 180), allow_redirects=False)
                if response.status_code in (401, 403) and attempt == 0:
                    response.close()
                    response = None
                    self._refresh(credential)
                    continue
                break
            if response.status_code in (401, 403):
                raise GatewayError("智谱登录态失效或无聊天权限，请重新扫码", response.status_code, "credential_expired")
            if not 200 <= response.status_code < 300:
                status = response.status_code if response.status_code in (400, 402, 404, 429) else 502
                raise GatewayError(f"智谱聊天接口返回 HTTP {response.status_code}", status, "upstream_error")
            if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                raise GatewayError("智谱未返回 SSE 聊天流", 502, "invalid_stream")
            yield from _chat_chunks(response, model)
        except requests.RequestException:
            raise GatewayError("智谱聊天连接中断", 502, "upstream_unavailable") from None
        finally:
            if response is not None:
                response.close()

    def checkin(self, credential, dry=False):
        def outcome(status, message, credits=0):
            return {"platform": self.name, "success": status in ("success", "already"),
                    "status": status, "message": message, "credits": credits}
        if not credential.get("access_token"):
            raise GatewayError("智谱登录凭据缺失", code="invalid_credential")
        if dry:
            return outcome("skipped", "已读取智谱登录态，预演未发送 HTTP 请求")
        try:
            result = self._json("POST", "/chatglm/member-api/member/daily_login_score", credential)
        except GatewayError as exc:
            if exc.code != "credential_expired":
                raise
            self._refresh(credential)
            result = self._json("POST", "/chatglm/member-api/member/daily_login_score", credential)
        if result.get("status") == 10001:
            return outcome("already", "智谱接口确认今日已领取登录积分")
        payload = result.get("result")
        payload_path = "result"
        if not isinstance(payload, dict) and isinstance(result.get("data"), dict):
            # Some API wrappers expose the same explicit score under `data`.
            payload = result["data"]
            payload_path = "data"
        raw_score = payload.get("score") if isinstance(payload, dict) else None
        score = None
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
            try:
                score = float(raw_score)
            except OverflowError:
                pass
        elif isinstance(raw_score, str) and re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", raw_score.strip()):
            # JSON clients occasionally stringify numeric reward amounts.
            try:
                score = float(raw_score.strip())
            except OverflowError:
                pass
        if (result.get("status") in (0, 200) and score is not None and
                math.isfinite(score) and score > 0):
            return outcome("success", "智谱每日登录积分领取成功", score)
        if isinstance(payload, dict):
            result_shape = payload_path + "字段=" + ",".join(sorted(str(key)[:40] for key in payload)[:12])
        else:
            result_shape = "result类型=" + type(result.get("result")).__name__
        status = result.get("status")
        if isinstance(status, (int, str)):
            safe_status = str(status).replace("\r", " " ).replace("\n", " " )[:40]
            status_shape = f"status={safe_status}"
        else:
            status_shape = f"status类型={type(status).__name__}"
        top_keys = ",".join(sorted(str(key)[:40] for key in result)[:12])
        return outcome("failed",
                       f"智谱每日积分接口未明确确认奖励或今日已领取（{status_shape}；{result_shape}；响应字段={top_keys}）")
