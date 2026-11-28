import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from modules.ai_gateway.base import GatewayError
from modules.ai_gateway.compat import collect_completion
from modules.ai_gateway.providers.kimi import KimiProvider, WORK_BASE, _read_work_key


class Response:
    def __init__(self, value=None, *, status=200, events=(), headers=None):
        self.value = value
        self.status_code = status
        self.events = events
        self.headers = headers if headers is not None else {"Content-Type": "text/event-stream"}
        self.closed = False

    def json(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value

    def iter_lines(self):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            if isinstance(event, dict):
                yield b"data: " + json.dumps(event, ensure_ascii=False).encode()
                yield b""
            else:
                yield event

    def close(self):
        self.closed = True


class SharedSession:
    def __init__(self):
        self.calls = []
        self.cookies = {"session": "must-not-leak"}
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "POST":
            return Response({"id": "login-1"})
        return Response({"status": "pending"})


class ModelSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def credential():
    return {"msh_user_id": "account-a", "access_token": "desktop-access-secret",
            "refresh_token": "desktop-refresh-secret",
            "work_api_key": "work-key-secret", "work_key_user_id": "account-a"}


def chunk(delta=None, finish=None, **kwargs):
    return {"id": "chat-test", "created": 1, "model": "k3d1-agent",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], **kwargs}


