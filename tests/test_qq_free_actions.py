"""小勋播放器免费动作离线测试：只使用假接口，不读账号、不请求网络。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.qq_tasks.free_actions import (ActionError, COMPANION_TASK_TYPE,
                                         parse_companion_target, select_free_companion)


URL = "https://y.qq.com/n3/client/pages/ai_player_companion/index.html?roleId=17&bizId=singer_42"


def task(url=URL):
    return {"TaskType": COMPANION_TASK_TYPE, "BtnJumpURL": url}


def home(*, wearing=False, role=0, biz="", current=""):
    return {"wearing": {"isWearing": wearing, "roleId": role, "bizID": biz},
            "ext": {"curBizID": current}, "currRoleInfo": {"roleID": role}}


def atlas(*, in_use=False):
    return {"unlockable": True, "roleIsUse": in_use, "payConfig": {"isNeedPay": False}}


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def _medal_call(self, module, method, params):
        self.calls.append((module, method, deepcopy(params)))
        if not self.replies:
            raise AssertionError("unexpected call")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def writes(self):
        return [call for call in self.calls if call[1] == "SelectAiRole"]


class CompanionActionTests(unittest.TestCase):
    def successful_replies(self):
        return [(0, home()), (0, atlas()), (0, None), (0, atlas(in_use=True)),
                (0, home(wearing=True, role=17, biz="singer_42", current="singer_42"))]

    def test_valid_direct_and_wrapped_official_targets(self):
        for url in (URL, URL.replace("&", "&amp;"),
                    "qqmusic://qq.com/ui/openUrl?url=" + quote(URL, safe=""),
                    "qqmusic://qq.com/ui/openUrl?weburl=" + quote(URL, safe=""),
                    "qqmusic://open?url=" + quote("qqmusic://open?url=" + quote(URL, safe=""), safe=""),
                    URL.replace("y.qq.com", "i.y.qq.com")):
            with self.subTest(url=url):
                self.assertEqual(parse_companion_target(task(url)), (17, "singer_42"))

    def test_invalid_urls_or_parameters_never_call_account(self):
        urls = [URL.replace("y.qq.com", "y.qq.com.evil.example"),
                URL.replace("y.qq.com", "evil.qq.com"),
                URL.replace("y.qq.com", "user:password@y.qq.com"),
                URL.replace("y.qq.com", "y.qq.com:444"),
                URL.replace("index.html", "other.html"),
                URL.replace("ai_player_companion", "other_activity"),
                URL.replace("roleId=17", "roleId=0"),
                URL.replace("roleId=17", "roleId=-1"),
                URL.replace("roleId=17", "roleId=true"),
                URL.replace("roleId=17", "roleId=2147483648"),
                URL + "&roleId=18", URL + "&bizId=other", URL.replace("singer_42", "a" * 129),
                URL.replace("singer_42", "bad%2Fid"), URL.replace("roleId=17", "id=17"),
                "file:///" + URL, "custom://open?url=" + quote(URL, safe=""),
                "qqmusic://open?url=" + quote(URL, safe="") + "&weburl=" + quote(URL, safe=""),
                URL.replace("y.qq.com", "y.qq.com\n.evil.example")]
        for url in urls:
            with self.subTest(url=url):
                client = FakeClient([])
                with self.assertRaises(ActionError) as caught:
                    select_free_companion(client, task(url))
                self.assertEqual(caught.exception.code, "invalid_target")
                self.assertFalse(caught.exception.changed)
                self.assertEqual(client.calls, [])

    def test_other_task_type_is_rejected(self):
        value = task()
        value["TaskType"] = "unknown"
        with self.assertRaises(ActionError):
            parse_companion_target(value)

    def test_free_selection_writes_once_and_verifies_both_states(self):
        client = FakeClient(self.successful_replies())
        result = select_free_companion(client, task())
        self.assertEqual(result, {"ok": True, "changed": True, "verified": True,
                                  "already_selected": False, "role_id": 17, "biz_id": "singer_42"})
        self.assertEqual([call[1] for call in client.calls],
                         ["GetHomePageInfo", "QueryAtlasV2", "SelectAiRole", "QueryAtlasV2", "GetHomePageInfo"])
        self.assertEqual(client.calls[0], ("music.aiPlayerCompanion.AiPlayerCompanionHomepageSrv",
                                         "GetHomePageInfo", {"roleId": 17, "needRoleList": False,
                                                             "companionType": 0, "bizID": "singer_42"}))
        self.assertEqual(client.calls[1][0], "music.aiPlayerCompanion.AiPlayerCompanionScoreSrv")
        self.assertEqual(client.writes(), [("music.aiPlayerCompanion.AiPlayerCompanionRoleSrv",
                                           "SelectAiRole", {"roleId": 17, "Inviter": "", "bizID": "singer_42"})])

    def test_already_using_matching_artist_does_not_write(self):
        client = FakeClient([(0, home(wearing=True, role=17, biz="singer_42", current="singer_42")),
                             (0, atlas(in_use=True))])
        result = select_free_companion(client, task())
        self.assertTrue(result["already_selected"])
        self.assertFalse(result["changed"])
        self.assertTrue(result["verified"])
        self.assertEqual(client.writes(), [])

    def test_paid_unknown_or_not_unlockable_is_never_selected(self):
        cases = [{"unlockable": True, "roleIsUse": False},
                 {"unlockable": True, "roleIsUse": False, "payConfig": {}},
                 {"unlockable": True, "roleIsUse": False, "payConfig": {"isNeedPay": True}},
                 {"unlockable": True, "roleIsUse": False, "payConfig": {"isNeedPay": 0}},
                 {"unlockable": True, "roleIsUse": False, "payConfig": {"isNeedPay": "false"}},
                 {"unlockable": False, "roleIsUse": False, "payConfig": {"isNeedPay": False}},
                 {"unlockable": 1, "roleIsUse": False, "payConfig": {"isNeedPay": False}},
                 {"roleIsUse": False, "payConfig": {"isNeedPay": False}},
                 {"unlockable": True, "payConfig": {"isNeedPay": False}},
                 {"unlockable": True, "roleIsUse": "false", "payConfig": {"isNeedPay": False}}]
        for value in cases:
            with self.subTest(value=value):
                client = FakeClient([(0, home()), (0, value)])
                with self.assertRaises(ActionError) as caught:
                    select_free_companion(client, task())
                self.assertFalse(caught.exception.changed)
                self.assertEqual(client.writes(), [])

    def test_other_artist_binding_never_changes_even_when_role_not_in_use(self):
        for value in (home(current="other_singer"), home(biz="other_singer"),
                      home(wearing=True, role=17, biz="other_singer", current="singer_42")):
            with self.subTest(value=value):
                client = FakeClient([(0, value), (0, atlas())])
                with self.assertRaises(ActionError) as caught:
                    select_free_companion(client, task())
                self.assertEqual(caught.exception.code, "binding_conflict")
                self.assertEqual(client.writes(), [])

    def test_missing_binding_fields_fail_closed(self):
        cases = [{}, {"ext": {}, "wearing": {}}, home()]
        del cases[-1]["wearing"]["bizID"]
        for value in cases:
            with self.subTest(value=value):
                client = FakeClient([(0, value), (0, atlas())])
                with self.assertRaises(ActionError) as caught:
                    select_free_companion(client, task())
                self.assertEqual(caught.exception.code, "missing_binding")
                self.assertEqual(client.writes(), [])

    def test_no_biz_id_omits_select_parameter_but_still_checks_binding(self):
        client = FakeClient([(0, home()), (0, atlas()), (0, {}),
                             (0, atlas(in_use=True)), (0, home(wearing=True, role=17))])
        result = select_free_companion(client, task(URL.split("&bizId=")[0]))
        self.assertEqual(result["biz_id"], "")
        self.assertEqual(client.writes()[0][2], {"roleId": 17, "Inviter": ""})

    def test_nonzero_or_malformed_read_response_never_writes(self):
        for reply in ((1, home()), (False, home()), ("0", home()), (0, None),
                      {"code": 0}, (0, [])):
            with self.subTest(reply=reply):
                client = FakeClient([reply])
                with self.assertRaises(ActionError):
                    select_free_companion(client, task())
                self.assertEqual(client.writes(), [])
        client = FakeClient([(0, home()), (7, atlas())])
        with self.assertRaises(ActionError):
            select_free_companion(client, task())
        self.assertEqual(client.writes(), [])

    def test_select_error_does_not_retry_and_marks_write_attempt(self):
        for reply in ((8, None), RuntimeError("private-offline-error")):
            with self.subTest(reply=type(reply).__name__):
                client = FakeClient([(0, home()), (0, atlas()), reply])
                with self.assertRaises(ActionError) as caught:
                    select_free_companion(client, task())
                self.assertTrue(caught.exception.changed)
                self.assertNotIn("private-offline-error", str(caught.exception))
                self.assertEqual(len(client.writes()), 1)
                self.assertEqual(len(client.calls), 3)

    def test_failed_post_write_verification_is_not_success_or_retried(self):
        cases = [(0, atlas()), (0, {"roleIsUse": True}), (9, atlas()),
                 RuntimeError("private-offline-error")]
        for reply in cases:
            with self.subTest(reply=type(reply).__name__):
                client = FakeClient([(0, home()), (0, atlas()), (0, {}), reply,
                                     (0, home(wearing=True, role=17, biz="singer_42"))])
                with self.assertRaises(ActionError) as caught:
                    select_free_companion(client, task())
                self.assertTrue(caught.exception.changed)
                self.assertEqual(len(client.writes()), 1)

    def test_post_write_wrong_role_or_artist_fails(self):
        for value in (home(wearing=True, role=18, biz="singer_42"),
                      home(wearing=True, role=17, biz="other_singer"), home()):
            with self.subTest(value=value):
                client = FakeClient([(0, home()), (0, atlas()), (0, {}),
                                     (0, atlas(in_use=True)), (0, value)])
                with self.assertRaises(ActionError) as caught:
                    select_free_companion(client, task())
                self.assertTrue(caught.exception.changed)
                self.assertEqual(len(client.writes()), 1)


if __name__ == "__main__":
    unittest.main()
