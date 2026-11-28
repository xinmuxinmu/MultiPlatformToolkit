import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from modules.ai_gateway import routes
from modules.ai_gateway.base import GatewayError
from modules.ai_gateway.core import Gateway
from test_ai_gateway_core import FakeProvider, MemoryStore


def sse_events(text):
    events = []
    for block in text.split("\n\n"):
        event, value = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                value = line[6:]
        if value is not None:
            events.append((event, value if value == "[DONE]" else json.loads(value)))
    return events


def tool_frames():
    return [
        {"id": "chat-test", "choices": [{"index": 0, "delta": {"role": "assistant",
            "tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                            "function": {"name": "weather", "arguments": '{"city":'}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
            "function": {"arguments": '"Shanghai"}'}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}},
    ]


class GatewayRouteTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.provider = FakeProvider()
        self.account = self.store.upsert("fake", {"token": "private-account-token"}, "user", "User")
        self.gateway = Gateway(store=self.store, providers={"fake": self.provider})
        self.patch = patch.object(routes, "GATEWAY", self.gateway)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.app = FastAPI()
        self.app.add_exception_handler(GatewayError, routes.error_response)
        self.app.include_router(routes.router)
        self.app.include_router(routes.proxy_router)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 44000))
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer " + self.store.key}
        self.payload = {"model": "fake/text", "messages": [{"role": "user", "content": "Hi"}]}

    def test_proxy_requires_valid_key_and_accepts_both_key_headers(self):
        self.assertEqual(self.client.get("/v1/models").status_code, 401)
        self.assertEqual(self.client.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        for headers in (self.headers, {"x-api-key": self.store.key}):
            response = self.client.get("/v1/models", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual({row["id"] for row in response.json()["data"]}, {"fake/text", "fake/tools"})

    def test_management_rejects_foreign_origin_host_and_remote_clients(self):
        response = self.client.get("/ai-gateway/api/state", headers={"Origin": "https://evil.invalid"})
        self.assertEqual(response.status_code, 403)
        response = self.client.get("/ai-gateway/api/state", headers={"Host": "evil.invalid"})
        self.assertEqual(response.status_code, 403)
        with TestClient(self.app, base_url="http://127.0.0.1:8765", client=("192.0.2.10", 1234)) as remote:
            self.assertEqual(remote.get("/ai-gateway/api/state").status_code, 403)
        allowed = self.client.get("/ai-gateway/api/state", headers={"Origin": "http://127.0.0.1:8765"})
        self.assertEqual(allowed.status_code, 200)
        self.assertNotIn("private-account-token", allowed.text)

    def test_management_login_never_returns_private_verifier_or_token(self):
        response = self.client.post("/ai-gateway/api/login/fake", json={})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("private-verifier", response.text)
        session = response.json()
        done = self.client.get("/ai-gateway/api/login/" + session["id"])
        self.assertEqual(done.json()["status"], "authorized")
        self.assertNotIn("private-token", done.text)
        self.assertNotIn("credential", done.text)

    def test_management_bulk_local_import_route_returns_per_provider_results(self):
        self.gateway.import_all_local = lambda: {
            "results": [{"provider": "fake", "status": "imported", "count": 1}],
            "imported": 1, "failed": 0, "chat_providers": ["fake"]}

        response = self.client.post("/ai-gateway/api/import-local", json={})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["imported"], 1)
        self.assertEqual(response.json()["results"][0]["provider"], "fake")

    def test_openai_nonstream_collects_tools_usage_and_native_model_name(self):
        self.provider.frames = tool_frames()
        response = self.client.post("/v1/chat/completions", headers=self.headers, json=self.payload)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["model"], "fake/text")
        tool = result["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(tool["function"], {"name": "weather", "arguments": '{"city":"Shanghai"}'})
        self.assertIsNone(result["choices"][0]["message"]["content"])
        self.assertEqual(result["usage"]["total_tokens"], 18)
        self.assertEqual(self.provider.requests[0]["model"], "text")
        self.assertTrue(self.provider.requests[0]["stream"])

    def test_openai_stream_passes_tool_deltas_usage_and_done(self):
        self.provider.frames = tool_frames()
        response = self.client.post("/v1/chat/completions", headers=self.headers, json={**self.payload, "stream": True})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["Content-Type"])
        events = sse_events(response.text)
        self.assertEqual(events[-1][1], "[DONE]")
        self.assertEqual(events[-2][1]["usage"]["completion_tokens"], 7)
        self.assertEqual(events[0][1]["choices"][0]["delta"]["tool_calls"][0]["id"], "call-1")
        self.assertEqual(self.provider.closed, 1)

    def anthropic_body(self, stream=False):
        return {"model": "fake/tools", "max_tokens": 128, "stream": stream,
                "system": [{"type": "text", "text": "Be concise"}],
                "messages": [{"role": "user", "content": "Weather?"}],
                "tools": [{"name": "weather", "description": "weather lookup",
                            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}],
                "tool_choice": {"type": "tool", "name": "weather", "disable_parallel_tool_use": True}}

    def test_anthropic_nonstream_converts_tools_usage_and_parameters(self):
        self.provider.frames = tool_frames()
        response = self.client.post("/v1/messages", headers={"x-api-key": self.store.key}, json=self.anthropic_body())
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["stop_reason"], "tool_use")
        self.assertEqual(result["content"], [{"type": "tool_use", "id": "call-1", "name": "weather", "input": {"city": "Shanghai"}}])
        self.assertEqual(result["usage"], {"input_tokens": 11, "output_tokens": 7})
        request = self.provider.requests[0]
        self.assertEqual(request["messages"][0], {"role": "system", "content": "Be concise"})
        self.assertEqual(request["max_tokens"], 128)
        self.assertFalse(request["parallel_tool_calls"])
        self.assertEqual(request["tool_choice"]["function"]["name"], "weather")
        self.assertEqual(request["tools"][0]["function"]["parameters"]["type"], "object")

    def test_anthropic_stream_produces_tool_blocks_and_usage(self):
        self.provider.frames = tool_frames()
        response = self.client.post("/v1/messages", headers=self.headers, json=self.anthropic_body(stream=True))
        events = sse_events(response.text)
        self.assertEqual([name for name, _ in events][0], "message_start")
        self.assertEqual([name for name, _ in events][-1], "message_stop")
        start = next(value for name, value in events if name == "content_block_start")
        self.assertEqual(start["content_block"]["name"], "weather")
        fragments = [value["delta"]["partial_json"] for name, value in events if name == "content_block_delta"]
        self.assertEqual(json.loads("".join(fragments)), {"city": "Shanghai"})
        delta = next(value for name, value in events if name == "message_delta")
        self.assertEqual(delta["usage"], {"input_tokens": 11, "output_tokens": 7})
        self.assertEqual(delta["delta"]["stop_reason"], "tool_use")

    def test_interrupted_openai_stream_reports_error_and_releases_account(self):
        self.provider.frames = self.provider.frames[:1]
        response = self.client.post("/v1/chat/completions", headers=self.headers, json={**self.payload, "stream": True})
        events = sse_events(response.text)
        error = next(value["error"] for _, value in events if isinstance(value, dict) and "error" in value)
        self.assertEqual(error["code"], "incomplete_stream")
        lock = self.gateway.account_lock(self.account["id"])
        self.assertTrue(lock.acquire(blocking=False)); lock.release()

    def test_interrupted_anthropic_stream_has_error_and_no_success_stop(self):
        self.provider.frames = self.provider.frames[:1]
        response = self.client.post("/v1/messages", headers=self.headers, json=self.anthropic_body(stream=True))
        names = [name for name, _ in sse_events(response.text)]
        self.assertIn("error", names)
        self.assertNotIn("message_stop", names)

    def test_raw_upstream_exception_is_not_exposed(self):
        self.provider.frames = [RuntimeError("private-account-token")]
        response = self.client.post("/v1/chat/completions", headers=self.headers, json=self.payload)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private-account-token", response.text)

    def test_invalid_model_and_unsupported_n_fail_before_upstream(self):
        invalid = self.client.post("/v1/chat/completions", headers=self.headers, json={**self.payload, "model": "text"})
        self.assertEqual(invalid.status_code, 400)
        invalid = self.client.post("/v1/chat/completions", headers=self.headers, json={**self.payload, "n": 2})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(self.provider.requests, [])


if __name__ == "__main__":
    unittest.main()
