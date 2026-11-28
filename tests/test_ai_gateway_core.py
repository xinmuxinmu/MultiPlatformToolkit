import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.ai_gateway.base import GatewayError
from modules.ai_gateway.core import Gateway, default_providers
from modules.ai_gateway.store import AccountStore


class FakeProvider:
    id = "fake"
    name = "Test provider"
    capabilities = dict(login=True, local_import=True, chat=True, checkin=True)

    def __init__(self):
        self.logins = []
        self.polls = []
        self.requests = []
        self.closed = 0
        self.frames = [
            {"id": "response-1", "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
        ]

    def start_login(self, options):
        state = str(len(self.logins))
        self.logins.append(state)
        return dict(auth_url="https://official.invalid/login?state=" + state,
                    verifier="private-verifier-" + state, state=state, expires_in=60)

    def poll_login(self, session):
        self.polls.append(session["state"])
        return {"status": "authorized", "external_id": "user-" + session["state"],
                "label": "User " + session["state"],
                "credential": {"access_token": "private-token-" + session["state"]}}

    def models(self, credential):
        if credential.get("token") == "broken":
            raise GatewayError("stale account")
        return ["text", {"id": "tools"}]

    def chat(self, credential, payload):
        self.requests.append(copy.deepcopy(payload))
        try:
            for frame in self.frames:
                if isinstance(frame, Exception):
                    raise frame
                yield copy.deepcopy(frame)
        finally:
            self.closed += 1


class LocalCredentialProvider:
    id = "zhipu"
    name = "智谱清言"
    capabilities = dict(login=True, local_import=True, chat=False, checkin=True)

    def __init__(self, *, local_identity="same", local_token="fresh", failure_code="credential_expired"):
        self.local_identity = local_identity
        self.local_token = local_token
        self.failure_code = failure_code
        self.import_calls = 0
        self.checkin_tokens = []

    def import_local(self):
        self.import_calls += 1
        return [{"external_id": self.local_identity,
                 "credential": {"access_token": self.local_token}}]

    def checkin(self, credential, dry=False):
        self.checkin_tokens.append(credential.get("access_token"))
        if credential.get("access_token") == "stale":
            raise GatewayError("expired", 401, self.failure_code)
        return {"status": "success", "message": "claimed"}


class MemoryStore:
    """A test store with public metadata and independent credential copies."""
    def __init__(self):
        self.rows = []
        self.credentials = {}
        self.writes = 0
        self.key = "test-private-api-key"

    def accounts(self, provider=None):
        return [copy.deepcopy(row) for row in self.rows if provider is None or row["provider"] == provider]

    def upsert(self, provider, credential, external_id, label=""):
        self.writes += 1
        row = next((row for row in self.rows if row["provider"] == provider and row["external_id"] == external_id), None)
        if row is None:
            row = dict(id=str(len(self.rows)), provider=provider, external_id=external_id, enabled=True, label=label)
            self.rows.append(row)
        self.credentials[row["id"]] = copy.deepcopy(credential)
        return copy.deepcopy(row)

    def credential(self, account_id):
        return copy.deepcopy(self.credentials[account_id])

    def save_credential(self, account_id, credential):
        self.credentials[account_id] = copy.deepcopy(credential)

    def api_key(self, rotate=False):
        return self.key


class LockedCredentialStore(MemoryStore):
    def credential(self, account_id):
        raise GatewayError("vault locked", 503, "vault_unavailable")


class PartiallyLockedCredentialStore(MemoryStore):
    def __init__(self, locked_id=None):
        super().__init__()
        self.locked_id = locked_id

    def credential(self, account_id):
        if account_id == self.locked_id:
            raise GatewayError("vault locked", 503, "vault_unavailable")
        return super().credential(account_id)


class LocalImportProvider:
    def __init__(self, provider_id, *, rows=None, error=None, chat=False):
        self.id = provider_id
        self.name = provider_id.upper()
        self.capabilities = dict(login=False, local_import=True, chat=chat, checkin=False)
        self.rows = rows or []
        self.error = error

    def import_local(self):
        if self.error:
            raise self.error
        return copy.deepcopy(self.rows)


@unittest.skipUnless(os.name == "nt", "real current-user DPAPI is Windows-only")
class AccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "accounts.json"
        self.store = AccountStore(self.path)

    def test_credentials_and_proxy_key_are_encrypted_on_disk(self):
        account = self.store.upsert("fake", {"token": "private-access-token", "refresh": "private-refresh"}, "1", "User")
        key = self.store.api_key()
        raw = self.path.read_text(encoding="utf8")
        for secret in ("private-access-token", "private-refresh", key):
            self.assertNotIn(secret, raw)
        self.assertEqual(self.store.credential(account["id"])["token"], "private-access-token")
        self.assertEqual(AccountStore(self.path).api_key(), key)
        self.assertNotIn("credential", account)
        self.assertNotIn("token", json.dumps(self.store.accounts()))

    def test_identity_dedup_and_provider_isolation(self):
        first = self.store.upsert("fake", {"token": "one"}, "id", "Before")
        second = self.store.upsert("fake", {"token": "two"}, "id", "After")
        third = self.store.upsert("other", {"token": "three"}, "id", "Other")
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(second["id"], third["id"])
        self.assertEqual(len(self.store.accounts()), 2)
        self.assertEqual(self.store.credential(first["id"])["token"], "two")

    def test_update_delete_and_stale_refresh_never_recreates_account(self):
        row = self.store.upsert("fake", {"token": "one"}, "id")
        result = self.store.update(row["id"], enabled=False, label="Renamed")
        self.assertFalse(result["enabled"])
        self.assertEqual(result["label"], "Renamed")
        self.store.delete(row["id"])
        self.store.save_credential(row["id"], {"token": "new"})
        self.assertEqual(self.store.accounts(), [])
        with self.assertRaises(GatewayError):
            self.store.credential(row["id"])

    def test_corrupt_ciphertext_fails_without_plaintext_fallback(self):
        row = self.store.upsert("fake", {"token": "private"}, "id")
        data = json.loads(self.path.read_text())
        data["accounts"][0]["credential"] = "invalid-base64"
        self.path.write_text(json.dumps(data))
        with self.assertRaises(GatewayError) as caught:
            self.store.credential(row["id"])
        self.assertEqual(caught.exception.code, "vault_unavailable")


class GatewayCoreTests(unittest.TestCase):
    def test_unimplemented_checkin_platforms_are_visible_without_gateway_capabilities(self):
        providers = default_providers()
        for provider_id in ("minimax", "dumate", "lobster"):
            with self.subTest(provider=provider_id):
                provider = providers[provider_id]
                for capability in ("chat", "login", "local_import", "checkin"):
                    self.assertFalse(provider.capabilities[capability])
                self.assertIn("AI 签到页", provider.capabilities["reason"])

    def setUp(self):
        self.store = MemoryStore()
        self.provider = FakeProvider()
        self.gateway = Gateway(store=self.store, providers={"fake": self.provider})

    def add_account(self, identity="one", provider="fake"):
        return self.store.upsert(provider, {"token": "private"}, identity, identity)

    def test_invalid_model_id_is_actionable_without_catalog_provider_or_account_access(self):
        invalid_ids = (None, 123, {}, "", "deepseek-v4.1-flash", "/text", "  /text",
                       "fake/", "fake/  ")
        with patch.object(self.gateway, "provider") as provider, \
                patch.object(self.gateway, "models") as models, \
                patch.object(self.gateway, "select_account") as account:
            for model in invalid_ids:
                with self.subTest(model=model):
                    with self.assertRaises(GatewayError) as caught:
                        list(self.gateway.chat({"model": model, "messages": [{"role": "user", "content": "Hi"}]}))
                    self.assertEqual(caught.exception.status_code, 400)
                    self.assertEqual(caught.exception.code, "invalid_model")
                    self.assertIn("复制完整 ID", str(caught.exception))
                    self.assertIn("workbuddy/default", str(caught.exception))
            provider.assert_not_called()
            models.assert_not_called()
            account.assert_not_called()

    def test_qualified_model_preserves_nested_upstream_model_name(self):
        self.add_account()
        chunks = list(self.gateway.chat({"model": "fake/vendor/text",
                                         "messages": [{"role": "user", "content": "Hi"}]}))
        self.assertEqual(self.provider.requests[0]["model"], "vendor/text")
        self.assertEqual(chunks[0]["model"], "fake/vendor/text")

    def test_bulk_local_import_continues_after_one_provider_fails(self):
        providers = {
            "ready": LocalImportProvider("ready", chat=True, rows=[
                {"credential": {"token": "secret"}, "external_id": "user-1", "label": "User 1"}]),
            "missing": LocalImportProvider("missing", error=GatewayError("client not found", 400)),
            "empty": LocalImportProvider("empty"),
        }
        providers["unsupported"] = type("NoLocalImport", (), {
            "id": "unsupported", "name": "Unsupported",
            "capabilities": dict(login=False, local_import=False, chat=True, checkin=False),
        })()
        gateway = Gateway(store=self.store, providers=providers)

        result = gateway.import_all_local()

        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["chat_providers"], ["ready", "unsupported"])
        by_provider = {row["provider"]: row for row in result["results"]}
        self.assertEqual(by_provider["ready"]["status"], "imported")
        self.assertEqual(by_provider["empty"]["status"], "empty")
        self.assertEqual(by_provider["missing"]["status"], "failed")
        self.assertEqual(by_provider["unsupported"]["status"], "unsupported")
        self.assertEqual(self.store.credential("0"), {"token": "secret"})

    def test_zhipu_expired_account_refreshes_only_matching_local_identity(self):
        provider = LocalCredentialProvider()
        gateway = Gateway(store=self.store, providers={"zhipu": provider})
        account = self.store.upsert("zhipu", {"access_token": "stale"}, "same", "Saved label")

        result = gateway.checkin("zhipu")

        self.assertEqual(result["status"], "success")
        self.assertEqual(provider.import_calls, 1)
        self.assertEqual(provider.checkin_tokens, ["stale", "fresh"])
        self.assertEqual(self.store.credential(account["id"]), {"access_token": "fresh"})
        saved = self.store.accounts("zhipu")[0]
        self.assertEqual(saved["id"], account["id"])
        self.assertEqual(saved["label"], "Saved label")

    def test_zhipu_expired_account_does_not_overwrite_mismatched_local_identity(self):
        provider = LocalCredentialProvider(local_identity="different")
        gateway = Gateway(store=self.store, providers={"zhipu": provider})
        account = self.store.upsert("zhipu", {"access_token": "stale"}, "same")

        result = gateway.checkin("zhipu")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(provider.checkin_tokens, ["stale"])
        self.assertEqual(self.store.credential(account["id"]), {"access_token": "stale"})

    def test_zhipu_does_not_import_local_credentials_for_other_failures_or_dry_runs(self):
        for dry, failure_code in ((False, "upstream_error"), (True, "credential_expired")):
            with self.subTest(dry=dry, failure_code=failure_code):
                store = MemoryStore()
                provider = LocalCredentialProvider(failure_code=failure_code)
                gateway = Gateway(store=store, providers={"zhipu": provider})
                store.upsert("zhipu", {"access_token": "stale"}, "same")

                gateway.checkin("zhipu", dry=dry)

                self.assertEqual(provider.import_calls, 0)
                self.assertEqual(provider.checkin_tokens, ["stale"])

    def test_workbuddy_recovers_locked_vault_only_for_the_matching_local_account(self):
        store = LockedCredentialStore()
        provider = LocalCredentialProvider()
        gateway = Gateway(store=store, providers={"workbuddy": provider})
        account = store.upsert("workbuddy", {"access_token": "unreadable"}, "same", "Saved label")

        result = gateway.checkin("workbuddy")

        self.assertEqual(result["status"], "success")
        self.assertEqual(provider.import_calls, 1)
        self.assertEqual(provider.checkin_tokens, ["fresh"])
        self.assertEqual(store.credentials[account["id"]], {"access_token": "fresh"})
        self.assertEqual(store.accounts("workbuddy")[0]["id"], account["id"])

    def test_workbuddy_does_not_use_a_local_credential_for_another_account(self):
        store = LockedCredentialStore()
        provider = LocalCredentialProvider(local_identity="different")
        gateway = Gateway(store=store, providers={"workbuddy": provider})
        account = store.upsert("workbuddy", {"access_token": "unreadable"}, "same")

        result = gateway.checkin("workbuddy")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(provider.import_calls, 1)
        self.assertEqual(provider.checkin_tokens, [])
        self.assertEqual(store.credentials[account["id"]], {"access_token": "unreadable"})

    def test_simultaneous_logins_keep_private_state_and_save_once(self):
        first = self.gateway.start_login("fake")
        second = self.gateway.start_login("fake")
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotIn("verifier", json.dumps(first))
        first_result = self.gateway.poll_login(first["id"])
        self.assertEqual(self.gateway.poll_login(first["id"]), first_result)
        self.gateway.poll_login(second["id"])
        self.assertEqual(self.store.writes, 2)
        self.assertEqual(self.provider.polls, ["0", "1"])
        self.assertEqual({row["external_id"] for row in self.store.accounts()}, {"user-0", "user-1"})
        self.assertNotIn("private-token", json.dumps(first_result))
        self.assertEqual(self.gateway.sessions[first["id"]]["private"], {})

    def test_login_uses_provider_qr_image_and_keeps_copyable_auth_url(self):
        import base64
        # The provider's QR is already rendered; encoding its image URL again
        # would produce a QR that points to the image instead of the login flow.
        png = b"\x89PNG\r\n\x1a\n" + b"verified-test-image"
        self.provider.start_login = lambda _options: {
            "auth_url": "https://official.invalid/login",
            "qr_image": "data:image/png;base64," + base64.b64encode(png).decode(),
            "expires_in": 60,
            "state": "private-state",
        }
        row = self.gateway.start_login("fake")
        self.assertEqual(row["auth_url"], "https://official.invalid/login")
        self.assertEqual(base64.b64decode(row["qr_data"].split(",", 1)[1]), png)
        self.assertNotIn("private-state", json.dumps(row))

    def test_login_rejects_malformed_provider_qr_image(self):
        self.provider.start_login = lambda _options: {
            "auth_url": "https://official.invalid/login",
            "qr_image": "data:image/png;base64,aW52YWxpZA==",
            "expires_in": 60,
        }
        with self.assertRaises(GatewayError) as caught:
            self.gateway.start_login("fake")
        self.assertEqual(caught.exception.code, "invalid_login_response")

    def test_models_returns_healthy_account_catalog_when_another_account_fails(self):
        self.store.upsert("fake", {"token": "broken"}, "stale", "Stale")
        self.store.upsert("fake", {"token": "healthy"}, "working", "Working")
        self.assertEqual({row["id"] for row in self.gateway.models()}, {"fake/text", "fake/tools"})

    def test_models_recovers_one_unreadable_account_from_matching_local_session(self):
        class RecoverProvider(LocalImportProvider):
            def __init__(self):
                super().__init__("recover", chat=True, rows=[
                    {"credential": {"token": "desktop"}, "external_id": "same"}])

            def models(self, credential):
                self.last_credential = credential
                return ["model"]

        store = PartiallyLockedCredentialStore()
        provider = RecoverProvider()
        gateway = Gateway(store=store, providers={"recover": provider})
        account = store.upsert("recover", {"token": "old"}, "same", "Saved")
        store.locked_id = account["id"]

        self.assertEqual([row["id"] for row in gateway.models()], ["recover/model"])
        self.assertEqual(provider.last_credential, {"token": "desktop"})

    def test_chat_recovers_unreadable_account_from_matching_local_session(self):
        class RecoverProvider(LocalImportProvider):
            def __init__(self):
                super().__init__("recover", chat=True, rows=[
                    {"credential": {"token": "desktop"}, "external_id": "same"}])
                self.last_credential = None

            def chat(self, credential, payload):
                self.last_credential = credential
                yield {"choices": [{"finish_reason": "stop"}]}

        store = PartiallyLockedCredentialStore()
        provider = RecoverProvider()
        gateway = Gateway(store=store, providers={"recover": provider})
        account = store.upsert("recover", {"token": "old"}, "same", "Saved")
        store.locked_id = account["id"]

        chunks = list(gateway.chat({"model": "recover/model",
                                    "messages": [{"role": "user", "content": "Hi"}]}))
        self.assertEqual(chunks[0]["model"], "recover/model")
        self.assertEqual(provider.last_credential, {"token": "desktop"})

    def test_expired_or_cancelled_login_never_polls(self):
        row = self.gateway.start_login("fake")
        self.gateway.sessions[row["id"]]["expires_at"] = 0
        self.assertEqual(self.gateway.poll_login(row["id"])["status"], "expired")
        self.gateway.cancel_login(row["id"])
        with self.assertRaises(GatewayError):
            self.gateway.poll_login(row["id"])
        self.assertEqual(self.provider.polls, [])

    def test_disabled_accounts_are_excluded_from_rotation(self):
        a = self.add_account("a"); self.add_account("disabled"); b = self.add_account("b")
        self.store.rows[1]["enabled"] = False
        self.assertEqual([self.gateway.select_account("fake")["id"] for _ in range(4)],
                         [a["id"], b["id"], a["id"], b["id"]])

    def test_explicit_cross_provider_or_disabled_account_is_rejected(self):
        other = self.add_account(provider="other")
        with self.assertRaises(GatewayError):
            self.gateway.select_account("fake", other["id"])
        self.store.rows[0]["provider"] = "fake"
        self.store.rows[0]["enabled"] = False
        with self.assertRaises(GatewayError):
            self.gateway.select_account("fake", other["id"])

    def test_missing_finish_reason_is_an_incomplete_stream_error(self):
        self.add_account()
        self.provider.frames = self.provider.frames[:1]
        with self.assertRaises(GatewayError) as caught:
            list(self.gateway.chat({"model": "fake/text", "messages": [{"role": "user", "content": "Hi"}]}))
        self.assertEqual(caught.exception.code, "incomplete_stream")
        self.assertEqual(self.provider.closed, 1)

    def test_empty_finish_reason_does_not_hide_an_interrupted_desktop_stream(self):
        self.add_account()
        self.provider.frames = [{"choices": [{"index": 0, "delta": {"content": "partial"},
                                              "finish_reason": ""}]}]
        with self.assertRaises(GatewayError) as caught:
            list(self.gateway.chat({"model": "fake/text",
                                    "messages": [{"role": "user", "content": "Hi"}]}))
        self.assertEqual(caught.exception.code, "incomplete_stream")
        self.assertEqual(self.provider.closed, 1)

    def test_generator_can_advance_and_release_lock_on_distinct_workers(self):
        row = self.add_account()
        chunks = self.gateway.chat({"model": "fake/text", "messages": [{"role": "user", "content": "Hi"}]})
        first_done = threading.Event()
        proceed = threading.Event()
        def first_worker():
            value = next(chunks)
            first_done.set()
            proceed.wait(timeout=3)
            return value, threading.get_ident()
        def second_worker():
            self.assertTrue(first_done.wait(timeout=3))
            remaining = list(chunks)
            proceed.set()
            return remaining, threading.get_ident()
        with ThreadPoolExecutor(max_workers=2) as workers:
            one = workers.submit(first_worker); two = workers.submit(second_worker)
            first, tid_one = one.result(timeout=5)
            rest, tid_two = two.result(timeout=5)
        self.assertNotEqual(tid_one, tid_two)
        self.assertEqual(first["model"], "fake/text")
        self.assertEqual(len(rest), 2)
        lock = self.gateway.account_lock(row["id"])
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()


if __name__ == "__main__":
    unittest.main()
