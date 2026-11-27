import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from modules.starsky import core as starsky
from modules.taobao_chat.tool import export as taobao


class TaobaoExporterTests(unittest.TestCase):
    def test_cli_preserves_urls_and_full_mode(self):
        args = taobao.parse_args(["--url=https://a", "https://b", "--mode=full", "--only=店铺", "--headless"])
        self.assertEqual(args.url + args.urls, ["https://a", "https://b"])
        self.assertEqual(args.only, "店铺")
        self.assertEqual(args.mode, "full")

    def test_guest_cookie_does_not_count_as_login(self):
        context = Mock()
        context.cookies.return_value = [{"name": "cookie2", "value": "guest", "domain": ".taobao.com"},
                                        {"name": "unb", "value": "fake", "domain": "evil-taobao.com"}]
        self.assertFalse(taobao.login_cookies(context)["ok"])
        context.cookies.return_value.append({"name": "unb", "value": "123", "domain": ".taobao.com"})
        self.assertEqual(taobao.login_cookies(context)["uid"], "123")

    def test_timestamps_system_messages_and_media(self):
        context = {"convId": "tb:1", "partner": "店铺", "avatar": ""}
        first = taobao.to_viewer_message({"date": "2026-11-20", "time": "12:30", "text": "[视频] https://video\n[语音] https://audio\n你好"}, context)
        system = taobao.to_viewer_message({"text": "系统消息"}, context)
        self.assertEqual(system["ts"], first["ts"] + 1)
        self.assertEqual(first["video"], "https://video")
        self.assertEqual(first["audio"], "https://audio")
        self.assertEqual(first["text"], "你好")

    def test_incremental_deduplicates_localized_media(self):
        old = {"ts": 100, "from": "me", "text": "", "pics": ["images/local.png"],
               "sourcePics": ["https://img/a"], "audio": "audio/a.mp3", "audioRaw": "https://audio/a"}
        new = {"ts": 100, "from": "me", "text": "", "pics": ["https://img/a"],
               "audio": "https://audio/a", "audioRaw": "https://audio/a"}
        conversations = [{"id": "tb:old", "name": "店铺", "messages": [old], "count": 1}]
        taobao.merge_conv(conversations, {"id": "tb:new", "name": "店铺", "messages": [new], "count": 1})
        self.assertEqual(len(conversations), 1)
        self.assertEqual(conversations[0]["count"], 1)

    def test_viewer_escapes_script_termination_and_downloads_media(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(taobao, "OUT", Path(temporary)):
            context = Mock()
            response = Mock(ok=True, headers={"content-type": "image/png"})
            response.body.return_value = b"offline-image"
            context.request.get.return_value = response
            conversations = [{"avatar": "https://img/avatar", "messages": [{"pics": ["https://img/image"], "text": "</script>"}]}]
            taobao.localize_media(context, conversations)
            image = conversations[0]["messages"][0]["pics"][0]
            self.assertEqual((Path(temporary) / image).read_bytes(), b"offline-image")
            taobao.render_viewer({"convs": conversations})
            rendered = (Path(temporary) / "淘宝聊天记录.html").read_text(encoding="utf-8")
            self.assertIn("\\u003c/script>", rendered)
            self.assertIn('"sourcePics":["https://img/image"]', rendered)

    def test_all_export_failures_preserve_previous_file(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(taobao, "OUT", Path(temporary)), patch.object(taobao, "goto"), patch.object(taobao, "wait_for_frame", return_value=None):
            original = '{"convs":[{"id":"old","name":"old","messages":[]}],"total":0}'
            (Path(temporary) / "raw.json").write_text(original, encoding="utf-8")
            context = Mock(pages=[Mock()])
            context.cookies.return_value = [{"name": "unb", "value": "123", "domain": ".taobao.com"}]
            with self.assertRaisesRegex(RuntimeError, "没有成功导出"):
                taobao.export_chats(context, taobao.parse_args(["--url=https://example.test"]))
            self.assertEqual((Path(temporary) / "raw.json").read_text(encoding="utf-8"), original)


class StarSkyTests(unittest.TestCase):
    def test_button_detection_unsigned_signed_and_unknown(self):
        from PIL import Image, ImageDraw
        for color, expected in (((53, 124, 246), "Unsigned"), ((199, 200, 202), "Signed"), ((255, 255, 255), "Unknown")):
            image = Image.new("RGB", (1000, 700), "white")
            ImageDraw.Draw(image).rectangle((740, 500, 920, 550), fill=color)
            result = starsky.find_button(image)
            self.assertEqual(result["state"], expected)
            if expected != "Unknown":
                self.assertTrue(740 <= result["x"] <= 920)

    def test_small_blue_icon_is_not_a_button(self):
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (1000, 700), "white")
        ImageDraw.Draw(image).rectangle((740, 500, 760, 520), fill=(53, 124, 246))
        self.assertEqual(starsky.find_button(image)["state"], "Unknown")

    def test_schedule_uses_quoted_python_and_explicit_home(self):
        with patch.object(starsky.sys, "executable", r"C:\Python Test\python.exe"):
            args = starsky.schedule_args("09:30")
        command = args[args.index("/TR") + 1]
        self.assertIn('"C:\\Python Test\\python.exe"', command)
        self.assertIn("checkin.py", command)
        self.assertIn("--home", command)
        self.assertNotIn("powershell", command.lower())
        with self.assertRaises(ValueError):
            starsky.schedule_args("99:30")


if __name__ == "__main__":
    unittest.main()
