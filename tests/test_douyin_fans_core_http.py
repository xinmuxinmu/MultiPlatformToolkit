"""HTTP wiring tests for the authenticated circle refresh path."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.douyin_fans import core


FANDOM = "27933289767426"
COOKIE_VALUES = {
    "sessionid": "test-session",
    "sessionid_ss": "test-session-ss",
    "sid_tt": "test-sid-tt",
    "sid_guard": "test-sid-guard",
    "uid_tt": "test-uid-tt",
    "uid_tt_ss": "test-uid-tt-ss",
}


def cookie_rows():
    return [
        {"name": name, "value": value, "domain": "douyin.com"}
        for name, value in COOKIE_VALUES.items()
    ] + [
        {"name": "sessionid", "value": "wrong-domain", "domain": "sub.douyin.com"},
        {"name": "web_id", "value": "not-required", "domain": "douyin.com"},
        {"name": "sessionid", "value": "wrong-host", "domain": "iesdouyin.com"},
    ]


class FansCoreHttpTests(unittest.TestCase):
    def test_native_circle_credentials_never_reach_other_official_hosts(self):
        circle = {"fandom_id": FANDOM, "title": "测试圈子",
                  "discuss_group_id": "27941944430338",
                  "live_fans_club_info": {"anchor_uid": "123456", "room_id": "", "fans_type": 1}}
        response = {
            "authenticated": True, "circle": circle, "join_status": 1, "signed_today": False,
            "probe": {"checked_at": "2026-11-27T12:00:00+08:00", "attempts": []},
            "result": {"status": "observed_http", "verified": False, "message": "只读刷新。"},
        }
        web = {"cookies": [], "headers": {"Cookie": "sessionid=offline-web-secret"},
               "source": "shared_account_session"}
        native = {"cookies": [{"name": "sessionid", "value": "offline-native-secret",
                               "domain": "fandom.starplaylet.com", "path": "/"}],
                  "headers": {"User-Agent": "Offline native"}, "common_params": {"aid": 1128},
                  "source": "account_bound_native_circle_session"}
        config = {"account_id": "selected-account", "artist": "测试",
                  "circle_url": "https://www.douyin.com/", "_account_resolved": True,
                  "native_auth_file": "offline-native-session.json"}
        with (
            patch.object(core, "_http_credentials", return_value=web),
            patch.object(core, "shared_accounts", return_value={"accounts": [
                {"id": "selected-account", "uid": "123456"}]}),
            patch.object(core, "_write_json") as write_json,
            patch("modules.douyin_fans.credentials.load_circle_session", return_value=native) as load_native,
            patch("modules.douyin_fans.http_client.fetch_circle", return_value=response) as fetch_circle,
            patch("modules.douyin_fans.fans_tasks.fetch_tasks", return_value={
                "ok": True, "tasks": [], "attempts": [], "message": "只读任务。"}) as fetch_tasks,
            patch("modules.douyin_fans.discussion.fetch_discussion_context", return_value={
                "ok": False, "context": {}, "attempts": [], "message": "尚未确认。"}) as discussion,
        ):
            result = core._perform_http(config, {"fandom_id": FANDOM}, checkin=False)

        load_native.assert_called_once_with("offline-native-session.json", account_id="selected-account",
                                            expected_uid="123456")
        fetch_circle.assert_called_once_with(FANDOM, account_id="selected-account",
                                             cookies=native["cookies"], headers=native["headers"],
                                             common_params={"aid": 1128}, checkin=False)
        fetch_tasks.assert_called_once_with(circle["live_fans_club_info"],
                                           cookies=[], headers=web["headers"])
        discussion.assert_called_once_with(circle, headers=web["headers"])
        snapshot = write_json.call_args.args[1]
        self.assertEqual(snapshot["http_probe"]["credential_source"], native["source"])
        self.assertNotIn("offline-native-secret", str(snapshot))
        self.assertNotIn("offline-web-secret", str(snapshot))
        self.assertEqual(result["status"], "observed_http")

    def test_account_switch_rejects_previous_accounts_native_session(self):
        from types import SimpleNamespace
        import json

        config = {"account_id": "next-account", "artist": "测试",
                  "circle_url": "https://www.douyin.com/", "_account_resolved": True,
                  "native_auth_file": "offline-native-session.json"}
        old_session = {"account_id": "previous-account", "uid": "111111", "verified": True,
                       "identity_match": True, "cookies": [{"name": "sessionid", "value": "old-native-secret",
                       "domain": "fandom.starplaylet.com", "path": "/"}], "common_params": {"aid": 1128}}
        response = {"authenticated": False, "circle": {}, "join_status": None, "signed_today": None,
                    "probe": {"checked_at": "2026-11-27T12:00:00+08:00", "attempts": []},
                    "result": {"status": "authentication_required", "verified": False, "message": "未登录。"}}
        web = {"cookies": [], "headers": {"Cookie": "sessionid=next-web-secret"},
               "source": "shared_account_session"}
        with (
            patch.object(core, "_http_credentials", return_value=web),
            patch.object(core, "shared_accounts", return_value={"accounts": [
                {"id": "previous-account", "uid": "111111"}, {"id": "next-account", "uid": "222222"}]}),
            patch.object(core, "_write_json") as write_json,
            patch("modules.douyin_fans.credentials.Path") as path,
            patch("modules.douyin_fans.http_client.fetch_circle", return_value=response) as fetch_circle,
            patch("modules.douyin_fans.fans_tasks.fetch_tasks") as fetch_tasks,
            patch("modules.douyin_fans.discussion.fetch_discussion_context") as discussion,
        ):
            path.return_value.stat.return_value = SimpleNamespace(st_size=500)
            path.return_value.read_text.return_value = json.dumps(old_session)
            result = core._perform_http(config, {"fandom_id": FANDOM}, checkin=False)

        fetch_circle.assert_called_once_with(FANDOM, account_id="next-account", cookies=[],
                                            headers=web["headers"], checkin=False)
        fetch_tasks.assert_not_called()
        discussion.assert_not_called()
        self.assertEqual(write_json.call_args.args[1]["http_probe"]["credential_source"], web["source"])
        self.assertEqual(result["status"], "authentication_required")

    def test_http_credentials_allowlist_exact_domain_and_names(self):
        with patch.object(core, "_account_cookies", return_value=cookie_rows()):
            credentials = core._http_credentials({"account_id": "selected-account"})

        expected = "; ".join(f"{name}={value}" for name, value in COOKIE_VALUES.items())
        self.assertEqual(credentials, {
            "cookies": [],
            "headers": {"Cookie": expected},
            "source": "shared_account_session",
        })

    def test_refresh_wires_pinned_account_credentials_to_circle_and_task_gets(self):
        circle_response = {
            "authenticated": True,
            "circle": {
                "fandom_id": FANDOM,
                "title": "测试圈子",
                "live_fans_club_info": {"anchor_uid": "123456", "room_id": "", "fans_type": 1},
            },
            "join_status": 1,
            "signed_today": False,
            "probe": {"checked_at": "2026-11-27T12:00:00+08:00", "attempts": []},
            "result": {"status": "observed_http", "verified": False, "message": "只读刷新。"},
        }
        task_response = {
            "ok": True,
            "tasks": [{"id": "live", "title": "观看直播", "kind": "playback", "status": "进度 0 / 20"}],
            "attempts": [{"method": "GET", "endpoint": "https://webcast.amemv.com/webcast/fansclub/homepage/"}],
            "message": "读取到官方任务。",
        }
        expected_header = "; ".join(f"{name}={value}" for name, value in COOKIE_VALUES.items())
        config = {"account_id": "selected-account", "artist": "测试", "circle_url": "https://www.douyin.com/", "_account_resolved": True}
        entry = {"fandom_id": FANDOM, "circle_title": "测试圈子"}

        with (
            patch.object(core, "_account_cookies", return_value=cookie_rows()),
            patch.object(core, "_write_json"),
            patch("modules.douyin_fans.http_client.fetch_circle", return_value=circle_response) as fetch_circle,
            patch("modules.douyin_fans.fans_tasks.fetch_tasks", return_value=task_response) as fetch_tasks,
        ):
            result = core._perform_http(config, entry, checkin=False)

        self.assertEqual(result["status"], "observed_http")
        fetch_circle.assert_called_once_with(
            FANDOM, account_id="selected-account", cookies=[],
            headers={"Cookie": expected_header}, checkin=False,
        )
        fetch_tasks.assert_called_once_with(
            circle_response["circle"]["live_fans_club_info"],
            cookies=[], headers={"Cookie": expected_header},
        )
        self.assertEqual(circle_response["probe"]["credential_source"], "shared_account_session")
        self.assertTrue(circle_response["probe"]["authenticated"])
        self.assertEqual(circle_response["probe"]["task_count"], 1)

    def test_refresh_uses_native_webcast_domain_cookies_for_task_get(self):
        circle = {"fandom_id": FANDOM, "title": "测试圈子",
                  "live_fans_club_info": {"anchor_uid": "123456", "room_id": "", "fans_type": 1}}
        response = {
            "authenticated": True, "circle": circle, "join_status": 2, "signed_today": True,
            "probe": {"checked_at": "2026-11-27T12:00:00+08:00", "attempts": []},
            "result": {"status": "already", "verified": True, "message": "已签到。"},
        }
        circle_credentials = {"cookies": [{"domain": "fandom.starplaylet.com"}],
                              "headers": {}, "common_params": {},
                              "source": "account_bound_native_circle_session"}
        task_credentials = {"cookies": [{"domain": "webcast.amemv.com"}],
                            "headers": {"User-Agent": "Offline native"},
                            "common_params": {},
                            "source": "account_bound_native_webcast_session"}
        config = {"account_id": "selected-account", "artist": "测试",
                  "circle_url": "https://www.douyin.com/", "_account_resolved": True,
                  "native_auth_file": "offline-auth.json",
                  "native_cookie_store_file": "offline-store.xml"}
        with (
            patch.object(core, "_http_credentials", return_value={
                "cookies": [], "headers": {"Cookie": "wrong-domain-session"},
                "source": "shared_account_session",
            }),
            patch.object(core, "shared_accounts", return_value={"accounts": [
                {"id": "selected-account", "uid": "123456"}]}),
            patch.object(core, "_write_json"),
            patch("modules.douyin_fans.credentials.load_circle_session",
                  return_value=circle_credentials),
            patch("modules.douyin_fans.credentials.load_webcast_session",
                  return_value=task_credentials) as load_webcast,
            patch("modules.douyin_fans.http_client.fetch_circle", return_value=response) as fetch_circle,
            patch("modules.douyin_fans.fans_tasks.fetch_tasks", return_value={
                "ok": True, "tasks": [], "attempts": [], "message": "任务读取成功。",
            }) as fetch_tasks,
        ):
            core._perform_http(config, {"fandom_id": FANDOM}, checkin=False)

        load_webcast.assert_called_once_with("offline-auth.json", "offline-store.xml",
                                             account_id="selected-account", expected_uid="123456")
        fetch_circle.assert_called_once_with(FANDOM, account_id="selected-account",
                                             cookies=circle_credentials["cookies"],
                                             headers=circle_credentials["headers"],
                                             common_params={}, checkin=False)
        fetch_tasks.assert_called_once_with(circle["live_fans_club_info"],
                                           cookies=task_credentials["cookies"],
                                           headers=task_credentials["headers"])
        self.assertEqual(response["probe"]["task_credential_source"], task_credentials["source"])

    def test_readonly_refresh_preserves_safe_discussion_group_in_snapshot(self):
        circle = {"fandom_id": FANDOM, "title": "测试圈子",
                  "discuss_group_id": "27941944430338"}
        circle_response = {
            "authenticated": True, "circle": circle,
            "join_status": 1, "signed_today": False,
            "probe": {"checked_at": "2026-11-27T12:00:00+08:00", "attempts": []},
            "result": {"status": "observed_http", "verified": False, "message": "只读刷新。"},
        }
        config = {"account_id": "selected-account", "artist": "测试",
                  "circle_url": "https://www.douyin.com/", "_account_resolved": True}
        credentials = {"cookies": [], "headers": {}, "source": "anonymous"}
        context = {"business_id": FANDOM, "group_id": "27941944430338",
                   "discuss_scene": "fandom"}

        with (
            patch.object(core, "_http_credentials", return_value=credentials),
            patch.object(core, "_write_json") as write_json,
            patch("modules.douyin_fans.http_client.fetch_circle", return_value=circle_response) as fetch_circle,
            patch("modules.douyin_fans.fans_tasks.fetch_tasks") as fetch_tasks,
            patch("modules.douyin_fans.discussion.fetch_discussion_context", return_value={
                "ok": True, "context": context, "attempts": [], "message": "只读核对通过。",
            }) as fetch_discussion,
        ):
            result = core._perform_http(config, {"fandom_id": FANDOM}, checkin=False)

        fetch_circle.assert_called_once_with(
            FANDOM, account_id="selected-account", cookies=[], headers={}, checkin=False,
        )
        fetch_tasks.assert_not_called()
        fetch_discussion.assert_called_once_with(circle, headers={})
        write_json.assert_called_once()
        snapshot = write_json.call_args.args[1]
        self.assertEqual(snapshot["circle"], circle)
        self.assertEqual(snapshot["circle"]["discuss_group_id"], "27941944430338")
        self.assertEqual(snapshot["discussion_context"], context)
        self.assertTrue(snapshot["http_probe"]["discussion_context_available"])
        discussion_task = next(task for task in snapshot["tasks"] if task["id"] == "discussion")
        self.assertEqual(discussion_task["source"], "user_screenshot")
        self.assertEqual(discussion_task["status"], "未同步")
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "observed_http")

    def test_failed_discussion_get_does_not_discard_verified_sign_in(self):
        circle = {"fandom_id": FANDOM, "discuss_group_id": "27941944430338"}
        circle_response = {
            "authenticated": True, "circle": circle, "join_status": 2, "signed_today": True,
            "probe": {"checked_at": "2026-11-27T12:00:00+08:00", "attempts": []},
            "result": {"status": "already", "verified": True, "message": "已签到。"},
        }
        config = {"account_id": "selected-account", "artist": "测试",
                  "circle_url": "https://www.douyin.com/", "_account_resolved": True}
        attempt = {"method": "GET", "endpoint": "https://aweme.snssdk.com/aweme/v1/medium/loadmore/comments/",
                   "http_status": 200, "business_code": 5}
        with (
            patch.object(core, "_http_credentials", return_value={
                "cookies": [], "headers": {}, "source": "anonymous",
            }),
            patch.object(core, "_write_json") as write_json,
            patch("modules.douyin_fans.http_client.fetch_circle", return_value=circle_response),
            patch("modules.douyin_fans.discussion.fetch_discussion_context", return_value={
                "ok": False, "context": {}, "attempts": [attempt], "message": "业务请求未通过。",
            }),
        ):
            result = core._perform_http(config, {"fandom_id": FANDOM}, checkin=False)

        snapshot = write_json.call_args.args[1]
        self.assertEqual(snapshot["discussion_context"], {})
        self.assertFalse(snapshot["http_probe"]["discussion_context_available"])
        self.assertEqual(snapshot["http_probe"]["attempts"], [attempt])
        self.assertEqual(result, circle_response["result"])
        task = next(task for task in snapshot["tasks"] if task["id"] == "heat_checkin")
        self.assertTrue(task["progress"]["signed_today"])
        self.assertFalse(task["can_auto_checkin"])

    def test_invalid_circle_id_stops_before_network_or_cookie_loading(self):
        with (
            patch.object(core, "_account_cookies") as load_cookies,
            patch("modules.douyin_fans.http_client.fetch_circle") as fetch_circle,
        ):
            result = core._perform_http({"account_id": "selected-account"}, {"fandom_id": "https://example.com"})

        self.assertEqual(result["status"], "invalid_circle")
        load_cookies.assert_not_called()
        fetch_circle.assert_not_called()

    def test_failed_auth_does_not_request_discussions_or_reuse_context(self):
        circle_response = {
            "authenticated": False,
            "circle": {"fandom_id": FANDOM, "discuss_group_id": "27941944430338"},
            "join_status": None, "signed_today": None,
            "probe": {"checked_at": "2026-11-27T12:00:00+08:00", "attempts": []},
            "result": {"status": "authentication_required", "verified": False, "message": "未登录。"},
        }
        config = {"account_id": "selected-account", "artist": "测试",
                  "circle_url": "https://www.douyin.com/", "_account_resolved": True}
        with (
            patch.object(core, "_http_credentials", return_value={
                "cookies": [], "headers": {}, "source": "anonymous",
            }),
            patch.object(core, "_write_json") as write_json,
            patch("modules.douyin_fans.http_client.fetch_circle", return_value=circle_response),
            patch("modules.douyin_fans.discussion.fetch_discussion_context") as fetch_discussion,
        ):
            result = core._perform_http(config, {"fandom_id": FANDOM}, checkin=False)

        fetch_discussion.assert_not_called()
        snapshot = write_json.call_args.args[1]
        self.assertEqual(snapshot["discussion_context"], {})
        self.assertIsNone(snapshot["observed_at"])
        self.assertFalse(snapshot["http_probe"]["authenticated"])
        self.assertEqual(result["status"], "authentication_required")


if __name__ == "__main__":
    unittest.main()
