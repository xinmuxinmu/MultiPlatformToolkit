"""Offline protocol/state regression tests; no real account or network."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.douyin_fans.http_client import BASE_URL, CALENDAR, IS_LOGIN, JOIN, META, fetch_circle


FANDOM = "27933289767426"
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def calendar(signed=False, *, day=None, fandom_id=FANDOM):
    date = day or NOW
    return {"status_code": 0, "fandom_id": fandom_id,
            "query_end_date": int(date.timestamp()), "months": [
                {"year": date.year, "month": date.month,
                 "days": [{"date": int(date.timestamp()), "type": 1 if signed else 0}]}]}


def meta(status=1, fandom_id=FANDOM):
    return {"status_code": 0, "fandom": {"fandom_id": fandom_id, "title": "测试圈子",
            "user_join_status": status, "secret": "server-secret-never-forward"}}


class FandomHttpTests(unittest.TestCase):
    def transport(self, responses):
        self.requests = []

        def handle(request):
            self.requests.append(request)
            self.assertEqual(str(request.url.copy_with(query=None)), BASE_URL + request.url.path)
            self.assertEqual(request.url.host, "fandom.starplaylet.com")
            self.assertTrue(responses, "Unexpected additional HTTP request")
            value = responses.pop(0)
            if isinstance(value, httpx.Response):
                return value
            if isinstance(value, Exception):
                raise value
            return httpx.Response(200, json=value)

        return httpx.MockTransport(handle)

    def fetch(self, responses, **kwargs):
        return fetch_circle(FANDOM, now=NOW, transport=self.transport(responses), **kwargs)

    def test_authenticated_read_uses_official_paths_and_scoped_fields(self):
        result = self.fetch([{"status_code": 0, "is_login": True}, meta(), calendar()])
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["circle"], {"fandom_id": FANDOM, "title": "测试圈子"})
        self.assertEqual(result["join_status"], 1)
        self.assertIs(result["signed_today"], False)
        self.assertFalse(result["result"]["verified"])
        self.assertEqual([r.url.path for r in self.requests], [IS_LOGIN, META, CALENDAR])

    def test_not_logged_in_never_posts_or_adopts_signed_calendar(self):
        result = self.fetch([{"status_code": 8, "is_login": False}, meta(2), calendar(True)], checkin=True)
        self.assertFalse(result["authenticated"])
        self.assertIsNone(result["signed_today"])
        self.assertFalse(result["result"]["verified"])
        self.assertTrue(all(r.method == "GET" for r in self.requests))

    def test_empty_200_is_not_login_success(self):
        result = self.fetch([httpx.Response(200, content=b""), meta(), calendar()], checkin=True)
        self.assertFalse(result["authenticated"])
        self.assertEqual(result["probe"]["attempts"][0]["body_kind"], "empty")
        self.assertFalse(result["result"]["verified"])

    def test_other_circle_is_rejected_and_never_posts(self):
        result = self.fetch([{"status_code": 0, "is_login": True}, meta(fandom_id="123"), calendar()], checkin=True)
        self.assertEqual(result["circle"], {})
        self.assertIsNone(result["signed_today"])
        self.assertEqual(result["result"]["status"], "invalid_circle")
        self.assertEqual(len(self.requests), 3)

    def test_other_circle_calendar_cannot_authorize_sign(self):
        result = self.fetch([{"status_code": 0, "is_login": True}, meta(), calendar(fandom_id="123")], checkin=True)
        self.assertIsNone(result["signed_today"])
        self.assertEqual(len(self.requests), 3)

    def test_joined_false_is_not_joined_automatically(self):
        result = self.fetch([{"status_code": 0, "is_login": True}, meta(0), calendar()], checkin=True)
        self.assertEqual(result["result"]["status"], "join_required")
        self.assertEqual(len(self.requests), 3)

    def test_calendar_requires_today_not_previous_day(self):
        previous = NOW - timedelta(days=1)
        result = self.fetch([{"status_code": 0, "is_login": True}, meta(2), calendar(True, day=previous)])
        self.assertIs(result["signed_today"], False)
        self.assertFalse(result["result"]["verified"])

    def test_shanghai_date_can_differ_from_utc_date(self):
        shanghai_time = datetime(2026, 11, 27, 0, 30, tzinfo=timezone(timedelta(hours=8)))
        transport = self.transport([{"status_code": 0, "is_login": True}, meta(2), calendar(True, day=shanghai_time)])
        result = fetch_circle(FANDOM, now=shanghai_time.astimezone(timezone.utc), transport=transport)
        self.assertTrue(result["signed_today"])
        self.assertTrue(result["result"]["verified"])

    def test_prior_month_or_missing_day_array_is_unknown(self):
        for value in (
            calendar(True, day=NOW.replace(month=10)),
            {"status_code": 0, "months": [{"year": 2026, "month": 11}]},
            {"status_code": 0, "months": []},
        ):
            result = self.fetch([{"status_code": 0, "is_login": True}, meta(), value], checkin=True)
            self.assertIsNone(result["signed_today"])
            self.assertEqual(len(self.requests), 3)

    def test_live_zero_month_count_without_days_is_explicitly_unsigned(self):
        data = {"status_code": 0, "fandom_id": FANDOM, "query_end_date": int(NOW.timestamp()),
                "months": [{"year": NOW.year, "month": NOW.month, "month_signed_days": 0}]}
        result = self.fetch([{"status_code": 0, "is_login": True}, meta(), data])
        self.assertIs(result["signed_today"], False)
        self.assertEqual(result["result"]["status"], "observed_http")

    def test_positive_month_count_never_proves_today_without_day_records(self):
        data = {"status_code": 0, "fandom_id": FANDOM, "query_end_date": int(NOW.timestamp()),
                "months": [{"year": NOW.year, "month": NOW.month, "month_signed_days": 5}]}
        result = self.fetch([{"status_code": 0, "is_login": True}, meta(2), data], checkin=True)
        self.assertIsNone(result["signed_today"])
        self.assertFalse(result["result"]["verified"])
        self.assertEqual(len(self.requests), 3)

    def test_live_month_count_plus_records_still_requires_today(self):
        for day, expected in ((NOW, True), (NOW - timedelta(days=1), False)):
            data = calendar(True, day=day)
            data["months"][0]["month_signed_days"] = 1
            result = self.fetch([{"status_code": 0, "is_login": True}, meta(2), data])
            self.assertIs(result["signed_today"], expected)
            self.assertIs(result["result"]["verified"], expected)

    def test_only_type_1_or_2_is_signed(self):
        for value, expected in ((0, False), (1, True), (2, True), (3, False)):
            data = calendar()
            data["months"][0]["days"][0]["type"] = value
            result = self.fetch([{"status_code": 0, "is_login": True}, meta(2), data])
            self.assertIs(result["signed_today"], expected)

    def test_success_requires_post_and_both_fresh_confirmations(self):
        result = self.fetch([{"status_code": 0, "is_login": True}, meta(), calendar(),
                             {"status_code": 0}, meta(2), calendar(True)], checkin=True)
        self.assertTrue(result["result"]["verified"])
        self.assertEqual(result["result"]["status"], "success")
        posted = self.requests[3]
        self.assertEqual(posted.method, "POST")
        self.assertEqual(posted.url.path, JOIN)
        self.assertEqual(posted.url.params["join_type"], "3")
        self.assertEqual(posted.url.params["fandom_id"], FANDOM)
        self.assertEqual(posted.content, b"")

    def test_post_success_without_fresh_state_is_unconfirmed(self):
        result = self.fetch([{"status_code": 0, "is_login": True}, meta(), calendar(),
                             {"status_code": 0}, meta(), calendar()], checkin=True)
        self.assertFalse(result["result"]["verified"])
        self.assertEqual(result["result"]["status"], "unconfirmed")

    def test_inconsistent_initial_state_never_submits(self):
        for state, signed in ((1, True), (2, False)):
            result = self.fetch([{"status_code": 0, "is_login": True}, meta(state), calendar(signed)], checkin=True)
            self.assertEqual(result["result"]["status"], "unconfirmed")
            self.assertFalse(result["result"]["verified"])
            self.assertEqual(len(self.requests), 3)

    def test_post_success_with_failed_meta_cannot_reuse_old_state(self):
        result = self.fetch([{"status_code": 0, "is_login": True}, meta(), calendar(),
                             {"status_code": 0}, {"status_code": 8}, calendar(True)], checkin=True)
        self.assertFalse(result["result"]["verified"])
        self.assertEqual(result["circle"], {})
        self.assertIsNone(result["signed_today"])

    def test_already_signed_does_not_post(self):
        result = self.fetch([{"status_code": 0, "is_login": True}, meta(2), calendar(True)], checkin=True)
        self.assertTrue(result["result"]["verified"])
        self.assertEqual(result["result"]["status"], "already")
        self.assertEqual(len(self.requests), 3)

    def test_http_error_redirect_html_and_bad_json_are_not_success(self):
        for bad in (
            httpx.Response(403, json={"status_code": 0, "is_login": True}),
            httpx.Response(302, headers={"Location": "https://example.com/?token=private"}, json={"status_code": 0}),
            httpx.Response(200, text="<!doctype html><html>login</html>"),
            httpx.Response(200, json=[{"status_code": 0, "is_login": True}]),
        ):
            result = self.fetch([bad, meta(), calendar()], checkin=True)
            self.assertFalse(result["authenticated"])
            self.assertFalse(result["result"]["verified"])
            self.assertEqual(len(self.requests), 3)

    def test_common_params_never_override_business_fields_or_leak(self):
        secret = "private-native-common-secret"
        result = self.fetch([{"status_code": 0, "is_login": True}, meta(), calendar()],
                            common_params={"aid": 1128, "device_id": secret, "fandom_id": "123",
                                           "join_type": 5, "level": 99, "nested": {"bad": True}})
        self.assertEqual(self.requests[1].url.params["fandom_id"], FANDOM)
        self.assertEqual(self.requests[1].url.params["level"], "0")
        self.assertEqual(self.requests[1].url.params["aid"], "1128")
        self.assertNotIn("nested", self.requests[1].url.params)
        self.assertNotIn(secret, json.dumps(result))
        self.assertTrue(all("?" not in a["endpoint"] for a in result["probe"]["attempts"]))

    def test_cookie_header_and_server_message_never_leak(self):
        secret = "private-native-cookie-secret"
        result = self.fetch([{"status_code": 8, "status_msg": secret}, meta(), calendar()],
                            headers={"Cookie": "sessionid=" + secret, "X-Tt-Token": secret})
        self.assertTrue(result["probe"]["used_account_cookies"])
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn("server-secret-never-forward", json.dumps(result))

    def test_discussion_group_id_is_a_numeric_public_whitelist(self):
        for group_id in ("27941944430338", 27941944430338, "0", "0" * 32):
            with self.subTest(group_id=group_id):
                data = meta()
                data["fandom"]["discuss_group_id"] = group_id
                data["fandom"]["discuss_context"] = {"token": "private-discuss-context"}
                result = self.fetch([{"status_code": 0, "is_login": True}, data, calendar()])
                self.assertEqual(result["circle"]["discuss_group_id"], str(group_id))
                self.assertNotIn("private-discuss-context", json.dumps(result))
                self.assertNotIn("server-secret-never-forward", json.dumps(result))

    def test_malformed_discussion_group_id_is_not_forwarded(self):
        for group_id in (None, True, False, -123, 1.25, "", "-123", "123 ", " 123",
                         "123\n", "1" * 33, 10 ** 32, "１２３", "https://example.com/123",
                         {"group_id": "123", "token": "private-discuss-context"}, ["123"]):
            with self.subTest(group_id=group_id):
                data = meta()
                data["fandom"]["discuss_group_id"] = group_id
                result = self.fetch([{"status_code": 0, "is_login": True}, data, calendar()])
                self.assertNotIn("discuss_group_id", result["circle"])
                self.assertNotIn("private-discuss-context", json.dumps(result))

    def test_other_circle_cannot_supply_discussion_group_metadata(self):
        data = meta(fandom_id="123")
        data["fandom"]["discuss_group_id"] = "27941944430338"
        result = self.fetch([{"status_code": 0, "is_login": True}, data, calendar()])
        self.assertEqual(result["circle"], {})
        self.assertNotIn("27941944430338", json.dumps(result))
        self.assertEqual(result["result"]["status"], "invalid_circle")

    def test_live_fans_club_metadata_is_a_numeric_public_whitelist(self):
        data = meta()
        data["fandom"]["live_fans_club_info"] = {
            "anchor_uid": "123456789012345", "room_id": 0, "fans_type": 8,
            "anchor_sec_uid": "private-anchor-sec-never-forward",
            "user_id": "private-current-user-never-forward",
            "cookies": {"sessionid": "private-session-never-forward"},
        }
        result = self.fetch([{"status_code": 0, "is_login": True}, data, calendar()])
        self.assertEqual(result["circle"]["live_fans_club_info"], {
            "anchor_uid": "123456789012345", "room_id": 0, "fans_type": 8})
        self.assertNotIn("private-", json.dumps(result))
        mismatched = meta(fandom_id="123")
        mismatched["fandom"]["live_fans_club_info"] = data["fandom"]["live_fans_club_info"]
        result = self.fetch([{"status_code": 0, "is_login": True}, mismatched, calendar()])
        self.assertEqual(result["circle"], {})

    def test_banned_or_invalid_live_metadata_is_not_forwarded(self):
        for live in (
            {"anchor_uid": "123", "room_id": "456", "fans_type": 8, "is_ban": True},
            {"anchor_uid": "123", "room_id": "456", "fans_type": 8, "is_ban": 1},
            {"anchor_uid": "x" * 12, "room_id": "1" * 33, "fans_type": True},
            {"anchor_uid": -123, "room_id": 1.25, "fans_type": {"token": "private"}},
        ):
            data = meta()
            data["fandom"]["live_fans_club_info"] = live
            result = self.fetch([{"status_code": 0, "is_login": True}, data, calendar()])
            self.assertNotIn("live_fans_club_info", result["circle"])

    def test_offline_anchor_room_id_can_be_explicit_empty_string(self):
        data = meta()
        data["fandom"]["live_fans_club_info"] = {
            "anchor_uid": "123456789", "room_id": "", "fans_type": "8"}
        result = self.fetch([{"status_code": 0, "is_login": True}, data, calendar()])
        self.assertEqual(result["circle"]["live_fans_club_info"], {
            "anchor_uid": "123456789", "room_id": "", "fans_type": "8"})

    def test_cookie_domains_are_not_silently_rebound(self):
        secret = "private-web-cookie-secret"
        result = self.fetch([{"status_code": 8}, meta(), calendar()], cookies=[
            {"name": "sessionid", "value": secret, "domain": ".douyin.com", "path": "/"}])
        self.assertFalse(result["probe"]["used_account_cookies"])
        self.assertNotIn("cookie", self.requests[0].headers)

    def test_network_exception_url_is_not_leaked(self):
        result = self.fetch([httpx.ConnectError("https://host/?token=secret-never-log"), meta(), calendar()])
        self.assertNotIn("secret-never-log", json.dumps(result))
        self.assertEqual(result["probe"]["attempts"][0]["body_kind"], "network_error")

    def test_invalid_circle_input_never_requests(self):
        for fandom_id in ("", "123/../../evil", "https://example.com", "1?token=secret"):
            with self.assertRaises(ValueError):
                fetch_circle(fandom_id, transport=httpx.MockTransport(lambda _: self.fail("Network must not run")))


if __name__ == "__main__":
    unittest.main()
