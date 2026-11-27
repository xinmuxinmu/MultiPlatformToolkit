"""Synthetic-only tests for offline Python chat screenshot rendering."""
import base64
import gc
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

DOUYIN_ROOT = Path(__file__).resolve().parents[1] / "modules" / "douyin_chat"
if str(DOUYIN_ROOT) not in sys.path:
    sys.path.insert(0, str(DOUYIN_ROOT))

from dy_backend import image_renderer as renderer
from dy_backend import screenshot


def payload_message(payload, **fields):
    return {"msg_id": "test", "msg_type": 1, "sender_name": "测试发送者",
            "sender_uid": "1", "timestamp": 1700000000,
            "raw_data": json.dumps({"content_json": json.dumps(payload, ensure_ascii=False)}, ensure_ascii=False),
            **fields}


class PythonImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.media = Path(self.temp.name)

    def test_chinese_text_reply_forward_and_image_render_to_real_png(self):
        picture = self.media / "picture.png"
        Image.new("RGB", (80, 60), "#ff0000").save(picture)
        messages = [
            {"sender_name": "张三", "sender_uid": "1", "content": "你好，这是一条离线聊天记录。\n支持换行。",
             "timestamp": 1700000000, "msg_type": 1, "ref_msg": json.dumps({"nickname": "李四", "content": "引用正文"})},
            {"sender_name": "李四", "sender_uid": "2", "content": "[图片]", "msg_type": 3,
             "media_local_path": "picture.png"},
            payload_message({"aweType": 13600, "title": "合并聊天"},
                            forward_detail={"title": "转发记录", "total": 1,
                                            "items": [{"content": "转发里的正文", "sender_name": "王五"}]}),
        ]
        png = renderer.render_chat_png(messages, media_dir=self.media, title="测试会话", self_uid="1")
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.width, 1040)
            self.assertGreater(image.height, 400)
            self.assertLessEqual(image.width * image.height, renderer.MAX_PIXELS)
            colors = image.getcolors(image.width * image.height)
            self.assertTrue(any(color == (255, 0, 0) for count, color in colors))

    def test_media_never_reads_remote_or_outside_account_root(self):
        self.assertIsNone(renderer._local_image("https://example.invalid/private.png", self.media))
        with tempfile.TemporaryDirectory() as outside:
            path = Path(outside) / "private.png"
            Image.new("RGB", (8, 8), "red").save(path)
            self.assertIsNone(renderer._local_image(str(path), self.media))
        self.assertIsNone(renderer._local_image("../../Windows/system.ini", self.media))

    def test_inline_thumbnail_decodes_and_corrupt_media_degrades_gracefully(self):
        raw = io.BytesIO()
        Image.new("RGB", (12, 10), "green").save(raw, "PNG")
        source = "data:image/png;base64," + base64.b64encode(raw.getvalue()).decode()
        image = renderer._local_image(source, self.media)
        self.assertEqual(image.size, (12, 10))
        self.assertIsNone(renderer._local_image("data:image/png;base64,not an image", self.media))

    def test_text_payloads_do_not_dump_json_and_keep_literal_brackets(self):
        self.assertEqual(renderer.message_text({"content": "[测试] 普通文字"}), "[测试] 普通文字")
        self.assertEqual(renderer.message_text({"content": '{"unknown":"secret"}'}), "[消息]")
        self.assertIn("语音", renderer.message_text(payload_message({"resource_url": {"url_list": ["local"]},
                                                                     "duration": 1500},
                                                                    voice_transcription="已转文字")))
        self.assertIn("已转文字", renderer.message_text(payload_message({"tkey": "voice", "duration": 1500},
                                                                        voice_transcription="已转文字")))

    def test_system_templates_and_replies_are_readable(self):
        row = payload_message({"tips": "{{1}} 加入群聊", "template": [{"key": 1, "name": "小明"}]})
        self.assertEqual(renderer.message_text(row), "小明 加入群聊")
        self.assertEqual(renderer.reference_text({"ref_msg": {"nickname": "朋友", "content_json": '{"text":"原文"}'}}),
                         "引用 朋友：原文")

    def test_long_ranges_are_rejected_instead_of_cropping_silently(self):
        with self.assertRaises(renderer.RenderTooLarge):
            renderer.render_chat_png([{"content": "很长的内容" * 4000}], media_dir=self.media,
                                     width=1200, scale=3)


class ScreenshotApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Previous Qt smoke tests create signal/slot reference cycles. Dispose
        # those on the main thread before TestClient starts its worker thread.
        gc.collect()
        app = FastAPI()
        app.include_router(screenshot.screenshot_router)
        cls.client = TestClient(app)

    def test_original_endpoint_returns_png_with_no_browser_or_frontend(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(screenshot.database, "get_conversation", return_value={"name": "测试聊天"}), \
                 patch.object(screenshot.database, "get_messages_range", return_value=[{"content": "测试正文"}]) as read, \
                 patch.dict(screenshot.paths.__dict__, {"MEDIA_DIR": directory}):
                response = self.client.get("/api/conversations/test/screenshot?start_seq=0&end_seq=20&scale=1")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "image/png")
                self.assertEqual(response.headers["x-image-renderer"], "Pillow")
                self.assertTrue(response.content.startswith(b"\x89PNG"))
                read.assert_called_once_with("test", 0, 20, limit=2002)

    def test_validation_and_missing_conversation_preserve_statuses(self):
        self.assertEqual(self.client.get("/api/conversations/test/screenshot?start_seq=2&end_seq=1").status_code, 400)
        self.assertEqual(self.client.get("/api/conversations/test/screenshot?start_seq=0&end_seq=2001").status_code, 400)
        self.assertEqual(self.client.get("/api/conversations/test/screenshot?start_seq=0&end_seq=1&theme=invalid").status_code, 400)
        with patch.object(screenshot.database, "get_conversation", return_value=None):
            self.assertEqual(self.client.get("/api/conversations/test/screenshot?start_seq=0&end_seq=1").status_code, 404)

    def test_too_large_range_returns_actionable_413(self):
        with patch.object(screenshot.database, "get_conversation", return_value={"name": "测试"}), \
             patch.object(screenshot, "_render_range", side_effect=renderer.RenderTooLarge("请缩小区间分段导出")):
            response = self.client.get("/api/conversations/test/screenshot?start_seq=0&end_seq=20")
            self.assertEqual(response.status_code, 413)
            self.assertIn("分段", response.json()["detail"])

    def test_merged_forward_resolves_locally_and_closes_connection(self):
        connection = Mock()
        row = payload_message({"aweType": 13600, "title": "聊天记录"})
        with patch.object(screenshot.database, "get_messages_range", return_value=[row]), \
             patch.object(screenshot.database, "get_db", return_value=connection), \
             patch.dict(screenshot.paths.__dict__, {"MEDIA_DIR": "synthetic-media"}), \
             patch.object(screenshot, "resolve_forward", return_value={"title": "聊天记录", "items": [], "total": 0}) as resolve, \
             patch.object(screenshot, "render_chat_png", return_value=b"PNG") as render:
            result = screenshot._render_range("conv", 0, 20, theme="dark", title="测试", subtitle="", self_uid="", width=520, scale=1)
            self.assertEqual(result, b"PNG")
            resolve.assert_called_once_with(row, connection)
            self.assertIn("forward_detail", render.call_args.args[0][0])
            connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
