import base64
import json
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.ai_checkin.vendor.platforms import qwenwork as qwen
from modules.ai_gateway.base import GatewayError
from modules.ai_gateway.providers.qwen import QwenProvider


def credential():
    return {"schemaVersion": 2, "token": "local-test-token", "refreshToken": "refresh",
            "expiresAt": int(time.time() + 3600), "user": {"id": "test-user", "name": "test"}}


class QwenCheckinTests(unittest.TestCase):
    def test_existing_checkin_does_not_claim_again(self):
        with patch.object(qwen, "request_json", return_value={"data": {"status": "CLAIMED_TODAY"}}) as request:
            result = qwen.QwenWorkCheckin(account=credential()).run()
        self.assertEqual(result["status"], "already")
        self.assertEqual(request.call_count, 1)

    def test_404_is_unknown_skipped_and_never_claimed(self):
        error = urllib.error.HTTPError(qwen.API_ORIGIN + qwen.STATUS_PATH, 404, "missing", {}, None)
        with patch.object(qwen, "request_json", side_effect=error) as request:
            result = qwen.QwenWorkCheckin(account=credential()).run()
        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["success"])
        self.assertEqual(request.call_count, 1)

    def test_claim_requires_explicit_success(self):
        for response, expected in [({"code": 0}, "failed"), ({"data": {"result": "CLAIMED", "rewardCredits": 100}}, "success"),
                                   ({"result": "ALREADY_CLAIMED"}, "already")]:
            with self.subTest(expected=expected), patch.object(qwen, "request_json", side_effect=[{"status": "CLAIMABLE"}, response]):
                result = qwen.QwenWorkCheckin(account=credential()).run()
                self.assertEqual(result["status"], expected)

    def test_unknown_status_never_posts_claim(self):
        with patch.object(qwen, "request_json", return_value={"code": 0}) as request:
            result = qwen.QwenWorkCheckin(account=credential()).run()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(request.call_count, 1)

    def test_dry_run_never_uses_http(self):
        with patch.object(qwen, "request_json") as request:
            result = qwen.QwenWorkCheckin(dry=True, account=credential()).run()
        request.assert_not_called()
        self.assertEqual(result["status"], "skipped")

    def test_expired_session_never_uses_http(self):
        data = credential(); data["expiresAt"] = int(time.time() - 1)
        with patch.object(qwen, "request_json") as request:
            result = qwen.QwenWorkCheckin(account=data).run()
        request.assert_not_called()
        self.assertEqual(result["status"], "failed")

    def test_plaintext_local_import_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / "auth-v2.dat.json"
            content = json.dumps(credential()); target.write_text(content, encoding="utf8")
            self.assertEqual(qwen.read_local_session(root)["user"]["id"], "test-user")
            self.assertEqual(target.read_text(encoding="utf8"), content)

    def test_local_session_from_other_deployment_rejected(self):
        data = credential(); data["endpoints"] = {"apiOrigin": "https://other.invalid"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "auth-v2.dat.json").write_text(json.dumps(data), encoding="utf8")
            with self.assertRaises(RuntimeError):
                qwen.read_local_session(root)

    def test_encrypted_local_session_is_decrypted_before_use(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = b"v10" + b"n" * 12 + b"c" * 32
            (root / "auth-v2.dat").write_bytes(raw)
            (root / "Local State").write_text(json.dumps({"os_crypt": {"encrypted_key":
                base64.b64encode(b"DPAPIwrapped").decode()}}), encoding="utf8")
            with patch.object(qwen._crypto, "_dpapi_unprotect", return_value=b"key") as unprotect, \
                    patch.object(qwen._crypto, "_decrypt_gcm", return_value=json.dumps(credential()).encode()) as decrypt:
                result = qwen.read_local_session(root)
            self.assertEqual(result["token"], "local-test-token")
            unprotect.assert_called_once_with(b"wrapped")
            decrypt.assert_called_once_with(b"key", b"n" * 12, b"c" * 32)
            self.assertEqual((root / "auth-v2.dat").read_bytes(), raw)


class QwenLoginTests(unittest.TestCase):
    def setUp(self):
        self.provider = QwenProvider()
        self.session = self.provider.start_login()

    def test_authorization_link_uses_production_device_flow(self):
        url = urllib.parse.urlparse(self.session["auth_url"])
        self.assertEqual(url.netloc, "gateway.qwenwork.cn")
        self.assertEqual(url.path, "/device/selectAccounts")
        query = urllib.parse.parse_qs(url.query)
        self.assertEqual(query["challenge_method"], ["S256"])
        self.assertEqual(query["client_id"], [qwen.CLIENT_ID])
        self.assertNotIn("verifier", query)

    def test_missing_poll_record_is_pending(self):
        error = urllib.error.HTTPError("url", 404, "pending", {}, None)
        with patch.object(qwen, "request_json", side_effect=error):
            self.assertEqual(self.provider.poll_login(self.session)["status"], "pending")

    def test_success_requires_matching_pkce_nonce(self):
        response = {"token": "test", "refresh_token": "test", "expires_in": 3600,
                    "user_id": "account-2", "nonce": "wrong",
                    "code_challenge": self.session["challenge"], "code_challenge_method": "S256"}
        with patch.object(qwen, "request_json", return_value=response):
            with self.assertRaises(GatewayError):
                self.provider.poll_login(self.session)
        response["nonce"] = self.session["nonce"]
        with patch.object(qwen, "request_json", return_value=response):
            result = self.provider.poll_login(self.session)
        self.assertEqual(result["status"], "authorized")
        self.assertEqual(result["external_id"], "account-2")

    def test_expired_login_does_not_poll(self):
        self.session["created_at"] = time.time() - 301
        with patch.object(qwen, "request_json") as request:
            self.assertEqual(self.provider.poll_login(self.session)["status"], "expired")
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
