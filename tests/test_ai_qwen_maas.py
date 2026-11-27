import copy
import json
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.ai_gateway.base import GatewayError
from modules.ai_gateway.compat import collect_completion
from modules.ai_gateway.providers.qwen import QwenProvider
from modules.ai_gateway.providers.qwen_maas import MAAS_BASE, MAAS_ORIGIN, _anthropic_request
from modules.ai_checkin.vendor.platforms import qwenwork as protocol


def credential():
    return {"schemaVersion": 3, "token": "host-private-token", "refreshToken": "desktop-private-refresh",
            "expiresAt": datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat(),
            "endpoints": {"apiOrigin": protocol.API_ORIGIN},
            "user": {"id": "test-user"}, "webIdentitySession": {"phase": "active"}}


def openai_stream():
    frames = [
        {"id": "chat-1", "choices": [{"index": 0, "delta": {"role": "assistant",
            "tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                            "function": {"name": "weather", "arguments": '{"city":'}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
            "function": {"arguments": '"Shanghai"}'}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}},
    ]
    return "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"


def anthropic_stream(complete=True):
    frames = [
        {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 11}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Need tool"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "call-1", "name": "weather", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"city":'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '"Shanghai"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
    ]
    if complete:
        frames.append({"type": "message_stop"})
    return "".join("event: " + frame["type"] + "\ndata: " + json.dumps(frame) + "\n\n" for frame in frames)


class QwenMaasTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.cred = credential()
        self.catalog = {"schema_version": 2, "catalog": [
            {"key": "flash", "format": "openai/chat-completions", "harness": "oc", "enable": True},
            {"key": "qwen4-max", "format": "anthropic/messages", "harness": "oc", "enable": True},
            {"key": "off", "format": "openai/chat-completions", "enable": False},
            {"key": "responses-only", "format": "openai/responses", "enable": True},
        ]}
        self.mint = {"access_token": "maas-private-token", "expires_at_ms": (time.time() + 3600) * 1000,
                     "ttl_ms": 3600000, "session_id": "session-new", "base_url": MAAS_ORIGIN}
        self.exchange = {"access_token": "maas-rotated-token", "expires_at": (time.time() + 3600) * 1000}
        self.stream = openai_stream()
        self.exchange_status, self.chat_status = 200, 200
        transport = httpx.MockTransport(self.handle)
        self.provider = QwenProvider(client_factory=lambda **kwargs: httpx.Client(transport=transport, **kwargs))
        self.payload = {"model": "flash", "messages": [{"role": "user", "content": "Hi"}]}

    def handle(self, request):
        self.requests.append(request)
        if request.url.path == "/api/desktop/chat-modes":
            return httpx.Response(200, json=self.catalog)
        if request.url.path == "/api/desktop-sessions":
            return httpx.Response(200, json=self.mint)
        if request.url.path == "/v1/access-tokens/exchange":
            return httpx.Response(self.exchange_status, json=self.exchange)
        if request.url.path in ("/v1/chat/completions", "/v1/messages"):
            return httpx.Response(self.chat_status, text=self.stream, headers={"content-type": "text/event-stream"})
        raise AssertionError("Unexpected request: " + str(request.url))

    def cached_lease(self, remaining=3600):
        self.cred["_qwen_maas"] = {**self.mint, "base_url": MAAS_BASE,
                                    "expires_at_ms": (time.time() + remaining) * 1000}

    def test_models_use_host_token_and_do_not_mint_or_invent_models(self):
        models = self.provider.models(self.cred)
        self.assertEqual([row["id"] for row in models], ["flash", "qwen4-max"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.host, "gateway.qwenwork.cn")
        self.assertEqual(self.requests[0].headers["Authorization"], "Bearer host-private-token")
        self.provider.models(self.cred)
        self.assertEqual(len(self.requests), 1)

    def test_other_host_origin_is_rejected_before_any_http(self):
        self.cred["endpoints"]["apiOrigin"] = "https://gateway.qwenwork.cn.evil.invalid"
        with self.assertRaises(GatewayError) as caught:
            self.provider.models(self.cred)
        self.assertEqual(caught.exception.code, "invalid_upstream_origin")
        self.assertEqual(self.requests, [])

    def test_expired_host_is_rejected_without_refreshing_desktop_credentials(self):
        self.cred["expiresAt"] = "2000-01-01T00:00:00Z"
        before = copy.deepcopy(self.cred)
        with self.assertRaises(GatewayError):
            list(self.provider.chat(self.cred, self.payload))
        self.assertEqual(self.cred, before)
        self.assertEqual(self.requests, [])

    def test_mint_creates_separate_lease_and_openai_preserves_tool_usage(self):
        result = collect_completion(self.provider.chat(self.cred, self.payload), "flash")
        self.assertEqual(self.cred["token"], "host-private-token")
        self.assertEqual(self.cred["refreshToken"], "desktop-private-refresh")
        self.assertEqual(self.cred["_qwen_maas"]["access_token"], "maas-private-token")
        mint = next(r for r in self.requests if r.url.path == "/api/desktop-sessions")
        self.assertEqual(json.loads(mint.content), {})
        chat = self.requests[-1]
        self.assertEqual(str(chat.url), MAAS_BASE + "/chat/completions")
        self.assertEqual(chat.headers["Authorization"], "Bearer maas-private-token")
        self.assertEqual(chat.headers["x-session-id"], "session-new")
        self.assertEqual(json.loads(chat.content)["stream_options"], {"include_usage": True})
        self.assertEqual(result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"], '{"city":"Shanghai"}')
        self.assertEqual(result["usage"]["total_tokens"], 18)

    def test_unexpired_lease_is_reused_without_mint(self):
        self.cached_lease()
        list(self.provider.chat(self.cred, self.payload))
        self.assertEqual([r.url.path for r in self.requests], ["/api/desktop/chat-modes", "/v1/chat/completions"])

    def test_half_life_lease_rotates_and_keeps_same_session(self):
        self.cached_lease(remaining=120)
        list(self.provider.chat(self.cred, self.payload))
        exchange = next(r for r in self.requests if r.url.path.endswith("/exchange"))
        self.assertEqual(json.loads(exchange.content), {"ttl_ms": 3600000})
        self.assertEqual(exchange.headers["Authorization"], "Bearer maas-private-token")
        self.assertEqual(self.requests[-1].headers["Authorization"], "Bearer maas-rotated-token")
        self.assertEqual(self.cred["_qwen_maas"]["session_id"], "session-new")

    def test_temporary_exchange_failure_reuses_still_valid_lease(self):
        self.cached_lease(remaining=120)
        self.exchange_status = 503
        list(self.provider.chat(self.cred, self.payload))
        self.assertFalse(any(r.url.path == "/api/desktop-sessions" for r in self.requests))
        self.assertEqual(self.requests[-1].headers["Authorization"], "Bearer maas-private-token")

    def test_expired_lease_mints_again_for_same_session(self):
        self.cached_lease(remaining=-1)
        list(self.provider.chat(self.cred, self.payload))
        mint = next(r for r in self.requests if r.url.path == "/api/desktop-sessions")
        self.assertEqual(json.loads(mint.content), {"session_id": "session-new"})
        self.assertFalse(any(r.url.path.endswith("/exchange") for r in self.requests))

    def test_mint_rejects_changed_session_and_unverified_model_origin(self):
        for changes in ({"session_id": "other-session"}, {"base_url": "https://evil.invalid/v1"},
                        {"base_url": "https://maas.xiaoluozi.cn@evil.invalid/v1"}, {"expires_at_ms": 0}):
            with self.subTest(changes=changes):
                self.cred = credential(); self.cached_lease(remaining=-1)
                before = copy.deepcopy(self.mint)
                self.mint.update(changes)
                with self.assertRaises(GatewayError):
                    list(self.provider.chat(self.cred, self.payload))
                self.assertNotIn("evil.invalid", " ".join(str(r.url) for r in self.requests))
                self.mint = before

    def test_minted_regional_origin_is_exactly_allowlisted(self):
        self.mint["base_url"] = "https://api9.qwenwork.cn"
        list(self.provider.chat(self.cred, self.payload))
        self.assertEqual(str(self.requests[-1].url), "https://api9.qwenwork.cn/v1/chat/completions")
        self.assertEqual(self.requests[-1].headers["Authorization"], "Bearer maas-private-token")

    def test_unverified_scheme_path_port_and_userinfo_are_rejected(self):
        for origin in ("http://api9.qwenwork.cn", "https://api9.qwenwork.cn.evil.invalid",
                       "https://api9.qwenwork.cn:8443", "https://user@api9.qwenwork.cn",
                       "https://api9.qwenwork.cn/other"):
            with self.subTest(origin=origin):
                self.cred = credential(); self.mint = {"access_token": "maas-private-token",
                    "expires_at_ms": (time.time() + 3600) * 1000, "ttl_ms": 3600000,
                    "session_id": "session-new", "base_url": origin}
                with self.assertRaises(GatewayError) as caught:
                    list(self.provider.chat(self.cred, self.payload))
                self.assertEqual(caught.exception.code, "invalid_upstream_origin")
                self.assertFalse(any(r.url.host == "api9.qwenwork.cn" for r in self.requests))

    def test_unauthorized_chat_invalidates_lease_and_never_replays_chat(self):
        self.chat_status = 401
        with self.assertRaises(GatewayError) as caught:
            list(self.provider.chat(self.cred, self.payload))
        self.assertEqual(caught.exception.status_code, 401)
        self.assertNotIn("_qwen_maas", self.cred)
        self.assertEqual(sum(r.url.path == "/v1/chat/completions" for r in self.requests), 1)

    def test_unknown_model_does_not_mint_or_send_chat(self):
        with self.assertRaises(GatewayError) as caught:
            list(self.provider.chat(self.cred, {**self.payload, "model": "invented-model"}))
        self.assertEqual(caught.exception.code, "invalid_model")
        self.assertEqual(len(self.requests), 1)

    def test_anthropic_model_preserves_tool_fragments_reasoning_and_usage(self):
        self.stream = anthropic_stream()
        payload = {**self.payload, "model": "qwen4-max", "max_tokens": 64,
                   "tools": [{"type": "function", "function": {"name": "weather", "parameters": {"type": "object"}}}],
                   "tool_choice": {"type": "function", "function": {"name": "weather"}}}
        result = collect_completion(self.provider.chat(self.cred, payload), "qwen4-max")
        choice = result["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["reasoning_content"], "Need tool")
        self.assertEqual(choice["message"]["tool_calls"][0]["function"], {"name": "weather", "arguments": '{"city":"Shanghai"}'})
        self.assertEqual(result["usage"], {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})
        chat = self.requests[-1]
        self.assertEqual(chat.url.path, "/v1/messages")
        self.assertEqual(chat.headers["anthropic-version"], "2023-06-01")
        request = json.loads(chat.content)
        self.assertEqual(request["tools"][0]["input_schema"], {"type": "object"})
        self.assertEqual(request["tool_choice"], {"type": "tool", "name": "weather"})
        self.assertNotIn("stream_options", request)

    def test_anthropic_interruption_does_not_return_successful_completion(self):
        self.stream = anthropic_stream(complete=False)
        with self.assertRaises(GatewayError) as caught:
            list(self.provider.chat(self.cred, {**self.payload, "model": "qwen4-max"}))
        self.assertEqual(caught.exception.code, "incomplete_stream")

    def test_anthropic_request_converts_tool_roundtrip_and_base64_image(self):
        result = _anthropic_request({"model": "qwen4-max", "messages": [
            {"role": "system", "content": "System"},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "tool-1", "type": "function", "function": {"name": "weather", "arguments": '{"city":"Shanghai"}'}}]},
            {"role": "tool", "tool_call_id": "tool-1", "content": "Rain"},
        ]})
        self.assertEqual(result["system"], "System")
        self.assertEqual(result["messages"][0]["content"][0]["source"]["media_type"], "image/png")
        self.assertEqual(result["messages"][1]["content"][0]["input"], {"city": "Shanghai"})
        self.assertEqual(result["messages"][2]["content"][0]["tool_use_id"], "tool-1")

    def test_local_import_permission_error_is_distinct(self):
        with patch.object(protocol, "read_local_session", side_effect=PermissionError):
            with self.assertRaises(GatewayError) as caught:
                self.provider.import_local()
        self.assertEqual(caught.exception.code, "local_credentials_permission_denied")


if __name__ == "__main__":
    unittest.main()
