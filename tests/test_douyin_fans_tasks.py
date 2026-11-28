"""Offline task-list normalization; no real credentials or account files."""
import json
from pathlib import Path
import sys
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.douyin_fans.fans_tasks import BASE_URL, TASK_PATH, fetch_tasks


LIVE = {"anchor_uid": "123456", "room_id": "", "fans_type": 1}


def payload(items):
    return {"status_code": 0, "data": {"user_task": {"items": items}}}


class FansTasksTests(unittest.TestCase):
    def test_exact_endpoint_params_cookie_and_actual_progress(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=payload([
                {"task_type": 2, "title": "观看直播", "description": "观看20分钟", "status": 2,
                 "completed_num": 0, "total_num": 20, "button": {"content": ""}},
            ]))
        out = fetch_tasks(LIVE, headers={"Cookie": "sessionid=offline-session"}, transport=httpx.MockTransport(handler))
        self.assertTrue(out["ok"])
        request = seen[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url.copy_with(query=None)), BASE_URL + TASK_PATH)
        self.assertEqual(dict(request.url.params), {
            "anchor_id": "123456", "anchor_open_id": "123456", "room_id": "",
            "source": "2", "fansclub_mode": "1", "aid": "1128", "device_platform": "android",
        })
        self.assertEqual(request.headers["Cookie"], "sessionid=offline-session")
        task = out["tasks"][0]
        self.assertEqual(task["id"], "live")
        self.assertEqual(task["progress"], {"completed_num": 0, "total_num": 20})
        self.assertEqual(task["status"], "进度 0 / 20")
        self.assertNotIn("已完成", task["status"])
        self.assertFalse(task["can_auto_checkin"])
        self.assertNotIn("offline-session", json.dumps(out))

    def test_paid_manual_and_checkin_types_never_execute(self):
        types = [1, 3, 9997, 11000, 11001, 11002, 12345]
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload([
            {"task_type": kind, "title": "官方任务", "description": "实际规则", "status": 2,
             "completed_num": 0, "total_num": 1, "button": {"content": "去处理", "schema": "aweme://private?token=not-returned"}}
            for kind in types
        ])))
        out = fetch_tasks(LIVE, transport=transport)
        self.assertEqual([task["id"] for task in out["tasks"]], ["light", "gift", "heat_checkin", "publish", "discussion", "invite", "http_task_12345"])
        self.assertEqual([task["kind"] for task in out["tasks"][:3]], ["paid", "paid", "checkin"])
        for task in out["tasks"]:
            self.assertEqual(task["source"], "official_http_snapshot")
            self.assertFalse(task["can_auto_checkin"])
            self.assertFalse(task["can_open"])
            self.assertEqual(task["manual_url"], "")
        self.assertEqual([task["can_open_entry"] for task in out["tasks"]],
                         [False, False, True, True, True, True, False])
        self.assertNotIn("not-returned", json.dumps(out))

    def test_status_two_is_not_completion_even_when_button_present(self):
        out = fetch_tasks(LIVE, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload([
            {"task_type": 11000, "title": "投稿", "status": 2, "completed_num": 0, "total_num": 1,
             "button": {"content": "去发布"}},
        ]))))
        self.assertEqual(out["tasks"][0]["status"], "进度 0 / 1 · 去发布")

    def test_empty_business_error_http_error_and_bad_schema_fail_closed(self):
        responses = [
            httpx.Response(200, content=b""),
            httpx.Response(200, json={"status_code": 8, "status_msg": "sessionid=offline-secret"}),
            httpx.Response(500, json=payload([])),
            httpx.Response(200, json={"status_code": 0, "data": {}}),
            httpx.Response(200, json=payload(["wrong-type"])),
            httpx.Response(200, text="<html>login</html>"),
            httpx.Response(200, json={"status_code": False, "data": {"user_task": {"items": []}}}),
        ]
        for response in responses:
            with self.subTest(status=response.status_code, body=response.content[:50]):
                out = fetch_tasks(LIVE, transport=httpx.MockTransport(lambda request, r=response: r))
                self.assertFalse(out["ok"])
                self.assertEqual(out["tasks"], [])
                self.assertNotIn("offline-secret", json.dumps(out))

    def test_redirect_is_not_followed_and_query_never_leaks(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={"Location": "https://example.com/?token=private"})
        out = fetch_tasks(LIVE, headers={"Cookie": "sessionid=offline-secret"}, transport=httpx.MockTransport(handler))
        self.assertFalse(out["ok"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(out["attempts"][0]["body_kind"], "redirect")
        self.assertEqual(out["attempts"][0]["endpoint"], BASE_URL + TASK_PATH)
        self.assertNotIn("?", out["attempts"][0]["endpoint"])
        self.assertNotIn("offline-secret", json.dumps(out))
        self.assertNotIn("example.com", json.dumps(out))

    def test_network_error_does_not_echo_request_or_credentials(self):
        def handler(request):
            raise httpx.ReadTimeout("private: " + request.headers["Cookie"], request=request)
        out = fetch_tasks(LIVE, headers={"Cookie": "sessionid=offline-secret"}, transport=httpx.MockTransport(handler))
        self.assertFalse(out["ok"])
        self.assertIn("ReadTimeout", out["message"])
        self.assertNotIn("offline-secret", json.dumps(out))

    def test_invalid_anchor_room_or_missing_mode_does_not_request(self):
        seen = []
        transport = httpx.MockTransport(lambda request: seen.append(request))
        for entry in [None, {}, {"anchor_uid": "bad", "fans_type": 1},
                      {"anchor_uid": "123", "room_id": "https://example.com", "fans_type": 1},
                      {"anchor_uid": "123"}, {"anchor_uid": "123", "fans_type": True}]:
            self.assertFalse(fetch_tasks(entry, transport=transport)["ok"])
        self.assertEqual(seen, [])

    def test_empty_valid_list_is_success_and_duplicate_types_remain_distinct(self):
        out = fetch_tasks(LIVE, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload([]))))
        self.assertTrue(out["ok"])
        duplicate = fetch_tasks(LIVE, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload([
            {"task_type": 5, "title": "任务甲"}, {"task_type": 5, "title": "任务乙"},
        ]))))
        self.assertEqual([task["id"] for task in duplicate["tasks"]], ["http_task_5", "http_task_5_2"])

    def test_supplied_credentials_are_redacted_if_echoed_in_task_text(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload([
            {"task_type": 5, "title": "server offline-secret", "description": "sessionid=offline-secret"},
        ])))
        out = fetch_tasks(LIVE, headers={"Cookie": "sessionid=offline-secret"}, transport=transport)
        self.assertTrue(out["ok"])
        self.assertNotIn("offline-secret", json.dumps(out))
        self.assertIn("已隐藏", out["tasks"][0]["title"])


if __name__ == "__main__":
    unittest.main()
