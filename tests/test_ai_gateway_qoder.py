import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from modules.ai_gateway.base import GatewayError
from modules.ai_gateway.providers.qoder import QoderCNProvider, QoderProvider


class Response:
    status_code = 200

    def __init__(self, value, status=200):
        self.status_code = status
        self.value = value

    def json(self):
        return self.value


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class QoderGatewayTests(unittest.TestCase):
    def test_import_local_resolves_userinfo_and_keeps_device_headers(self):
        http = FakeHttp(Response({"id": "u-1", "name": "Alice", "email": "a@example.com"}))
        provider = QoderProvider(http=http)
        with patch("modules.ai_gateway.providers.qoder.qoder_cred.device",
                   return_value=({"machineId": "m-1"}, "")), \
             patch("modules.ai_gateway.providers.qoder.qoder_cred.get_token",
                   return_value=("access-1", "")), \
             patch("modules.ai_gateway.providers.qoder.qoder_cred.device_headers",
                   return_value={"Cosy-ClientType": "10"}), \
             patch("modules.ai_gateway.providers.qoder.qoder_cred.read_cache", return_value={}):
            rows = provider.import_local()
        self.assertEqual(rows[0]["external_id"], "u-1")
        self.assertEqual(rows[0]["label"], "Alice")
        self.assertEqual(rows[0]["credential"]["access_token"], "access-1")
        self.assertEqual(http.calls[0][0], "https://openapi.qoder.sh/api/v1/userinfo")
        self.assertEqual(http.calls[0][1]["headers"]["Cosy-ClientType"], "10")

    def test_domestic_provider_uses_domestic_openapi_origin(self):
        http = FakeHttp(Response({"uid": "cn-1", "username": "CN"}))
        provider = QoderCNProvider(http=http)
        with patch("modules.ai_gateway.providers.qoder.qoder_cred.device",
                   return_value=({}, "")), \
             patch("modules.ai_gateway.providers.qoder.qoder_cred.get_token",
                   return_value=("access-cn", "")), \
             patch("modules.ai_gateway.providers.qoder.qoder_cred.device_headers",
                   return_value={}), \
             patch("modules.ai_gateway.providers.qoder.qoder_cred.read_cache", return_value={}):
            rows = provider.import_local()
        self.assertEqual(rows[0]["external_id"], "cn-1")
        self.assertTrue(http.calls[0][0].startswith("https://openapi.qoder.com.cn/"))

    def test_expired_desktop_token_is_reported(self):
        provider = QoderProvider(http=FakeHttp(Response({})))
        with patch("modules.ai_gateway.providers.qoder.qoder_cred.device", return_value=({}, "")), \
             patch("modules.ai_gateway.providers.qoder.qoder_cred.get_token",
                   return_value=("", "令牌已过期")):
            with self.assertRaises(GatewayError) as ctx:
                provider.import_local()
        self.assertEqual(ctx.exception.code, "login_required")

    def test_chat_is_explicitly_unavailable_until_wasm_protocol_is_closed(self):
        with self.assertRaises(GatewayError) as ctx:
            list(QoderProvider().chat({}, {"model": "x", "messages": []}))
        self.assertEqual(ctx.exception.code, "unsupported_capability")


if __name__ == "__main__":
    unittest.main()
