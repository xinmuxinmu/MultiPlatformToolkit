"""Bilibili sessions, private messages and fan-group messages via Python Playwright."""
from __future__ import annotations

import json
import hashlib
import re
import time
from pathlib import Path
from urllib.parse import urlencode

from .common import MediaStore, finish, merge_messages, read_baseline

NAV_API = "https://api.bilibili.com/x/web-interface/nav"
SESSIONS_API = "https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions"
MESSAGES_API = "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs"
LOGIN_URL = "https://passport.bilibili.com/login"


def identity(session) -> dict:
    response = session.ctx.request.get(NAV_API, timeout=20000)
    value = response.json() if response.ok else {}
    data = value.get("data") or {}
    if value.get("code") == 0 and data.get("isLogin") and data.get("mid"):
        return {"uid": str(data["mid"]), "name": data.get("uname") or "", "avatar": data.get("face") or ""}
    return {}


def json_to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(item.get("text") or item.get("content") or "") for item in value if isinstance(item, dict))
    if not isinstance(value, dict):
        return ""
    parts = [str(value[key]) for key in ("title", "text", "desc") if value.get(key) and value[key] != "-"]
    inner = value.get("content")
    if isinstance(inner, str):
        try:
            inner = json_to_text(json.loads(inner)) if inner.strip().startswith(("{", "[")) else inner
        except ValueError:
            pass
        parts.append(inner)
    for module in value.get("modules") or []:
        if isinstance(module, dict):
            parts.append("：".join(str(module[key]) for key in ("title", "detail") if module.get(key)))
    return "\n".join(filter(None, parts))