class KimiGatewayTests(unittest.TestCase):
    def test_device_login_and_poll_suppress_shared_session_cookies(self):
        http = SharedSession()
        provider = KimiProvider(http=http)
        login = provider.start_login()
        self.assertEqual(provider.poll_login(login), {"status": "pending"})
        self.assertEqual(len(http.calls), 2)
        for _, _, kwargs in http.calls:
            self.assertEqual(kwargs["headers"].get("Cookie"), "")
            self.assertIn("x-msh-device-id", kwargs["headers"])

    def test_import_uses_matching_native_work_key_without_changing_client_files(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile"
            share = Path(directory) / "moved-share"
            (profile / "bridge-store").mkdir(parents=True)
            (share / "daimon").mkdir(parents=True)
            token_store = profile / "bridge-store" / "token-store.json"
            token_store.write_text(json.dumps({"origin": "https://www.kimi.com", "tokens": {
                "access_token": "desktop-secret", "msh_user_id": "account-a"}}), encoding="utf-8")
            pointer = profile / "daimon-storage.json"
            pointer.write_text(json.dumps({"version": 1, "shareDir": str(share)}), encoding="utf-8")
            keys = share / "daimon" / "kimi-code-key.json"
            keys.write_text(json.dumps({"v": 2, "keys": [
                {"userId": "account-b", "apiKey": "other-key"},
                {"userId": "account-a", "apiKey": "matching-key", "keyId": "key-id"}]}), encoding="utf-8")
            before = {p: p.read_bytes() for p in (token_store, pointer, keys)}
            with patch.dict(os.environ, {"KIMI_USER_DATA_DIR": str(profile)}):
                rows = KimiProvider().import_local()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["external_id"], "account-a")
            self.assertEqual(rows[0]["credential"]["work_api_key"], "matching-key")
            self.assertEqual(rows[0]["credential"]["work_key_user_id"], "account-a")
            self.assertEqual(rows[0]["credential"]["work_key_id"], "key-id")
            self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_absent_or_other_account_work_key_never_sends_desktop_token(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            store = profile / "daimon-share" / "daimon" / "kimi-code-key.json"
            store.parent.mkdir(parents=True)
            for keys in ([], [{"userId": "account-b", "apiKey": "foreign-secret"}]):
                store.write_text(json.dumps({"v": 2, "keys": keys}), encoding="utf-8")
                before = store.read_bytes()
                http = ModelSession()
                value = {"msh_user_id": "account-a", "access_token": "desktop-secret"}
                with patch.dict(os.environ, {"KIMI_USER_DATA_DIR": str(profile)}):
                    with self.assertRaises(GatewayError) as caught:
                        KimiProvider(http=http).models(value)
                    self.assertEqual(caught.exception.code, "work_credential_unavailable")
                    with self.assertRaises(GatewayError):
                        list(KimiProvider(http=http).chat(value, {"model": "k3d1-agent",
                                                               "messages": [{"role": "user", "content": "hi"}]}))
                self.assertEqual(http.calls, [])
                self.assertEqual(store.read_bytes(), before)

    def test_cached_work_key_requires_matching_owner_and_is_not_desktop_token(self):
        for key, owner, code in (("work-key", "account-b", "credential_identity_mismatch"),
                                 ("desktop-access-secret", "account-a", "work_credential_unavailable"),
                                 ("desktop-refresh-secret", "account-a", "work_credential_unavailable"),
                                 ("injected\r\nheader", "account-a", "work_credential_unavailable")):
            with self.subTest(owner=owner, code=code):
                value = {**credential(), "work_api_key": key, "work_key_user_id": owner}
                http = ModelSession()
                with self.assertRaises(GatewayError) as caught:
                    KimiProvider(http=http).models(value)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(http.calls, [])

    def test_model_catalog_recovers_exact_account_key_for_vault_persistence(self):
        response = Response({"data": [{"id": "k3d1-agent"}, {"id": "k3-agent"}, None, {"id": ""}]})
        http = ModelSession(response)
        value = {"msh_user_id": "account-a", "access_token": "desktop-access-secret"}
        with patch("modules.ai_gateway.providers.kimi._read_work_key", return_value={
                "work_api_key": "work-key-secret", "work_key_user_id": "account-a"}) as reader:
            self.assertEqual(KimiProvider(http=http).models(value),
                             [{"id": "k3d1-agent"}, {"id": "k3-agent"}])
        self.assertEqual(reader.call_args.args[1], "account-a")
        self.assertEqual(value["work_api_key"], "work-key-secret")
        method, url, options = http.calls[0]
        self.assertEqual((method, url), ("GET", WORK_BASE + "/models"))
        self.assertEqual(options["headers"]["Authorization"], "Bearer work-key-secret")
        self.assertEqual(options["headers"]["Cookie"], "")
        self.assertFalse(options["allow_redirects"])
        self.assertNotIn("desktop-access-secret", str(options))
        self.assertTrue(response.closed)

    def test_native_store_rejects_unsupported_versions_and_relative_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            store = profile / "daimon-share" / "daimon" / "kimi-code-key.json"
            store.parent.mkdir(parents=True)
            (profile / "daimon-storage.json").write_text(json.dumps({"shareDir": "relative-path"}), encoding="utf-8")
            for version in (1, 3):
                store.write_text(json.dumps({"v": version, "keys": [
                    {"userId": "account-a", "apiKey": "model-key"}]}), encoding="utf-8")
                self.assertEqual(_read_work_key(profile, "account-a"), {})
            store.write_text(json.dumps({"v": 2, "keys": [
                {"userId": "account-a", "apiKey": "model-key"}]}), encoding="utf-8")
            self.assertEqual(_read_work_key(profile, "account-a")["work_api_key"], "model-key")

    def test_models_reject_bad_json_and_close_response(self):
        for value in ([], {}, {"data": {}}, ValueError("secret-error")):
            response = Response(value)
            with self.assertRaises(GatewayError) as caught:
                KimiProvider(http=ModelSession(response)).models(credential())
            self.assertEqual(caught.exception.code, "invalid_upstream_response")
            self.assertNotIn("secret-error", str(caught.exception))
            self.assertTrue(response.closed)

    def test_model_http_errors_are_closed_and_redirects_are_not_followed(self):
        for status in (302, 401, 402, 403, 429, 500):
            response = Response({"error": "upstream-secret"}, status=status)
            http = ModelSession(response)
            with self.assertRaises(GatewayError) as caught:
                KimiProvider(http=http).models(credential())
            self.assertNotIn("upstream-secret", str(caught.exception))
            self.assertFalse(http.calls[0][2]["allow_redirects"])
            self.assertEqual(len(http.calls), 1)
            self.assertTrue(response.closed)

    def test_chat_preserves_tool_and_reasoning_deltas_and_native_usage(self):
        usage = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
        finish = chunk(finish="tool_calls")
        finish["choices"][0]["usage"] = usage
        response = Response(events=[
            chunk({"role": "assistant", "reasoning_content": "reason"}),
            chunk({"content": "OK", "tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                  "function": {"name": "lookup", "arguments": '{"q":'}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"hello"}'}}]}),
            finish, b"data: [DONE]", b"",
        ])
        http = ModelSession(response)
        request = {"model": "k3d1-agent", "messages": [{"role": "user", "content": "hi"}],
                   "max_tokens": 128, "stream": False, "stream_options": {"include_usage": False}}
        before = copy.deepcopy(request)
        completion = collect_completion(KimiProvider(http=http).chat(credential(), request), "kimi/k3d1-agent")
        message = completion["choices"][0]["message"]
        self.assertEqual(message["content"], "OK")
        self.assertEqual(message["reasoning_content"], "reason")
        self.assertEqual(message["tool_calls"][0]["function"], {"name": "lookup", "arguments": '{"q":"hello"}'})
        self.assertEqual(completion["usage"], usage)
        self.assertEqual(completion["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(request, before)
        method, url, options = http.calls[0]
        self.assertEqual((method, url), ("POST", WORK_BASE + "/chat/completions"))
        self.assertTrue(options["json"]["stream"])
        self.assertEqual(options["json"]["max_completion_tokens"], 128)
        self.assertNotIn("max_tokens", options["json"])
        self.assertEqual(options["json"]["thinking"], {"type": "disabled"})
        self.assertEqual(options["json"]["stream_options"], {"include_usage": True})
        self.assertTrue(response.closed)

    def test_chat_errors_and_incomplete_streams_are_never_successful(self):
        cases = [
            ([], "incomplete_stream"),
            ([chunk({"content": "partial"})], "incomplete_stream"),
            ([chunk(finish="stop")], "incomplete_stream"),
            ([b"data: [DONE]", b""], "incomplete_stream"),
            ([b"event: error", b"data: secret-error", b""], "upstream_error"),
            ([{"error": {"message": "secret-error"}}], "upstream_error"),
            ([{"error": {}}], "upstream_error"),
            ([{"type": "error"}], "upstream_error"),
            ([b"data: not-json", b""], "invalid_stream"),
            ([b"data: []", b""], "invalid_stream"),
            ([{"choices": [None]}], "invalid_stream"),
            ([{"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"},
                           {"index": 1, "delta": {"content": "partial"}}]},
              b"data: [DONE]", b""], "invalid_stream"),
            ([{"choices": [{"index": 1, "delta": {}, "finish_reason": "stop"}]}], "invalid_stream"),
            ([{"choices": [{"delta": []}]}], "invalid_stream"),
            ([chunk(finish="error")], "invalid_stream"),
            ([b"data: \xff"], "invalid_stream"),
            ([chunk({"content": "partial"}), requests.ConnectionError("secret-error")], "upstream_unavailable"),
        ]
        for events, code in cases:
            with self.subTest(code=code, events=repr(events)[:60]):
                response = Response(events=events)
                with self.assertRaises(GatewayError) as caught:
                    list(KimiProvider(http=ModelSession(response)).chat(credential(), {"model": "k3d1-agent"}))
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("secret-error", str(caught.exception))
                self.assertTrue(response.closed)

    def test_chat_rejects_non_sse_and_closes_on_client_cancellation(self):
        response = Response(headers={"Content-Type": "application/json"})
        with self.assertRaises(GatewayError) as caught:
            list(KimiProvider(http=ModelSession(response)).chat(credential(), {"model": "k3d1-agent"}))
        self.assertEqual(caught.exception.code, "invalid_stream")
        self.assertTrue(response.closed)
        response = Response(events=[chunk({"content": "partial"})])
        stream = KimiProvider(http=ModelSession(response)).chat(credential(), {"model": "k3d1-agent"})
        next(stream)
        stream.close()
        self.assertTrue(response.closed)

    def test_network_errors_do_not_reveal_request_credentials(self):
        http = ModelSession(requests.ConnectionError("work-key-secret"))
        with self.assertRaises(GatewayError) as caught:
            KimiProvider(http=http).models(credential())
        self.assertEqual(caught.exception.code, "upstream_unavailable")
        self.assertNotIn("work-key-secret", str(caught.exception))

    def test_multiple_completions_are_rejected_before_sending(self):
        for count in (2, 0, None, True, "1"):
            http = ModelSession()
            with self.assertRaises(GatewayError) as caught:
                list(KimiProvider(http=http).chat(credential(), {"model": "k3d1-agent", "n": count}))
            self.assertEqual(caught.exception.code, "unsupported_parameter")
            self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()
