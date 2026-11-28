import json
import io
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from modules.heybox import core  # noqa: E402
from modules.heybox import native  # noqa: E402
from modules.heybox.native import _daily_nonce_alphabet, sign_native  # noqa: E402


class _FakeAppClient:
    login_result = {
        "status": "ok",
        "result": {
            "profile": {"heybox_id": "12345", "nickname": "tester"},
            "account_detail": {"userid": "12345"},
            "pkey": "new-pkey",
        },
    }

    def __init__(self, session, log=None):
        self.session = session

    def native_raw(self, endpoint, extra=None, body=None):
        return dict(self.login_result)

    def tasks(self):
        return {
            "user": {"userid": "12345", "username": "tester",
                      "level_info": {"exp": 100, "coin": 20}},
            "task_list": [{"title": "每日任务", "tasks": [
                {"title": "签到", "state": "waiting", "report_extra": {"task_id": "sign"}},
                *[{"title": f"任务 {task_id}", "state": "waiting",
                   "report_extra": {"task_id": task_id}}
                  for task_id in core.SHARE_IDS],
            ]}],
        }


class HeyboxAuthTests(unittest.TestCase):
    def setUp(self):
        core._APP_LOGIN.update(stage="idle", expires_at=0.0, phone="",
                               session=None, captcha_app_id="", captcha_for="", msg="",
                               captcha_id="", sent_at=0.0, attempts=0)

    def test_phone_and_captcha_are_validated(self):
        self.assertEqual(core.normalize_phone("13800138000"), "+8613800138000")
        self.assertEqual(core.normalize_phone("+86 13800138000"), "+8613800138000")
        with self.assertRaises(core.HeyboxError):
            core.normalize_phone("not-a-phone")
        with self.assertRaises(core.HeyboxError):
            core.captcha_query({"ticket": "trerror_123", "randstr": "x"})

    def test_success_only_saves_after_task_snapshot_and_preserves_web_cookie(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps({"cookie": "pkey=web-old", "heybox_id": "777"}), encoding="utf-8")
            session = dict(core.APP_DEFAULTS)
            session.update({"device_id": "device-1", "x_xhh_tokenid": "token-1", "heybox_id": "-1"})
            core._APP_LOGIN.update(stage="code", phone="+8613800138000",
                                   session=session, expires_at=core.time.time() + 60)
            with patch.object(core, "CONFIG_PATH", config_path), patch.object(core, "AppClient", _FakeAppClient):
                result = core.app_login_verify("13800138000", "123456")
                saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(result["stage"], "success")
            self.assertEqual(saved["cookie"], "pkey=web-old")
            self.assertEqual(saved["app_session"]["pkey"], "new-pkey")
            self.assertEqual(saved["app_session"]["heybox_id"], "12345")

    def test_task_account_mismatch_does_not_save_new_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "config.json"
            original = {"cookie": "pkey=web-old", "heybox_id": "777", "marker": "keep"}
            config_path.write_text(json.dumps(original), encoding="utf-8")
            session = dict(core.APP_DEFAULTS)
            session.update({"device_id": "device-1", "x_xhh_tokenid": "token-1", "heybox_id": "-1"})
            core._APP_LOGIN.update(stage="code", phone="+8613800138000",
                                   session=session, expires_at=core.time.time() + 60)
            class WrongAccountClient(_FakeAppClient):
                def tasks(self):
                    result = super().tasks()
                    result["user"]["userid"] = "99999"
                    return result
            with patch.object(core, "CONFIG_PATH", config_path), patch.object(core, "AppClient", WrongAccountClient):
                with self.assertRaises(core.HeyboxError):
                    core.app_login_verify("13800138000", "123456")
            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), original)

    def test_send_code_captcha_state_does_not_expose_phone_or_device_token(self):
        class CaptchaClient(_FakeAppClient):
            def native_raw(self, endpoint, extra=None, body=None):
                self.endpoint, self.extra, self.body = endpoint, extra, body
                return {"status": "show_captcha", "result": {"appid": "1234567890"}}
        with patch("modules.heybox.device_token.get_device_token", return_value="secret-device-token"), \
                patch.object(core, "AppClient", CaptchaClient):
            result = core.app_login_send_code("13800138000")
        self.assertEqual(result["stage"], "captcha")
        self.assertEqual(result["captcha_for"], "send")
        self.assertEqual(result["captcha_app_id"], "1234567890")
        self.assertNotIn("13800138000", repr(result))
        self.assertNotIn("secret-device-token", repr(result))

    def test_verify_captcha_challenges_do_not_consume_code_attempts(self):
        class ChallengeClient(_FakeAppClient):
            responses = [
                {"status": "show_captcha", "result": {"appid": "1234567890"}},
                {"status": "show_captcha", "result": {"appid": "1234567890"}},
                _FakeAppClient.login_result,
            ]

            def native_raw(self, endpoint, extra=None, body=None):
                return self.responses.pop(0)

        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "config.json"
            config_path.write_text(json.dumps({"cookie": "pkey=web-old"}), encoding="utf-8")
            session = dict(core.APP_DEFAULTS)
            session.update({"device_id": "device-1", "x_xhh_tokenid": "token-1",
                            "heybox_id": "-1"})
            core._APP_LOGIN.update(stage="code", phone="+8613800138000",
                                   session=session, expires_at=core.time.time() + 60)
            with patch.object(core, "CONFIG_PATH", config_path), \
                    patch.object(core, "AppClient", ChallengeClient):
                first = core.app_login_verify("13800138000", "123456")
                self.assertEqual(first["stage"], "captcha")
                self.assertEqual(core._APP_LOGIN["attempts"], 0)
                proof = {"ticket": "ticket-1", "randstr": "rand-1",
                         "captcha_id": first["captcha_id"]}
                second = core.app_login_verify("13800138000", "123456", proof)
                self.assertEqual(second["stage"], "captcha")
                self.assertEqual(core._APP_LOGIN["attempts"], 0)
                self.assertNotEqual(first["captcha_id"], second["captcha_id"])
                proof["captcha_id"] = second["captcha_id"]
                final = core.app_login_verify("13800138000", "123456", proof)

        self.assertEqual(final["stage"], "success")
        self.assertEqual(core._APP_LOGIN["attempts"], 1)

    def test_captcha_proof_cannot_resume_a_different_login_step(self):
        core._APP_LOGIN.update(stage="captcha", captcha_for="send",
                               captcha_id="current-challenge",
                               phone="+8613800138000", expires_at=core.time.time() + 60)
        proof = {"ticket": "ticket-1", "randstr": "rand-1",
                 "captcha_id": "current-challenge"}
        with patch.object(core, "AppClient") as client:
            with self.assertRaises(core.HeyboxError):
                core.app_login_verify("13800138000", "123456", proof)
            core._APP_LOGIN["captcha_for"] = "verify"
            with self.assertRaises(core.HeyboxError):
                core.app_login_send_code("13800138000", proof)
        client.assert_not_called()

    def test_captcha_proof_is_bound_to_current_challenge_and_phone(self):
        session = {**core.APP_DEFAULTS, "x_xhh_tokenid": "device-token"}
        core._APP_LOGIN.update(stage="captcha", captcha_for="send",
                               captcha_id="current-challenge", session=session,
                               phone="+8613800138000", expires_at=core.time.time() + 60)
        with patch.object(core, "AppClient") as client, \
                patch("modules.heybox.device_token.get_device_token") as token:
            for phone, challenge in (("13800138000", "old-challenge"),
                                     ("13900139000", "current-challenge"),
                                     ("13800138000", "")):
                with self.subTest(phone=phone, challenge=challenge):
                    with self.assertRaises(core.HeyboxError):
                        core.app_login_send_code(phone, {
                            "ticket": "ticket-1", "randstr": "rand-1",
                            "captcha_id": challenge,
                        })
            self.assertEqual(core._APP_LOGIN["stage"], "captcha")
            self.assertEqual(core._APP_LOGIN["captcha_id"], "current-challenge")
        client.assert_not_called()
        token.assert_not_called()

    def test_expired_challenge_cannot_send_another_sms(self):
        core._APP_LOGIN.update(stage="captcha", captcha_for="send",
                               captcha_id="expired-challenge",
                               phone="+8613800138000", expires_at=core.time.time() - 1)
        proof = {"ticket": "ticket-1", "randstr": "rand-1",
                 "captcha_id": "expired-challenge"}
        with patch.object(core, "AppClient") as client:
            with self.assertRaises(core.HeyboxError):
                core.app_login_send_code("13800138000", proof)
        client.assert_not_called()

    def test_cancel_discards_challenge_and_session_but_keeps_sms_cooldown(self):
        sent_at = core.time.time() - 15
        core._APP_LOGIN.update(stage="captcha", captcha_for="verify",
                               captcha_id="challenge", phone="+8613800138000",
                               session={"x_xhh_tokenid": "secret-token"},
                               sent_at=sent_at, attempts=2,
                               expires_at=core.time.time() + 60)
        result = core.app_login_cancel()
        self.assertEqual(result["stage"], "idle")
        self.assertEqual(result["captcha_id"], "")
        self.assertEqual(core._APP_LOGIN["phone"], "")
        self.assertIsNone(core._APP_LOGIN["session"])
        self.assertEqual(core._APP_LOGIN["attempts"], 0)
        self.assertEqual(core._APP_LOGIN["sent_at"], sent_at)
        self.assertGreater(result["resend_in"], 0)
        with patch.object(core, "AppClient") as client, \
                patch("modules.heybox.device_token.get_device_token") as token:
            with self.assertRaises(core.HeyboxError):
                core.app_login_send_code("13800138000")
        client.assert_not_called()
        token.assert_not_called()

    def test_cancel_does_not_interrupt_an_active_login_request(self):
        for stage in ("sending", "verifying"):
            with self.subTest(stage=stage):
                core._APP_LOGIN.update(stage=stage, phone="+8613800138000")
                with self.assertRaises(core.HeyboxError):
                    core.app_login_cancel()
                self.assertEqual(core._APP_LOGIN["stage"], stage)
                self.assertEqual(core._APP_LOGIN["phone"], "+8613800138000")

    def test_invalid_login_response_leaves_config_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "config.json"
            original = {"cookie": "pkey=web-old", "heybox_id": "777", "marker": "keep"}
            config_path.write_text(json.dumps(original), encoding="utf-8")
            session = dict(core.APP_DEFAULTS)
            session.update({"device_id": "device-1", "x_xhh_tokenid": "token-1", "heybox_id": "-1"})
            core._APP_LOGIN.update(stage="code", phone="+8613800138000",
                                   session=session, expires_at=core.time.time() + 60)
            class BadClient(_FakeAppClient):
                login_result = {"status": "ok", "result": {"profile": {"heybox_id": "1"},
                    "account_detail": {"userid": "2"}, "pkey": "new"}}
            with patch.object(core, "CONFIG_PATH", config_path), patch.object(core, "AppClient", BadClient):
                with self.assertRaises(core.HeyboxError):
                    core.app_login_verify("13800138000", "123456")
            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), original)

    def test_native_signature_matches_verified_app_vector(self):
        stamp = "1793040000"
        nonce = "QCEy6EpviiXkYJMovJrjpDhjQEqEJRoq"
        alphabet = _daily_nonce_alphabet(stamp)
        choices = iter(alphabet.index(char) for char in nonce)
        signed = sign_native("/account/login_code/", stamp,
                             "offline-recovery-probe-1", "17", "1.3.398",
                             nonce_index_source=lambda _size: next(choices))
        self.assertEqual(signed["_rnd"], "14:8226521A")
        self.assertEqual(signed["hkey"], "BFDB9883")
        self.assertEqual(signed["nonce"], nonce)

    def test_app_web_client_unwraps_result_before_task_parsing(self):
        client = core.AppClient({
            "pkey": "p", "x_xhh_tokenid": "t", "heybox_id": "12345",
            "device_id": "device",
        })
        with patch.object(client, "_send", return_value={"status": "ok", "result": {
            "user": {"userid": "12345"},
            "task_list": [{"title": "每日任务", "tasks": [
                {"state": "waiting", "report_extra": {"task_id": "1"}},
            ]}],
        }}):
            info = core.parse_tasks(client.tasks())
        self.assertEqual(info["userid"], "12345")
        self.assertEqual(info["tasks"][0]["id"], "1")

    def test_native_login_request_assembles_signed_post_and_app_credentials(self):
        client = core.AppClient({
            "pkey": "pkey-secret", "x_xhh_tokenid": "device-token",
            "heybox_id": "12345", "device_id": "device-1",
        })
        response = io.BytesIO(b'{"status":"ok","result":{}}')
        response.__enter__ = lambda: response
        response.__exit__ = lambda *_: None
        signature = {"_time": "1793040000", "nonce": "n" * 32,
                     "hkey": "ABCD1234", "_rnd": "14:1234ABCD"}
        with patch.object(native, "current_native_signature", return_value=signature) as sign, \
                patch.object(core.urllib.request, "urlopen", return_value=response) as urlopen:
            result = client.native_raw(
                "/account/get_login_code/", {"captcha_type": "confirm", "ticket": "ticket-1"},
                {"phone_num": "encrypted-phone"})

        request = urlopen.call_args.args[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        body = urllib.parse.parse_qs(request.data.decode("ascii"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(request.method, "POST")
        self.assertEqual(urllib.parse.urlsplit(request.full_url).path,
                         "/account/get_login_code/")
        self.assertEqual(query["heybox_id"], ["12345"])
        self.assertEqual(query["imei"], ["device-1"])
        self.assertEqual(query["dw"], ["360"])
        self.assertNotIn("netmode", query)
        self.assertEqual(query["captcha_type"], ["confirm"])
        self.assertEqual(query["ticket"], ["ticket-1"])
        self.assertEqual(query["_time"], [signature["_time"]])
        self.assertEqual(query["nonce"], [signature["nonce"]])
        self.assertEqual(body, {"phone_num": ["encrypted-phone"]})
        self.assertEqual(request.get_header("Cookie"),
                         "pkey=pkey-secret; x_xhh_tokenid=device-token")
        self.assertEqual(request.get_header("Content-type"),
                         "application/x-www-form-urlencoded")
        sign.assert_called_once_with("/account/get_login_code/", "device-1",
                                     "12345", "17", "1.3.398")

    def test_report_with_body_uses_post(self):
        client = core.Client("pkey=p", "12345", "device-1")
        response = io.BytesIO(b'{"status":"ok","result":{}}')
        report = {"time": "100", "data": "encrypted", "key": "key", "sid": "sid"}
        with patch.object(core, "encrypt_report", return_value=report), \
                patch.object(core.urllib.request, "urlopen", return_value=response) as urlopen:
            client.report({"event": "share"}, "session-1")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(urllib.parse.parse_qs(request.data.decode("ascii")), {
            "data": ["encrypted"], "key": ["key"], "sid": ["sid"],
        })

    def test_report_encryption_pads_aes_payload_and_round_trips(self):
        aes_key = "00000000499602d2"
        events = [{"action": "share", "name": "长文本" * 20}]
        with patch.object(core.random, "getrandbits", return_value=1234567890):
            payload = core.encrypt_report(events, stamp="1793040000")

        encrypted = core.base64.b64decode(payload["data"])
        self.assertEqual(len(encrypted) % 16, 0)
        cipher = core.Cipher(core.algorithms.AES(aes_key.encode()),
                             core.modes.CBC(b"abcdefghijklmnop"))
        decryptor = cipher.decryptor()
        padded = decryptor.update(encrypted) + decryptor.finalize()
        unpadder = core.padding.PKCS7(128).unpadder()
        compressed = unpadder.update(padded) + unpadder.finalize()
        self.assertEqual(json.loads(core.gzip.decompress(compressed)), {"events": events})


class HeyboxRunTests(unittest.TestCase):
    def test_web_cookie_alone_stops_before_any_task_request(self):
        cfg = {"cookie": "pkey=web-only", "heybox_id": "12345"}
        task = core.Task()
        with patch.object(core.urllib.request, "urlopen") as urlopen:
            task._run(cfg)
        urlopen.assert_not_called()
        self.assertEqual(task.summary["status"], "failed")
        self.assertIn("App 短信登录", task.summary["error"])
        self.assertTrue(task.done)
        self.assertFalse(task.running)
        self.assertFalse(any(line["l"] == "finish" for line in task.logs))
        self.assertNotIn("全部完成", str(task.logs))

    def test_finish_status_distinguishes_partial_and_success(self):
        for failures, expected in ((1, "partial"), (0, "success")):
            with self.subTest(status=expected):
                task = core.Task()
                with patch.object(core.HeyboxDaily, "run",
                                  return_value={"ok": 2, "fail": failures, "skipped": 0}):
                    task._run({})
                self.assertEqual(task.summary["status"], expected)
                self.assertEqual(task.logs[-1]["l"], "warn" if failures else "finish")

    def test_app_session_is_preferred_over_web_cookie(self):
        cfg = {"cookie": "pkey=web-only", "app_session": {
            **core.APP_DEFAULTS, "pkey": "app-key", "x_xhh_tokenid": "app-token",
            "heybox_id": "12345", "device_id": "device-1",
        }}
        client = core.HeyboxDaily(cfg, lambda *_: None)._client()
        self.assertIsInstance(client, core.AppClient)
        self.assertIn("pkey=app-key", client.native_cookie)
        self.assertNotIn("web-only", client.native_cookie)


if __name__ == "__main__":
    unittest.main()
