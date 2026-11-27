import importlib.util
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "vendor" / "doubao_checkin.py"
SPEC = importlib.util.spec_from_file_location("doubao_checkin_http_test", MODULE_PATH)
doubao = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(doubao)


class DoubaoHttpCheckinTests(TestCase):
    def setUp(self):
        self.cookies = ([{"name": "sessionid", "value": "test-session", "domain": ".doubao.com"}], "test")

    def test_checkin_uses_discovered_campaign_and_confirmed_response(self):
        responses = [
            (200, {"code": 0, "data": {"ug_campaign_info": {"campaign_group_code": "campaign-x"}}}),
            (200, {"code": 0, "data": {"main_page_info": {"buttons": []}}}),
            (200, {"code": 0, "data": {"checked_in": True}}),
        ]
        with patch.object(doubao, "read_client_cookies", return_value=self.cookies), \
             patch.object(doubao, "_doubao_post", side_effect=responses) as post:
            result = doubao.checkin_http()

        self.assertEqual((result["ok"], result["status"]), (True, "success"))
        self.assertEqual(post.call_args_list[0].args[:2], ("/alice/basic/launch", None))
        self.assertEqual(post.call_args_list[1].args[:2], ("/alice/ug/campaign/landing/get", {"scene": "campaign-x"}))
        self.assertEqual(post.call_args_list[2].args[:2], ("/alice/ug/campaign/landing/checkin", {"scene": "campaign-x"}))

    def test_explicit_existing_state_short_circuits_mutating_checkin(self):
        responses = [
            (200, {"code": 0, "data": {"ug_campaign_info": {"campaign_group_code": "campaign-x"}}}),
            (200, {"code": 0, "data": {"checked_in": True}}),
        ]
        with patch.object(doubao, "read_client_cookies", return_value=self.cookies), \
             patch.object(doubao, "_doubao_post", side_effect=responses) as post:
            result = doubao.checkin_http()

        self.assertEqual(result["status"], "already")
        self.assertEqual(post.call_count, 2)

    def test_missing_success_flag_is_not_reported_as_success(self):
        responses = [
            (200, {"code": 0, "data": {"ug_campaign_info": {"campaign_group_code": "campaign-x"}}}),
            (200, {"code": 0, "data": {"main_page_info": {"buttons": []}}}),
            (200, {"code": 0, "data": {"credits": 200}}),
        ]
        with patch.object(doubao, "read_client_cookies", return_value=self.cookies), \
             patch.object(doubao, "_doubao_post", side_effect=responses):
            result = doubao.checkin_http()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("checked_in", result["message"])

    def test_no_local_credentials_fails_without_network(self):
        with patch.object(doubao, "read_client_cookies", return_value=([], "no client session")), \
             patch.object(doubao, "_doubao_post") as post:
            result = doubao.checkin_http()

        self.assertFalse(result["ok"])
        self.assertIn("Cookie", result["message"])
        post.assert_not_called()

    def test_dry_run_does_not_send_http_requests(self):
        with patch.object(doubao, "read_client_cookies", return_value=self.cookies), \
             patch.object(doubao, "_doubao_post") as post:
            result = doubao.checkin_http(dry=True)

        self.assertEqual(result["status"], "skipped")
        post.assert_not_called()

    def test_cookie_header_keeps_only_doubao_cookie_values(self):
        cookie = doubao._cookie_header([
            {"name": "sid", "value": "abc", "domain": ".doubao.com"},
            {"name": "other", "value": "xyz", "domain": ".example.com"},
        ])
        self.assertEqual(cookie, "sid=abc")

