import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


VENDOR_DIR = Path(__file__).resolve().parents[1] / "src" / "modules" / "ai_checkin" / "vendor"
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

from platforms.workbuddy import WorkBuddyCheckin  # noqa: E402


_TOKEN = "h" * 20 + "." + "p" * 20 + "." + "s" * 20


class WorkBuddyCredentialTests(unittest.TestCase):
    def _write_auth(self, root: str, access_token) -> str:
        path = os.path.join(root, "workbuddy-desktop.info")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({
                "auth": {"accessToken": access_token, "domain": "www.workbuddy.cn"},
                "account": {"uid": "test-user"},
            }, stream)
        return path

    def test_plaintext_desktop_token_loads_without_network(self):
        with tempfile.TemporaryDirectory() as root:
            auth_path = self._write_auth(root, _TOKEN)
            with patch.object(WorkBuddyCheckin, "AUTH_FILE_CANDIDATES", [auth_path]):
                checkin = WorkBuddyCheckin(dry=True)
            with patch("urllib.request.urlopen", side_effect=AssertionError("network call")):
                token, domain, uid, _auth = checkin._load_auth()

        self.assertEqual(token, _TOKEN)
        self.assertEqual(domain, "www.workbuddy.cn")
        self.assertEqual(uid, "test-user")

    def test_encrypted_desktop_token_decryption_failure_precedes_and_blocks_http(self):
        encrypted = {
            "$wbEncrypted": 1,
            "envelope": base64.b64encode(b"{}").decode("ascii"),
        }
        with tempfile.TemporaryDirectory() as root:
            auth_path = self._write_auth(root, encrypted)
            with patch.object(WorkBuddyCheckin, "AUTH_FILE_CANDIDATES", [auth_path]):
                checkin = WorkBuddyCheckin()
            events = []
            failed = subprocess.CompletedProcess(
                args=["WorkBuddy.exe"], returncode=1, stdout=b"",
                stderr=b"WB_HELPER_ERROR: invalid fixture",
            )
            with patch.object(checkin, "_find_workbuddy_exe", return_value="WorkBuddy.exe"), \
                    patch("platforms.workbuddy.subprocess.run",
                          side_effect=lambda *a, **kw: (events.append("decrypt"), failed)[1]), \
                    patch("urllib.request.urlopen",
                          side_effect=lambda *a, **kw: (events.append("http"), None)[1]):
                result = checkin.run()

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("解密失败", result["message"])
        self.assertEqual(events, ["decrypt"])

    def test_encrypted_desktop_token_is_decrypted_before_http(self):
        encrypted = {
            "$wbEncrypted": 1,
            "envelope": base64.b64encode(b"fixture").decode("ascii"),
        }
        with tempfile.TemporaryDirectory() as root:
            auth_path = self._write_auth(root, encrypted)
            with patch.object(WorkBuddyCheckin, "AUTH_FILE_CANDIDATES", [auth_path]):
                checkin = WorkBuddyCheckin()
            events = []
            response = unittest.mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({
                "data": {"today_checked_in": True, "streak_days": 3},
            }).encode()
            completed = subprocess.CompletedProcess(
                args=["WorkBuddy.exe"], returncode=0,
                stdout=_TOKEN.encode(), stderr=b"",
            )

            def fake_run(*args, **kwargs):
                events.append("decrypt")
                self.assertIn("wb_helper.js", args[0][1])
                self.assertEqual(kwargs["env"]["ELECTRON_RUN_AS_NODE"], "1")
                return completed

            def fake_urlopen(*args, **kwargs):
                events.append("http")
                return response

            with patch.object(checkin, "_find_workbuddy_exe", return_value="WorkBuddy.exe"), \
                    patch("platforms.workbuddy.subprocess.run", side_effect=fake_run), \
                    patch("urllib.request.urlopen", side_effect=fake_urlopen):
                result = checkin.checkin()

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "already")
        self.assertEqual(events, ["decrypt", "http"])

    def test_local_account_token_loads_without_network(self):
        with tempfile.TemporaryDirectory() as root:
            auth_path = os.path.join(root, "workbuddy-account.json")
            with open(auth_path, "w", encoding="utf-8") as stream:
                json.dump({
                    "auth": {
                        "accessToken": _TOKEN,
                        "refreshToken": "",
                        "domain": "www.workbuddy.cn",
                    },
                    "account": {"uid": "test-user"},
                }, stream)
            checkin = WorkBuddyCheckin(
                dry=True,
                account={"path": auth_path, "label": "test"},
            )
            with patch("urllib.request.urlopen", side_effect=AssertionError("network call")):
                token, domain, uid, _auth = checkin._load_auth()

        self.assertEqual(token, _TOKEN)
        self.assertEqual(domain, "www.workbuddy.cn")
        self.assertEqual(uid, "test-user")

    def _checkin_with_http_responses(self, daily_result, *, daily_error=None):
        checkin = WorkBuddyCheckin(dry=False, account={"path": "unused", "label": "test"})
        checkin._load_auth = lambda: (_TOKEN, "www.workbuddy.cn", "test-user", {})
        responses = [
            {"data": {"today_checked_in": False, "streak_days": 1}},
            daily_result,
        ]
        if daily_error is not None:
            def fake_http(*_args, **_kwargs):
                if len(responses) == 1:
                    raise daily_error
                return responses.pop(0)
        else:
            def fake_http(*_args, **_kwargs):
                return responses.pop(0)
        checkin._http_json = fake_http
        return checkin

    def test_daily_checkin_nonzero_business_code_is_failure(self):
        checkin = self._checkin_with_http_responses({
            "code": 12001,
            "msg": "permission denied",
        })

        result = checkin.checkin()

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("code=12001", result["message"])
        self.assertIn("permission denied", result["message"])

    def test_daily_checkin_only_zero_code_is_success(self):
        checkin = self._checkin_with_http_responses({
            "code": "0",
            "data": {"credits": 25},
        })

        result = checkin.checkin()

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["credits"], 25)

    def test_daily_checkin_known_already_code_is_idempotent(self):
        checkin = self._checkin_with_http_responses({
            "code": 10001,
            "msg": "already checked in",
        })

        result = checkin.checkin()

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "already")

    def test_http_error_with_known_already_code_is_idempotent(self):
        checkin = self._checkin_with_http_responses(
            None,
            daily_error=RuntimeError('HTTP 400: {"code":10001,"msg":"already checked in"}'),
        )

        result = checkin.checkin()

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "already")

    def test_http_error_text_mentioning_already_is_not_success(self):
        checkin = self._checkin_with_http_responses(
            None,
            daily_error=RuntimeError('HTTP 500: {"code":90001,"msg":"already service unavailable"}'),
        )

        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            checkin.checkin()


if __name__ == "__main__":
    unittest.main()
