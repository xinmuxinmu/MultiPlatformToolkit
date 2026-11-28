"""Trae CN desktop import and verified single-turn text chat.

The official v2 endpoint, directory-issued model parameters and incremental SSE
output were verified on 2026-11-28. Only the verified text-only request shape is
exposed; desktop credentials and opaque model parameters stay internal.
"""
from __future__ import annotations

import json
import time
import uuid

import requests

from modules.ai_gateway.base import GatewayError

ORIGIN = "https://api.trae.cn"
CHAT_ORIGIN = "https://trae-api-cn.mchost.guru"
CATALOG_PATH = "/api/ide/v1/get_detail_param"
CHAT_PATH = "/api/ide/v2/llm_raw_chat"
APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
CLIENT_VERSION = "3.4.20"
CLIENT_VERSION_CODE = "20261110"


def _stream_error(code):
    if str(code) == "4011":
        # Official clients name 4011 ABNORMAL_ACCOUNT_LIMIT / ErrAccessReject,
        # distinct from 4007 ErrRateLimitExceeded. Do not promise a short retry.
        raise GatewayError("Trae CN 账号或场景访问受限（业务码 4011），请在官方客户端检查账号状态",
                           429, "account_limited")
    if str(code) == "4007":
        raise GatewayError("Trae CN 请求限流，请稍后重试", 429, "rate_limited")
    raise GatewayError("Trae CN 聊天返回错误事件", 502, "upstream_error")


def _token_payload(token: str) -> dict:
    try:
        part = str(token).split(".")[1]
        part += "=" * (-len(part) % 4)
        import base64
        value = json.loads(base64.urlsafe_b64decode(part.encode()).decode())
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _checkin_adapter():
    """Load the legacy check-in adapter without imposing its script path."""
    import importlib
    import sys
    from pathlib import Path
    vendor = str(Path(__file__).resolve().parents[2] / "ai_checkin" / "vendor")
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    return importlib.import_module("platforms.traework").TraeWorkCheckin


