import base64
import json
import sys
import unittest

import requests
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from modules.ai_gateway.base import GatewayError
from modules.ai_gateway.core import Gateway
from modules.ai_gateway.providers.trae import CHAT_ORIGIN, CHAT_PATH, TraeProvider


class HttpSpy:
    def __init__(self):
        self.get_calls = []
        self.post_calls = []

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        raise AssertionError("unverified Trae model endpoint must not be called")

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        raise AssertionError("unverified Trae chat endpoint must not be called")


class StoreSpy:
    def accounts(self, provider=None):
        rows = [{"id": "trae-account", "provider": "trae",
                 "enabled": True, "external_id": "user-1"}]
        return rows if provider in (None, "trae") else []

    def credential(self, account_id):
        return dict(TraeChatTests.credential)


class TraeGatewayTests(unittest.TestCase):
    def test_public_models_exposes_names_without_signed_model_params(self):
        response = Response(body={"config_info_list": [catalog_row()]})
        provider = TraeProvider(HttpQueue(response))
        self.assertEqual(provider.models(TraeChatTests.credential), [{"id": "Seed"}])
        self.assertTrue(response.closed)

    def test_verified_chat_provider_is_exposed_by_gateway(self):
        http = HttpQueue(Response(body={"config_info_list": [catalog_row()]}))
        provider = TraeProvider(http)
        gateway = Gateway(store=StoreSpy(), providers={"trae": provider})
        self.assertTrue(provider.capabilities["chat"])
        self.assertEqual([row["id"] for row in gateway.models()], ["trae/Seed"])

    def test_local_import_uses_desktop_login_state(self):
        payload = base64.urlsafe_b64encode(json.dumps({"user_id": "user-1"}).encode()).decode().rstrip("=")

        class Adapter:
            def __init__(self, dry=False):
                self.token = "header." + payload + ".signature"
                self.device_id = "123"
                self.machine_id = "machine-a"
                self.user_id = "user-1"
                self.host = "https://trae-api-cn.mchost.guru"

            def _load_credential(self):
                return None

        with patch("modules.ai_gateway.providers.trae._checkin_adapter", return_value=Adapter):
            imported = TraeProvider(HttpSpy()).import_local()

        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0]["external_id"], "user-1")
        self.assertEqual(imported[0]["credential"]["device_id"], "123")
        self.assertEqual(imported[0]["credential"]["machine_id"], "machine-a")
        self.assertEqual(imported[0]["credential"]["api_host"], "https://trae-api-cn.mchost.guru")

    def test_local_import_failure_does_not_expose_adapter_error_or_credentials(self):
        class Adapter:
            def __init__(self, dry=False):
                pass

            def _load_credential(self):
                raise ValueError("private-token")

        with patch("modules.ai_gateway.providers.trae._checkin_adapter", return_value=Adapter):
            with self.assertRaises(GatewayError) as caught:
                TraeProvider(HttpSpy()).import_local()

        self.assertEqual(caught.exception.code, "local_credential_unavailable")
        self.assertNotIn("private-token", str(caught.exception))

    def test_local_import_rejects_missing_token(self):
        class Adapter:
            token = ""

            def __init__(self, dry=False):
                pass

            def _load_credential(self):
                pass

        with patch("modules.ai_gateway.providers.trae._checkin_adapter", return_value=Adapter):
            with self.assertRaises(GatewayError) as caught:
                TraeProvider(HttpSpy()).import_local()

        self.assertEqual(caught.exception.code, "invalid_credential")

    def test_local_import_rejects_missing_account_identity(self):
        with patch.object(TraeProvider, "_credential_from_desktop",
                          return_value={"access_token": "opaque-token"}):
            with self.assertRaises(GatewayError) as caught:
                TraeProvider(HttpSpy()).import_local()

        self.assertEqual(caught.exception.code, "invalid_identity")


def catalog_row(name="Seed", **changes):
    row = {"config_name": name, "config_source": 1, "config_switch": True,
           "is_invisible_to_user": False,
           "display_config": {"is_custom_model": False, "model_capability": "chat_model"},
           "model_detail_list": [{"model_name": name + "__dev",
                                  "encrypted_model_params": "opaque-private-signature"}]}
    row.update(changes)
    return row


