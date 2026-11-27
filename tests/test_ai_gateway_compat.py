import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import unittest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from modules.ai_gateway.base import GatewayError
from modules.ai_gateway.compat import (anthropic_events, anthropic_to_openai, collect_completion,
                                      completion_to_anthropic)
from modules.ai_gateway.core import Gateway
from modules.ai_gateway.routes import _operation, _sse


def chunk(delta=None, finish=None, usage=None):
    value = {"id": "chatcmpl-example", "model": "workbuddy/model-a", "created": 123,
             "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        value["usage"] = usage
    return value


class CompatibilityTests(unittest.TestCase):
    def test_full_parallel_tool_round_trip_keeps_ids_schemas_and_error_results(self):
        body = {"model": "workbuddy/model-a", "max_tokens": 100,
                "system": [{"type": "text", "text": "Use tools."}],
                "tools": [{"name": "weather", "description": "Weather",
                           "input_schema": {"type": "object", "properties": {"city": {"type": "string"}},
                                            "required": ["city"]}}],
                "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
                "messages": [
                    {"role": "user", "content": "Weather for both cities?"},
                    {"role": "assistant", "content": [
                        {"type": "text", "text": "Checking."},
                        {"type": "tool_use", "id": "call-a", "name": "weather", "input": {"city": "A"}},
                        {"type": "tool_use", "id": "call-b", "name": "weather", "input": {"city": "B"}}]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "call-a", "content": [{"type": "text", "text": "Sunny"}]},
                        {"type": "tool_result", "tool_use_id": "call-b", "content": "Unavailable", "is_error": True},
                        {"type": "text", "text": "Summarize."}]}]}
        before = copy.deepcopy(body)
        actual = anthropic_to_openai(body)
        self.assertEqual(body, before)
        self.assertEqual(actual["messages"][0], {"role": "system", "content": "Use tools."})
        assistant = actual["messages"][2]
        self.assertEqual([t["id"] for t in assistant["tool_calls"]], ["call-a", "call-b"])
        self.assertEqual(json.loads(assistant["tool_calls"][1]["function"]["arguments"]), {"city": "B"})
        self.assertEqual([m["tool_call_id"] for m in actual["messages"][3:5]], ["call-a", "call-b"])
        self.assertIn("Unavailable", actual["messages"][4]["content"])
        self.assertEqual(actual["messages"][5]["role"], "user")
        self.assertEqual(actual["tool_choice"], "required")
        self.assertFalse(actual["parallel_tool_calls"])
        self.assertEqual(actual["tools"][0]["function"]["parameters"], body["tools"][0]["input_schema"])

    def test_nonstream_aggregation_keeps_parallel_argument_fragments_and_usage(self):
        source = [
            chunk({"role": "assistant", "reasoning_content": "reason", "tool_calls": [
                {"index": 0, "id": "a", "function": {"name": "get_", "arguments": '{"city":'}},
                {"index": 1, "id": "b", "function": {"name": "time", "arguments": "{"}}]}),
            chunk({"tool_calls": [{"index": 1, "function": {"arguments": "}"}},
                                   {"index": 0, "function": {"name": "weather", "arguments": '"A"}'}}]}, "tool_calls"),
            {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 9, "total_tokens": 21,
                "prompt_tokens_details": {"cached_tokens": 5}}},
        ]
        actual = collect_completion(iter(source), "workbuddy/model-a")
        self.assertIsNone(actual["choices"][0]["message"]["content"])
        tools = actual["choices"][0]["message"]["tool_calls"]
        self.assertEqual(tools[0]["function"]["name"], "get_weather")
        self.assertEqual(json.loads(tools[0]["function"]["arguments"]), {"city": "A"})
        self.assertEqual(actual["usage"]["prompt_tokens_details"]["cached_tokens"], 5)
        anthropic = completion_to_anthropic(actual)
        self.assertEqual(anthropic["stop_reason"], "tool_use")
        self.assertEqual([b["id"] for b in anthropic["content"]], ["a", "b"])
        self.assertEqual(anthropic["usage"], {"input_tokens": 12, "output_tokens": 9})

    def test_nonstream_tool_input_requires_json_object(self):
        for arguments in ("[]", "123", '"string"', "null", "{broken"):
            with self.subTest(arguments=arguments):
                completion = {"id": "completion", "model": "workbuddy/model-a", "choices": [{
                    "finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": [{
                        "id": "a", "type": "function", "function": {"name": "lookup", "arguments": arguments}}]}}]}
                with self.assertRaises(GatewayError):
                    completion_to_anthropic(completion)

    def test_anthropic_stream_keeps_fragmented_tool_name_and_parallel_ids(self):
        source = [
            chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "get_"}},
                                   {"index": 1, "id": "b", "function": {"name": "time"}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"name": "weather", "arguments": '{"city":'}},
                                   {"index": 1, "function": {"arguments": "{"}}]}),
            chunk({"tool_calls": [{"index": 1, "function": {"arguments": "}"}},
                                   {"index": 0, "function": {"arguments": '"A"}'}}]}, "tool_calls"),
        ]
        events = list(anthropic_events(iter(source), "workbuddy/model-a"))
        blocks = [d["content_block"] for e, d in events if e == "content_block_start"]
        self.assertEqual({b["id"]: b["name"] for b in blocks}, {"a": "get_weather", "b": "time"})
        arguments = {}
        for event, data in events:
            if event == "content_block_start":
                arguments[data["index"]] = ""
            if event == "content_block_delta" and data["delta"]["type"] == "input_json_delta":
                arguments[data["index"]] += data["delta"]["partial_json"]
        self.assertEqual(sorted(json.dumps(json.loads(v), sort_keys=True) for v in arguments.values()), ['{"city": "A"}', '{}'])

    def test_anthropic_stream_blocks_are_started_and_closed_sequentially(self):
        source = [chunk({"content": "Checking", "tool_calls": [
            {"index": 0, "id": "a", "function": {"name": "one", "arguments": "{}"}},
            {"index": 1, "id": "b", "function": {"name": "two", "arguments": "{}"}}]}, "tool_calls")]
        active = None
        for event, data in anthropic_events(iter(source), "workbuddy/model-a"):
            if event == "content_block_start":
                self.assertIsNone(active, "new block started before previous block_stop")
                active = data["index"]
            elif event == "content_block_delta":
                self.assertEqual(active, data["index"])
            elif event == "content_block_stop":
                self.assertEqual(active, data["index"])
                active = None
        self.assertIsNone(active)

    def test_late_raw_exception_is_sanitized_for_both_stream_formats(self):
        def failing():
            yield chunk({"content": "start"})
            raise RuntimeError("secret-token-and-cookie")
        openai = "".join(_sse(failing()))
        self.assertNotIn("secret-token-and-cookie", openai)
        self.assertIn('"error"', openai)
        self.assertTrue(openai.endswith("data: [DONE]\n\n"))
        anthropic = "".join(_sse(anthropic_events(failing(), "workbuddy/model-a"), anthropic=True))
        self.assertNotIn("secret-token-and-cookie", anthropic)
        self.assertIn("event: error", anthropic)
        self.assertNotIn("event: message_stop", anthropic)

    def test_early_raw_exception_is_sanitized(self):
        def fail():
            raise RuntimeError("secret-access-token")
        with self.assertRaises(GatewayError) as caught:
            _operation(fail)
        self.assertNotIn("secret-access-token", str(caught.exception))


