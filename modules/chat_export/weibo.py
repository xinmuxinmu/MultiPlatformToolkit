"""Weibo private/group chat exporter using the Python Playwright browser session."""
from __future__ import annotations

import email.utils
import io
import time
from pathlib import Path
from urllib.parse import urlencode

from .common import MediaStore, convert_audio, finish, merge_messages, read_baseline, safe_name

LOGIN_URL = "https://api.weibo.com/chat"
HEADERS = {"Referer": LOGIN_URL}
FETCH = """async (url) => {
  const response = await fetch(url, {credentials: 'include'});
  if (!response.ok) throw new Error('HTTP ' + response.status);
  return await response.json();
}"""


def api(session, path: str, parameters: dict | None = None):
    if "api.weibo.com/chat" not in session.page.url:
        session.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    values = {"source": "209678993", "t": int(time.time() * 1000), **(parameters or {})}
    value = session.page.evaluate(FETCH, path + "?" + urlencode(values))
    if isinstance(value, dict) and (value.get("error_code") or value.get("errno") not in (None, 0, "0", 100000, "100000")):
        raise RuntimeError(f"微博接口：{value.get('error') or value.get('msg') or value.get('error_code') or value.get('errno')}")
    return value


def identity(session):
    profile = api(session, "/webim/query_primary_info.json").get("profile") or {}
    return {"uid": str(profile["id"]), "name": profile.get("screen_name") or "", "avatar": profile.get("profile_image_url") or profile.get("avatar_large") or ""} if profile.get("id") else {}


