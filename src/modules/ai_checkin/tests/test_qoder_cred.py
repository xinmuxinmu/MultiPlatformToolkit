import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


VENDOR = Path(__file__).resolve().parents[1] / "vendor"
sys.path.insert(0, str(VENDOR))

import qoder_cred  # noqa: E402


class QoderCredentialsTests(unittest.TestCase):
    def test_appdata_dirs_prioritizes_matching_edition(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = Path(tmp) / "roaming"
            appdata.mkdir()
            international = appdata / "com.qoder.app.stable"
            domestic = appdata / "com.qodercn.app.stable"
            international.mkdir()
            domestic.mkdir()
            with patch.dict(os.environ, {"APPDATA": str(appdata), "QODER_AUTH_DIR": ""}):
                self.assertEqual(qoder_cred.appdata_dirs("https://openapi.qoder.com.cn")[0], str(domestic))
                self.assertEqual(qoder_cred.appdata_dirs("https://openapi.qoder.sh")[0], str(international))

    def test_appdata_dirs_does_not_use_the_other_editions_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = Path(tmp) / "roaming"
            appdata.mkdir()
            international = appdata / "com.qoder.app.stable"
            international.mkdir()
            with patch.dict(os.environ, {"APPDATA": str(appdata), "QODER_AUTH_DIR": ""}):
                self.assertEqual(qoder_cred.appdata_dirs("https://openapi.qoder.com.cn"), [])

    def test_device_reads_local_profile_files_without_starting_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            appdata = Path(tmp) / "roaming"
            home.mkdir()
            appdata.mkdir()
            auth_dir = appdata / "com.qodercn.app.stable"
            auth_dir.mkdir()
            profile = home / ".qoder-cn"
            profile.mkdir()
            (profile / "umid-cache.json").write_text(json.dumps({
                "info": {"machineCode": "local-code", "machineToken": "local-machine-token", "machineType": "pc"}
            }), encoding="utf-8")
            (auth_dir / "auth.machine-id").write_text("local-machine-id", encoding="utf-8")

            with patch.object(Path, "home", return_value=home), \
                    patch.object(qoder_cred, "appdata_dirs", return_value=[str(auth_dir)]):
                device, helper_path = qoder_cred.device("https://openapi.qoder.com.cn")

            self.assertEqual(helper_path, "")
            self.assertEqual(device["machineCode"], "local-code")
            self.assertEqual(device["machineToken"], "local-machine-token")
            self.assertEqual(device["machineId"], "local-machine-id")

    def test_extract_decrypts_auth_store_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_dir = Path(tmp) / "com.qoder.app.stable"
            auth_dir.mkdir()
            (auth_dir / "auth.v1.dat").write_bytes(b"v10" + b"n" * 12 + b"encrypted")
            encrypted_key = base64.b64encode(b"DPAPI" + b"wrapped-key")
            (auth_dir / "Local State").write_text(json.dumps({
                "os_crypt": {"encrypted_key": encrypted_key.decode("ascii")}
            }), encoding="utf-8")
            decrypt_session = lambda key, nonce, blob: json.dumps({
                "token": "local-access", "refreshToken": "local-refresh", "expiresAt": 1_900_000_000
            }).encode("utf-8")
            with patch.object(qoder_cred, "appdata_dirs", return_value=[str(auth_dir)]), \
                    patch.object(qoder_cred, "_dpapi_unprotect", return_value=b"test-key"), \
                    patch.object(qoder_cred, "_decrypt_gcm", side_effect=decrypt_session):
                token = qoder_cred.extract("https://openapi.qoder.sh")

            self.assertEqual(token, {
                "accessToken": "local-access",
                "refreshToken": "local-refresh",
                "expiresAt": 1_900_000_000,
            })

    def test_get_token_refreshes_expired_local_session_over_mocked_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "qoder_session.json"
            base = "https://openapi.qoder.sh"
            with patch.object(qoder_cred, "CACHE_FILE", cache_file):
                qoder_cred._write_cache({base: {
                    "accessToken": "stale-access", "refreshToken": "cached-refresh", "expiresAt": 1,
                }})
                refresh_calls = []

                def fake_refresh(selected_base, refresh_token, device):
                    refresh_calls.append((selected_base, refresh_token, device))
                    if refresh_token == "cached-refresh":
                        return None
                    return {"accessToken": "renewed-access", "refreshToken": "next-refresh",
                            "expiresAt": 1_900_000_000}

                with patch.object(qoder_cred, "extract", return_value={
                        "accessToken": "expired-local-access", "refreshToken": "local-refresh", "expiresAt": 1}), \
                        patch.object(qoder_cred, "device", return_value=({"machineCode": "local"}, "")), \
                        patch.object(qoder_cred, "refresh", side_effect=fake_refresh):
                    access_token, error = qoder_cred.get_token(base)

                self.assertEqual(access_token, "renewed-access")
                self.assertEqual(error, "")
                self.assertEqual([call[1] for call in refresh_calls], ["cached-refresh", "local-refresh"])
                self.assertEqual(json.loads(cache_file.read_text(encoding="utf-8"))[base]["accessToken"],
                                 "renewed-access")

    def test_get_token_does_not_return_expired_access_token(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(qoder_cred, "CACHE_FILE", Path(tmp) / "qoder_session.json"), \
                patch.object(qoder_cred, "extract", return_value={
                    "accessToken": "expired", "refreshToken": "", "expiresAt": 1
                }), \
                patch.object(qoder_cred, "device", return_value=({}, "")), \
                patch.object(qoder_cred, "refresh", return_value=None):
            access_token, error = qoder_cred.get_token("https://openapi.qoder.sh")

        self.assertEqual(access_token, "")
        self.assertIn("已过期", error)


if __name__ == "__main__":
    unittest.main()
