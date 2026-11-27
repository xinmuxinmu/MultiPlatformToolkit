import base64
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from PIL import Image
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from modules.ai_gateway.base import GatewayError
from modules.ai_gateway.providers import zhipu


class Response:
    def __init__(self, status=200, *, json_value=None, text="", content=b"", content_type="application/json"):
        self.status_code = status
        self._json = json_value
        self.text = text
        self.content = content
        self.headers = {"Content-Type": content_type}

    def json(self):
        return self._json

    def iter_content(self, _chunk_size):
        yield self.content


class FakeHttp:
    def __init__(self, *, login_code=405, checkin=None):
        self.calls = []
        self.login_code = login_code
        self.checkin = checkin or Response(json_value={"status": 0, "result": {"score": 5}})
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
        if url.endswith("/user/info"):
            return Response(json_value={"status": 0, "result": {"user_id": "user-1", "nickname": "Test"}})
        if url.endswith("/daily_login_score"):
            return self.checkin
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
            (Response(json_value={"status": 0, "result": {"score": 0}}), "failed"),
            (Response(json_value={"status": 7001, "result": {"score": 5}}), "failed"),
        ]:
            with self.subTest(expected=expected):
                provider = zhipu.ZhipuProvider(http=FakeHttp(checkin=response))
                self.assertEqual(provider.checkin({"access_token": "opaque"})["status"], expected)

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


if __name__ == "__main__":
    unittest.main()