def timestamp(value: str) -> int:
    try:
        return int(email.utils.parsedate_to_datetime(value).timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        return 0


def normalize(message: dict, group: bool) -> dict:
    sender = message.get("from_user") if group else message.get("sender")
    sender = sender or {}
    return {"id": str(message.get("id")), "text": (message.get("content") if group else message.get("text")) or "",
        "ts": (message.get("time") or 0) * 1000 if group else timestamp(message.get("created_at")),
        "from": str(message.get("from_uid" if group else "sender_id") or ""),
        "fromName": (sender.get("screen_name") if group else (message.get("sender_screen_name") or sender.get("screen_name"))) or "",
        "avatar": sender.get("profile_image_url") or sender.get("avatar_large") or "",
        "media": message.get("media_type") or 0, "fids": message.get("att_ids") or [],
        "recall": message.get("recall_status") or 0, "large": not group and message.get("isLargeDm") is True}


def localize(store: MediaStore, convs: list[dict], me: dict) -> str:
    media = {}
    jobs = {}
    for conv in convs:
        for message in conv["messages"]:
            kind = "audio" if message.get("media") == 4 else "video" if message.get("media") == 10 else "image"
            for fid in message.get("fids") or []:
                jobs[(kind, str(fid))] = None
    print(f"待处理媒体：{len(jobs)}", flush=True)
    for index, (kind, fid) in enumerate(jobs, 1):
        safe = safe_name(fid)
        if kind == "image":
            relative = store.download("https://upload.api.weibo.com/2/mss/msget_thumbnail?" + urlencode(dict(
                fid=fid, high=600, width=600, size="600,600", source="209678993", imageType="origin")), f"images/{safe}")
            if relative:
                body = (store.out / relative).read_bytes()
                if b"ftypheic" in body[4:20] or b"ftypheif" in body[4:20]:
                    try:
                        from PIL import Image
                        from pillow_heif import register_heif_opener
                        register_heif_opener()
                        with Image.open(io.BytesIO(body)) as image:
                            output = io.BytesIO()
                            image.convert("RGB").save(output, format="JPEG")
                        relative = store.save(f"images/{safe}.jpg", output.getvalue())
                    except (ImportError, OSError):
                        print("  ! HEIC 转换不可用，保留原图片文件", flush=True)
            media[(kind, fid)] = relative
        else:
            folder, extensions = ("videos", ("mp4",)) if kind == "video" else ("audio", ("amr",))
            relative = store.download("https://upload.api.weibo.com/2/mss/msget?" + urlencode(dict(source="209678993", fid=fid)),
                                      f"{folder}/{safe}", extensions, timeout=90000)
            media[(kind, fid)] = convert_audio(store, relative) if kind == "audio" and relative else relative
            if kind == "audio":
                media[("raw", fid)] = relative
        if index % 40 == 0:
            print(f"  媒体 {index}/{len(jobs)}", flush=True)
        time.sleep(.05)
    me_avatar = store.image(me.get("avatar") or "", "avatars")
    for conv in convs:
        if not conv.get("avatar", "").startswith("avatars/"):
            conv["avatar"] = store.image(conv.get("avatar") or "", "avatars")
        for message in conv["messages"]:
            fids = [str(fid) for fid in message.get("fids") or []]
            if fids:
                if message.get("media") == 4:
                    message["audio"] = media.get(("audio", fids[0])) or message.get("audio") or ""
                    message["audioRaw"] = media.get(("raw", fids[0])) or message.get("audioRaw") or ""
                elif message.get("media") == 10:
                    message["video"] = media.get(("video", fids[0])) or message.get("video") or ""
                else:
                    message["pics"] = list(filter(None, [media.get(("image", fid)) for fid in fids])) or message.get("pics") or []
            message.setdefault("pics", [])
            if not message.get("avatar", "").startswith("avatars/"):
                message["avatar"] = store.image(message.get("avatar") or "", "avatars")
            # Keep failed attachment IDs for a later retry rather than silently losing media.
            complete = (bool(message.get("audio")) if message.get("media") == 4 else
                        bool(message.get("video")) if message.get("media") == 10 else len(message.get("pics") or []) == len(fids))
            if fids and complete:
                message.pop("fids", None)
            if not message.get("large"):
                message.pop("large", None)
    return me_avatar


def export(session, args, tool: Path) -> dict:
    me = identity(session)
    if not me:
        raise RuntimeError("登录态已失效")
    print(f"账号：{me['name']} ({me['uid']})", flush=True)
    response = api(session, "/webim/2/direct_messages/contacts.json", dict(special_source=3, add_virtual_user="3,4",
        is_include_group=1, need_back="0,0", is_include_folder=1, count=200))
    contacts = [item for item in response.get("contacts") or [] if (item.get("user") or {}).get("id")]
    if args.only_uid:
        contacts = [item for item in contacts if str(item["user"]["id"]) == args.only_uid]
        if not contacts:
            raise RuntimeError(f"未找到要更新的会话 UID：{args.only_uid}")
    baseline = read_baseline(session.out)
    previous = {str(item["uid"]): item for item in baseline.get("convs", [])} if baseline.get("fixed") else {}
    incremental = args.mode != "full"
    print(f"会话数：{len(contacts)}；{'增量' if incremental and previous else '全量'}模式", flush=True)
    convs = []
    for index, contact in enumerate(contacts, 1):
        user = contact["user"]
        uid = str(user["id"])
        group = user.get("type") == 2 or user.get("group_type") == 1
        old = previous.get(uid, {}).get("messages", []) if incremental else []
        known = {str(message["id"]) for message in old}
        messages, cursor, hit = [], "0", False
        limit = args.max_group if group else args.max_dm
        while len(messages) < limit:
            if group:
                data = api(session, "/webim/groupchat/query_messages.json", dict(convert_emoji=1, query_sender=1, count=20, id=uid, max_mid=cursor))
            else:
                data = api(session, "/webim/2/direct_messages/conversation.json", dict(convert_emoji=1, count=100, max_id=cursor, uid=uid, is_include_group=0, from_contacts=1))
            items = data.get("messages" if group else "direct_messages") or []
            if not items:
                break
            for message in items:
                if str(message.get("id")) in known:
                    hit = True
                    break
                if len(messages) >= limit:
                    break
                messages.append(normalize(message, group))
            next_cursor = str(items[-1].get("id") or "0")
            if hit or next_cursor == cursor or next_cursor == "0":
                break
            cursor = next_cursor
            time.sleep(.18)
        name = user.get("remark") or user.get("name") or uid
        merged = merge_messages(messages, old)
        convs.append({"uid": uid, "name": name, "isGroup": group,
                      "avatar": user.get("profile_image_url") or user.get("avatar_large") or "", "messages": merged})
        print(f"[{index}/{len(contacts)}] {name} -> +{len(messages)} 条（共 {len(merged)}）", flush=True)
    if args.only_uid or incremental:
        updated = {item["uid"] for item in convs}
        convs.extend(item for uid, item in previous.items() if uid not in updated)
    large = [message for conv in convs for message in conv["messages"] if message.get("large")]
    for start in range(0, len(large), 20):
        batch = large[start:start + 20]
        try:
            values = api(session, "/webim/2/direct_messages/show_batch.json", dict(is_complete=1, is_encoded=0,
                dmids=",".join(item["id"] for item in batch)))
            full = {str(item["id"]): item.get("text") for item in values if isinstance(item, dict)}
            for message in batch:
                text = full.get(message["id"])
                if isinstance(text, str) and len(text) >= len(message["text"]):
                    message["text"] = text
                    message.pop("large", None)
        except Exception:
            print("  ! 部分长消息暂未补全，下次增量导出会重试", flush=True)
        time.sleep(.2)
    avatar = localize(MediaStore(session.out, session.ctx.request, HEADERS), convs, me)
    return finish(session.out, tool / "template.html", "微博聊天记录.html", {"uid": me["uid"], "name": me["name"], "avatar": avatar}, convs)
