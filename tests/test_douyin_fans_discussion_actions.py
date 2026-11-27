"""Offline protocol checks for one-shot discussion submission."""
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.douyin_fans.discussion_actions import (
    PUBLISH_PATH, SELF_PATH, VERIFY_PATH, publish_discussion,
)


CONTEXT = {"business_id": "27933289767426", "group_id": "27941944430338"}
TEXT = "祝菌菌每天开心，期待更多精彩作品！"


class DiscussionSubmissionTests(unittest.TestCase):
    def run_submission(self, responses, *, uid="123456", context=CONTEXT):
        requests = []

        def handler(request):
            requests.append(request)
            response = responses[len(requests) - 1]
            return response if isinstance(response, httpx.Response) else httpx.Response(200, json=response)

        result = publish_discussion(
            context, TEXT, expected_uid=uid,
            cookies=[{"name": "sessionid", "value": "offline-secret",
                      "domain": ".aweme.snssdk.com", "path": "/"}],
            common_params={"aid": 1128, "unknown": "discard-me"},
            transport=httpx.MockTransport(handler),
        )
        return result, requests

    def test_submit_once_with_bound_identity_then_get_readback(self):
        result, requests = self.run_submission([
            {"status_code": 0, "user": {"uid": "123456"}},
            {"status_code": 0, "comment": {"cid": "778899",
                                                   "user": {"uid": "123456"}}},
            {"status_code": 0, "comment_list": [{"cid": "778899", "text": TEXT}]},
        ])
        self.assertEqual([request.method for request in requests], ["GET", "POST", "GET"])
        self.assertTrue(requests[0].url.path.endswith(SELF_PATH))
        self.assertTrue(requests[1].url.path.endswith(PUBLISH_PATH))
        self.assertTrue(requests[2].url.path.endswith(VERIFY_PATH))
        form = parse_qs(requests[1].content.decode())
        self.assertEqual(form["text"], [TEXT])
        self.assertEqual(form["business_id"], [CONTEXT["business_id"]])
        self.assertEqual(form["business_scene"], ["fandom"])
        self.assertEqual(form["group_id"], [CONTEXT["group_id"]])
        self.assertEqual(form["channel_obj_id"], [
            '{"business_id":"27933289767426","discuss_scene":"fandom"}'])
        self.assertEqual(parse_qs(requests[2].url.query.decode())["discuss_id_list"], ["778899"])
        self.assertTrue(result["submitted"])
        self.assertTrue(result["read_back"])
        self.assertFalse(result["task_completed"])
        self.assertEqual(result["comment_id"], "778899")

    def test_identity_mismatch_stops_before_public_post(self):
        result, requests = self.run_submission([
            {"status_code": 0, "user": {"uid": "999999"}},
        ])
        self.assertEqual([request.method for request in requests], ["GET"])
        self.assertFalse(result["submitted"])
        self.assertEqual(result["status"], "not_submitted")

    def test_ambiguous_post_is_not_retried(self):
        result, requests = self.run_submission([
            {"status_code": 0, "user": {"uid": "123456"}},
            httpx.Response(500, json={"status_code": 0}),
        ])
        self.assertEqual([request.method for request in requests], ["GET", "POST"])
        self.assertFalse(result["submitted"])
        self.assertEqual(result["status"], "unconfirmed")

    def test_invalid_context_stops_without_request(self):
        result, requests = self.run_submission([], context={"business_id": CONTEXT["business_id"]})
        self.assertEqual(requests, [])
        self.assertEqual(result["status"], "not_submitted")


if __name__ == "__main__":
    unittest.main()
