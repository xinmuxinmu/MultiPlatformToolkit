import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from modules.ai_gateway.providers.kimi import KimiProvider


class Response:
    status_code = 200
    def __init__(self, value):
        self.value = value
    def json(self):
        return self.value


class SharedSession:
    def __init__(self):
        self.calls = []
        self.cookies = {"session": "must-not-leak"}
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "POST":
            return Response({"id": "login-1"})
        return Response({"status": "pending"})


class KimiGatewayTests(unittest.TestCase):
    def test_device_login_and_poll_suppress_shared_session_cookies(self):
        http = SharedSession()
        provider = KimiProvider(http=http)
        login = provider.start_login()
        self.assertEqual(provider.poll_login(login), {"status": "pending"})
        self.assertEqual(len(http.calls), 2)
        for _, _, kwargs in http.calls:
            self.assertEqual(kwargs["headers"].get("Cookie"), "")
            self.assertIn("x-msh-device-id", kwargs["headers"])


if __name__ == "__main__":
    unittest.main()