class TraeProvider:
    id = "trae"
    name = "Trae CN"
    capabilities = dict(
        login=False, local_import=True, chat=True, checkin=True,
        reason="支持 Trae CN 本地登录态导入、模型目录及单轮纯文本聊天；暂不支持 system、历史消息、工具和多模态",
    )

    def __init__(self, http=None):
        self.http = http if http is not None else requests.Session()

    @staticmethod
    def _headers(credential, stream=False):
        token = credential.get("access_token")
        if not isinstance(token, str) or not token.strip() or any(c in token for c in "\r\n"):
            raise GatewayError("Trae CN 登录态缺少有效访问令牌", 401, "invalid_credential")
        request_id = str(uuid.uuid4())
        headers = {
            "Authorization": "Cloud-IDE-JWT " + token, "X-Cloudide-Token": token,
            "X-Ide-Token": token, "X-App-Id": APP_ID,
            "x-ide-version": CLIENT_VERSION, "x-app-version": CLIENT_VERSION,
            "x-ide-type": "trae", "x-ide-version-type": "stable",
            "X-IDE-Version-Code": CLIENT_VERSION_CODE, "X-App-Version-Code": CLIENT_VERSION_CODE,
            "X-Trae-Client-Type": "ide", "request-traffic-type": "prod",
            "X-App-Function": "chat_v3", "X-Ide-Function": "chat_v3",
            "x-harness": "1", "x-agent-host-type": "0",
            "x-device-type": "windows", "x-user-region": "CN",
            "User-Agent": "TraeClient/TTNet",
            "X-Request-ID": request_id, "X-Trae-Request-ID": request_id,
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "Cookie": "",
        }
        for source, target in (("device_id", "x-device-id"),
                               ("machine_id", "x-machine-id"), ("user_id", "x-uid")):
            value = credential.get(source)
            if source != "user_id" and (not isinstance(value, str) or not value.strip()):
                raise GatewayError("Trae CN 登录态缺少设备上下文，请重新导入本机登录态",
                                   401, "local_context_required")
            if value:
                if not isinstance(value, str) or any(c in value for c in "\r\n"):
                    raise GatewayError("Trae CN 登录态字段无效", 401, "invalid_credential")
                headers[target] = value
        return headers

    @staticmethod
    def _check_http(response):
        status = response.status_code
        if status == 429:
            raise GatewayError("Trae CN 请求限流，请稍后重试", 429, "rate_limited")
        if status in (401, 403):
            raise GatewayError("Trae CN 登录态无效或无访问权限", status, "credential_expired")
        if status != 200:
            raise GatewayError(f"Trae CN 接口返回 HTTP {status}", 502, "upstream_error")

    def _fetch_models(self, credential):
        """Read account-scoped configs, including opaque private request parameters.

        Refetch for each chat; never cache signatures across accounts or expose
        this internal return value directly from the public models method.
        """
        response = None
        try:
            response = self.http.post(CHAT_ORIGIN + CATALOG_PATH,
                headers=self._headers(credential), timeout=30, allow_redirects=False,
                json={"function": "chat_v3", "config_names": None,
                      "need_prompt": False, "current_config_info": None,
                      "poly_prompt": True, "mode_type": None, "agent_type": None})
            self._check_http(response)
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("invalid catalog")
            if body.get("code") not in (None, 0, "0"):
                _stream_error(body["code"])
            rows = body.get("config_info_list")
            if not isinstance(rows, list):
                raise ValueError("missing catalog")
            models = {}
            for row in rows:
                if not isinstance(row, dict) or row.get("config_switch") is not True or row.get("is_invisible_to_user"):
                    continue
                display = row.get("display_config")
                details = row.get("model_detail_list")
                if (row.get("config_source") != 1 or not isinstance(display, dict)
                        or display.get("is_custom_model") is not False
                        or display.get("model_capability") not in ("chat_model", "reasoning_model")
                        or not isinstance(details, list) or len(details) != 1
                        or not isinstance(details[0], dict)):
                    continue
                name, model = row.get("config_name"), details[0].get("model_name")
                signed_params = details[0].get("encrypted_model_params")
                if (isinstance(name, str) and name.strip() and isinstance(model, str)
                        and model.strip() and not any(c in name + model for c in "\r\n")
                        and isinstance(signed_params, str) and signed_params.strip()):
                    models[name] = {"id": name, "model_name": model,
                                    "encrypted_model_params": signed_params}
            if not models:
                raise ValueError("empty catalog")
            return list(models.values())
        except requests.RequestException:
            raise GatewayError("Trae CN 模型目录连接失败", 502, "upstream_unavailable") from None
        except (ValueError, TypeError, UnicodeError):
            raise GatewayError("Trae CN 模型目录响应无效", 502, "invalid_response") from None
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def _credential_from_desktop() -> dict:
        try:
            adapter_cls = _checkin_adapter()
            adapter = adapter_cls(dry=True)
            adapter._load_credential()
        except Exception:
            raise GatewayError("未找到 Trae CN 本地登录态，请先打开桌面端登录", 400,
                               "local_credential_unavailable") from None
        token = str(adapter.token or "").strip()
        if not token:
            raise GatewayError("Trae CN 本地登录态缺少访问令牌", 401, "invalid_credential")
        claims = _token_payload(token)
        user_id = str(adapter.user_id or claims.get("user_id") or claims.get("uid") or "").strip()
        return {"access_token": token, "device_id": str(adapter.device_id or ""),
                "machine_id": str(getattr(adapter, "machine_id", "") or ""),
                "user_id": user_id, "api_host": str(adapter.host or ORIGIN)}

    def import_local(self):
        credential = self._credential_from_desktop()
        external_id = credential.get("user_id") or _token_payload(credential["access_token"]).get("sub")
        if not external_id:
            raise GatewayError("Trae CN 登录态缺少账号标识，无法导入", 502, "invalid_identity")
        return [{"credential": credential, "external_id": str(external_id),
                 "label": "Trae CN 账号"}]

    def models(self, credential):
        return [{"id": row["id"]} for row in self._fetch_models(credential)]

    @staticmethod
    def _prepare(payload):
        if not isinstance(payload, dict):
            raise GatewayError("Trae CN 请求格式无效", 400, "invalid_request")
        if set(payload) - {"model", "messages", "stream", "n"}:
            raise GatewayError("Trae CN 仅支持纯文本 messages、model、stream 和 n=1",
                               400, "unsupported_parameter")
        if (not isinstance(payload.get("n", 1), int) or isinstance(payload.get("n", 1), bool)
                or payload.get("n", 1) != 1
                or not isinstance(payload.get("stream", True), bool)):
            raise GatewayError("Trae CN 仅支持 n=1 和布尔 stream", 400, "unsupported_parameter")
        model = payload.get("model")
        messages = payload.get("messages")
        if not isinstance(model, str) or not model.strip():
            raise GatewayError("Trae CN 需要目录中的模型配置名", 400, "invalid_model")
        if not isinstance(messages, list) or len(messages) != 1:
            raise GatewayError("Trae CN 当前仅支持一条 user 纯文本消息", 400, "unsupported_messages")
        message = messages[0]
        if (not isinstance(message, dict) or set(message) - {"role", "content"}
                or message.get("role") != "user"
                or not isinstance(message.get("content"), str)):
            raise GatewayError("Trae CN 当前仅支持一条 user 纯文本消息",
                               400, "unsupported_messages")
        if not message["content"].strip():
            raise GatewayError("Trae CN 需要非空用户消息", 400, "invalid_messages")
        return model, [{"role": "user",
                        "content": [{"type": "text", "text": message["content"]}]}]

    @staticmethod
    def _events(response):
        event, data, data_size = "", [], 0
        for raw in response.iter_lines():
            line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            if not isinstance(line, str) or len(line) > 1024 * 1024:
                raise GatewayError("Trae CN 流事件无效", 502, "invalid_stream")
            if not line:
                if data:
                    try:
                        value = json.loads("\n".join(data))
                    except ValueError:
                        raise GatewayError("Trae CN 流事件不是有效 JSON", 502, "invalid_stream") from None
                    if not event or not isinstance(value, dict):
                        raise GatewayError("Trae CN 缺少命名流事件", 502, "invalid_stream")
                    yield event, value
                event, data, data_size = "", [], 0
            elif line.startswith(":"):
                continue
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                part = line[5:].lstrip(" ")
                data_size += len(part.encode("utf-8")) + 1
                if data_size > 4 * 1024 * 1024:
                    raise GatewayError("Trae CN 单个流事件过大", 502, "invalid_stream")
                data.append(part)
            elif not line.startswith(("id:", "retry:")):
                raise GatewayError("Trae CN 流事件字段无效", 502, "invalid_stream")
        if data or event:
            raise GatewayError("Trae CN 流事件提前结束", 502, "invalid_stream")

    def chat(self, credential, payload):
        """Translate verified named SSE deltas into OpenAI-compatible chunks."""
        config_name, messages = self._prepare(payload)
        models = self._fetch_models(credential)
        model = next((row for row in models if row["id"] == config_name), None)
        if model is None:
            raise GatewayError("Trae CN 当前目录中没有该模型配置", 400, "invalid_model")
        response = None
        completion_id, created = "chatcmpl-" + uuid.uuid4().hex, int(time.time())
        emitted, finished, role_sent, usage = False, False, False, None
        session_id = str(uuid.uuid4())
        try:
            response = self.http.post(CHAT_ORIGIN + CHAT_PATH,
                headers=self._headers(credential, stream=True), stream=True,
                timeout=(15, 90), allow_redirects=False,
                json={"config_name": config_name, "model_name": model["model_name"],
                      "config_source": 1, "conversation_id": session_id,
                      "session_id": session_id, "stream": True, "messages": messages,
                      "encrypted_model_params": model["encrypted_model_params"]})
            self._check_http(response)
            if "text/event-stream" not in response.headers.get("content-type", "").lower():
                raise GatewayError("Trae CN 没有返回对话事件流", 502, "invalid_stream")
            for event, value in self._events(response):
                if event == "error":
                    _stream_error(value.get("code"))
                if event == "done":
                    if (set(value) - {"finish_reason"}
                            or value.get("finish_reason") != "stop" or not emitted):
                        raise GatewayError("Trae CN 未返回完整有效的文本回答", 502, "invalid_stream")
                    finished = True
                    break
                if event in {"metadata", "timing_cost", "extra_info"}:
                    # Observed control events may contain private server context;
                    # do not reinterpret any of their fields as assistant text.
                    continue
                if event == "token_usage":
                    usage = {}
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        count = value.get(key)
                        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                            raise GatewayError("Trae CN token 用量事件无效", 502, "invalid_stream")
                        usage[key] = count
                    continue
                if (event != "output"
                        or set(value) - {"response", "reasoning_content", "tool_calls",
                                         "multimodal_contents", "phase"}
                        or not ({"response", "reasoning_content"} & set(value))):
                    raise GatewayError("Trae CN 返回了未核实的流事件", 502, "invalid_stream")
                if value.get("tool_calls") not in (None, []) or value.get("multimodal_contents") not in (None, []):
                    raise GatewayError("Trae CN 当前不支持工具或多模态输出", 502, "unsupported_response")
                delta = {}
                for source, target in (("response", "content"), ("reasoning_content", "reasoning_content")):
                    if source not in value:
                        continue
                    addition = value[source]
                    # Live output is incremental, including repeated words and
                    # punctuation. A null reasoning field adds nothing this frame.
                    if source == "reasoning_content" and addition is None:
                        continue
                    if not isinstance(addition, str):
                        raise GatewayError("Trae CN 文本增量格式无效", 502, "invalid_stream")
                    if addition:
                        delta[target] = addition
                        emitted = emitted or (source == "response" and bool(addition.strip()))
                if delta:
                    # Do not yield an empty role chunk before the first useful
                    # delta: a leading upstream error must retain its HTTP status.
                    if not role_sent:
                        delta["role"] = "assistant"
                        role_sent = True
                    yield {"id": completion_id, "object": "chat.completion.chunk",
                           "created": created, "model": config_name,
                           "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
            if not finished:
                raise GatewayError("Trae CN 对话流缺少有效结束事件", 502, "invalid_stream")
            terminal = {"id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": config_name,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            if usage is not None:
                terminal["usage"] = usage
            yield terminal
        except requests.RequestException:
            raise GatewayError("Trae CN 对话连接中断", 502, "upstream_unavailable") from None
        except (ValueError, UnicodeError):
            raise GatewayError("Trae CN 对话事件流无效", 502, "invalid_stream") from None
        finally:
            if response is not None:
                response.close()
