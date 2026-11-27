"""Render an archived message range to PNG entirely in Python (Pillow).

The HTTP API is unchanged. Rendering reads local archived messages/media and
never needs a running frontend, browser, transient auth token or remote image.
"""
import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from dy_backend import database
from dy_backend.forwarded import content_json, resolve_forward
from dy_backend.image_renderer import RenderTooLarge, THEMES, render_chat_png
from dy_common import paths

screenshot_router = APIRouter()
_THEMES = set(THEMES)
_render_lock = asyncio.Lock()


def _render_range(conv_id, start_seq, end_seq, *, theme, title, subtitle, self_uid, width, scale):
    media_dir = paths.MEDIA_DIR
    # The endpoint permits a seq span of 2000: fetch every possible row, never
    # silently inherit the database helper's historical 1000-row truncation.
    messages = database.get_messages_range(conv_id, start_seq, end_seq, limit=2002)
    connection = None
    try:
        for message in messages:
            payload = content_json(message)
            if "voice_transcription" not in message and (payload.get("tkey") or payload.get("voice_wave")):
                enriched = database.get_message(message["msg_id"])
                if enriched and enriched.get("voice_transcription"):
                    message["voice_transcription"] = enriched["voice_transcription"]
            if str(payload.get("aweType")) == "13600":
                if connection is None:
                    connection = database.get_db()
                message["forward_detail"] = resolve_forward(message, connection)
        return render_chat_png(messages, media_dir=media_dir, width=width, scale=scale,
                               theme=theme, title=title, subtitle=subtitle, self_uid=self_uid)
    finally:
        if connection is not None:
            connection.close()


@screenshot_router.get("/api/conversations/{conv_id}/screenshot")
async def render_screenshot(
    request: Request,
    conv_id: str,
    start_seq: int = Query(..., ge=0),
    end_seq: int = Query(..., ge=0),
    theme: str = Query("dark"),
    title: str = Query("", max_length=100),
    subtitle: str = Query("", max_length=100),
    self_uid: str = Query(""),
    width: int = Query(520, ge=320, le=1200),
    scale: int = Query(2, ge=1, le=3),
):
    if end_seq < start_seq:
        raise HTTPException(400, "end_seq 不能小于 start_seq")
    if end_seq - start_seq > 2000:
        raise HTTPException(400, "区间过大（seq 跨度上限 2000）")
    if theme not in _THEMES:
        raise HTTPException(400, f"未知主题（可选：{'/'.join(sorted(_THEMES))}）")
    conv = await asyncio.to_thread(database.get_conversation, conv_id)
    if not conv:
        raise HTTPException(404, "会话不存在")
    try:
        async with _render_lock:
            png = await asyncio.to_thread(
                _render_range, conv_id, start_seq, end_seq, theme=theme,
                title=title or conv.get("name") or "聊天记录",
                subtitle=subtitle or f"序号 {start_seq}–{end_seq} · 中国标准时间 UTC+8",
                self_uid=self_uid, width=width, scale=scale,
            )
    except RenderTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, f"渲染失败: {exc}") from exc
    return Response(content=png, media_type="image/png",
                    headers={"X-Image-Renderer": "Pillow", "Cache-Control": "no-store"})
