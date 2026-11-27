"""Offline chat long-image rendering with Pillow, without a web UI or browser."""
from __future__ import annotations

import base64
import io
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from .forwarded import as_object, content_json

MAX_PIXELS = 40_000_000
MAX_HEIGHT = 30_000
MAX_MESSAGES = 600
MAX_MEDIA_BYTES = 20 * 1024 * 1024
THEMES = {
    "dark": ("#171c21", "#262e36", "#183f30", "#e7edf1", "#a6b2bb", "#4bd18a"),
    "light": ("#f3f5f7", "#ffffff", "#d8f5e4", "#202c36", "#657580", "#158c57"),
    "wechat": ("#ededed", "#ffffff", "#a9e87b", "#202020", "#68716d", "#248636"),
    "warm": ("#f5efe6", "#fffaf2", "#e3ebd0", "#443d33", "#827464", "#74804b"),
    "purple": ("#211e2c", "#332d43", "#493858", "#eee8f7", "#b1a4bf", "#c4a4ea"),
}


class RenderTooLarge(ValueError):
    pass


@lru_cache(maxsize=1)
def _font_file() -> str:
    candidates = [
        os.environ.get("APP_IMAGE_FONT", ""),
        str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"),
        str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/simhei.ttf"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise ValueError("未找到中文字体，请安装微软雅黑/Noto Sans CJK，或设置 APP_IMAGE_FONT")


def _plain(value) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


def _first_url(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return _first_url(value[0]) if value else ""
    if isinstance(value, dict):
        for key in ("url_list", "origin_url_list", "medium_url_list", "thumb_url_list", "url"):
            found = _first_url(value.get(key))
            if found:
                return found
    return ""


def message_text(message: dict, self_uid: str = "") -> str:
    """Use preserved payloads, never dump raw JSON into the image."""
    cj = content_json(message)
    raw = as_object(message.get("raw_data"))
    content = _plain(message.get("content"))
    plain = content if not content.lstrip().startswith("{") else ""
    if cj.get("tips"):
        text = _plain(cj["tips"])
        if self_uid and str(cj.get("aweType")) == "126" and "赞了" in text:
            text = ("你赞了对方分享的 {{2}}" if str(message.get("sender_uid")) == self_uid
                    else "对方赞了你分享的 {{2}}")
        for entry in cj.get("template") or []:
            if isinstance(entry, dict) and entry.get("key") is not None:
                text = text.replace("{{" + str(entry["key"]) + "}}", _plain(entry.get("name")))
    elif cj.get("name") and (cj.get("secUID") or cj.get("sec_uid") or cj.get("source") == "others_homepage"):
        text = "[用户名片] " + _plain(cj["name"])
        if cj.get("desc"):
            text += "\n" + _plain(cj["desc"])
    elif str(cj.get("aweType")) == "13600":
        detail = message.get("forward_detail") or {}
        text = "[合并转发] " + _plain(detail.get("title") or cj.get("title") or "聊天记录")
        total = detail.get("total")
        if total is not None:
            text += f"（{total} 条"
            if detail.get("missing"):
                text += f"，{detail['missing']} 条详情未采集"
            text += "）"
    elif (cj.get("tkey") or cj.get("voice_wave") or as_object(cj.get("resource_url")).get("is_voice")
          or (cj.get("resource_url") and cj.get("duration") is not None
              and not cj.get("video") and str(cj.get("aweType")) not in ("2702", "2703", "2704"))):
        duration = cj.get("duration", as_object(cj.get("resource_url")).get("duration"))
        try:
            seconds = f"{float(duration) / 1000:g} 秒"
        except (TypeError, ValueError):
            seconds = "时长未知"
        text = "[语音] " + seconds
        if message.get("voice_transcription"):
            text += "\n" + _plain(message["voice_transcription"])
    else:
        layout = as_object(as_object(cj.get("im_dynamic_patch")).get("raw_data"))
        text = _plain(cj.get("text") or cj.get("content_title") or cj.get("aweme_title")
                      or cj.get("poi_name") or cj.get("hint_text") or cj.get("title")
                      or as_object(layout.get("top_bottom_top")).get("content")
                      or as_object(layout.get("content_top")).get("content") or plain)
        comment = cj.get("comment")
        if comment and _plain(comment) not in text:
            text += "\n评论：" + _plain(comment)
        kind = message.get("msg_type")
        local = _plain(message.get("media_local_path")).lower()
        if kind == 5 or local.endswith((".mp4", ".webm", ".mov")) or as_object(cj.get("video")).get("vid"):
            text = "[视频]" + (" " + text if text and text != "[视频]" else "")
        elif kind == 4:
            text = "[分享] " + text
        elif not text:
            text = "[图片]" if kind == 3 else ("[表情]" if kind == 2 else "[消息]")
    if raw.get("is_recalled") or cj.get("is_recalled"):
        text = "[已撤回] " + text
    if message.get("detail_missing"):
        text = "[详情未采集] " + text
    return text.strip()


def reference_text(message: dict) -> str:
    reference = as_object(message.get("ref_msg"))
    if not reference:
        return ""
    name = _plain(reference.get("nickname"))
    embedded = as_object(reference.get("content_json") or reference.get("refmsg_content"))
    text = (_plain(reference.get("content")) or _plain(embedded.get("text"))
            or _plain(embedded.get("content_title")) or "[引用的消息未归档]")
    return "引用 " + (name + "：" if name else "") + text


def _local_image(source: str, media_dir: Path) -> Image.Image | None:
    """Read only images under the active account, or bounded inline thumbnails."""
    if not source:
        return None
    try:
        if source.startswith("data:image/"):
            encoded = source.partition(",")[2]
            if len(encoded) > MAX_MEDIA_BYTES * 4 // 3:
                return None
            raw = base64.b64decode(re.sub(r"\s", "", encoded), validate=True)
            opened = Image.open(io.BytesIO(raw))
        else:
            if "://" in source:
                return None
            candidate = Path(source)
            target = (candidate if candidate.is_absolute() else media_dir / candidate).resolve()
            root = media_dir.resolve()
            if not target.is_relative_to(root) or not target.is_file() or target.stat().st_size > MAX_MEDIA_BYTES:
                return None
            opened = Image.open(target)
        with opened:
            if opened.width * opened.height > MAX_PIXELS:
                return None
            opened.seek(0)
            return ImageOps.exif_transpose(opened).convert("RGBA")
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return None


def _image_source(message: dict) -> str:
    local = _plain(message.get("media_local_path"))
    if local and not local.lower().endswith((".mp4", ".webm", ".mov", ".mp3", ".wav", ".ogg", ".m4a", ".aac")):
        return local
    inline = content_json(message).get("inline_pic")
    if isinstance(inline, str) and inline:
        return "data:image/webp;base64," + inline
    return ""


@dataclass
class _Block:
    lines: list[str]
    refs: list[str]
    sender: str
    stamp: str
    own: bool
    depth: int
    system: bool
    height: int
    image: Image.Image | None = None


def render_chat_png(messages: list[dict], *, media_dir: str | Path, width: int = 520,
                    scale: int = 2, theme: str = "dark", title: str = "聊天记录",
                    subtitle: str = "", self_uid: str = "") -> bytes:
    """Return one bounded PNG; too-large ranges must be exported in smaller chunks."""
    if theme not in THEMES or not 320 <= width <= 1200 or not 1 <= scale <= 3:
        raise ValueError("截图主题、宽度或倍率不受支持")
    bg, bubble, own_bubble, fg, muted, accent = THEMES[theme]
    font_file = _font_file()
    font = ImageFont.truetype(font_file, 15 * scale)
    small_font = ImageFont.truetype(font_file, 11 * scale)
    title_font = ImageFont.truetype(font_file, 20 * scale)
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    pixel_width = width * scale
    pad, gap, line_height = 16 * scale, 10 * scale, 23 * scale
    max_height = min(MAX_HEIGHT, MAX_PIXELS // pixel_width)
    root = Path(media_dir)

    def wrap(text, available, use_font=font):
        lines = []
        for paragraph in _plain(text).split("\n"):
            line = ""
            for char in paragraph:
                if line and measure.textlength(line + char, font=use_font) > available:
                    lines.append(line)
                    line = char
                    if len(lines) * line_height > max_height:
                        raise RenderTooLarge("单条消息过长，请缩小消息区间或减少倍率后分段导出")
                else:
                    line += char
            lines.append(line)
        return lines

    title_lines = wrap(title or "聊天记录", pixel_width - 2 * pad, title_font)
    subtitle_lines = wrap(subtitle, pixel_width - 2 * pad, small_font) if subtitle else []
    header_height = pad * 2 + len(title_lines) * 30 * scale + len(subtitle_lines) * 19 * scale
    total_height = header_height + gap
    blocks = []

    def append_message(message, depth=0):
        nonlocal total_height
        if len(blocks) >= MAX_MESSAGES:
            raise RenderTooLarge("消息和转发记录超过 600 条，请缩小区间后分段导出")
        if depth > 5:
            raise RenderTooLarge("合并转发层级过深，请单独导出该条转发记录")
        own = bool(self_uid) and str(message.get("sender_uid") or "") == str(self_uid)
        sender = _plain(message.get("sender_name") or ("我" if own else "") or message.get("sender_uid") or "未知发送者")
        content = message_text(message, str(self_uid))
        # Type-0 voice/media cards remain ordinary bubbles.
        cj = content_json(message)
        system = message.get("msg_type") == 0 and bool(cj.get("tips") or not cj)
        available = pixel_width - pad * 4 - depth * 14 * scale
        lines = wrap(content, available)
        refs = wrap(reference_text(message), available, small_font) if message.get("ref_msg") else []
        picture = _local_image(_image_source(message), root)
        if picture:
            picture.thumbnail((available, 280 * scale), Image.Resampling.LANCZOS)
        try:
            stamp = datetime.fromtimestamp(float(message.get("timestamp") or 0),
                                           timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
        except (ValueError, TypeError, OSError, OverflowError):
            stamp = ""
        if not message.get("timestamp"):
            stamp = ""
        height = (len(lines) * line_height + len(refs) * 18 * scale + pad * 2
                  + (0 if system else 22 * scale) + ((picture.height + gap) if picture else 0))
        total_height += height + gap
        if total_height + pad > max_height:
            raise RenderTooLarge("长图超过安全尺寸（最多 4000 万像素 / 30000 像素高），请减少倍率或缩小区间分段导出")
        blocks.append(_Block(lines, refs, sender, stamp, own, depth, system, height, picture))
        detail = message.get("forward_detail")
        if isinstance(detail, dict):
            for child in detail.get("items") or []:
                append_message(child, depth + 1)
        elif str(cj.get("aweType")) == "13600":
            for preview in cj.get("list_content") or []:
                if isinstance(preview, dict):
                    append_message({"sender_name": preview.get("nick_name"),
                                    "content": preview.get("text") or "[消息详情未采集]",
                                    "msg_type": 1}, depth + 1)

    for message in messages:
        append_message(message)
    if not messages:
        append_message({"content": "当前区间没有消息", "msg_type": 0})
    image = Image.new("RGB", (pixel_width, max(total_height + pad, 120 * scale)), bg)
    draw = ImageDraw.Draw(image)
    y = pad
    for line in title_lines:
        draw.text((pad, y), line, fill=accent, font=title_font)
        y += 30 * scale
    for line in subtitle_lines:
        draw.text((pad, y), line, fill=muted, font=small_font)
        y += 19 * scale
    y = header_height + gap
    for block in blocks:
        left = pad + block.depth * 14 * scale + (pad if block.own else 0)
        right = pixel_width - pad - (0 if block.own else pad)
        color = bg if block.system else (own_bubble if block.own else bubble)
        draw.rounded_rectangle((left, y, right, y + block.height), radius=9 * scale, fill=color)
        tx, ty = left + pad, y + pad
        if not block.system:
            header = block.sender + ("  ·  " + block.stamp if block.stamp else "")
            # Names and timestamps stay inside the bubble as a single measured row.
            while header and draw.textlength(header, font=small_font) > right - tx - pad:
                header = header[:-2] + "…" if not header.endswith("…") else header[:-2] + "…"
            draw.text((tx, ty), header, fill=accent if block.own else muted, font=small_font)
            ty += 22 * scale
        for line in block.refs:
            draw.text((tx, ty), line, fill=muted, font=small_font)
            ty += 18 * scale
        for line in block.lines:
            draw.text((tx, ty), line, fill=muted if block.system else fg, font=font)
            ty += line_height
        if block.image:
            image.paste(block.image, (tx, ty + gap), block.image)
        y += block.height + gap
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False)
    return output.getvalue()