class Response:
    def __init__(self, body=None, events=(), status=200, content_type="text/event-stream", tail=()):
        self.body = body
        self.events = events
        self.status_code = status
        self.headers = {"content-type": content_type}
        self.closed = False
        self.tail = tail

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    def iter_lines(self):
        for event, value in self.events:
            yield ("event: " + event).encode()
            yield ("data: " + json.dumps(value)).encode()
            yield b""
        yield from self.tail

    def close(self):
        self.closed = True


class HttpQueue:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected request")
        return self.responses.pop(0)


class TraeChatTests(unittest.TestCase):
    credential = {"access_token": "private-token", "device_id": "device-a",
                  "machine_id": "machine-a",
                  "user_id": "user-a", "refresh_token": "private-refresh",
                  "api_host": "https://untrusted.invalid"}
    payload = {"model": "Seed", "messages": [{"role": "user", "content": "hi"}]}

    def adapter(self, events=(), **kwargs):
        catalog = Response(body={"config_info_list": [catalog_row()]})
        stream = Response(events=events, **kwargs)
        http = HttpQueue(catalog, stream)
        return TraeProvider(http), http, catalog, stream

    def test_verified_named_error_4011_wins_over_later_done_stop(self):
        provider, http, catalog, stream = self.adapter([
            ("error", {"code": 4011, "message": "private-token private-refresh", "extra": {}}),
            ("done", {"finish_reason": "stop"})])
        with self.assertRaises(GatewayError) as caught:
            list(provider.chat(self.credential, self.payload))
        self.assertEqual((caught.exception.status_code, caught.exception.code), (429, "account_limited"))
        self.assertNotIn("private-token", str(caught.exception))
        self.assertNotIn("private-refresh", str(caught.exception))
        self.assertTrue(catalog.closed and stream.closed)

    def test_success_fixture_matches_live_incremental_schema(self):
        # Synthetic values following the observed v2 event schema. The actual
        # live sample returned 'OK' plus incremental reasoning and done(stop).
        provider, http, catalog, stream = self.adapter([
            ("metadata", {"model": "unverified-alias", "session_id": "private-session"}),
            ("timing_cost", {"name": "chat", "preprocess_timing": 1}),
            ("output", {"response": "", "reasoning_content": "Think",
                         "tool_calls": [], "multimodal_contents": [], "phase": None}),
            ("output", {"response": "", "reasoning_content": " twice",
                         "tool_calls": None, "multimodal_contents": None, "phase": 1}),
            ("output", {"response": "O", "reasoning_content": None}),
            ("output", {"response": "K", "reasoning_content": None}),
            ("extra_info", {"content": "opaque-private-signature", "input_token": 10}),
            ("token_usage", {"name": "chat", "prompt_tokens": 10,
                              "completion_tokens": 4, "total_tokens": 14,
                              "reasoning_tokens": 2, "prompt_tokens_total": 10}),
            ("done", {"finish_reason": "stop"})])
        chunks = list(provider.chat(self.credential, self.payload))
        self.assertEqual("".join(c["choices"][0]["delta"].get("content", "") for c in chunks), "OK")
        self.assertEqual("".join(c["choices"][0]["delta"].get("reasoning_content", "") for c in chunks), "Think twice")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertTrue(provider.capabilities["chat"])
        self.assertEqual(chunks[0]["choices"][0]["delta"]["role"], "assistant")
        self.assertEqual(chunks[-1]["usage"], {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14})
        for secret in ("opaque-private-signature", "private-session", "private-token", "machine-a"):
            self.assertNotIn(secret, json.dumps(chunks))
        self.assertTrue(catalog.closed and stream.closed)
        body = http.calls[1][1]["json"]
        self.assertEqual(body["session_id"], body["conversation_id"])
        self.assertRegex(body["session_id"], r"^[a-f0-9-]{36}$")
        self.assertEqual(body, {"config_name": "Seed", "model_name": "Seed__dev",
            "config_source": 1, "stream": True, "session_id": body["session_id"],
            "conversation_id": body["session_id"], "encrypted_model_params": "opaque-private-signature",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]})
        self.assertEqual(http.calls[1][0], CHAT_ORIGIN + CHAT_PATH)
        for url, options in http.calls:
            self.assertTrue(url.startswith(CHAT_ORIGIN + "/"))
            self.assertFalse(options["allow_redirects"])
            self.assertEqual(options["headers"]["Cookie"], "")
            self.assertEqual(options["headers"]["Authorization"], "Cloud-IDE-JWT private-token")
            self.assertEqual(options["headers"]["X-Ide-Token"], "private-token")
            self.assertEqual(options["headers"]["X-App-Version-Code"], "20261110")
            self.assertEqual(options["headers"]["X-IDE-Version-Code"], "20261110")
            self.assertEqual(options["headers"]["x-app-version"], "3.4.20")
            self.assertEqual(options["headers"]["x-machine-id"], "machine-a")
            self.assertEqual(options["headers"]["User-Agent"], "TraeClient/TTNet")
            self.assertEqual(options["headers"]["X-App-Function"], "chat_v3")
            self.assertEqual(options["headers"]["X-Ide-Function"], "chat_v3")
            self.assertNotIn("private-refresh", json.dumps(options))
            self.assertEqual(options["headers"]["X-Request-ID"], options["headers"]["X-Trae-Request-ID"])

    def test_no_output_eof_unknown_and_invalid_streams_fail_closed(self):
        fixtures = [[], [("done", {"finish_reason": "stop"})],
            [("output", {"response": "OK"})],
            [("output", {"response": ""}), ("done", {"finish_reason": "stop"})],
            [("output", {"response": "  "}), ("done", {"finish_reason": "stop"})],
            [("output", {"reasoning_content": "thinking"}), ("done", {"finish_reason": "stop"})],
            [("mystery", {"response": "OK"})],
            [("output", {"response": None})],
            [("output", {"response": 4})],
            [("output", {"response": "OK", "reasoning_content": {}})],
            [("output", {"response": "OK"}), ("done", {"finish_reason": "error"})],
            [("output", {"unknown_content": "OK"})]]
        for events in fixtures:
            with self.subTest(events=events):
                provider, _, catalog, stream = self.adapter(events)
                with self.assertRaises(GatewayError) as caught:
                    list(provider.chat(self.credential, self.payload))
                self.assertEqual(caught.exception.code, "invalid_stream")
                self.assertTrue(catalog.closed and stream.closed)

    def test_partial_malformed_and_non_sse_responses_fail_and_close(self):
        for options in [{"tail": [b"event: output", b"data: {}"]},
                        {"tail": [b"event: output", b"data: not-json", b""]},
                        {"tail": [b"event: output", b"data: \xff", b""]},
                        {"content_type": "application/json"}, {"status": 302}]:
            with self.subTest(options=options):
                provider, _, catalog, stream = self.adapter(**options)
                with self.assertRaises(GatewayError):
                    list(provider.chat(self.credential, self.payload))
                self.assertTrue(catalog.closed and stream.closed)

    def test_response_closes_on_consumer_cancellation(self):
        provider, _, catalog, stream = self.adapter([("output", {"response": "O"}),
                                                    ("output", {"response": "OK"})])
        iterator = provider.chat(self.credential, self.payload)
        next(iterator)
        iterator.close()
        self.assertTrue(catalog.closed and stream.closed)

    def test_response_closes_on_transport_failure_and_tools_are_rejected(self):
        provider, _, catalog, stream = self.adapter([("output", {"response": "", "tool_calls": [{"name": "unverified"}]})])
        with self.assertRaises(GatewayError) as caught:
            list(provider.chat(self.credential, self.payload))
        self.assertEqual(caught.exception.code, "unsupported_response")
        self.assertTrue(catalog.closed and stream.closed)

        def interrupted():
            raise requests.ConnectionError("private-token")
            yield b""

        provider, _, catalog, stream = self.adapter()
        stream.iter_lines = interrupted
        with self.assertRaises(GatewayError) as caught:
            list(provider.chat(self.credential, self.payload))
        self.assertEqual(caught.exception.code, "upstream_unavailable")
        self.assertNotIn("private-token", str(caught.exception))
        self.assertTrue(catalog.closed and stream.closed)

    def test_catalog_filters_hidden_disabled_custom_and_ambiguous_models(self):
        rows = [catalog_row(), catalog_row("hidden", is_invisible_to_user=True),
                catalog_row("disabled", config_switch=False), catalog_row("custom", config_source=3),
                catalog_row("placeholder", display_config={"is_custom_model": True, "model_capability": "chat_model"}),
                catalog_row("image", display_config={"is_custom_model": False, "model_capability": "image_model"}),
                catalog_row("ambiguous", model_detail_list=[{"model_name": "a"}, {"model_name": "b"}]),
                catalog_row("unsigned", model_detail_list=[{"model_name": "unsigned__dev"}]),
                catalog_row("empty_signature", model_detail_list=[{"model_name": "unsigned__dev",
                                                                 "encrypted_model_params": " "}])]
        response = Response(body={"config_info_list": rows})
        provider = TraeProvider(HttpQueue(response))
        self.assertEqual(provider._fetch_models(self.credential), [{"id": "Seed", "model_name": "Seed__dev",
                                                                  "encrypted_model_params": "opaque-private-signature"}])
        self.assertTrue(response.closed)

    def test_catalog_invalid_auth_and_errors_close_without_revealing_data(self):
        for response in [Response(body={}), Response(body=[]), Response(body=ValueError("private-token")),
                         Response(body={"code": 4011}), Response(status=401), Response(status=429)]:
            with self.subTest(response=response):
                with self.assertRaises(GatewayError) as caught:
                    TraeProvider(HttpQueue(response))._fetch_models(self.credential)
                self.assertNotIn("private-token", str(caught.exception))
                self.assertTrue(response.closed)

    def test_unknown_model_does_not_send_chat(self):
        provider, http, _, stream = self.adapter()
        with self.assertRaises(GatewayError) as caught:
            list(provider.chat(self.credential, dict(self.payload, model="unknown")))
        self.assertEqual(caught.exception.code, "invalid_model")
        self.assertEqual(len(http.calls), 1)
        self.assertFalse(stream.closed)

    def test_unverified_parameters_and_non_text_messages_reject_before_network(self):
        changes = [{"n": 2}, {"n": True}, {"n": 1.0}, {"stream": "yes"}, {"tools": []},
                   {"temperature": 0.1}, {"messages": []},
                   {"messages": [{"role": "tool", "content": "hi"}]},
                   {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]},
                   {"messages": [{"role": "assistant", "content": "hi"}]},
                   {"messages": [{"role": "system", "content": "hi"}]},
                   {"messages": [{"role": "user", "content": "hi"},
                                 {"role": "user", "content": "again"}]},
                   {"messages": [{"role": "user", "content": "hi", "name": "extra"}]},
                   {"messages": [{"role": "user", "content": " "}]},
                   {"encrypted_model_params": "client-override"}]
        for change in changes:
            with self.subTest(change=change):
                http = HttpQueue()
                with self.assertRaises(GatewayError):
                    list(TraeProvider(http).chat(self.credential, dict(self.payload, **change)))
                self.assertEqual(http.calls, [])

    def test_repeated_deltas_are_appended_without_deduplication(self):
        provider, _, _, stream = self.adapter([
            ("output", {"response": "ha", "reasoning_content": "."}),
            ("output", {"response": "ha", "reasoning_content": "."}),
            ("output", {"response": "!", "reasoning_content": None}),
            ("done", {"finish_reason": "stop"})])
        chunks = list(provider.chat(self.credential, self.payload))
        self.assertEqual("".join(c["choices"][0]["delta"].get("content", "") for c in chunks), "haha!")
        self.assertEqual("".join(c["choices"][0]["delta"].get("reasoning_content", "") for c in chunks), "..")
        self.assertTrue(stream.closed)

    def test_leading_control_events_do_not_mask_initial_upstream_error(self):
        for code, expected in [(4011, "account_limited"), (4007, "rate_limited"), (9999, "upstream_error")]:
            with self.subTest(code=code):
                provider, _, _, stream = self.adapter([
                    ("metadata", {"model": "private-model-alias"}),
                    ("timing_cost", {"name": "chat"}),
                    ("error", {"code": code, "message": "opaque-private-signature"}),
                    ("done", {"finish_reason": "stop"})])
                iterator = provider.chat(self.credential, self.payload)
                with self.assertRaises(GatewayError) as caught:
                    next(iterator)
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn("opaque-private-signature", str(caught.exception))
                self.assertTrue(stream.closed)

    def test_missing_or_unsafe_desktop_context_rejects_before_network(self):
        for field, value in [("machine_id", None), ("device_id", ""),
                             ("access_token", "bad\r\nheader"),
                             ("machine_id", "bad\nheader"), ("user_id", "bad\rheader")]:
            with self.subTest(field=field):
                http = HttpQueue()
                credential = dict(self.credential, **{field: value})
                with self.assertRaises(GatewayError) as caught:
                    list(TraeProvider(http).chat(credential, self.payload))
                self.assertIn(caught.exception.code, ("local_context_required", "invalid_credential"))
                self.assertEqual(http.calls, [])

    def test_multimodal_or_tool_content_is_never_silently_discarded(self):
        for field, value in [("multimodal_contents", [{"type": "image"}]),
                             ("tool_calls", [{"function": {"name": "tool"}}])]:
            with self.subTest(field=field):
                provider, _, _, stream = self.adapter([
                    ("output", {"response": "OK", field: value}),
                    ("done", {"finish_reason": "stop"})])
                with self.assertRaises(GatewayError) as caught:
                    list(provider.chat(self.credential, self.payload))
                self.assertEqual(caught.exception.code, "unsupported_response")
                self.assertTrue(stream.closed)

    def test_invalid_usage_events_fail_and_close(self):
        for bad in (None, True, -1, 1.5, "10"):
            with self.subTest(bad=bad):
                provider, _, _, stream = self.adapter([
                    ("output", {"response": "OK"}),
                    ("token_usage", {"prompt_tokens": bad, "completion_tokens": 4, "total_tokens": 14}),
                    ("done", {"finish_reason": "stop"})])
                with self.assertRaises(GatewayError) as caught:
                    list(provider.chat(self.credential, self.payload))
                self.assertEqual(caught.exception.code, "invalid_stream")
                self.assertTrue(stream.closed)

    def test_private_signatures_are_refetched_and_scoped_to_current_account(self):
        def signed_row(signature):
            return catalog_row(model_detail_list=[{"model_name": "Seed__dev",
                                                   "encrypted_model_params": signature}])
        signatures = ["opaque-first\nexact", "opaque-second"]
        responses = []
        for signature in signatures:
            responses.extend([Response(body={"config_info_list": [signed_row(signature)]}),
                              Response(events=[("output", {"response": "OK"}),
                                               ("done", {"finish_reason": "stop"})])])
        http = HttpQueue(*responses)
        provider = TraeProvider(http)
        for index, signature in enumerate(signatures):
            credential = dict(self.credential, access_token="account-token-" + str(index))
            chunks = list(provider.chat(credential, self.payload))
            self.assertEqual(http.calls[index * 2 + 1][1]["json"]["encrypted_model_params"], signature)
            self.assertEqual(http.calls[index * 2 + 1][1]["headers"]["X-Ide-Token"], credential["access_token"])
            self.assertNotIn(signature, json.dumps(chunks))
            self.assertNotIn("encrypted_model_params", credential)
        self.assertEqual(len(http.calls), 4)

    def test_multi_data_line_event_size_is_bounded(self):
        # Each line fits the single-line limit, but their aggregate must not.
        provider, _, _, stream = self.adapter(tail=[b"event: output"] +
            [b"data: " + b" " + b"x" * (900 * 1024)] * 5 + [b""])
        with self.assertRaises(GatewayError) as caught:
            list(provider.chat(self.credential, self.payload))
        self.assertEqual(caught.exception.code, "invalid_stream")
        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
