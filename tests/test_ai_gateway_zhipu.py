import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
import requests
from PIL import Image
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from modules.ai_gateway.base import GatewayError
from modules.ai_gateway.compat import collect_completion
from modules.ai_gateway.providers import zhipu


class Response:
    def __init__(self, status=200, *, json_value=None, text="", content=b"", content_type="application/json", events=()):
        self.status_code = status
        self._json = json_value
        self.text = text
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.events = events
        self.closed = False

    def json(self):
        return self._json

    def iter_content(self, _chunk_size):
        yield self.content

    def iter_lines(self):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            if isinstance(event, dict):
                yield b"data: " + json.dumps(event, ensure_ascii=False).encode("utf-8")
                yield b""
            else:
                yield event

    def close(self):
        self.closed = True


class ChatHttp:
    def __init__(self, *responses):
        self.calls = []
        self.responses = list(responses)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def chat_credential():
    return {"access_token": "desktop-secret", "refresh_token": "refresh-secret",
            "device_id": "device-id",
            "_zhipu_chat_models": {"ids": ["glm-5.3-flash"], "expires_at": time.time() + 300}}


def chat_payload(**values):
    return {"model": "glm-5.3-flash", "messages": [{"role": "user", "content": "Reply exactly OK"}],
            **values}


def chat_frame(text="", *, status="init", incremental=True, part_status=None, key="part-1"):
    return {"id": "server-history", "conversation_id": "server-conversation", "status": status,
            "last_error": {}, "meta_data": {"if_increase_push": incremental},
            "parts": [{"id": key, "logic_id": key, "role": "assistant",
                       "status": part_status or status, "content": [{"type": "text", "text": text}]}]}


class FakeHttp:
    def __init__(self, *, login_code=405, checkin=None, refresh=None, identity=None):
        self.calls = []
        self.login_code = login_code
        self.checkin = list(checkin) if isinstance(checkin, list) else [
            checkin or Response(json_value={"status": 0, "result": {"score": 5}})
        ]
        self.refresh = refresh or Response(json_value={"status": 0, "result": {
            "access_token": "refreshed-access", "refresh_token": "refreshed-refresh"}})
        self.identity = list(identity) if isinstance(identity, list) else [
            identity or Response(json_value={"status": 0, "result": {"user_id": "user-1", "nickname": "Test"}})
        ]
        out = io.BytesIO()
        Image.new("RGB", (16, 16), "white").save(out, format="JPEG")
        self.jpeg = out.getvalue()

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if "connect/qrconnect?" in url:
            return Response(text='<img src="/connect/qrcode/uuid-123">', content_type="text/html")
        if "connect/qrcode/" in url:
            return Response(content=self.jpeg, content_type="image/jpeg")
        if "long.open.weixin.qq.com" in url:
            return Response(text=f"window.wx_errcode={self.login_code}; window.wx_code='one-time-code';", content_type="application/javascript")
        raise AssertionError("unexpected URL")

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert kwargs["headers"].get("Cookie") == ""
        if url.endswith("/user/wechat_qr_login"):
            return Response(json_value={"status": 0, "result": {"has_login": True,
                "access_token": "opaque-access", "refresh_token": "opaque-refresh"}})
        if url.endswith("/user/refresh"):
            return self.refresh
        if url.endswith("/user/info"):
            return self.identity.pop(0) if len(self.identity) > 1 else self.identity[0]
        if url.endswith("/daily_login_score"):
            return self.checkin.pop(0) if len(self.checkin) > 1 else self.checkin[0]
        raise AssertionError("unexpected URL")


