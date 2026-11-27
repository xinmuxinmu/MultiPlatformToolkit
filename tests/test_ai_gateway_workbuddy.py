import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from modules.ai_gateway.base import GatewayError
from modules.ai_gateway.providers.workbuddy import WorkBuddyProvider, WorkBuddyAIProvider


class WorkBuddyGatewayTests(unittest.TestCase):
    def provider(self, handler, cls=WorkBuddyProvider):
        transport = httpx.MockTransport(handler)
        return cls(client_factory=lambda **kwargs: httpx.Client(transport=transport, **kwargs))

    def test_device_oauth_is_per_session_and_keeps_regions(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path.endswith("/state"):
                self.assertEqual(request.url.params["platform"], "CLI")
                return httpx.Response(200, json={"code": 0, "data": {"state": "signed-state",
                    "authUrl": "https://www.workbuddy.ai/login?state=signed-state"}})
            if request.url.path.endswith("/token"):
                self.assertEqual(request.url.params["state"], "signed-state")
                return httpx.Response(200, json={"code": 0, "data": {"auth": {
                    "accessToken": "private-access", "refreshToken": "private-refresh", "expiresIn": 3600}}})
            return httpx.Response(200, json={"code": 0, "data": {"account": {"uid": "user-1", "nickname": "Example"}}})

        provider = self.provider(handler, WorkBuddyAIProvider)
        session = provider.start_login()
        result = provider.poll_login(session)
        self.assertEqual(result["status"], "authorized")
        self.assertEqual(result["external_id"], "user-1")
        self.assertEqual(result["label"], "Example")
        self.assertEqual(result["credential"]["realm"], "intl")
        self.assertTrue(all(r.url.host == "www.workbuddy.ai" for r in requests))

    def test_business_pending_is_not_success_even_on_http_error(self):
        provider = self.provider(lambda r: httpx.Response(400, json={"code": 11217}))
        result = provider.poll_login({"state": "state", "expires_at": time.time() + 60})
        self.assertEqual(result, {"status": "pending"})

    def test_business_error_never_imports_a_token_or_echoes_response(self):
        provider = self.provider(lambda r: httpx.Response(200, json={"code": 7000, "msg": "secret-token",
            "data": {"auth": {"accessToken": "secret-token"}}}))
        with self.assertRaises(GatewayError) as caught:
            provider.poll_login({"state": "state", "expires_at": time.time() + 60})
        self.assertNotIn("secret-token", str(caught.exception))

    def test_refresh_rotates_only_the_passed_credential(self):
        def handler(request):
            self.assertEqual(request.headers["X-Refresh-Token"], "old-refresh")
            self.assertEqual(request.headers["X-Auth-Refresh-Source"], "workbuddy")
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "new-access",
                "refreshToken": "new-refresh", "expiresIn": 3600}})
        provider = self.provider(handler)
        credential = {"realm": "cn", "access_token": "old-access", "refresh_token": "old-refresh"}
        self.assertIs(provider.refresh(credential), credential)
        self.assertEqual(credential["refresh_token"], "new-refresh")
        self.assertGreater(credential["expires_at"], time.time() + 3500)

    def test_model_list_reads_real_catalog_and_falls_back(self):
        paths = []
        def handler(request):
            paths.append(request.url.path)
            if request.url.path == "/v3/config":
                return httpx.Response(404, json={"code": 404})
            return httpx.Response(200, json={"code": 0, "data": {"models": [{"id": "model-real"}]}})
        provider = self.provider(handler)
        models = provider.models({"access_token": "access", "realm": "cn"})
        self.assertEqual(models[0]["id"], "model-real")
        self.assertEqual(paths, ["/v3/config", "/v2/enterprises/personal/models"])

    def test_stream_preserves_tools_reasoning_usage_and_excludes_refresh(self):
        frames = [
            {"choices": [{"index": 0, "delta": {"reasoning_content": "think", "tool_calls": [{
                "index": 0, "id": "call-1", "type": "function", "function": {"name": "weather", "arguments": "{"}}]},
                "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "} "}}]},
                "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}},
        ]
        original = {"model": "model-real", "stream": False, "max_completion_tokens": 100,
                    "messages": [{"role": "user", "content": "weather?"}]}
        def handler(request):
            self.assertNotIn("X-Refresh-Token", request.headers)
            outgoing = json.loads(request.content)
            self.assertTrue(outgoing["stream"])
            self.assertTrue(outgoing["stream_options"]["include_usage"])
            self.assertEqual(outgoing["messages"][0]["role"], "system")
            self.assertEqual(outgoing["max_tokens"], 100)
            stream = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"
            return httpx.Response(200, text=stream, headers={"Content-Type": "text/event-stream"})
        provider = self.provider(handler)
        actual = list(provider.chat({"access_token": "access", "refresh_token": "refresh", "uid": "user"}, original))
        self.assertEqual(actual, frames)
        self.assertFalse(original["stream"])
        self.assertEqual(original["messages"][0]["role"], "user")

    def test_stream_business_error_and_empty_done_fail(self):
        for stream in ("data: {\"error\":{\"message\":\"private\"}}\n\n", "data: [DONE]\n\n"):
            with self.subTest(stream=stream):
                provider = self.provider(lambda r: httpx.Response(200, text=stream))
                with self.assertRaises(GatewayError):
                    list(provider.chat({"access_token": "access"}, {"model": "m", "messages": [{"role": "user", "content": "hello"}]}))

    def test_local_import_decrypts_with_hidden_node_mode_and_no_http(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            auth_dir = root / "CodeBuddyExtension/Data/Public/auth"
            auth_dir.mkdir(parents=True)
            path = auth_dir / "workbuddy-desktop.info"
            encrypted = {"$wbEncrypted": 1, "envelope": "opaque"}
            path.write_text(json.dumps({"account": {"uid": "local-1", "nickname": "Local"},
                "auth": {"accessToken": encrypted, "refreshToken": encrypted, "domain": "www.workbuddy.cn"}}), encoding="utf-8")
            provider = self.provider(lambda r: self.fail("local import must not call HTTP"))
            seen = []
            def fake_run(args, **kwargs):
                seen.append(args[-1])
                self.assertEqual(kwargs["env"]["ELECTRON_RUN_AS_NODE"], "1")
                self.assertNotIn("NODE_OPTIONS", kwargs["env"])
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                self.assertTrue(str(args[1]).endswith("wb_helper.js"))
                return subprocess.CompletedProcess(args, 0, stdout=b"local-secret", stderr=b"")
            with (
                patch.dict(os.environ, {"LOCALAPPDATA": folder, "WB_REFERENCE_ACCOUNTS_DIR": str(root / "reference"), "NODE_OPTIONS": "unsafe"}),
                patch.object(Path, "home", return_value=root),
                patch.object(provider, "_runtime", return_value=root / "WorkBuddy.exe"),
                patch("modules.ai_gateway.providers.workbuddy.subprocess.run", side_effect=fake_run),
            ):
                accounts = provider.import_local()
            self.assertEqual(seen, ["auth.accessToken", "auth.refreshToken"])
            self.assertEqual(accounts[0]["external_id"], "local-1")
            self.assertEqual(accounts[0]["credential"]["refresh_token"], "local-secret")

    def test_reference_multi_account_import_keeps_realm_and_does_not_mutate_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reference = root / "reference"
            reference.mkdir()
            for realm in ("cn", "intl"):
                (reference / (realm + ".json")).write_text(json.dumps({"realm": realm, "uid": realm + "-user",
                    "accessToken": "access", "refreshToken": "refresh", "expiresAt": 123456}), encoding="utf-8")
            before = {p.name: p.read_bytes() for p in reference.iterdir()}
            with (
                patch.dict(os.environ, {"LOCALAPPDATA": folder, "WB_REFERENCE_ACCOUNTS_DIR": str(reference)}),
                patch.object(Path, "home", return_value=root),
            ):
                cn = self.provider(lambda r: self.fail("unexpected network")).import_local()
                intl = self.provider(lambda r: self.fail("unexpected network"), WorkBuddyAIProvider).import_local()
            self.assertEqual([x["external_id"] for x in cn], ["cn-user"])
            self.assertEqual([x["external_id"] for x in intl], ["intl-user"])
            self.assertEqual(before, {p.name: p.read_bytes() for p in reference.iterdir()})

    def test_already_signed_skips_claim_and_business_idempotency_is_success(self):
        paths = []
        def handler(request):
            paths.append(request.url.path)
            self.assertEqual(request.url.host, "www.workbuddy.cn")
            return httpx.Response(200, json={"code": 0, "data": {"today_checked_in": True}})
        provider = self.provider(handler)
        result = provider.checkin({"access_token": "access", "realm": "cn"})
        self.assertEqual(result["status"], "already")
        self.assertEqual(paths, ["/v2/billing/meter/checkin-activity-status"])
        replies = iter([httpx.Response(200, json={"code": 0, "data": {"today_checked_in": False}}),
                        httpx.Response(400, json={"code": 10001})])
        provider = self.provider(lambda r: next(replies))
        self.assertEqual(provider.checkin({"access_token": "access"})["status"], "already")

    def test_dry_and_international_checkin_have_no_network(self):
        provider = self.provider(lambda r: self.fail("unexpected network"))
        self.assertEqual(provider.checkin({}, dry=True)["status"], "skipped")
        self.assertEqual(provider.checkin({"realm": "intl"})["status"], "skipped")

    def test_inactive_and_unrecognized_checkin_state_never_claim(self):
        for state in ({"active": False}, {}):
            with self.subTest(state=state):
                paths = []
                def handler(request):
                    paths.append(request.url.path)
                    return httpx.Response(200, json={"code": 0, "data": state})
                provider = self.provider(handler)
                if state:
                    self.assertEqual(provider.checkin({"access_token": "access"})["status"], "skipped")
                else:
                    with self.assertRaises(GatewayError):
                        provider.checkin({"access_token": "access"})
                self.assertEqual(paths, ["/v2/billing/meter/checkin-activity-status"])


if __name__ == "__main__":
    unittest.main()