class MemoryStore:
    def __init__(self):
        self.value = {"access_token": "initial"}
        self.saved = []

    def accounts(self, provider=None):
        return [{"id": "account-a", "provider": "fake", "enabled": True, "label": "Example"}]

    def credential(self, _account_id):
        return copy.deepcopy(self.value)

    def save_credential(self, _account_id, value):
        self.value = copy.deepcopy(value)
        self.saved.append(copy.deepcopy(value))


class GatewayLifetimeTests(unittest.TestCase):
    def gateway(self, chat):
        provider = type("Provider", (), {"id": "fake", "name": "Fake",
                    "capabilities": {"chat": True}, "chat": staticmethod(chat)})()
        return Gateway(store=MemoryStore(), providers={"fake": provider})

    def test_generator_can_close_on_different_worker_and_save_refresh(self):
        finalized = []
        def provider(credential, payload):
            credential["access_token"] = "rotated"
            try:
                yield chunk({"content": "start"})
                yield chunk(finish="stop")
            finally:
                finalized.append(True)
        gateway = self.gateway(provider)
        stream = gateway.chat({"model": "fake/model-a", "messages": [{"role": "user", "content": "Hello"}]})
        with ThreadPoolExecutor(max_workers=1) as first:
            self.assertEqual(first.submit(next, stream).result()["model"], "fake/model-a")
        self.assertTrue(gateway.account_lock("account-a").locked())
        with ThreadPoolExecutor(max_workers=1) as second:
            second.submit(stream.close).result()
        self.assertFalse(gateway.account_lock("account-a").locked())
        self.assertTrue(finalized)
        self.assertEqual(gateway.store.value["access_token"], "rotated")

    def test_missing_finish_reason_is_failed_and_unlocks_account(self):
        def provider(credential, payload):
            yield chunk({"content": "partial"})
        gateway = self.gateway(provider)
        stream = gateway.chat({"model": "fake/model-a", "messages": [{"role": "user", "content": "Hello"}]})
        with self.assertRaises(GatewayError) as caught:
            collect_completion(stream, "fake/model-a")
        self.assertEqual(caught.exception.code, "incomplete_stream")
        self.assertFalse(gateway.account_lock("account-a").locked())

    def test_exception_before_first_frame_saves_refreshed_credential(self):
        def provider(credential, payload):
            credential["access_token"] = "rotated-before-failure"
            raise RuntimeError("upstream failed")
            yield
        gateway = self.gateway(provider)
        stream = gateway.chat({"model": "fake/model-a", "messages": [{"role": "user", "content": "Hello"}]})
        with self.assertRaises(RuntimeError):
            next(stream)
        self.assertEqual(gateway.store.value["access_token"], "rotated-before-failure")
        self.assertFalse(gateway.account_lock("account-a").locked())


if __name__ == "__main__":
    unittest.main()
