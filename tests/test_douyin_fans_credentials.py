"""Offline validation of account-bound native circle sessions."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.douyin_fans.credentials import (load_aweme_session, load_circle_session,
                                              load_webcast_session)


SESSION_PATH = "offline-native-session.json"
ACCOUNT = "selected-account"
UID = "123456"
SECRET = "offline-native-secret-never-log"


def session():
    return {
        "verified": True, "identity_match": True, "account_id": ACCOUNT, "uid": UID,
        "cookies": [{"name": "sessionid", "value": SECRET,
                     "domain": ".fandom.starplaylet.com", "path": "/", "expires": 2000}],
        "headers": {"User-Agent": "Offline client", "x-scm-version": "1.0.0.757"},
        "common_params": {"aid": 1128, "device_platform": "android"},
    }


class CircleSessionCredentialsTests(unittest.TestCase):
    def load(self, data=None, *, raw=None, account_id=ACCOUNT, expected_uid=UID,
             size=None, read_error=None, stat_error=None):
        content = json.dumps(session() if data is None else data) if raw is None else raw
        with (
            patch("modules.douyin_fans.credentials.Path") as path,
            patch("modules.douyin_fans.credentials.time.time", return_value=1000),
        ):
            source = path.return_value
            source.stat.return_value = SimpleNamespace(
                st_size=len(content.encode("utf-8")) if size is None else size)
            if stat_error:
                source.stat.side_effect = stat_error
            if read_error:
                source.read_text.side_effect = read_error
            else:
                source.read_text.return_value = content
            result = load_circle_session(SESSION_PATH, account_id=account_id,
                                         expected_uid=expected_uid)
            self.last_source = source
            return result

    def test_verified_matching_identity_accepts_only_scoped_native_credentials(self):
        data = session()
        data["unrelated_secret"] = SECRET
        out = self.load(data)
        self.assertEqual(out, {
            "cookies": data["cookies"], "headers": data["headers"],
            "common_params": data["common_params"],
            "source": "account_bound_native_circle_session",
        })
        self.assertNotIn("unrelated_secret", out)
        self.last_source.write_text.assert_not_called()

    def test_account_and_uid_must_both_match_selected_identity(self):
        for account_id, uid in (("another-account", UID), (ACCOUNT, "654321")):
            with self.subTest(account_id=account_id, uid=uid):
                self.assertIsNone(self.load(account_id=account_id, expected_uid=uid))

    def test_verification_flags_require_actual_true(self):
        for flag in ("verified", "identity_match"):
            for value in (False, None, 0, 1, "true", "True", []):
                with self.subTest(flag=flag, value=value):
                    data = session()
                    data[flag] = value
                    self.assertIsNone(self.load(data))
            data = session()
            del data[flag]
            self.assertIsNone(self.load(data))

    def test_missing_identity_or_path_stops_before_file_access(self):
        for path, account_id, uid in (("", ACCOUNT, UID), (None, ACCOUNT, UID),
                                      (SESSION_PATH, "", UID), (SESSION_PATH, ACCOUNT, "")):
            with self.subTest(path=path, account_id=account_id, uid=uid):
                with patch("modules.douyin_fans.credentials.Path") as source:
                    self.assertIsNone(load_circle_session(path, account_id=account_id,
                                                         expected_uid=uid))
                    source.assert_not_called()

    def test_expired_cookies_are_rejected_including_exact_expiry(self):
        for expires in (999, 1000):
            data = session()
            data["cookies"][0]["expires"] = expires
            self.assertIsNone(self.load(data))
        data = session()
        del data["cookies"][0]["expires"]
        data["cookies"][0]["expirationDate"] = 999
        self.assertIsNone(self.load(data))

    def test_session_cookies_and_future_cookies_remain_usable(self):
        for expires in (-1, 0, 1001):
            data = session()
            data["cookies"][0]["expires"] = expires
            out = self.load(data)
            self.assertIsNotNone(out)
            self.assertEqual("expires" in out["cookies"][0], expires > 0)

    def test_malformed_expiry_cannot_be_treated_as_session_cookie(self):
        for expires in (None, False, True, "999", "2000", [], {},
                        float("nan"), float("inf"), -float("inf")):
            with self.subTest(expires=expires):
                data = session()
                data["cookies"][0]["expires"] = expires
                self.assertIsNone(self.load(data))

    def test_foreign_parent_subdomain_and_empty_cookie_jars_are_rejected(self):
        for domain in ("douyin.com", "starplaylet.com", "aweme.snssdk.com",
                       "evil.fandom.starplaylet.com", "fandom.starplaylet.com.evil", ""):
            data = session()
            data["cookies"][0]["domain"] = domain
            with self.subTest(domain=domain):
                self.assertIsNone(self.load(data))
        for rows in (None, [], {}, [None, "bad"]):
            data = session()
            data["cookies"] = rows
            self.assertIsNone(self.load(data))

    def test_only_valid_target_cookies_survive_mixed_jar(self):
        data = session()
        valid = copy.deepcopy(data["cookies"][0])
        invalid = [None, {}, {**valid, "domain": "douyin.com"},
                   {**valid, "expires": 999}, {**valid, "value": ""},
                   {**valid, "name": True}, {**valid, "value": "bad\r\nvalue"},
                   {**valid, "name": "bad;name"}]
        data["cookies"] = invalid + [valid]
        self.assertEqual(self.load(data)["cookies"], [valid])

    def test_cookie_paths_are_normalized_without_copying_extra_fields(self):
        for path, expected in (("/ent/fandom/", "/ent/fandom/"), (None, "/"), ("bad", "/")):
            data = session()
            data["cookies"][0].update(path=path, private_metadata=SECRET)
            cookie = self.load(data)["cookies"][0]
            self.assertEqual(cookie["path"], expected)
            self.assertNotIn("private_metadata", cookie)

    def test_headers_cannot_inject_cookie_authorization_or_host(self):
        data = session()
        data["headers"] = {
            "Cookie": SECRET, "cookie": SECRET, "Authorization": SECRET,
            "Host": "evil.example", "X-Tt-Token": SECRET,
            "X-Tma-Host-Sessionid": SECRET, "User-Agent": "Offline client",
            "Accept": "application/json", "x-scm-version": "1.0.0.757",
            "accept": "bad\r\nInjected: value", "connection": "keep-alive",
        }
        out = self.load(data)
        self.assertEqual(out["headers"], {
            "User-Agent": "Offline client", "Accept": "application/json",
            "x-scm-version": "1.0.0.757",
        })

    def test_only_public_common_parameter_keys_and_scalar_types_survive(self):
        data = session()
        data["common_params"] = {
            "aid": 1128, "device_platform": "android", "scm_version": "1.0.0.757",
            "from_app": "fandom_detail_douyin", "device_id": SECRET,
            "code": SECRET, "sessionid": SECRET, "fandom_id": "999", "biz": 999,
            "unknown": {"secret": SECRET},
        }
        self.assertEqual(self.load(data)["common_params"], {
            "aid": 1128, "device_platform": "android", "scm_version": "1.0.0.757",
            "from_app": "fandom_detail_douyin",
        })
        for value in (True, None, [], {}, 1.5):
            data["common_params"] = {"aid": value}
            self.assertEqual(self.load(data)["common_params"], {})

    def test_bad_json_wrong_root_and_io_errors_fail_without_secret_output(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            for raw in ("{invalid " + SECRET, "null", "[]", "1", '"' + SECRET + '"'):
                self.assertIsNone(self.load(raw=raw))
            for error in (OSError(SECRET), UnicodeError(SECRET)):
                self.assertIsNone(self.load(read_error=error))
            self.assertIsNone(self.load(stat_error=OSError(SECRET)))
        self.assertEqual(output.getvalue(), "")

    def test_oversized_source_is_rejected_before_reading(self):
        self.assertIsNone(self.load(size=1_048_577))
        self.last_source.read_text.assert_not_called()

    def test_aweme_store_remains_independently_usable_when_circle_cookie_has_expired(self):
        data = session()
        data["cookies"][0]["expires"] = 999
        content = json.dumps(data)
        native_cookies = [{"name": "sessionid", "value": SECRET,
                           "domain": ".aweme.snssdk.com", "path": "/"}]
        with (patch("modules.douyin_fans.credentials.Path") as path,
              patch("modules.douyin_fans.credentials.time.time", return_value=1000),
              patch("modules.douyin_fans.native_cookie_store.decode_store",
                    return_value=native_cookies) as decode):
            source = path.return_value
            source.stat.return_value = SimpleNamespace(st_size=len(content.encode()))
            source.read_text.return_value = content
            source.read_bytes.return_value = b"offline-cookie-store"
            out = load_aweme_session(SESSION_PATH, "offline-cookie-store.xml",
                                     account_id=ACCOUNT, expected_uid=UID)
        self.assertEqual(out, {
            "cookies": native_cookies, "headers": session()["headers"],
            "common_params": session()["common_params"],
            "source": "account_bound_native_aweme_session",
        })
        decode.assert_called_once_with(
            b"offline-cookie-store", allowed_domains=("snssdk.com", "aweme.snssdk.com"))

    def test_aweme_store_is_not_decoded_for_another_account(self):
        content = json.dumps(session())
        with (patch("modules.douyin_fans.credentials.Path") as path,
              patch("modules.douyin_fans.native_cookie_store.decode_store") as decode):
            source = path.return_value
            source.stat.return_value = SimpleNamespace(st_size=len(content.encode()))
            source.read_text.return_value = content
            self.assertIsNone(load_aweme_session(SESSION_PATH, "offline-cookie-store.xml",
                                                 account_id="another-account", expected_uid=UID))
            decode.assert_not_called()

    def test_webcast_store_uses_only_its_original_host_allowlist(self):
        content = json.dumps(session())
        native_cookies = [{"name": "sessionid", "value": SECRET,
                           "domain": ".webcast.amemv.com", "path": "/"}]
        with (patch("modules.douyin_fans.credentials.Path") as path,
              patch("modules.douyin_fans.native_cookie_store.decode_store",
                    return_value=native_cookies) as decode):
            source = path.return_value
            source.stat.return_value = SimpleNamespace(st_size=len(content.encode()))
            source.read_text.return_value = content
            source.read_bytes.return_value = b"offline-cookie-store"
            out = load_webcast_session(SESSION_PATH, "offline-cookie-store.xml",
                                       account_id=ACCOUNT, expected_uid=UID)
        self.assertEqual(out["cookies"], native_cookies)
        self.assertEqual(out["source"], "account_bound_native_webcast_session")
        decode.assert_called_once_with(
            b"offline-cookie-store", allowed_domains=("webcast.amemv.com", "amemv.com"))


if __name__ == "__main__":
    unittest.main()
