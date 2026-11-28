import json
from unittest import TestCase
from unittest.mock import patch

from modules.ai_gateway.base import GatewayError
from modules.ai_gateway.providers.doubao import DoubaoProvider


class _Response:
    status_code = 200
    is_success = True
    headers = {"content-type": "text/event-stream"}

    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Client:
    response = None
    request = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, method, url, **kwargs):
        self.request = (method, url, kwargs)
        type(self).request = self.request
        return self.response


class DoubaoProviderTests(TestCase):
    def test_import_local_keeps_cookie_private_and_stable_identity(self):
        provider = DoubaoProvider()
        cookies = ([{"name": "sessionid", "value": "session-a", "domain": ".doubao.com"}], "ok")
        with patch("modules.ai_gateway.providers.doubao.protocol.read_client_cookies", return_value=cookies):
            first = provider.import_local()[0]
            second = provider.import_local()[0]
        self.assertEqual(first["external_id"], second["external_id"])
        self.assertEqual(first["credential"], {"cookies": "sessionid=session-a"})

    def test_chat_translates_sse_completion_and_finish(self):
        cmpl = {
            "event_type": 2001,
            "event_data": json.dumps({"message": {"content": json.dumps({"text": "hello"})}}),
        }
        fin = {"event_type": 2003, "event_data": "{}"}
        _Client.response = _Response([f"data: {json.dumps(cmpl)}", "", f"data: {json.dumps(fin)}", ""])
        provider = DoubaoProvider(client_factory=_Client)
        chunks = list(provider.chat({"cookies": "sessionid=session-a"}, {"model": "doubao", "messages": [{"role": "user", "content": "hi"}]}))
        self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "hello")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(_Client.request[0:2], ("POST", "https://www.doubao.com/chat/completion"))
        body = _Client.request[2]["json"]
        self.assertEqual(body["messages"][0]["content_block"][0]["content"]["text_block"]["text"], "hi")
        self.assertTrue(body["client_meta"]["local_conversation_id"].startswith("local_"))
        self.assertTrue(body["option"]["need_create_conversation"])
        self.assertEqual(_Client.request[2]["params"]["aid"], 497858)

    def test_chat_rejects_non_text_messages(self):
        provider = DoubaoProvider()
        with self.assertRaises(GatewayError) as error:
            list(provider.chat({"cookies": "sessionid=session-a"}, {"model": "doubao", "messages": [{"role": "user", "content": [{"type": "image_url"}]}]}))
        self.assertEqual(error.exception.code, "unsupported_content")

    @staticmethod
    def _lines(events):
        lines = []
        for name, value in events:
            lines.extend(["event: " + name, "data: " + json.dumps(value), ""])
        return lines

    @staticmethod
    def _block(text, **extra):
        return {"block_id": "b1", "block_type": 10000,
                "content": {"text_block": {"text": text}}, **extra}

    def _chat(self, events):
        _Client.response = _Response(self._lines(events))
        return DoubaoProvider(client_factory=_Client).chat(
            {"cookies": "sessionid=test"},
            {"model": "doubao", "messages": [{"role": "user", "content": "hi"}]},
        )

    def test_named_sse_patches_do_not_duplicate_final_snapshot_or_echo_user(self):
        events = [
            ("SSE_HEARTBEAT", {}),
            ("FULL_MSG_NOTIFY", {"message": {"user_type": 1, "message_id": "question",
                                                 "content_block": [self._block("hi")]}}),
            ("STREAM_MSG_NOTIFY", {"meta": {"message_id": "answer", "user_type": 2},
                                     "content": {"content_block": [self._block("O")]}}),
            ("STREAM_CHUNK", {"message_id": "answer", "patch_op": [
                {"patch_object": 1, "patch_type": 1,
                 "patch_value": {"content_block": [self._block("K", patch_type=1)]}},
            ]}),
            ("FULL_MSG_NOTIFY", {"message": {"user_type": 2, "message_id": "answer",
                                                 "content_block": [self._block("OK")]}}),
            ("SSE_REPLY_END", {"end_type": 1}),
            ("SSE_REPLY_END", {"end_type": 3}),
        ]
        chunks = list(self._chat(events))
        self.assertEqual("".join(x["choices"][0]["delta"].get("content", "") for x in chunks), "OK")
        self.assertEqual(sum(x["choices"][0]["finish_reason"] == "stop" for x in chunks), 1)

    def test_http_200_stream_error_is_rate_limit_not_success(self):
        events = [("SSE_HEARTBEAT", {}),
                  ("STREAM_ERROR", {"error_code": 710022002, "error_msg": "secret-cookie"}),
                  ("SSE_REPLY_END", {"end_type": 3})]
        with self.assertRaises(GatewayError) as error:
            list(self._chat(events))
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(error.exception.code, "rate_limited")
        self.assertNotIn("secret-cookie", str(error.exception))

    def test_error_after_text_and_early_close_do_not_emit_success_stop(self):
        text = ("STREAM_MSG_NOTIFY", {"meta": {"message_id": "answer"},
                                        "content": {"content_block": [self._block("O")]}})
        for end, code in [([], "invalid_stream"),
                          ([("STREAM_ERROR", {"error_code": 710022002})], "rate_limited")]:
            with self.subTest(code=code):
                chunks = self._chat([text] + end)
                self.assertEqual(next(chunks)["choices"][0]["delta"]["content"], "O")
                with self.assertRaises(GatewayError) as error:
                    next(chunks)
                self.assertEqual(error.exception.code, code)

    def test_complete_stream_without_text_is_invalid(self):
        with self.assertRaises(GatewayError) as error:
            list(self._chat([("SSE_REPLY_END", {"end_type": 3})]))
        self.assertEqual(error.exception.code, "invalid_stream")

    def test_http_200_json_error_is_not_sse_success(self):
        class JsonResponse(_Response):
            headers = {"content-type": "application/json; charset=utf-8"}

            def read(self):
                return b""

            def json(self):
                return {"code": 710022002, "msg": "secret-cookie"}

        _Client.response = JsonResponse([])
        provider = DoubaoProvider(client_factory=_Client)
        with self.assertRaises(GatewayError) as error:
            list(provider.chat({"cookies": "sessionid=test"},
                               {"messages": [{"role": "user", "content": "hi"}]}))
        self.assertEqual(error.exception.status_code, 429)
        self.assertNotIn("secret-cookie", str(error.exception))

    def test_unverified_chat_not_advertised_as_an_available_model(self):
        provider = DoubaoProvider()
        self.assertFalse(provider.capabilities["chat"])
        self.assertIn("710022002", provider.capabilities["reason"])
        with self.assertRaises(GatewayError) as error:
            provider.models({"cookies": "sessionid=test"})
        self.assertEqual(error.exception.code, "protocol_unverified")