def parse_content(message: dict) -> dict:
    result = {"text": "", "pic": "", "audio": "", "recall": False}
    if message.get("msg_status") in (2, 4):
        result["recall"] = True
        return result
    raw = message.get("content", message.get("msg_content")) or {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        value = raw
    if isinstance(value, str):
        value = {"content": value}
    if isinstance(value, list):
        result["text"] = json_to_text(value) or "[非文本消息]"
        return result
    if not isinstance(value, dict):
        value = {"content": str(value or "")}
    text = value.get("content") if isinstance(value.get("content"), str) else ""
    url = value.get("url") if isinstance(value.get("url"), str) else ""
    kind = message.get("msg_type")
    if kind == 2 or (url and (value.get("width") or value.get("height") or value.get("imageType"))):
        result["pic"] = url or (text if text.startswith(("http://", "https://")) else "")
    elif kind == 3 or (text.startswith(("http://", "https://")) and re.search(r"\.(m4a|mp3|aac|amr)", text, re.I)):
        result.update(text="[语音消息]", audio=url or text)
    elif kind == 11:
        result["text"] = "\n".join(filter(None, ["【分享】" + str(value.get("title") or "卡片内容"),
            str(value.get("desc") or "") if value.get("desc") != "-" else "",
            "https://www.bilibili.com/video/" + str(value["bvid"]) if value.get("bvid") else ""]))
        result["pic"] = value.get("cover") or ""
    elif kind == 10:
        result["text"] = "\n".join(str(item) for item in ["【通知】" + str(value.get("title") or "系统通知"), value.get("text"), value.get("jump_uri")] if item)
    else:
        text = json_to_text(value).strip()
        result["text"] = text if text and not re.fullmatch(r"\d{10,}", text) else ("[系统消息]" if kind == 5 else "[非文本消息]")
    return result


def export(session, args, tool: Path) -> dict:
    def api(url):
        response = session.ctx.request.get(url, timeout=30000)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status}")
        value = response.json()
        if value.get("code") != 0:
            raise RuntimeError(f"API code={value.get('code')} {value.get('message', '')}")
        return value.get("data") or {}

    me = identity(session)
    if not me:
        raise RuntimeError("登录态已失效")
    print(f"账号：{me['name']} ({me['uid']})", flush=True)
    sessions = {}
    for session_type in (1, 2):
        cursor = 0
        for _ in range(200):
            try:
                data = api(SESSIONS_API + "?" + urlencode(dict(session_type=session_type, begin_ts=0,
                    end_ts=cursor, page_size=20, build=0, mobi_app="web")))
            except Exception:
                if session_type == 1:
                    raise
                print("  ! 群会话列表暂不可用，继续处理私信列表中的群聊", flush=True)
                break
            items = data.get("session_list") or []
            if not items:
                break
            for item in items:
                uid = str(item.get("talker_id") or (item.get("account_info") or {}).get("mid") or "")
                if uid:
                    item.setdefault("session_type", session_type)
                    sessions[(uid, item.get("session_type"))] = item
            timestamps = [item.get("session_ts") or (item.get("last_msg") or {}).get("timestamp") or 0 for item in items]
            next_cursor = min((value for value in timestamps if value > 0), default=0)
            if not data.get("has_more") or not next_cursor or (cursor and next_cursor >= cursor):
                break
            cursor = next_cursor
            time.sleep(.2)

    convs = {}
    for (uid, _), item in sessions.items():
        if re.fullmatch(r"8444249301319\d+", uid):
            continue
        info = item.get("account_info") or {}
        group = item.get("session_type") == 2 or item.get("group_type") == 1 or bool(item.get("group_name"))
        convs[uid] = {"uid": uid, "name": item.get("group_name") if group else (info.get("name") or info.get("username") or info.get("nickname") or ""),
                      "isGroup": group, "avatar": (item.get("group_cover") if group else (info.get("pic_url") or info.get("face"))) or ""}
    try:
        for group in api("https://api.vc.bilibili.com/link_group/v1/member/my_groups").get("list") or []:
            uid = str(group.get("group_id") or "")
            if uid in convs:
                convs[uid].update(isGroup=True, name=group.get("group_name") or f"群 {uid}", avatar=group.get("group_cover") or "")
    except Exception as exc:
        print(f"  ! 群资料：{exc}", flush=True)

    def cards(uids):
        result = {}
        for start in range(0, len(uids), 50):
            try:
                values = api("https://api.vc.bilibili.com/account/v1/user/cards?" + urlencode({"uids": ",".join(uids[start:start + 50])}))
                for card in values if isinstance(values, list) else []:
                    if card.get("mid"):
                        result[str(card["mid"])] = card
            except Exception as exc:
                print(f"  ! 用户名片：{exc}", flush=True)
            time.sleep(.2)
        return result

    profiles = cards([item["uid"] for item in convs.values() if not item["isGroup"] and (not item["name"] or not item["avatar"])])
    for uid, item in convs.items():
        card = profiles.get(uid) or {}
        item["name"] = item["name"] or card.get("name") or (f"群 {uid}" if item["isGroup"] else f"UID {uid}")
        item["avatar"] = item["avatar"] or card.get("face") or ""
    if args.only_uid:
        convs = {uid: item for uid, item in convs.items() if uid == args.only_uid}
        if not convs:
            raise RuntimeError(f"未找到要更新的会话 UID：{args.only_uid}")
    baseline = read_baseline(session.out)
    previous = {str(item["uid"]): item for item in baseline.get("convs", [])} if baseline.get("fixed") else {}
    incremental = args.mode != "full"
    print(f"会话数：{len(convs)}；{'增量' if incremental and previous else '全量'}模式", flush=True)
    results = []
    for index, (uid, conv) in enumerate(convs.items(), 1):
        old = previous.get(uid, {}).get("messages", []) if incremental else []
        known = {str(item["id"]) for item in old}
        messages, cursor, hit = [], 0, False
        limit = args.max_group if conv["isGroup"] else args.max_dm
        for _ in range(3000):
            data = api(MESSAGES_API + "?" + urlencode(dict(talker_id=uid, session_type=2 if conv["isGroup"] else 1,
                begin_seqno=0, end_seqno=cursor, size=40, build=0, mobi_app="web")))
            items = data.get("messages") or []
            if not items:
                break
            for message in items:
                if str(message.get("msg_seqno")) in known:
                    hit = True
                    break
                if len(messages) >= limit:
                    break
                messages.append(message)
            next_cursor = int(data.get("min_seqno") or min((int(item.get("msg_seqno") or 0) for item in items), default=0))
            if hit or len(messages) >= limit or not data.get("has_more") or next_cursor <= 0 or (cursor and next_cursor >= cursor):
                break
            cursor = next_cursor
            time.sleep(.2)
        normalized = []
        for message in messages:
            parsed = parse_content(message)
            sender = str(message.get("sender_uid") or "")
            normalized.append({"id": str(message.get("msg_seqno")), "text": parsed["text"], "ts": (message.get("timestamp") or 0) * 1000,
                "from": sender, "fromName": me["name"] if sender == me["uid"] else ("" if conv["isGroup"] else conv["name"]),
                "avatar": "", "recall": int(parsed["recall"]), "picUrl": parsed["pic"], "audioUrl": parsed["audio"]})
        conv["messages"] = merge_messages(normalized, old)
        results.append(conv)
        print(f"[{index}/{len(convs)}] {conv['name']} -> +{len(normalized)} 条（共 {len(conv['messages'])}）", flush=True)
    if args.only_uid or incremental:
        results.extend(item for uid, item in previous.items() if uid not in convs)
    store = MediaStore(session.out, session.ctx.request)
    group_uids = sorted({item["from"] for conv in results if conv["isGroup"] for item in conv["messages"] if item.get("from") and item["from"] != me["uid"]})
    group_profiles = cards(group_uids)
    me_avatar = store.image(me.get("avatar") or "", "avatars")
    for conv in results:
        if not conv.get("avatar", "").startswith("avatars/"):
            conv["avatar"] = store.image(conv.get("avatar") or "", "avatars")
        for item in conv["messages"]:
            if item.get("picUrl"):
                relative = store.image(item["picUrl"])
                item["pics"] = [relative] if relative else []
            item.setdefault("pics", [])
            if item.get("audioUrl"):
                item["audio"] = store.download(item["audioUrl"], "audio/" + hashlib.md5(item["audioUrl"].split("?")[0].encode()).hexdigest()[:16], ("m4a", "mp3", "aac", "amr"))
            sender = item.get("from") or ""
            card = group_profiles.get(sender) if conv["isGroup"] else None
            if card:
                item["fromName"] = card.get("name") or item.get("fromName") or f"成员 {sender}"
            if not item.get("avatar", "").startswith("avatars/"):
                item["avatar"] = (store.image(card.get("face") or "", "avatars") if card else "") or (me_avatar if sender == me["uid"] else conv["avatar"] if sender else "")
            if not item.get("fromName"):
                item["fromName"] = f"成员 {sender}" if sender else "系统"
            if item.get("pics"):
                item.pop("picUrl", None)
            if item.get("audio"):
                item.pop("audioUrl", None)
            for key in ("self", "avatarSrc"):
                item.pop(key, None)
    return finish(session.out, tool / "template.html", "B站聊天记录.html", {"uid": me["uid"], "name": me["name"], "avatar": me_avatar}, results)
