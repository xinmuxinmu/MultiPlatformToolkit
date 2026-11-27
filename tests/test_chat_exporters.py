import argparse
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from modules.chat_export import bilibili, weibo
from modules.chat_export.common import MediaStore, finish, merge_messages, read_baseline


class Response:
    def __init__(self, value=None, body=b"image", content_type="image/png", status=200):
        self.value, self._body, self.status = value, body, status
        self.ok = status == 200
        self.headers = {"content-type": content_type}

    def json(self):
        return self.value

    def body(self):
        return self._body


class ExporterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = Path(self.directory.name)
        self.tool = self.out / "tool"
        self.tool.mkdir()
        (self.tool / "template.html").write_text("<script>const DATA=__DATA__;</script>", encoding="utf-8")
        self.args = argparse.Namespace(mode="incremental", only_uid="", max_dm=100, max_group=100)
        self.baseline = {"fixed": True, "convs": [
            {"uid": "2", "name": "old", "isGroup": False, "avatar": "", "messages": [{"id": "8", "text": "old", "ts": 8000}]},
            {"uid": "3", "name": "unchanged", "isGroup": False, "avatar": "", "messages": [{"id": "1", "text": "keep", "ts": 1000}]}]}
        (self.out / "raw.json").write_text(json.dumps(self.baseline), encoding="utf-8")

    def weibo_session(self):
        def evaluate(script, url):
            path = urlsplit(url).path
            if "query_primary" in path:
                return {"profile": {"id": 1, "screen_name": "me"}}
            if "contacts" in path:
                return {"contacts": [{"user": {"id": 2, "name": "other"}}]}
            if "conversation" in path:
                cursor = parse_qs(urlsplit(url).query).get("max_id", ["0"])[0]
                return {"direct_messages": [] if cursor != "0" else [
                    {"id": 9, "text": "new", "created_at": "Thu, 01 Jan 1970 00:00:09 +0000", "sender_id": 2},
                    {"id": 8, "text": "refetched", "created_at": "Thu, 01 Jan 1970 00:00:08 +0000", "sender_id": 2},
                    {"id": 7, "text": "older", "created_at": "Thu, 01 Jan 1970 00:00:07 +0000", "sender_id": 2}]}
            raise AssertionError(url)
        return SimpleNamespace(out=self.out, page=SimpleNamespace(url=weibo.LOGIN_URL, evaluate=evaluate), ctx=SimpleNamespace(request=None))

    @patch("modules.chat_export.weibo.time.sleep")
    def test_weibo_full_ignores_known_baseline_and_fetches_older_history(self, _):
        self.args.mode = "full"
        payload = weibo.export(self.weibo_session(), self.args, self.tool)
        self.assertEqual([item["id"] for item in payload["convs"][0]["messages"]], ["7", "8", "9"])
        self.assertEqual(payload["convs"][0]["messages"][1]["text"], "refetched")

    @patch("modules.chat_export.weibo.time.sleep")
    def test_incremental_one_conversation_preserves_other_history(self, _):
        self.args.only_uid = "2"
        payload = weibo.export(self.weibo_session(), self.args, self.tool)
        convs = {item["uid"]: item for item in payload["convs"]}
        self.assertEqual([item["id"] for item in convs["2"]["messages"]], ["8", "9"])
        self.assertEqual(convs["3"]["messages"][0]["text"], "keep")

    @patch("modules.chat_export.weibo.time.sleep")
    def test_failed_fetch_does_not_replace_baseline(self, _):
        session = self.weibo_session()
        original = session.page.evaluate
        def evaluate(script, url):
            if "conversation" in url:
                raise RuntimeError("network error")
            return original(script, url)
        session.page.evaluate = evaluate
        before = (self.out / "raw.json").read_bytes()
        with self.assertRaises(RuntimeError):
            weibo.export(session, self.args, self.tool)
        self.assertEqual((self.out / "raw.json").read_bytes(), before)

    @patch("modules.chat_export.bilibili.time.sleep")
    def test_bilibili_group_type_and_incremental(self, _):
        def get(url, **kwargs):
            parts = urlsplit(url)
            params = parse_qs(parts.query)
            if url == bilibili.NAV_API:
                value = {"isLogin": True, "mid": 1, "uname": "me"}
            elif url.startswith(bilibili.SESSIONS_API):
                stype = params["session_type"][0]
                value = {"has_more": 0, "session_list": [{"talker_id": 2, "session_type": 2, "group_name": "group"}] if stype == "2" else []}
            elif "my_groups" in url:
                value = {"list": [{"group_id": 2, "group_name": "group"}]}
            elif url.startswith(bilibili.MESSAGES_API):
                self.assertEqual(params["session_type"], ["2"])
                value = {"has_more": 0, "messages": [
                    {"msg_seqno": 9, "timestamp": 9, "msg_type": 1, "content": '{"content":"new"}', "sender_uid": 1},
                    {"msg_seqno": 8, "timestamp": 8, "msg_type": 1, "content": '{"content":"refetch"}', "sender_uid": 1}]}
            elif "user/cards" in url:
                value = []
            else:
                raise AssertionError(url)
            return Response({"code": 0, "data": value})
        session = SimpleNamespace(out=self.out, ctx=SimpleNamespace(request=SimpleNamespace(get=get)))
        payload = bilibili.export(session, self.args, self.tool)
        self.assertTrue(payload["convs"][0]["isGroup"])
        self.assertEqual([item["id"] for item in payload["convs"][0]["messages"]], ["8", "9"])

    def test_bilibili_content_types_and_large_ids(self):
        self.assertEqual(bilibili.parse_content({"msg_type": 1, "content": '[{"text":"a"},{"content":"b"}]'})["text"], "ab")
        self.assertTrue(bilibili.parse_content({"msg_status": 4})["recall"])
        self.assertIn("BV123", bilibili.parse_content({"msg_type": 11, "content": '{"title":"a","bvid":"BV123"}'})["text"])
        messages = merge_messages([{"id": "9007199254740993", "ts": 1}], [{"id": "9007199254740992", "ts": 1}])
        self.assertEqual(len(messages), 2)

    def test_media_download_cache_and_html_rejection(self):
        calls = []
        def get(url, **kwargs):
            calls.append(url)
            return Response(content_type="text/html" if "login" in url else "image/png")
        store = MediaStore(self.out, SimpleNamespace(get=get))
        first = store.image("https://example.test/image")
        self.assertTrue(first.endswith(".png"))
        self.assertEqual(store.image("https://example.test/image"), first)
        self.assertEqual(len(calls), 1)
        self.assertEqual(store.image("https://example.test/login"), "")
        self.assertEqual(store.image("file:///private"), "")

    def test_bad_baseline_is_not_silently_discarded(self):
        (self.out / "raw.json").write_text("broken", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            read_baseline(self.out)

    def test_html_data_escapes_script_end(self):
        payload = finish(self.out, self.tool / "template.html", "viewer.html", {}, [{"uid": "1", "messages": [{"id": "1", "text": "</script>", "ts": 0}]}])
        self.assertEqual(payload["total"], 1)
        self.assertIn("\\u003c/script>", (self.out / "viewer.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
