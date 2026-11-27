"""QwenWork desktop MaaS HTTP transport, verified from its official package.

Host credentials are sent only to the fixed gateway. MaaS credentials are
independent, live in the gateway vault, and never update desktop source files.
"""
from __future__ import annotations

import copy
import json
import time
from urllib.parse import urlsplit
import uuid

import httpx

from modules.ai_gateway.base import GatewayError
from modules.ai_checkin.vendor.platforms import qwenwork as protocol

MAAS_ORIGIN = "https://maas.xiaoluozi.cn"
MAAS_BASE = MAAS_ORIGIN + "/v1"
VERIFIED_MAAS_HOSTS = {"maas.xiaoluozi.cn", "api9.qwenwork.cn"}
CATALOG_PATH = "/api/desktop/chat-modes"
MINT_PATH = "/api/desktop-sessions"
SUPPORTED_FORMATS = {"openai/chat-completions", "anthropic/messages"}


def _token(value):
    return isinstance(value, str) and bool(value.strip()) and not any(c in value for c in "\r\n")


def _base_url(value):
    """Only the verified production MaaS origin may receive its access token."""
    value = value.rstrip("/") if isinstance(value, str) else ""
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in VERIFIED_MAAS_HOSTS or parsed.port is not None or parsed.username or parsed.password or
            parsed.path not in ("", "/v1") or parsed.query or parsed.fragment):
        raise GatewayError("千问办公返回了尚未确认的模型服务地址，未发送凭据", 502, "invalid_upstream_origin")
    return "https://" + parsed.hostname + "/v1"


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else 0


def _events(response):
    event, lines, size = "", [], 0
    for line in response.iter_lines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            value = line[5:].lstrip(" ")
            size += len(value)
            if size > 4 * 1024 * 1024:
                raise GatewayError("千问办公流事件过大", 502, "invalid_stream")
            lines.append(value)
        elif not line and lines:
            yield event, "\n".join(lines)
            event, lines, size = "", [], 0
    if lines:
        yield event, "\n".join(lines)


def _decode(raw):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise GatewayError("千问办公返回非 JSON 流事件", 502, "invalid_stream") from None
    if not isinstance(value, dict):
        raise GatewayError("千问办公流事件格式无效", 502, "invalid_stream")
    if value.get("error") or value.get("type") == "error":
        raise GatewayError("千问办公模型服务返回错误", 502, "upstream_error")
    return value


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(isinstance(b, dict) and b.get("type") == "text" for b in content):
        return "\n".join(b.get("text", "") for b in content)
    raise GatewayError("该千问模型仅支持文本 system 与 tool 消息", 400, "unsupported_content")


