import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from modules.heybox import core, routes  # noqa: E402


class HeyboxRouteSecurityTests(unittest.TestCase):
    def test_run_rejects_web_cookie_without_starting_worker(self):
        with patch.object(core, "load_config", return_value={"cookie": "pkey=web"}), \
                patch.object(core.TASK, "start") as start:
            response = routes.run()
        self.assertEqual(response.status_code, 400)
        result = json.loads(response.body)
        self.assertFalse(result["ok"])
        self.assertTrue(result["app_login_required"])
        self.assertIn("App 短信登录", result["error"])
        start.assert_not_called()

    def test_run_accepts_app_session_without_web_cookie(self):
        cfg = {"app_session": {"pkey": "app-key", "x_xhh_tokenid": "token"}}
        with patch.object(core, "load_config", return_value=cfg), \
                patch.object(core.TASK, "start", return_value=True) as start:
            response = routes.run()
        self.assertTrue(response["ok"])
        start.assert_called_once_with(cfg)

    def test_state_does_not_return_app_session_secrets(self):
        cfg = core.load_config()
        cfg["cookie"] = "pkey=web-secret"
        cfg["app_session"] = {
            **core.APP_DEFAULTS, "pkey": "app-secret",
            "x_xhh_tokenid": "token-secret", "heybox_id": "12345",
        }
        with patch.object(core, "load_config", return_value=cfg), \
                patch.object(core, "load_state", return_value={}):
            result = routes.state()
        self.assertTrue(result["app_logged_in"])
        self.assertNotIn("app_session", result["config"])
        self.assertNotIn("app-secret", repr(result))
        self.assertNotIn("token-secret", repr(result))

    def test_regular_save_cannot_replace_app_session(self):
        cfg = {**core.DEFAULT_CONFIG, "app_session": {
            **core.APP_DEFAULTS, "pkey": "saved-app-key",
            "x_xhh_tokenid": "saved-token",
        }}
        with patch.object(core, "load_config", return_value=cfg), \
                patch.object(core, "save_config") as save:
            result = routes.save({"app_session": {"pkey": "attacker-value"}, "cookie": "pkey=web"})
        self.assertTrue(result["ok"])
        self.assertEqual(cfg["app_session"]["pkey"], "saved-app-key")
        self.assertEqual(cfg["cookie"], "pkey=web")
        save.assert_called_once_with(cfg)


if __name__ == "__main__":
    unittest.main()
