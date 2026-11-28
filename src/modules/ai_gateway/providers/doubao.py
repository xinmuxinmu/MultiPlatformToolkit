"""Doubao desktop-session adapter for the OpenAI-compatible gateway.

The current official web client uses ``/chat/completion``, content blocks and
named SSE events.  Keep chat hidden until an actual model reply is verified:
the local account currently receives upstream rate-limit error 710022002.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Iterator

import httpx

from modules.ai_gateway.base import GatewayError
from modules.ai_checkin.vendor import doubao_checkin as protocol

ORIGIN = "https://www.doubao.com"
CHAT_PATH = "/chat/completion"
MODEL_ID = "doubao"
DEFAULT_BOT_ID = "7338286299411103781"
# Public client metadata from Doubao's official web bundle. No device IDs are
# invented or copied from another account.
WEB_PARAMS = {"aid": 497858, "device_platform": "web", "os": "Windows",
              "web_platform": "browser", "version_code": 20800,
              "language": "zh", "pc_version": "3.47.4",
              "pkg_type": "release_version", "samantha_web": 1,
              "use-olympus-account": 1}
CHAT_UNVERIFIED = "豆包新聊天协议已实现；当前本机账号实测限流（710022002），尚未验证模型回复"


def _token(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not any(c in value for c in "\r\n")


def _cookie_header(cookies) -> str:
    value = protocol._cookie_header(cookies)
    if not _token(value):
        raise GatewayError("豆包本机登录态无效，请先在豆包电脑版登录", 401, "login_required")
    return value


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if not isinstance(block, dict) or block.get("type") != "text":
                raise GatewayError("豆包当前仅支持文本消息", 400, "unsupported_content")
            parts.append(str(block.get("text") or ""))
        return "\n".join(parts)
    if value is None:
        return ""
    raise GatewayError("豆包当前仅支持文本消息", 400, "unsupported_content")


def _message_text(message: dict) -> str:
    role = message.get("role")
    if role not in ("system", "user", "assistant"):
        raise GatewayError("豆包当前仅支持 system、user、assistant 消息", 400, "unsupported_content")
    text = _text(message.get("content"))
    if role == "system":
        return "[system]\n" + text
    if role == "assistant":
        return "[assistant]\n" + text
    return text


def _sse(response):
    event, data, size = "", [], 0
    for line in response.iter_lines():
        if line is None:
            continue
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            value = line[5:].lstrip(" ")
            size += len(value)
            if size > 4 * 1024 * 1024:
                raise GatewayError("豆包返回的流事件过大", 502, "invalid_stream")
            data.append(value)
        elif not line:
            if data:
                yield event, "\n".join(data)
            event, data, size = "", [], 0
    if data:
        yield event, "\n".join(data)


def _event(raw):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, UnicodeError):
        raise GatewayError("豆包返回非 JSON 流事件", 502, "invalid_stream") from None
    if not isinstance(value, dict):
        raise GatewayError("豆包流事件格式无效", 502, "invalid_stream")
    if value.get("error"):
        raise GatewayError("豆包模型服务返回错误", 502, "upstream_error")
    return value


def _content_from_event(value):
    payload = value.get("event_data")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return payload
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message")
    if isinstance(message, dict):
        payload = message
    content = payload.get("content_obj")
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    content = payload.get("content")
    if isinstance(content, str):
        try:
            decoded = json.loads(content)
        except (ValueError, TypeError):
            return content
        if isinstance(decoded, dict):
            return str(decoded.get("text") or decoded.get("content") or "")
        return content
    return str(payload.get("text") or "")


def _upstream_error(value):
    # Do not relay arbitrary upstream messages, which can contain request data.
    code = str(value.get("error_code") or value.get("code") or "")
    if code == "710022002":
        raise GatewayError("豆包请求过于频繁，请稍后重试（710022002）", 429, "rate_limited")
    if code == "710020202":
        raise GatewayError("豆包拒绝了聊天请求参数（710020202）", 502, "invalid_upstream_request")
    raise GatewayError("豆包模型服务返回错误", 502, "upstream_error")


def _block_text(block):
    content = block.get("content")
    if not isinstance(content, dict):
        return ""
    text = content.get("text_block")
    if isinstance(text, dict) and isinstance(text.get("text"), str):
        return text["text"]
    code = content.get("code_block")
    if isinstance(code, dict) and isinstance(code.get("code"), str):
        return code["code"]
    return ""


def _patch_blocks(blocks, operation):
    """Apply the official content-block append/replace/delete semantics."""
    if operation.get("patch_object") != 1:
        return
    value = operation.get("patch_value") or {}
    if not isinstance(value, dict):
        raise GatewayError("豆包流事件格式无效", 502, "invalid_stream")
    incoming = value.get("content_block") or []
    if not isinstance(incoming, list):
        raise GatewayError("豆包流事件格式无效", 502, "invalid_stream")
    patch_type = operation.get("patch_type")
    if patch_type in (2, 3):
        blocks.clear()
    if patch_type == 3:
        return
    for block in incoming:
        if not isinstance(block, dict):
            raise GatewayError("豆包流事件格式无效", 502, "invalid_stream")
        key = str(block.get("block_id") or "")
        if not key:
            continue
        kind = block.get("patch_type") if patch_type == 1 else 2
        if kind == 3:
            blocks.pop(key, None)
        elif kind == 2:
            blocks[key] = _block_text(block)
        elif kind == 1:
            blocks[key] = blocks.get(key, "") + _block_text(block)


class DoubaoProvider:
    id = "doubao"
    name = "豆包"
    capabilities = dict(login=False, local_import=True, chat=False, checkin=True,
                        reason=CHAT_UNVERIFIED)

    def __init__(self, client_factory=None):
        self._client_factory = client_factory or httpx.Client

    def import_local(self) -> list[dict]:
        try:
            cookies, _ = protocol.read_client_cookies()
            cookie = _cookie_header(cookies)
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError("豆包本机登录态不可读取，请在豆包电脑版登录后重试", 401, "local_import_failed") from exc
        # Keep account identity stable without exposing the cookie value.
        identity = next((str(c.get("value")) for c in cookies
                         if c.get("name") in ("uid", "uid_tt", "sessionid") and c.get("value")), cookie)
        external_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
        return [{"credential": {"cookies": cookie}, "external_id": external_id, "label": "豆包账号"}]

    def models(self, credential):
        if not isinstance(credential, dict) or not _token(credential.get("cookies")):
            raise GatewayError("豆包登录态缺失，请重新导入本机账号", 401, "login_required")
        raise GatewayError(CHAT_UNVERIFIED, 501, "protocol_unverified")

    @staticmethod
    def _payload(payload: dict) -> dict:
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise GatewayError("messages 必须是非空数组", 400, "invalid_messages")
        texts = [_message_text(item) for item in messages if isinstance(item, dict)]
        if len(texts) != len(messages) or not any(texts):
            raise GatewayError("豆包当前仅支持文本消息", 400, "unsupported_content")
        conversation_id = str(payload.get("_doubao_conversation_id") or "")
        created_ms = int(time.time() * 1000)
        local_conversation_id = "" if conversation_id else "local_" + str(uuid.uuid4().int % 10**18)
        # Doubao accepts one user message per completion; previous turns are
        # carried in the text to preserve the OpenAI request semantics.
        text = "\n\n".join(t for t in texts if t)
        message = {
            "local_message_id": str(uuid.uuid4()), "message_status": 0,
            "content_block": [{"block_type": 10000, "block_id": str(uuid.uuid4()),
                               "parent_id": "", "meta_info": [], "extra_info": {},
                               "append_fields": [],
                               "content": {"text_block": {"text": text, "icon_url": "",
                                                          "icon_url_dark": "", "summary": ""},
                                           "pc_event_block": ""}}],
        }
        return {
            "client_meta": {"conversation_id": conversation_id,
                            "local_conversation_id": local_conversation_id,
                            "bot_id": DEFAULT_BOT_ID, "last_section_id": "",
                            "last_message_index": 0, "local_permissions": []},
            "messages": [message],
            "option": {"is_regen": False, "is_replace": False, "answer_with_suggest": False,
                       "need_create_conversation": not bool(conversation_id),
                       "unique_key": str(uuid.uuid4()), "create_time_ms": created_ms,
                       "need_deep_think": 0, "scene_type": 0, "start_seq": 0,
                       "disable_sse_cache": False, "from_suggest": False,
                       "click_clear_context": False, "send_message_scene": "",
                       "message_from": 0, "message_storage_type": 0,
                       "sse_recv_event_options": {"support_chunk_delta": False},
                       "support_lazy_fetch_stream": False,
                       "recovery_option": {"is_recovery": False,
                                           "req_create_time_sec": created_ms // 1000,
                                           "append_sse_event_scene": 0}},
            "chat_ability": {}, "user_context": [], "ext": {},
        }

    def chat(self, credential: dict, payload: dict) -> Iterator[dict]:
        if not isinstance(credential, dict) or not _token(credential.get("cookies")):
            raise GatewayError("豆包登录态缺失，请重新导入本机账号", 401, "login_required")
        request = self._payload(payload)
        headers = {
            "User-Agent": protocol.CLIENT_UA, "Accept": "text/event-stream",
            "Content-Type": "application/json", "Origin": ORIGIN,
            "Referer": ORIGIN + "/chat/", "Cookie": credential["cookies"],
            "Agw-Js-Conv": "str",
        }
        created, completion_id = int(time.time()), "chatcmpl-" + uuid.uuid4().hex
        emitted, finished, previous_full = False, False, ""
        block_states, previous_messages = {}, {}
        try:
            with self._client_factory(timeout=httpx.Timeout(180, connect=20), follow_redirects=False) as client:
                with client.stream("POST", ORIGIN + CHAT_PATH, params=WEB_PARAMS,
                                   headers=headers, json=request) as response:
                    if not response.is_success:
                        status = response.status_code if response.status_code in (400, 401, 403, 429) else 502
                        raise GatewayError(f"豆包对话失败（HTTP {response.status_code}）", status, "upstream_error")
                    if "application/json" in getattr(response, "headers", {}).get("content-type", ""):
                        response.read()
                        try:
                            value = response.json()
                        except ValueError:
                            value = {}
                        _upstream_error(value if isinstance(value, dict) else {})
                    for event_name, raw in _sse(response):
                        if raw == "[DONE]":
                            continue
                        value = _event(raw)
                        text = ""
                        if event_name:
                            if event_name in ("STREAM_ERROR", "gateway-error"):
                                _upstream_error(value)
                            if event_name == "SSE_REPLY_END":
                                if value.get("end_type") != 3:
                                    continue
                                finished = True
                                break
                            if event_name not in ("STREAM_MSG_NOTIFY", "FULL_MSG_NOTIFY", "STREAM_CHUNK"):
                                continue
                            message = value.get("message") if event_name == "FULL_MSG_NOTIFY" else value.get("meta")
                            message = message if isinstance(message, dict) else {}
                            if message.get("user_type") == 1:
                                continue
                            key = str(message.get("message_id") or value.get("message_id") or "answer")
                            blocks = block_states.setdefault(key, {})
                            if event_name == "STREAM_CHUNK":
                                operations = value.get("patch_op") or []
                                if not isinstance(operations, list):
                                    raise GatewayError("豆包流事件格式无效", 502, "invalid_stream")
                                for operation in operations:
                                    if not isinstance(operation, dict):
                                        raise GatewayError("豆包流事件格式无效", 502, "invalid_stream")
                                    _patch_blocks(blocks, operation)
                            else:
                                content = message if event_name == "FULL_MSG_NOTIFY" else value.get("content")
                                if not isinstance(content, dict):
                                    continue
                                _patch_blocks(blocks, {"patch_object": 1, "patch_type": 2,
                                                       "patch_value": content})
                            full = "".join(blocks.values())
                            previous = previous_messages.get(key, "")
                            if not full.startswith(previous):
                                raise GatewayError("豆包返回的文本发生不兼容替换", 502, "invalid_stream")
                            text = full[len(previous):]
                            previous_messages[key] = full
                        else:
                            # Retain decoding for previously stored legacy fixtures.
                            raw_type = value.get("event_type")
                            event_type = str(raw_type or "").upper()
                            if raw_type in (2005, "2005") or event_type in ("ERR", "ERROR"):
                                _upstream_error(value)
                            if raw_type in (2003, "2003") or event_type in ("FIN", "DONE"):
                                finished = True
                                break
                            if raw_type not in (2001, "2001") and event_type not in ("CMPL", "MESSAGE", "TEXT"):
                                continue
                            text = _content_from_event(value)
                            event_data = value.get("event_data")
                            if isinstance(event_data, str):
                                try:
                                    event_data = json.loads(event_data)
                                except (ValueError, TypeError):
                                    event_data = {}
                            if isinstance(event_data, dict) and event_data.get("is_delta") is False:
                                if previous_full and text.startswith(previous_full):
                                    text = text[len(previous_full):]
                                previous_full += text
                        if not text:
                            continue
                        emitted = True
                        yield {"id": completion_id, "object": "chat.completion.chunk",
                               "created": created, "model": payload.get("model", MODEL_ID),
                               "choices": [{"index": 0, "delta": {"content": text},
                                             "finish_reason": None}]}
                    if not emitted:
                        raise GatewayError("豆包未返回有效的对话流", 502, "invalid_stream")
                    if not finished:
                        raise GatewayError("豆包对话流提前结束", 502, "invalid_stream")
                    yield {"id": completion_id, "object": "chat.completion.chunk",
                           "created": created, "model": payload.get("model", MODEL_ID),
                           "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        except httpx.HTTPError as exc:
            raise GatewayError("豆包对话连接中断", 502, "upstream_unavailable") from exc

    def checkin(self, credential, dry=False):
        # Reuse the already verified desktop-cookie check-in implementation.
        if dry:
            return {"status": "skipped", "message": "只读模式跳过豆包签到"}
        result = protocol.checkin_http(dry=False)
        status = result.get("status") if isinstance(result, dict) else "failed"
        return {"status": status, "message": result.get("message", "豆包签到结果未知")}