class ZhipuGatewayTests(unittest.TestCase):
    def test_signature_matches_desktop_algorithm(self):
        headers = zhipu.signed_headers({"access_token": "opaque", "device_id": "device-1"},
                                      milliseconds=1764200000123, nonce="nonce-1")
        digits = "1764200000123"
        stamp = digits[:-2] + str((sum(map(int, digits)) - int(digits[-2])) % 10) + digits[-1]
        self.assertEqual(headers["X-Timestamp"], stamp)
        self.assertEqual(headers["X-Sign"], hashlib.md5(
            f"{stamp}-nonce-1-{zhipu.SIGN_SECRET}".encode()).hexdigest())
        self.assertEqual(headers["Authorization"], "Bearer opaque")

    def test_qr_login_returns_png_data_and_separates_pending_and_authorized(self):
        http = FakeHttp(login_code=408)
        provider = zhipu.ZhipuProvider(http=http)
        login = provider.start_login()
        self.assertTrue(login["qr_image"].startswith("data:image/png;base64,"))
        self.assertTrue(login["auth_url"].startswith("https://open.weixin.qq.com/connect/qrconnect?"))
        self.assertEqual(provider.poll_login({"state": login["state"]}), {"status": "pending"})

        http.login_code = 405
        result = provider.poll_login({"state": login["state"]})
        self.assertEqual(result["status"], "authorized")
        self.assertEqual(result["external_id"], "user-1")
        self.assertEqual(result["credential"]["access_token"], "opaque-access")
        for _, _, kwargs in http.calls:
            if kwargs.get("headers"):
                self.assertEqual(kwargs["headers"].get("Cookie"), "")

    def test_qr_login_requires_phone_binding_and_does_not_echo_code(self):
        http = FakeHttp(login_code=405)
        provider = zhipu.ZhipuProvider(http=http)
        login = provider.start_login()
        http.request = lambda method, url, **kwargs: Response(json_value={"status": 0, "result": {"has_login": False}})
        with self.assertRaises(GatewayError) as caught:
            provider.poll_login({"state": login["state"]})
        self.assertEqual(caught.exception.code, "phone_binding_required")
        self.assertNotIn("one-time-code", str(caught.exception))

    def test_daily_checkin_requires_explicit_success_score(self):
        for response, expected in [
            (Response(400, json_value={"status": 10001}), "already"),
            (Response(json_value={"status": 0, "result": {"score": 5}}), "success"),
            (Response(json_value={"status": 200, "result": {"score": "5"}}), "success"),
            (Response(json_value={"status": 0, "data": {"score": 5}}), "success"),
            (Response(json_value={"status": 0, "result": {"score": 0}}), "failed"),
            (Response(json_value={"status": 0, "result": {"score": "NaN"}}), "failed"),
            (Response(json_value={"status": 7001, "result": {"score": 5}}), "failed"),
        ]:
            with self.subTest(expected=expected):
                provider = zhipu.ZhipuProvider(http=FakeHttp(checkin=response))
                self.assertEqual(provider.checkin({"access_token": "opaque"})["status"], expected)

    def test_unconfirmed_daily_checkin_reports_only_response_shape(self):
        response = Response(json_value={"status": 0, "result": {"point": 8, "private_value": "do-not-log"}})
        provider = zhipu.ZhipuProvider(http=FakeHttp(checkin=response))

        result = provider.checkin({"access_token": "opaque"})

        self.assertEqual(result["status"], "failed")
        self.assertIn("status=0", result["message"])
        self.assertIn("point,private_value", result["message"])
        self.assertNotIn("do-not-log", result["message"])

    def test_daily_checkin_refreshes_expired_token_once(self):
        http = FakeHttp(checkin=[
            Response(401, json_value={"status": 401}),
            Response(json_value={"status": 0, "result": {"score": 5}}),
        ])
        provider = zhipu.ZhipuProvider(http=http)
        credential = {"access_token": "stale", "refresh_token": "refresh", "device_id": "device-1"}

        result = provider.checkin(credential)

        self.assertEqual(result["status"], "success")
        self.assertEqual(credential["access_token"], "refreshed-access")
        self.assertEqual(credential["refresh_token"], "refreshed-refresh")
        refresh_calls = [call for call in http.calls if call[1].endswith("/user/refresh")]
        self.assertEqual(len(refresh_calls), 1)
        self.assertEqual(refresh_calls[0][2]["json"], {})
        self.assertEqual(refresh_calls[0][2]["headers"]["Authorization"], "Bearer refresh")

    def test_refresh_failure_stays_credential_expired(self):
        http = FakeHttp(
            checkin=Response(401, json_value={"status": 401}),
            refresh=Response(json_value={"status": 1, "result": {}}),
        )
        provider = zhipu.ZhipuProvider(http=http)

        with self.assertRaises(GatewayError) as caught:
            provider.checkin({"access_token": "stale", "refresh_token": "refresh"})

        self.assertEqual(caught.exception.code, "credential_expired")

    def test_identity_refreshes_expired_token_once(self):
        http = FakeHttp(identity=[
            Response(403, json_value={"status": 403}),
            Response(json_value={"status": 0, "result": {"user_id": "user-1", "nickname": "Test"}}),
        ])
        provider = zhipu.ZhipuProvider(http=http)
        credential = {"access_token": "stale", "refresh_token": "refresh", "device_id": "device-1"}

        result = provider._identity(credential)

        self.assertEqual(result["external_id"], "user-1")
        self.assertEqual(credential["access_token"], "refreshed-access")

    def test_cookie_import_decrypts_only_production_host_tokens(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder)
            (profile / "Local State").write_text(json.dumps({"os_crypt": {
                "encrypted_key": base64.b64encode(b"DPAPIkey").decode()}}), encoding="utf-8")
            network = profile / "Network"
            network.mkdir()
            db = network / "Cookies"
            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE meta (key TEXT, value INTEGER)")
                conn.execute("INSERT INTO meta VALUES ('version', 23)")
                conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, last_access_utc INTEGER)")
                conn.executemany("INSERT INTO cookies VALUES (?, ?, '', ?, ?)", [
                    (".chatglm.cn", "chatglm_token", b"v10" + b"a" * 12 + b"access", 2),
                    ("agentmore.chatglm.cn", "chatglm_refresh_token", b"v10" + b"b" * 12 + b"wrong", 3),
                    (".chatglm.cn", "chatglm_refresh_token", b"v10" + b"c" * 12 + b"refresh", 1),
                ])
                conn.commit()
            finally:
                conn.close()
            def decrypt(_key, nonce, _encrypted):
                return {b"a" * 12: b"access", b"c" * 12: b"refresh"}[nonce]
            with patch("modules.ai_checkin.vendor.qoder_cred._dpapi_unprotect", return_value=b"key"),                  patch("modules.ai_checkin.vendor.qoder_cred._decrypt_gcm", side_effect=decrypt):
                credential = zhipu._read_local_cookies(profile)
            self.assertEqual(credential["access_token"], "access")
            self.assertEqual(credential["refresh_token"], "refresh")

    def test_models_only_lists_main_chat_catalog_and_caches_verified_ids(self):
        response = Response(json_value={"status": 0, "result": {
            "agent_model": {"models": [{"selected_model": "unverified-agent"}]},
            "main_chat": {"models": [{"selected_model": "glm-5.3"},
                                      {"selected_model": "glm-5.3-flash"},
                                      {"selected_model": "glm-5.3"}, None, {"selected_model": ""}]}}})
        http = ChatHttp(response)
        credential = {"access_token": "desktop-secret"}
        self.assertEqual(zhipu.ZhipuProvider(http=http).models(credential),
                         [{"id": "glm-5.3"}, {"id": "glm-5.3-flash"}])
        self.assertEqual(credential["_zhipu_chat_models"]["ids"], ["glm-5.3", "glm-5.3-flash"])
        method, url, options = http.calls[0]
        self.assertEqual((method, url), ("GET", zhipu.ORIGIN + zhipu.MODEL_PATH))
        self.assertEqual(options["params"], {"tag": "available_models_v2"})
        self.assertEqual(options["headers"]["Cookie"], "")
        self.assertFalse(options["allow_redirects"])

    def test_models_rejects_missing_main_catalog_or_business_failure(self):
        for value in ({"status": 1, "result": {}}, {"status": 0, "result": {}},
                      {"status": 0, "result": {"main_chat": {"models": []}}}):
            with self.assertRaises(GatewayError):
                zhipu.ZhipuProvider(http=ChatHttp(Response(json_value=value))).models(chat_credential())

    def test_chat_converts_incremental_and_full_snapshots_without_repeating_text(self):
        init = {"status": "init", "parts": [], "last_error": {}, "meta_data": {"if_increase_push": False}}
        response = Response(content_type="text/event-stream; charset=utf-8", events=[
            b"data: HEARTBEAT", b"", init, chat_frame("O"), chat_frame("K"),
            chat_frame("OK", incremental=False, part_status="finish"),
            chat_frame("OK", status="finish", incremental=False),
        ])
        http = ChatHttp(response)
        payload = chat_payload(stream=False, n=1, stream_options={"include_usage": True})
        before = copy.deepcopy(payload)
        completion = collect_completion(zhipu.ZhipuProvider(http=http).chat(chat_credential(), payload),
                                        "zhipu/glm-5.3-flash")
        self.assertEqual(completion["choices"][0]["message"]["content"], "OK")
        self.assertEqual(completion["choices"][0]["finish_reason"], "stop")
        self.assertNotIn("usage", completion)
        self.assertEqual(payload, before)
        method, url, options = http.calls[0]
        self.assertEqual((method, url), ("POST", zhipu.ORIGIN + zhipu.CHAT_PATH))
        self.assertEqual(options["json"]["assistant_id"], zhipu.CHAT_ASSISTANT_ID)
        self.assertEqual(options["json"]["conversation_id"], "")
        self.assertEqual(options["json"]["messages"], [{"role": "user", "content": [{
            "type": "text", "text": "Reply exactly OK"}]}])
        self.assertEqual(options["json"]["meta_data"]["selected_model"], "glm-5.3-flash")
        self.assertFalse(options["json"]["meta_data"]["is_networking"])
        self.assertEqual(options["json"]["meta_data"]["chat_mode"], "")
        self.assertEqual(options["headers"]["Authorization"], "Bearer desktop-secret")
        self.assertEqual(options["headers"]["Cookie"], "")
        self.assertIn("X-Sign", options["headers"])
        self.assertFalse(options["allow_redirects"])
        self.assertTrue(options["stream"])
        self.assertTrue(response.closed)

    def test_chat_text_blocks_are_preserved_and_reasoning_parts_are_separate(self):
        think = chat_frame()
        think["parts"][0]["content"] = [{"type": "think", "think": "reason"}]
        response = Response(content_type="text/event-stream", events=[think, chat_frame("OK", key="part-2"),
            {"status": "finish", "parts": [], "meta_data": {"full": True}}])
        http = ChatHttp(response)
        payload = chat_payload(messages=[{"role": "user", "content": [
            {"type": "text", "text": "one"}, {"type": "text", "text": "two"}]}])
        result = collect_completion(zhipu.ZhipuProvider(http=http).chat(chat_credential(), payload), "m")
        message = result["choices"][0]["message"]
        self.assertEqual(message["content"], "OK")
        self.assertEqual(message["reasoning_content"], "reason")
        self.assertEqual(http.calls[0][2]["json"]["messages"][0]["content"][0]["text"], "one\ntwo")

    def test_chat_rejects_unverified_inputs_and_parameters_before_network(self):
        inputs = [
            chat_payload(messages=[{"role": "system", "content": "instruction"}]),
            chat_payload(messages=[{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]),
            chat_payload(messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "image"}}]}]),
            chat_payload(messages=[{"role": "user", "content": "hi", "tool_calls": []}]),
            chat_payload(messages=[{"role": "user", "content": " "}]),
            chat_payload(tools=[]), chat_payload(tool_choice="auto"), chat_payload(temperature=0.5),
            chat_payload(max_tokens=128), chat_payload(n=2), chat_payload(n=True),
            chat_payload(stream_options={"unknown": True}), chat_payload(stream_options=[]),
        ]
        for payload in inputs:
            http = ChatHttp()
            with self.assertRaises(GatewayError):
                list(zhipu.ZhipuProvider(http=http).chat(chat_credential(), payload))
            self.assertEqual(http.calls, [])

    def test_chat_rejects_model_outside_confirmed_main_catalog(self):
        http = ChatHttp()
        with self.assertRaises(GatewayError) as caught:
            list(zhipu.ZhipuProvider(http=http).chat(chat_credential(), chat_payload(model="agent-model")))
        self.assertEqual(caught.exception.code, "invalid_model")
        self.assertEqual(http.calls, [])

    def test_chat_errors_and_truncation_never_emit_successful_completion(self):
        bad_part = chat_frame("partial")
        bad_part["parts"][0]["status"] = "error"
        tool_part = chat_frame()
        tool_part["parts"][0]["content"] = [{"type": "tool_calls", "tool_calls": {"name": "hidden"}}]
        cases = [
            ([], "incomplete_stream"),
            ([chat_frame("partial")], "incomplete_stream"),
            ([chat_frame("partial", part_status="finish")], "incomplete_stream"),
            ([b"data: HEARTBEAT", b""], "incomplete_stream"),
            ([{"status": "error", "last_error": {"error_code": 10040, "err_msg": "secret-error"}}], "upstream_error"),
            ([{"status": "intervene"}], "upstream_error"),
            ([b"event: FatalError", b"data: secret-error", b""], "upstream_error"),
            ([bad_part], "upstream_error"), ([tool_part], "unsupported_upstream_content"),
            ([{"status": "unexpected"}], "invalid_stream"),
            ([b"data: not-json", b""], "invalid_stream"),
            ([b"data: []", b""], "invalid_stream"),
            ([b"data: \xff", b""], "invalid_stream"),
            ([chat_frame("original"), chat_frame("replacement", incremental=False)], "invalid_stream"),
            ([chat_frame(status="finish")], "empty_response"),
            ([chat_frame("partial"), requests.ConnectionError("secret-error")], "upstream_unavailable"),
        ]
        for events, code in cases:
            response = Response(content_type="text/event-stream", events=events)
            stream = zhipu.ZhipuProvider(http=ChatHttp(response)).chat(chat_credential(), chat_payload())
            with self.assertRaises(GatewayError) as caught:
                list(stream)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("secret-error", str(caught.exception))
            self.assertTrue(response.closed)

    def test_chat_closes_response_on_cancellation_and_non_sse(self):
        response = Response(content_type="text/event-stream", events=[chat_frame("partial")])
        stream = zhipu.ZhipuProvider(http=ChatHttp(response)).chat(chat_credential(), chat_payload())
        next(stream)
        stream.close()
        self.assertTrue(response.closed)
        response = Response(json_value={"message": "secret-error"})
        with self.assertRaises(GatewayError) as caught:
            list(zhipu.ZhipuProvider(http=ChatHttp(response)).chat(chat_credential(), chat_payload()))
        self.assertEqual(caught.exception.code, "invalid_stream")
        self.assertTrue(response.closed)

    def test_chat_auth_refreshes_once_before_any_stream_data(self):
        expired = Response(401)
        refresh = Response(json_value={"status": 0, "result": {
            "access_token": "new-access", "refresh_token": "new-refresh"}})
        response = Response(content_type="text/event-stream", events=[chat_frame("OK", status="finish")])
        http = ChatHttp(expired, refresh, response)
        credential = chat_credential()
        result = collect_completion(zhipu.ZhipuProvider(http=http).chat(credential, chat_payload()), "m")
        self.assertEqual(result["choices"][0]["message"]["content"], "OK")
        self.assertEqual(len(http.calls), 3)
        self.assertEqual(http.calls[1][2]["headers"]["Authorization"], "Bearer refresh-secret")
        self.assertEqual(http.calls[2][2]["headers"]["Authorization"], "Bearer new-access")
        self.assertEqual(credential["access_token"], "new-access")
        self.assertTrue(expired.closed)
        self.assertTrue(response.closed)

    def test_chat_http_errors_close_without_exposing_upstream_body(self):
        for status in (302, 400, 402, 404, 429, 500):
            response = Response(status, json_value={"message": "secret-error"})
            http = ChatHttp(response)
            with self.assertRaises(GatewayError) as caught:
                list(zhipu.ZhipuProvider(http=http).chat(chat_credential(), chat_payload()))
            self.assertEqual(caught.exception.code, "upstream_error")
            self.assertNotIn("secret-error", str(caught.exception))
            self.assertFalse(http.calls[0][2]["allow_redirects"])
            self.assertEqual(len(http.calls), 1)
            self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