def _anthropic_request(payload):
    unsupported = ("response_format", "logprobs", "top_logprobs", "logit_bias", "seed",
                   "presence_penalty", "frequency_penalty", "audio", "modalities")
    if any(payload.get(k) is not None for k in unsupported):
        raise GatewayError("该千问模型不支持所请求的 OpenAI 专有参数", 400, "unsupported_parameter")
    system, messages = [], []
    for message in payload.get("messages", []):
        if not isinstance(message, dict):
            raise GatewayError("messages 必须包含消息对象", 400, "invalid_messages")
        role, content = message.get("role"), message.get("content")
        if role in ("system", "developer"):
            system.append(_text(content))
            continue
        if role == "tool":
            if not message.get("tool_call_id"):
                raise GatewayError("tool 消息缺少 tool_call_id")
            blocks = [{"type": "tool_result", "tool_use_id": message["tool_call_id"],
                       "content": _text(content)}]
            role = "user"
        elif role in ("user", "assistant"):
            blocks = []
            if isinstance(content, str):
                if content:
                    blocks.append({"type": "text", "text": content})
            elif content is not None:
                if not isinstance(content, list):
                    raise GatewayError("不支持该消息内容格式", 400, "unsupported_content")
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        blocks.append({"type": "text", "text": block.get("text", "")})
                    elif isinstance(block, dict) and block.get("type") == "image_url":
                        image = block.get("image_url") or {}
                        url = image.get("url", "") if isinstance(image, dict) else ""
                        if not isinstance(url, str) or not url.startswith("data:image/") or ";base64," not in url:
                            raise GatewayError("该千问模型的图片仅支持 base64 data URL", 400, "unsupported_content")
                        media, data = url[5:].split(";base64,", 1)
                        blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
                    else:
                        raise GatewayError("不支持该消息内容块", 400, "unsupported_content")
            for tool in message.get("tool_calls", []):
                fn = tool.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (ValueError, TypeError):
                    raise GatewayError("工具参数必须是完整 JSON 对象") from None
                if role != "assistant" or not isinstance(args, dict) or not tool.get("id") or not fn.get("name"):
                    raise GatewayError("工具调用格式无效")
                blocks.append({"type": "tool_use", "id": tool["id"], "name": fn["name"], "input": args})
        else:
            raise GatewayError("不支持该消息角色", 400, "unsupported_content")
        if not blocks:
            continue
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})
    if not messages:
        raise GatewayError("需要非空的 user 或 assistant 消息")
    result = {"model": payload["model"], "messages": messages, "stream": True,
              "max_tokens": payload.get("max_tokens", payload.get("max_completion_tokens", 32768))}
    if system:
        result["system"] = "\n".join(system)
    for key in ("temperature", "top_p", "top_k"):
        if key in payload:
            result[key] = payload[key]
    if payload.get("stop"):
        result["stop_sequences"] = [payload["stop"]] if isinstance(payload["stop"], str) else payload["stop"]
    if payload.get("tools"):
        result["tools"] = []
        for tool in payload["tools"]:
            fn = tool.get("function") or {}
            if tool.get("type") != "function" or not fn.get("name"):
                raise GatewayError("该千问模型仅支持 function 工具")
            result["tools"].append({"name": fn["name"], "description": fn.get("description", ""),
                                    "input_schema": fn.get("parameters", {})})
    choice = payload.get("tool_choice")
    if choice is not None:
        if choice in ("auto", "required", "none"):
            result["tool_choice"] = {"type": {"required": "any"}.get(choice, choice)}
        elif isinstance(choice, dict) and choice.get("type") == "function" and (choice.get("function") or {}).get("name"):
            result["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
        else:
            raise GatewayError("工具选择格式无效")
    if "parallel_tool_calls" in payload:
        result.setdefault("tool_choice", {"type": "auto"})["disable_parallel_tool_use"] = not payload["parallel_tool_calls"]
    return result


def _anthropic_chunks(events, model):
    mid, created, tools, prompt, output, finished, stopped = "chatcmpl-" + uuid.uuid4().hex, int(time.time()), {}, 0, 0, False, False
    def chunk(delta=None, reason=None, usage=None):
        value = {"id": mid, "object": "chat.completion.chunk", "created": created, "model": model,
                 "choices": [] if usage is not None else [{"index": 0, "delta": delta or {}, "finish_reason": reason}]}
        if usage is not None:
            value["usage"] = usage
        return value
    for event, raw in events:
        if raw == "[DONE]":
            continue
        value = _decode(raw)
        kind = value.get("type") or event
        if kind == "message_start":
            message = value.get("message") or {}
            mid = str(message.get("id") or mid)
            prompt = _number((message.get("usage") or {}).get("input_tokens"))
            yield chunk({"role": "assistant", "content": ""})
        elif kind == "content_block_start":
            block, index = value.get("content_block") or {}, value.get("index")
            if block.get("type") == "tool_use":
                if not block.get("id") or not block.get("name"):
                    raise GatewayError("千问办公返回不完整工具调用", 502, "invalid_stream")
                tools[index] = len(tools)
                initial = json.dumps(block["input"], ensure_ascii=False) if block.get("input") else ""
                yield chunk({"tool_calls": [{"index": tools[index], "id": block["id"], "type": "function",
                             "function": {"name": block["name"], "arguments": initial}}]})
            elif block.get("type") == "text" and block.get("text"):
                yield chunk({"content": block["text"]})
        elif kind == "content_block_delta":
            delta = value.get("delta") or {}
            if delta.get("type") == "text_delta":
                yield chunk({"content": delta.get("text", "")})
            elif delta.get("type") == "thinking_delta":
                yield chunk({"reasoning_content": delta.get("thinking", "")})
            elif delta.get("type") == "input_json_delta":
                index = value.get("index")
                if index not in tools:
                    raise GatewayError("千问办公工具参数缺少对应调用", 502, "invalid_stream")
                yield chunk({"tool_calls": [{"index": tools[index], "function": {"arguments": delta.get("partial_json", "")}}]})
        elif kind == "message_delta":
            usage = value.get("usage") or {}
            prompt = _number(usage.get("input_tokens", prompt))
            output = _number(usage.get("output_tokens", output))
            reason = (value.get("delta") or {}).get("stop_reason")
            if reason:
                finished = True
                yield chunk(reason={"tool_use": "tool_calls", "max_tokens": "length", "refusal": "content_filter"}.get(reason, "stop"))
        elif kind == "message_stop":
            stopped = True
            break
    if not stopped or not finished:
        raise GatewayError("千问办公模型流缺少完整结束事件", 502, "incomplete_stream")
    yield chunk(usage={"prompt_tokens": prompt, "completion_tokens": output, "total_tokens": prompt + output})


class QwenMaasTransport:
    def __init__(self, client_factory=None):
        self.client_factory = client_factory or httpx.Client

    def _host_token(self, credential):
        endpoints = credential.get("endpoints") or {}
        if not isinstance(endpoints, dict) or endpoints.get("apiOrigin", protocol.API_ORIGIN) != protocol.API_ORIGIN:
            raise GatewayError("千问办公登录态属于其他部署，未发送请求", 400, "invalid_upstream_origin")
        try:
            return protocol.access_token(credential)
        except RuntimeError:
            raise GatewayError("千问办公登录态已过期或无效，请重新扫码授权", 401, "login_required") from None

    def _json(self, method, url, token, body=None):
        if not _token(token):
            raise GatewayError("千问办公访问凭据无效", 401, "login_required")
        headers = {**protocol.client_headers(), "Authorization": "Bearer " + token}
        try:
            with self.client_factory(timeout=30, follow_redirects=False) as client:
                response = client.request(method, url, headers=headers, json=body)
                if not response.is_success:
                    status = response.status_code if response.status_code in (401, 403, 429) else 502
                    raise GatewayError(f"千问办公接口失败（HTTP {response.status_code}）", status, "upstream_error")
                value = response.json()
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise GatewayError("千问办公接口连接失败或响应格式无效", 502, "upstream_unavailable") from None
        if not isinstance(value, dict):
            raise GatewayError("千问办公接口没有返回 JSON 对象", 502, "invalid_upstream_response")
        return value

    def _catalog(self, credential):
        token = self._host_token(credential)
        cache = credential.get("_qwen_models") or {}
        if isinstance(cache, dict) and _number(cache.get("fetched_at")) > time.time() - 300 and isinstance(cache.get("models"), list):
            return cache["models"]
        value = protocol.unwrap(self._json("GET", protocol.API_ORIGIN + CATALOG_PATH, token))
        if not isinstance(value.get("catalog"), list):
            raise GatewayError("千问办公没有返回模型目录", 502, "invalid_model_catalog")
        models = []
        for row in value["catalog"]:
            if (isinstance(row, dict) and row.get("enable") is not False and isinstance(row.get("key"), str)
                    and row["key"].strip() and row.get("format") in SUPPORTED_FORMATS):
                models.append({"id": row["key"], "format": row["format"]})
        if not models:
            raise GatewayError("千问办公没有返回可用的 HTTP 模型", 502, "missing_models")
        credential["_qwen_models"] = {"fetched_at": time.time(), "models": models}
        return models

    def models(self, credential):
        return [{"id": row["id"], "object": "model", "owned_by": "qwen"} for row in self._catalog(credential)]

    def _mint(self, credential, previous=None):
        token = self._host_token(credential)
        requested_id = previous.get("session_id") if isinstance(previous, dict) else None
        body = {"session_id": requested_id} if requested_id else {}
        value = self._json("POST", protocol.API_ORIGIN + MINT_PATH, token, body)
        session_id = value.get("session_id")
        expiry = _number(value.get("expires_at_ms"))
        if (not _token(value.get("access_token")) or not isinstance(session_id, str) or not session_id.strip()
                or (requested_id and requested_id.lower() != session_id.lower()) or not expiry or expiry <= time.time() * 1000):
            raise GatewayError("千问办公模型凭据缺少有效 token、会话或期限", 502, "invalid_upstream_response")
        base = _base_url(value.get("base_url") or MAAS_BASE)
        result = {"access_token": value["access_token"], "session_id": session_id,
                  "expires_at_ms": expiry, "ttl_ms": _number(value.get("ttl_ms")), "base_url": base}
        credential["_qwen_maas"] = result
        return result

    def _lease(self, credential):
        self._host_token(credential)
        value = credential.get("_qwen_maas")
        if not isinstance(value, dict) or not _token(value.get("access_token")) or not value.get("session_id"):
            return self._mint(credential)
        value["base_url"] = _base_url(value.get("base_url"))
        remaining = _number(value.get("expires_at_ms")) - time.time() * 1000
        ttl = _number(value.get("ttl_ms"))
        lead = ttl / 2 if ttl else 600000
        if remaining > lead:
            return value
        if remaining > 0 and ttl > 0:
            try:
                rotated = self._json("POST", value["base_url"] + "/access-tokens/exchange", value["access_token"], {"ttl_ms": ttl})
                expiry = _number(rotated.get("expires_at"))
                if _token(rotated.get("access_token")) and expiry > time.time() * 1000:
                    value.update(access_token=rotated["access_token"], expires_at_ms=expiry)
                    return value
            except GatewayError as exc:
                if exc.status_code not in (401, 403):
                    return value
        return self._mint(credential, value)

    def chat(self, credential, payload):
        models = self._catalog(credential)
        model = next((row for row in models if row["id"] == payload.get("model")), None)
        if model is None:
            raise GatewayError("千问办公当前账号没有该模型，请查询 /v1/models", 400, "invalid_model")
        anthropic = model["format"] == "anthropic/messages"
        request = _anthropic_request(payload) if anthropic else copy.deepcopy(payload)
        request["stream"] = True
        if not anthropic:
            options = request.get("stream_options") or {}
            if not isinstance(options, dict):
                raise GatewayError("stream_options 必须是对象", 400, "invalid_request")
            request["stream_options"] = {**options, "include_usage": True}
        lease = self._lease(credential)
        headers = {**protocol.client_headers(), "Authorization": "Bearer " + lease["access_token"],
                   "Accept": "text/event-stream", "x-session-id": lease["session_id"],
                   "x-mm-client-request-id": str(uuid.uuid4()), "x-mm-client-request-source": "qwenwork_desktop",
                   "x-mm-device-type": "windows" if protocol.os.name == "nt" else protocol.os.sys.platform}
        if anthropic:
            headers["anthropic-version"] = "2023-06-01"
        path = "/messages" if anthropic else "/chat/completions"
        try:
            with self.client_factory(timeout=httpx.Timeout(180, connect=20), follow_redirects=False) as client:
                with client.stream("POST", lease["base_url"] + path, headers=headers, json=request) as response:
                    if not response.is_success:
                        if response.status_code == 401:
                            credential.pop("_qwen_maas", None)
                        status = response.status_code if response.status_code in (400, 401, 402, 403, 404, 429) else 502
                        raise GatewayError(f"千问办公对话失败（HTTP {response.status_code}）", status, "upstream_error")
                    if anthropic:
                        yield from _anthropic_chunks(_events(response), payload["model"])
                        return
                    saw_frame = False
                    for _, raw in _events(response):
                        if raw == "[DONE]":
                            if not saw_frame:
                                raise GatewayError("千问办公模型流为空", 502, "invalid_stream")
                            return
                        value = _decode(raw)
                        if not isinstance(value.get("choices"), list):
                            raise GatewayError("千问办公模型流缺少 choices", 502, "invalid_stream")
                        saw_frame = True
                        yield value
                    if not saw_frame:
                        raise GatewayError("千问办公模型流为空", 502, "invalid_stream")
        except httpx.HTTPError:
            raise GatewayError("千问办公模型连接中断", 502, "upstream_unavailable") from None
