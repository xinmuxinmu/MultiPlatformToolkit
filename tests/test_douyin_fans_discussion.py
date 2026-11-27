"""Offline discussion-context checks; no account storage or real HTTP."""
import json
from pathlib import Path
import sys
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.douyin_fans.discussion import BASE_URL, DISCUSSION_PATH, fetch_discussion_context


CIRCLE = {"fandom_id": "27933289767426", "discuss_group_id": "27941944430338"}


def payload(**updates):
    value = {"status_code": 0, "group_id": CIRCLE["discuss_group_id"], "comment_list": []}
    value.update(updates)
    return value


class DiscussionContextTests(unittest.TestCase):
    def fetch(self, response, *, circle=CIRCLE, headers=None):
        self.requests = []

        def handler(request):
            self.requests.append(request)
            if isinstance(response, Exception):
                raise response
            return response if isinstance(response, httpx.Response) else httpx.Response(200, json=response)

        return fetch_discussion_context(circle, headers=headers, transport=httpx.MockTransport(handler))

    def assert_failed(self, result):
        self.assertFalse(result["ok"])
        self.assertEqual(result["context"], {})
        self.assertIsNone(result["comment_count"])

    def test_exact_official_get_and_context_only_allowlist(self):
        out = self.fetch(payload(comment_list=[{"text": "private-body", "user": {"uid": "private-user"}}],
                                 secret="server-secret"),
                         headers={"Cookie": "sessionid=offline-secret", "Host": "evil.example",
                                  "X-Scm-Version": "wrong-version"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["comment_count"], 1)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url.copy_with(query=None)), BASE_URL + DISCUSSION_PATH)
        self.assertEqual(request.headers["Host"], "aweme.snssdk.com")
        self.assertEqual(request.headers["x-scm-version"], "1.0.0.757")
        self.assertEqual(request.headers["Cookie"], "sessionid=offline-secret")
        self.assertEqual(dict(request.url.params), {
            "cid": "0", "sort_by": "recommend", "offset": "0", "count": "10",
            "business_id": CIRCLE["fandom_id"], "discuss_scene": "fandom",
            "scm_version": "1.0.0.757", "from_app": "fandom_detail_douyin",
        })
        self.assertEqual(out["context"], {
            "business_id": CIRCLE["fandom_id"], "discuss_scene": "fandom",
            "group_id": CIRCLE["discuss_group_id"],
            "channel_obj_id": '{"business_id":"27933289767426","discuss_scene":"fandom"}',
            "business_scene": "fandom", "publish_scene": "video_bottom_input",
        })
        serialized = json.dumps(out)
        for secret in ("private-body", "private-user", "server-secret", "offline-secret", "wrong-version"):
            self.assertNotIn(secret, serialized)

    def test_invalid_or_missing_ids_do_not_request(self):
        invalid_values = [None, "", "１２３", "١٢٣", "123\n", " 123", "123 ",
                          "-1", "+1", "1.0", 1.0, True, False, "1" * 33, {}, []]
        circles = [None, {}, {"fandom_id": CIRCLE["fandom_id"]}]
        for key in CIRCLE:
            circles.extend({**CIRCLE, key: value} for value in invalid_values)
        for circle in circles:
            with self.subTest(circle=circle):
                out = self.fetch(payload(), circle=circle)
                self.assert_failed(out)
                self.assertEqual(self.requests, [])
                self.assertEqual(out["attempts"], [])

    def test_int_identifiers_and_string_zero_business_status_are_supported(self):
        out = self.fetch(payload(status_code="0", group_id=int(CIRCLE["discuss_group_id"])),
                         circle={key: int(value) for key, value in CIRCLE.items()})
        self.assertTrue(out["ok"])
        self.assertEqual(out["comment_count"], 0)
        self.assertEqual(out["context"]["group_id"], CIRCLE["discuss_group_id"])

    def test_business_status_requires_exact_int_or_string_zero(self):
        for code in (None, False, True, 0.0, 5, 8, "5", "00", "0.0", "０", [], {}):
            with self.subTest(code=code):
                out = self.fetch(payload(status_code=code, status_msg="offline-secret"))
                self.assert_failed(out)
                self.assertNotIn("offline-secret", json.dumps(out))
                self.assertNotIn("未登录", out["message"])

    def test_missing_group_falls_back_to_circle_metadata_without_claiming_cross_check(self):
        responses = [
            {"status_code": 0, "comment_list": []},
            payload(group_id=None),
            payload(group_id=""),
        ]
        for response in responses:
            with self.subTest(response=response):
                out = self.fetch(response)
                self.assertTrue(out["ok"])
                self.assertEqual(out["context"]["group_id"], CIRCLE["discuss_group_id"])
                self.assertEqual(out["comment_count"], 0)
                self.assertEqual(out["group_source"], "circle_meta")
                self.assertIn("沿用圈子资料", out["message"])
                self.assertIn("未由列表响应交叉核对", out["message"])

    def test_malformed_or_foreign_response_groups_fail(self):
        responses = [payload(group_id=value) for value in
                     ("123", True, 1.0, "２７９４１９４４４３０３３８", "1" * 33,
                      CIRCLE["discuss_group_id"] + "\n")]
        for response in responses:
            with self.subTest(response=response):
                self.assert_failed(self.fetch(response))

    def test_non_2xx_redirect_empty_and_non_json_fail_closed(self):
        responses = [
            httpx.Response(199, json=payload()),
            httpx.Response(302, json=payload(), headers={"Location": "https://evil.example/?token=offline-secret"}),
            httpx.Response(500, json=payload()),
            httpx.Response(204),
            httpx.Response(200, text="<html>offline-secret</html>"),
            httpx.Response(200, json=[payload()]),
            httpx.Response(200, json=payload(comment_list=["offline-secret"])),
            httpx.Response(200, json=payload(comment_list={"unexpected": True})),
        ]
        for response in responses:
            with self.subTest(status=response.status_code):
                out = self.fetch(response, headers={"Cookie": "sessionid=offline-secret"})
                self.assert_failed(out)
                self.assertEqual(len(self.requests), 1)
                self.assertNotIn("offline-secret", json.dumps(out))
                self.assertNotIn("evil.example", json.dumps(out))

    def test_missing_and_null_comment_lists_follow_official_empty_page_semantics(self):
        for response in ({"status_code": 0, "group_id": CIRCLE["discuss_group_id"]},
                         payload(comment_list=None)):
            with self.subTest(response=response):
                out = self.fetch(response)
                self.assertTrue(out["ok"])
                self.assertEqual(out["comment_count"], 0)

    def test_business_5_remains_business_error_without_auth_guess(self):
        out = self.fetch(payload(status_code=5, status_msg="private-token"))
        self.assert_failed(out)
        self.assertEqual(out["attempts"][0]["business_code"], 5)
        self.assertNotIn("登录", out["message"])
        self.assertNotIn("private-token", json.dumps(out))

    def test_network_error_only_includes_exception_class(self):
        def handler(request):
            raise httpx.ReadTimeout("private: " + request.headers["Cookie"], request=request)

        out = fetch_discussion_context(CIRCLE, headers={"Cookie": "sessionid=offline-secret"},
                                       transport=httpx.MockTransport(handler))
        self.assert_failed(out)
        self.assertIn("ReadTimeout", out["message"])
        self.assertNotIn("offline-secret", json.dumps(out))
        self.assertEqual(out["attempts"][0]["endpoint"], BASE_URL + DISCUSSION_PATH)
        self.assertNotIn("?", out["attempts"][0]["endpoint"])

    def test_credential_echo_in_success_response_is_discarded(self):
        out = self.fetch(payload(comment_list=[{"text": "offline-secret", "user": {"token": "auth-secret"}}],
                                status_msg="sessionid=offline-secret", token="auth-secret"),
                         headers={"Cookie": "sessionid=offline-secret", "Authorization": "Bearer auth-secret"})
        self.assertTrue(out["ok"])
        self.assertNotIn("offline-secret", json.dumps(out))
        self.assertNotIn("auth-secret", json.dumps(out))


if __name__ == "__main__":
    unittest.main()
