#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网易云音乐 · 乐迷团每日任务

所有业务接口走移动端 eapi 协议：AES-128-ECB + `36cd479b6b5` 分隔符 + md5 摘要。
签名里的路径必须是 `/api/xxx` 而不是 `/eapi/xxx`——这点用匿名请求实测过，
传 `/eapi/` 服务端会直接回「接口未找到」。

任务完成情况不靠猜：执行完重新拉一次任务列表，比对 currentProgress 才算成功。
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import http.client
import json
import random
import re
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from app.paths import DATA_ROOT
from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

VERSION = "1.1.0"
APP_ID = "netease-fans"

HOME = DATA_ROOT / "netease_fans"
HOME.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = HOME / "config.json"
STATE_PATH = HOME / "state.json"
LIKED_PATH = HOME / "liked-notes.json"

EAPI_HOST = "https://interface3.music.163.com"
WEB_HOST = "https://music.163.com"
EAPI_KEY = b"e82ckenh8dichen8"
EAPI_SEP = "-36cd479b6b5-"

WEAPI_PRESET_KEY = b"0CoJUm6Qyw8W8jud"
WEAPI_IV = b"0102030405060708"
WEAPI_BASE62 = string.ascii_letters + string.digits
WEAPI_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDgtQn2JZ34ZC28NWYpAUd98iZ37BUrX/aKzmFbt7clFSs6sXqHauqKWqdtLkF2KexO40H1YTX8z2lSgBBOAxLsvaklV8k4cBFK9snQXE9/DDaFt6Rr7iVZMldczhC0JNgTz+SHXT6CBHuX3e9SdB1Ua44oncaTWz7OBGLbCiK45wIDAQAB
-----END PUBLIC KEY-----"""

# 移动端 UA：eapi 请求必须带 Android/iOS 的 UA + os cookie，
# 用 PC UA 打 eapi 会被当成异常客户端。
UA_ANDROID = ("NeteaseMusic/9.2.85.250902150000(9020085);Dalvik/2.1.0 "
              "(Linux; U; Android 14; PGT-AN20 Build/UKQ1.231003.002)")

# web 明文接口（评论、播放上报）用桌面 UA，注意 Cookie 里仍保留 os=android，
# 这是 App 侧的会话特征，去掉反而容易触发验证。
UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")

# 点赞缓存版本：v1 是没带 appLogExt 的空点，服务端不计数，
# 升级后旧缓存整体作废，否则同一批笔记会被误判成「7 天内点过」而跳过。
LIKED_VERSION = 2


class FansError(Exception):
    pass


# ---------------------------------------------------------------- eapi 加密

def eapi_encrypt(sign_path: str, payload: dict) -> str:
    """把业务参数打成 eapi 的 params 串。

    sign_path 形如 /api/fans/group/mission/all —— 注意是 api 不是 eapi。
    """
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.md5(
        f"nobody{sign_path}use{text}md5forencrypt".encode()).hexdigest()
    raw = f"{sign_path}{EAPI_SEP}{text}{EAPI_SEP}{digest}"
    padder = padding.PKCS7(128).padder()
    padded = padder.update(raw.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(EAPI_KEY), modes.ECB()).encryptor()
    return (enc.update(padded) + enc.finalize()).hex().upper()


# ---------------------------------------------------------------- weapi 加密

def _aes_cbc(data: bytes, key: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(WEAPI_IV)).encryptor()
    return enc.update(padded) + enc.finalize()


def weapi_encrypt(payload: dict) -> str:
    """weapi 的双层 AES + RSA 参数。播放/播放地址接口只认这套。"""
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    secret = "".join(random.choice(WEAPI_BASE62) for _ in range(16)).encode()
    first = base64.b64encode(_aes_cbc(text, WEAPI_PRESET_KEY))
    params = base64.b64encode(_aes_cbc(first, secret)).decode()
    pub = serialization.load_pem_public_key(WEAPI_PUBLIC_KEY).public_numbers()
    size = (pub.n.bit_length() + 7) // 8
    enc_sec = pow(int.from_bytes(secret[::-1], "big"),
                  pub.e, pub.n).to_bytes(size, "big").hex()
    return urllib.parse.urlencode({"params": params, "encSecKey": enc_sec})


def parse_cookie(text: str) -> dict:
    out = {}
    for part in re.split(r"[;\r\n]", text or ""):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, val = part.split("=", 1)
        if key.strip():
            out[key.strip()] = val.strip()
    return out


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def _post(url: str, body: bytes, headers: dict, timeout=15):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if "gzip" in (resp.headers.get("Content-Encoding") or "").lower():
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            return resp.status, _decode(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        if "gzip" in (exc.headers.get("Content-Encoding") or "").lower():
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        return exc.code, _decode(raw)


# ---------------------------------------------------------------- 配置

DEFAULT_CONFIG = {
    "cookie": "",
    "artist_name": "封茗囧菌",
    "group_id": "",
    "device_id": "",
    # 低风险：只改自己账号的收藏、点赞
    "collect_enable": True,
    "like_note_enable": True,
    "like_note_count": 5,
    # 中风险：分享只上报进度，不产生公开动态
    "share_enable": True,
    # 中风险：在任务指定的歌曲下面发一条评论，公开可见
    "comment_enable": True,
    "comment_text": "今天的循环就是这首，来打个卡～",
    # 高风险：会留下公开内容 / 等同刷播放量，默认关闭
    "publish_enable": False,
    "publish_title": "今天的循环是这首",
    "publish_text": "今天也在听歌，记一笔。",
    "play_enable": False,
    "play_count": 1,
    # 勾选要执行的任务 ID；None = 按上面的类型开关
    "mission_ids": None,
    # 每个动作之间的随机间隔（秒），别调太小
    "gap_min": 1.5,
    "gap_max": 3.5,
}

_cfg_lock = threading.Lock()


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        if CONFIG_PATH.exists():
            saved = json.loads(CONFIG_PATH.read_text("utf-8"))
            if isinstance(saved, dict):
                cfg.update({k: v for k, v in saved.items() if k in cfg})
    except Exception:
        pass
    if not cfg.get("device_id"):
        cfg["device_id"] = uuid.uuid4().hex[:16].upper()
        save_config(cfg)
    return cfg


def save_config(cfg: dict):
    with _cfg_lock:
        HOME.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(data: dict):
    try:
        STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    except OSError:
        pass


def load_liked() -> dict:
    """点过赞的 threadId，7 天内不重复点。"""
    try:
        data = json.loads(LIKED_PATH.read_text("utf-8"))
        if not isinstance(data, dict):
            return {}
    except Exception:
        return {}
    if data.get("_v") != LIKED_VERSION:
        # 旧版缓存记的是「点了但没计数」的无效点赞，作废重来。
        return {}
    cutoff = int(time.time()) - 7 * 86400
    return {k: v for k, v in data.items()
            if k != "_v" and isinstance(v, int) and v >= cutoff}


def save_liked(data: dict):
    try:
        payload = dict(data)
        payload["_v"] = LIKED_VERSION
        LIKED_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- 调用层

class Client:
    """带登录态的网易云 eapi 客户端。"""

    def __init__(self, cookie: str, device_id: str = "", log=None):
        self.log = log or (lambda *_: None)
        ck = parse_cookie(cookie)
        if "MUSIC_U" not in ck:
            raise FansError(
                "Cookie 里没有 MUSIC_U，这份是没登录的状态。"
                "请在浏览器登录 music.163.com，从任意请求里复制整行 Cookie。")
        # eapi 必须用移动端会话：os=android + 稳定的 deviceId。
        # 缺 deviceId 时每次请求都换一个设备指纹，是最容易被风控标记的行为。
        ck["os"] = "android"
        ck.setdefault("appver", "9.2.85")
        if device_id:
            ck.setdefault("deviceId", device_id)
        self.cookie = "; ".join(f"{k}={v}" for k, v in ck.items())
        self.uid = ck.get("MUSIC_U") and self._uid_from_cookie(ck)

    @staticmethod
    def _uid_from_cookie(ck: dict):
        return ck.get("__csrf") or ""

    def eapi(self, path: str, payload: dict, query: str = "", timeout=15,
             retries=2, host: str = EAPI_HOST):
        """path 形如 /eapi/fans/group/mission/all。

        interface3 偶尔会直接掐断连接（RemoteDisconnected），交给上层当业务
        失败处理太冤，这里退避重试几次。
        """
        sign_path = path.replace("/eapi/", "/api/", 1)
        url = host + path + (("?" + query) if query else "")
        body = urllib.parse.urlencode(
            {"params": eapi_encrypt(sign_path, payload)}).encode()
        headers = {
            "User-Agent": UA_ANDROID,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": WEB_HOST,
            "Accept": "*/*",
            "Cookie": self.cookie,
        }
        last = None
        status, text = 0, ""
        for attempt in range(retries + 1):
            try:
                status, text = _post(url, body, headers, timeout=timeout)
                break
            except (OSError, http.client.HTTPException) as exc:
                last = exc
                if attempt >= retries:
                    raise FansError(f"网络请求失败，重试 {retries} 次都没通：{exc}")
                time.sleep(1.0 + attempt)
        if not text and last is not None:
            raise FansError(f"网络请求失败：{last}")
        try:
            data = json.loads(text)
        except Exception:
            raise FansError(f"接口返回的不是 JSON（HTTP {status}）：{text[:160]}")
        code = data.get("code")
        if code == 404:
            raise FansError(f"接口不存在：{path}（平台可能改过路径）")
        if code in (301, 302, 401):
            raise FansError("登录态失效了，重新复制一次 Cookie")
        if code not in (200, None):
            raise FansError(f"接口返回 code={code}：{data.get('message') or ''}")
        return data

    def weapi(self, path: str, payload: dict, timeout=20):
        """path 形如 /weapi/feedback/weblog。"""
        ck = parse_cookie(self.cookie)
        body = dict(payload)
        body.setdefault("csrf_token", ck.get("__csrf", ""))
        _, text = _post(WEB_HOST + path, weapi_encrypt(body).encode(), {
            "User-Agent": UA_DESKTOP,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": WEB_HOST + "/",
            "Cookie": self.cookie,
        }, timeout=timeout)
        try:
            data = json.loads(text)
        except Exception:
            raise FansError(f"weapi 返回的不是 JSON：{text[:160]}")
        code = data.get("code")
        if code in (301, 302, 401):
            raise FansError("登录态失效了，重新复制一次 Cookie")
        if code not in (200, None):
            raise FansError(f"weapi 返回 code={code}：{data.get('message') or ''}")
        return data

    # ---- 业务接口
    def group_detail(self, group_id: str):
        return self.eapi("/eapi/social/fansgroup/bff/detail/get",
                         {"groupId": group_id}, query=f"groupId={group_id}")

    def my_detail(self, group_id: str):
        return self.eapi("/eapi/social/fansgroup/bff/user/group/detail/get",
                         {"groupId": group_id}, query=f"groupId={group_id}")

    def missions(self, group_id: str):
        return self.eapi("/eapi/fans/group/mission/all",
                         {"fansGroupId": group_id}, query=f"fansGroupId={group_id}")

    def feed(self, group_id: str, size=10):
        q = (f"artistSelf=0&cursor=0&fansGroupId={group_id}&size={size}")
        return self.eapi("/eapi/fans/group/feed/recommend/get",
                         {"artistSelf": "0", "cursor": "0",
                          "fansGroupId": group_id, "size": str(size)}, query=q)

    def like_resource(self, thread_id: str, group_id: str):
        """点赞乐迷笔记。

        appLogExt 必须写明乐迷团归属，服务端才把这次点赞算进团任务；
        传空串接口照样回 200，但进度一点不动——之前 5 条只涨 1 就是栽在这。
        """
        ext = json.dumps({
            "addRefer": {"resourceId": str(group_id), "resourceType": "E"},
            "multiRefer": [],
            "fansGroupId": str(group_id),
        }, separators=(",", ":"), ensure_ascii=False)
        return self.eapi("/eapi/resource/like",
                         {"threadId": str(thread_id), "appLogExt": ext})

    def song_like(self, song_id, like=True):
        return self.eapi("/eapi/song/like", {
            "trackId": str(song_id), "like": "true" if like else "false",
            "time": "3", "checkToken": "",
        })

    def share_progress(self, resource_id, rtype="4", action="share"):
        """分享进度上报。

        fansGroupId 按 App 真实请求填字符串 "null"，不是团 ID —— 填团 ID
        服务端会直接忽略这次上报。
        """
        flag = "null"
        q = (f"resourceId={resource_id}&action={action}"
             f"&fansGroupId={flag}&resourceType={rtype}")
        return self.eapi("/eapi/fans/group/mission/forward/progress", {
            "resourceId": str(resource_id), "action": action,
            "fansGroupId": flag, "resourceType": str(rtype),
        }, query=q)

    def comment_song(self, thread_id: str, content: str, timeout=15):
        """给歌曲发一条评论，走 web 明文接口。"""
        ck = parse_cookie(self.cookie)
        m = re.search(r"_(\d+)$", thread_id or "")
        song_id = m.group(1) if m else ""
        referer = f"{WEB_HOST}/song?id={song_id}" if song_id else f"{WEB_HOST}/"
        body = urllib.parse.urlencode({
            "threadId": thread_id,
            "content": content,
            "csrf_token": ck.get("__csrf", ""),
        }).encode()
        status, text = _post(WEB_HOST + "/api/v1/resource/comments/add", body, {
            "User-Agent": UA_DESKTOP,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": referer,
            "Cookie": self.cookie,
        }, timeout=timeout)
        try:
            data = json.loads(text)
        except Exception:
            raise FansError(
                f"评论接口返回的不是 JSON（HTTP {status}）：{text[:160]}")
        code = data.get("code")
        if code != 200:
            raise FansError(
                f"评论没发出去，code={code}：{data.get('message') or ''}")
        return data

    def publish_note(self, title: str, msg: str, board_id: str, group_name: str,
                     pics: str = ""):
        activity = json.dumps([{
            "id": str(board_id), "type": 3, "subType": 11,
            "name": group_name, "selected": True, "canChange": True,
        }], ensure_ascii=False)
        payload = {
            "title": title, "msg": msg, "type": "noresource",
            "uuid": uuid.uuid4().hex, "addComment": False,
            "privacySetting": "0", "socialSpaceVisible": 1,
            "activityInfoList": activity,
        }
        if pics:
            payload["pics"] = pics
        return self.eapi("/eapi/note/share/friends/resource", payload)

    def upload_node(self) -> str:
        req = urllib.request.Request(
            "http://wanproxy.127.net/lbs?version=1.0&bucketname=cloudmusic",
            headers={"User-Agent": UA_DESKTOP})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(_decode(resp.read()))
        except Exception as exc:  # noqa: BLE001
            raise FansError(f"获取图片上传节点失败：{exc}")
        nodes = data.get("upload") or []
        if not nodes:
            raise FansError("没有可用的图片上传节点")
        return nodes[0]

    def upload_image(self, image: bytes, ext: str = "jpg") -> str:
        """把图片传到 NOS，返回发布动态需要的 pics JSON。"""
        ext = (ext or "jpg").lower().lstrip(".")
        token = self.eapi("/eapi/nos/token/alloc", {
            "filename": f"cover.{ext}", "local": "false", "nos_product": 0,
            "fileSize": len(image), "md5": hashlib.md5(image).hexdigest(),
            "ext": ext, "type": "image",
        }, host=WEB_HOST)
        result = token.get("result") or {}
        if not result.get("docId"):
            raise FansError("获取图片上传凭证失败")
        node = self.upload_node()
        url = (f"{node}/{result['bucket']}/{result['objectKey']}"
               "?version=1.0&offset=0&complete=true")
        req = urllib.request.Request(url, data=image, method="PUT", headers={
            "X-Nos-Token": result["token"],
            "Content-Type": f"image/{'jpeg' if ext == 'jpg' else ext}",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except Exception as exc:  # noqa: BLE001
            raise FansError(f"上传图片失败：{exc}")
        info = self.eapi("/eapi/upload/event/img/v1",
                         {"imgid": result["docId"], "format": ext},
                         host=WEB_HOST)
        pic = info.get("picInfo") or {}
        if not pic.get("originId"):
            raise FansError("图片上传后取不到图片信息")
        return json.dumps([{
            "originId": str(pic["originId"]),
            "squareId": str(pic["squareId"]),
            "rectangleId": str(pic["rectangleId"]),
            "pcSquareId": str(pic["pcSquareId"]),
            "pcRectangleId": str(pic["pcRectangleId"]),
            "originJpgId": str(pic["originJpgId"]),
            "width": pic.get("width") or 0,
            "height": pic.get("height") or 0,
            "index": 0,
        }], ensure_ascii=False)

    def weblog(self, logs: list):
        data = self.weapi("/weapi/feedback/weblog", {
            "logs": json.dumps(logs, ensure_ascii=False)})
        if data.get("code") != 200:
            raise FansError(f"播放日志上报失败（code={data.get('code')}）")

    def song_detail(self, song_id) -> dict:
        data = self.weapi("/weapi/v3/song/detail", {
            "c": json.dumps([{"id": str(int(song_id)), "v": 0}],
                            separators=(",", ":")),
        })
        songs = data.get("songs") or []
        return songs[0] if songs else {}

    def player_url(self, song_id) -> str:
        data = self.weapi("/weapi/song/enhance/player/url/v1", {
            "ids": f"[{int(song_id)}]", "level": "exhigh",
            "encodeType": "aac",
        })
        item = (data.get("data") or [{}])[0]
        if item.get("code") != 200 or not item.get("url"):
            raise FansError(f"歌曲 {song_id} 没有可用播放地址")
        return item["url"]

    def play_once(self, song_id):
        """完整播放上报链路：startplay → 取地址 → 拉一段音频 → play。"""
        sid = int(song_id)
        source, source_id, duration = "toplist", "", 0
        try:
            song = self.song_detail(sid)
            duration = int(song.get("dt") or 0) // 1000
            album = (song.get("al") or {}).get("id")
            if album:
                source, source_id = "album", str(album)
        except FansError:
            pass
        content = f"id={source_id or sid}"
        self.weblog([{"action": "startplay", "json": {
            "id": sid, "type": "song", "content": content, "mainsite": "1"}}])
        url = self.player_url(sid)
        req = urllib.request.Request(url, headers={
            "User-Agent": UA_DESKTOP,
            "Referer": WEB_HOST + "/",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Range": "bytes=0-",
            "Cookie": self.cookie,
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read(512 * 1024)
        except Exception as exc:  # noqa: BLE001
            raise FansError(f"拉取歌曲 {song_id} 音频失败：{exc}")
        seconds = random.randint(3, 5)
        if duration and duration < seconds:
            seconds = duration
        self.weblog([{"action": "play", "json": {
            "type": "song", "wifi": 0, "download": 0, "id": sid,
            "time": seconds, "end": "interrupt",
            "source": source, "sourceId": source_id, "mainsite": "1",
            "content": content,
        }}])


# ---------------------------------------------------------------- 解析

def _loads_json(raw) -> dict:
    """任务里的参数既可能是 dict，也可能是一段 JSON 字符串。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _query_of(link: str) -> dict:
    """从 orpheus://comment?threadId=xxx 这种深链里抠查询参数。"""
    if not link:
        return {}
    try:
        qs = urllib.parse.urlparse(link).query
    except Exception:
        return {}
    out = {}
    for key, val in urllib.parse.parse_qsl(qs):
        out.setdefault(key, val)
    return out


def _first(data: dict, *keys):
    for key in keys:
        val = data.get(key)
        if val not in (None, "", [], {}):
            return val
    return ""


def _parse_action(mission: dict) -> dict:
    """把任务的按钮动作拆成可执行参数。

    按钮 url 是 JSON 字符串，目前见过三种形态，缺一种就会整条漏掉：
      mnb     播放歌曲，歌曲 ID 在 actionMnbParams.songIds
      custom  分享歌曲，歌曲 ID 在 actionCustomParams.progressParams.resourceId
      open    评论任务，只有 orpheus://comment?threadId=... 深链
    """
    out = {"type": "", "name": "", "song_ids": [], "resource_id": "",
           "resource_type": "", "thread_id": "", "link": ""}

    data = {}
    for raw in ((mission.get("button") or {}).get("url"),
                (mission.get("iconUi") or {}).get("targetUrl"),
                mission.get("logInfo"),
                mission.get("missionDetail")):
        data = _loads_json(raw)
        if data:
            break
    if not data:
        return out

    out["type"] = data.get("actionType") or ""
    if out["type"] == "mnb":
        out["name"] = data.get("actionMnbName") or ""
        params = data.get("actionMnbParams") or {}
    elif out["type"] == "custom":
        out["name"] = data.get("actionCustomName") or ""
        params = data.get("actionCustomParams") or {}
        # 分享任务把真正要上报的参数又裹了一层 progressParams
        inner = params.get("progressParams") if isinstance(params, dict) else None
        if isinstance(inner, dict):
            params = {**params, **inner}
    elif out["type"] == "open":
        out["link"] = data.get("actionOpenLink") or ""
        params = _query_of(out["link"])
    else:
        params = data

    if not isinstance(params, dict):
        params = {}

    song_ids = _first(params, "songIds", "songId", "trackIds", "trackId")
    if isinstance(song_ids, (int, str)):
        song_ids = [song_ids] if song_ids else []
    out["song_ids"] = song_ids if isinstance(song_ids, list) else []
    out["resource_id"] = _first(params, "resourceId", "songId") or (
        out["song_ids"][0] if out["song_ids"] else "")
    out["resource_type"] = str(_first(params, "resourceType") or "")
    out["thread_id"] = str(_first(params, "threadId") or "")
    return out


def _mission_item(m: dict, accelerate=False) -> dict:
    action = _parse_action(m)
    return {
        "id": m.get("missionId"),
        "type": m.get("missionType") or "",
        "title": m.get("title") or "",
        "subtitle": m.get("subtitle") or "",
        "archive": m.get("titleForArchives") or "",
        "status": m.get("status") or "INIT",
        "current": m.get("currentProgress") or 0,
        "total": m.get("allProgress") or 0,
        "integral": m.get("integral") or "",
        "accelerate": accelerate,
        "action": action,
    }


def parse_missions(data: dict) -> list:
    items = []
    normal = data.get("normal") or {}
    for m in (normal.get("data") or []):
        if isinstance(m, dict):
            items.append(_mission_item(m))
    # 加速任务是个单对象，不是数组
    original = data.get("originality") or {}
    one = original.get("data")
    if isinstance(one, dict) and one:
        items.append(_mission_item(one, accelerate=True))
    elif isinstance(one, list):
        items.extend(_mission_item(m, accelerate=True) for m in one if isinstance(m, dict))
    for item in items:
        item["kind"] = classify(item)
    return items


def classify(item: dict) -> str:
    """把任务标题归到具体动作上。标题会改，所以按关键词匹配。"""
    text = f"{item.get('title','')}{item.get('subtitle','')}"
    link = ((item.get("action") or {}).get("link") or "").lower()
    # 加速任务完成后副标题会被服务端改写成「任务已完成，明日0点更新」，
    # 所以除了关键词，再看一眼按钮深链：comment 就是评论任务。
    if "comment" in link or "评论" in text:
        return "comment"
    if "播放" in text or "听歌" in text or "收听" in text:
        return "play"
    if "笔记" in text and ("发布" in text or "图文" in text):
        return "publish"
    if "分享" in text:
        return "share"
    if "点赞" in text:
        return "like_note"
    if "收藏" in text or "红心" in text:
        return "collect"
    return ""


# ---------------------------------------------------------------- 日志

class Task:
    def __init__(self):
        self.lock = threading.Lock()
        self.logs = []
        self.seq = 0
        self.running = False
        self.done = False
        self.summary = {}

    def log(self, level, msg):
        with self.lock:
            self.seq += 1
            self.logs.append({
                "id": self.seq, "l": level, "m": msg,
                "t": time.strftime("%H:%M:%S"),
            })

    def start(self, cfg):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.done = False
            self.logs = []
            self.seq = 0
            self.summary = {}
        threading.Thread(target=self._run, args=(cfg,), daemon=True).start()
        return True

    def _run(self, cfg):
        result = {}
        try:
            result = NeteaseFans(cfg, self.log).run()
        except FansError as exc:
            self.log("error", str(exc))
            result = {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"出了点意外：{exc}")
            result = {"error": str(exc)}
        finally:
            with self.lock:
                self.summary = result
                self.running = False
                self.done = True
            self.log("finish", "全部完成")


TASK = Task()


# ---------------------------------------------------------------- 主流程

class NeteaseFans:
    def __init__(self, cfg, log):
        self.cfg = cfg or {}
        self.log = log

    def _client(self) -> Client:
        return Client(norm_cookie(self.cfg.get("cookie", "")),
                      self.cfg.get("device_id", ""), self.log)

    def _sleep(self):
        a = float(self.cfg.get("gap_min") or 1.5)
        b = float(self.cfg.get("gap_max") or 3.5)
        time.sleep(random.uniform(min(a, b), max(a, b)))

    # ---- 只读：拉一份完整状态
    def refresh(self) -> dict:
        client = self._client()
        group_id = self._resolve_group(client)

        info = {}
        try:
            detail = client.group_detail(group_id).get("data") or {}
            info = detail.get("fansGroupInfo") or {}
        except FansError as exc:
            self.log("warn", f"读团信息失败：{exc}")

        member = {}
        warn = ""
        try:
            payload = client.my_detail(group_id).get("data") or {}
            member = payload.get("fansGroupMemberDetail") or {}
            if not member:
                # 登录态失效时接口通常也回 200，只是 data 是空的，
                # 不判断的话界面会显示一片空白，看不出是没登录还是没入团。
                warn = "读不到你的团员信息：Cookie 可能已失效，或这个账号还没加入该乐迷团"
                self.log("warn", warn)
        except FansError as exc:
            warn = f"读亲密度失败：{exc}"
            self.log("warn", warn)

        missions, extra = [], {}
        try:
            raw = client.missions(group_id).get("data") or {}
            missions = parse_missions(raw)
            extra = {
                "daily_max": raw.get("dailyMaxIntimacy"),
                "remaining_integral": raw.get("remainingIntegral"),
            }
        except FansError as exc:
            self.log("warn", f"读任务列表失败：{exc}")

        level = member.get("level") or {}
        state = {
            "ok": True,
            "fetched_at": int(time.time()),
            "group": {
                "id": group_id,
                "name": info.get("fansGroupName") or "",
                "artist": info.get("artistName") or "",
                "avatar": info.get("headAvatarUrl") or "",
                "board_id": info.get("boardId") or "",
                "members": info.get("totalMembersCount"),
            },
            "me": {
                "nickname": member.get("nickname") or "",
                "avatar": member.get("avatarUrl") or "",
                "no": member.get("no") or "",
                "integral": member.get("integral") or "",
                "remaining": member.get("remainingUpgrade") or 0,
                "level": level.get("segment") or "",
                "level_code": level.get("level") or "",
                "level_integral": level.get("levelIntegral") or 0,
                "fan_title": level.get("fanTitle") or "",
                "joined": member.get("joined"),
            },
            "missions": missions,
            "warn": warn,
            **extra,
        }
        save_state(state)
        return state

    def _resolve_group(self, client: Client) -> str:
        """groupId 优先用配置里填的；没填就用艺人名去搜。"""
        gid = str(self.cfg.get("group_id") or "").strip()
        if gid:
            return gid
        name = str(self.cfg.get("artist_name") or "").strip()
        if not name:
            raise FansError("还没填乐迷团 ID 或艺人名，先填一个")

        body = urllib.parse.urlencode(
            {"s": name, "type": "100", "offset": "0", "limit": "10"}).encode()
        _, text = _post(WEB_HOST + "/api/search/get", body, {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"),
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": WEB_HOST + "/",
            "Cookie": client.cookie,
        })
        try:
            artists = (json.loads(text).get("result") or {}).get("artists") or []
        except Exception:
            raise FansError("搜索艺人失败，返回了非预期内容")

        for a in artists:
            group = a.get("fansGroup") or {}
            # 登录态下这个字段是 {"id": "...", "targetUrl": "orpheus://...groupId=..."}，
            # 字段名就叫 id，不是 fansGroupId；targetUrl 里再兜一层。
            gid = (group.get("fansGroupId") or group.get("groupId")
                   or group.get("id") or "")
            if not gid:
                m = re.search(r"groupId=(\d+)", str(group.get("targetUrl") or ""))
                gid = m.group(1) if m else ""
            if gid:
                self.log("info", f"按艺人「{a.get('name')}」找到乐迷团 {gid}")
                cfg = load_config()
                cfg["group_id"] = str(gid)
                save_config(cfg)
                self.cfg["group_id"] = str(gid)
                return str(gid)
        raise FansError("没从艺人信息里读到乐迷团 ID，请手动填写 groupId"
                        "（App 打开乐迷团 → 右上角分享 → 复制链接里那串数字）")

    # ---- 写操作
    def _selected_missions(self) -> set[str] | None:
        raw = self.cfg.get("mission_ids")
        if not isinstance(raw, (list, tuple, set)):
            return None
        return {str(x) for x in raw if str(x or "").strip()}

    def run(self) -> dict:
        client = self._client()
        group_id = self._resolve_group(client)
        before = self._snapshot(client, group_id)
        if before is None:
            self.log("error", "读取任务列表失败，本次先不执行")
            return {"ok": 0, "fail": 1, "skipped": 0, "error": "读取任务列表失败"}

        selected = self._selected_missions()
        pending = self._build_plan(before)
        plan = pending if selected is None else [
            m for m in pending if str(m.get("id")) in selected
        ]
        unselected = 0 if selected is None else sum(
            1 for m in pending if str(m.get("id")) not in selected
        )
        if not plan:
            if selected is None:
                self.log("info", "今天的任务都已经完成了，没有要动的")
            elif not selected:
                self.log("info", "没有勾选任何任务，跳过")
            else:
                self.log("info", "勾选的任务都完成了，没有要动的")
            return {"ok": 0, "already": len(before) - len(pending),
                    "fail": 0, "skipped": unselected}
        if unselected:
            self.log("info", f"未勾选 {unselected} 项，跳过")

        done = fail = 0
        skipped = unselected
        board_id = ""
        group_name = ""
        avatar = ""
        try:
            ginfo = (client.group_detail(group_id).get("data") or {}).get("fansGroupInfo") or {}
            board_id = ginfo.get("boardId") or ""
            group_name = ginfo.get("fansGroupName") or ""
            avatar = ginfo.get("headAvatarUrl") or ""
        except FansError:
            pass

        submitted = []
        for item in plan:
            kind = item.get("kind") or classify(item)
            label = item.get("subtitle") or item.get("title")
            if not kind:
                self.log("warn", f"不认识这个任务，跳过：{label}")
                skipped += 1
                continue
            if selected is None and not self._enabled(kind):
                self.log("info", f"「{label}」的开关是关的，跳过")
                skipped += 1
                continue
            try:
                if self._do(client, kind, item, group_id, board_id,
                            group_name, avatar):
                    submitted.append(item)
                    self.log("info", f"{label} → 已提交，等待核验")
                else:
                    skipped += 1
            except FansError as exc:
                fail += 1
                self.log("error", f"{label} 失败：{exc}")
            self._sleep()

        after = self._snapshot(client, group_id)
        if after is None:
            after = before
        elif submitted and any(not self._moved(before, after, m) for m in submitted):
            time.sleep(2)
            retry = self._snapshot(client, group_id)
            if retry is not None:
                after = retry

        old = {str(m["id"]): m for m in before}
        new = {str(m["id"]): m for m in after}
        for item in submitted:
            mid = str(item.get("id"))
            label = item.get("subtitle") or item.get("title")
            prev = (old.get(mid) or {}).get("current") or 0
            cur = (new.get(mid) or {}).get("current") or 0
            total = (new.get(mid) or {}).get("total") or item.get("total") or 0
            finished = ((new.get(mid) or {}).get("status") == "COMPLETED"
                        or (total and cur >= total))
            if finished or cur > prev:
                done += 1
                self.log("success", f"{label} → 已计入（{cur}/{total or '—'}）")
            else:
                fail += 1
                self.log("warn", f"{label} → 提交后进度没变"
                                  f"（{cur}/{total or '—'}），可能未计入")

        gained = self._gain(before, after)
        self.log("info", f"任务状态：完成 {done} · 失败 {fail} · 跳过 {skipped}")
        if gained:
            self.log("success", "本次亲密值变化：" + "，".join(gained))
        save_state(self.refresh())
        return {"ok": done, "fail": fail, "skipped": skipped,
                "already": max(0, len(before) - len(pending)), "gain": gained}

    @staticmethod
    def _moved(before: list, after: list, item: dict) -> bool:
        mid = str(item.get("id"))
        old = next((m for m in before if str(m.get("id")) == mid), {})
        new = next((m for m in after if str(m.get("id")) == mid), {})
        cur = new.get("current") or 0
        total = new.get("total") or 0
        return (new.get("status") == "COMPLETED"
                or (total and cur >= total)
                or cur > (old.get("current") or 0))

    def _enabled(self, kind: str) -> bool:
        return bool(self.cfg.get(f"{kind}_enable", kind in ("collect", "like_note")))

    def _snapshot(self, client, group_id):
        try:
            raw = client.missions(group_id).get("data") or {}
            return parse_missions(raw)
        except FansError as exc:
            self.log("warn", f"读任务列表失败：{exc}")
            return None

    @staticmethod
    def _gain(before: list, after: list) -> list:
        old = {m["id"]: (m["current"], m["total"]) for m in before}
        out = []
        for m in after:
            prev = old.get(m["id"])
            if prev and m["current"] > prev[0]:
                out.append(f"{m.get('subtitle') or m['title']} "
                           f"{prev[0]}→{m['current']}/{m['total']}")
        return out

    @staticmethod
    def _build_plan(missions: list) -> list:
        """只挑还没做完的项目。"""
        plan = []
        for m in missions:
            total = m.get("total") or 0
            current = m.get("current") or 0
            if m.get("status") == "COMPLETED" or (total and current >= total):
                continue
            plan.append(m)
        return plan

    def _do(self, client, kind, item, group_id, board_id, group_name,
            avatar: str = "") -> bool:
        action = item.get("action") or {}
        need = max(0, (item.get("total") or 1) - (item.get("current") or 0))

        if kind == "collect":
            song_id = action.get("resource_id") or action.get("song_ids")
            if isinstance(song_id, list):
                song_id = song_id[0] if song_id else ""
            if not song_id:
                self.log("warn", "这条收藏任务没带歌曲 ID，跳过")
                return False
            client.song_like(song_id, True)
            return True

        if kind == "like_note":
            return self._like_notes(client, group_id, need or 1)

        if kind == "share":
            # resourceId 在 custom 动作的 progressParams 里，_parse_action 已经挖出来了
            rid = action.get("resource_id")
            if not rid:
                self.log("warn", "分享任务没带资源 ID，跳过")
                return False
            rtype = action.get("resource_type") or "4"
            client.share_progress(rid, rtype)
            return True

        if kind == "comment":
            tid = action.get("thread_id")
            if not tid:
                self.log("warn", "评论任务没解析出 threadId，跳过")
                return False
            text = str(self.cfg.get("comment_text") or "").strip()
            if not text:
                self.log("warn", "评论文案是空的，先去设置里填一句")
                return False
            client.comment_song(tid, text)
            self.log("info", f"评论已发出：{text}")
            return True

        if kind == "publish":
            if not board_id:
                self.log("warn", "没拿到乐迷团 boardId，发不了笔记")
                return False
            if not avatar:
                self.log("warn", "没拿到乐迷团头像，发布图文笔记需要一张图片")
                return False
            try:
                req = urllib.request.Request(
                    avatar, headers={"User-Agent": UA_DESKTOP})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    image = resp.read()
            except Exception as exc:  # noqa: BLE001
                raise FansError(f"下载乐迷团头像失败：{exc}")
            if not image:
                raise FansError("乐迷团头像下载下来是空的")
            ext = "png" if ".png" in avatar.lower() else "jpg"
            pics = client.upload_image(image, ext)
            client.publish_note(
                str(self.cfg.get("publish_title") or "").strip(),
                str(self.cfg.get("publish_text") or "").strip(),
                board_id, group_name, pics)
            return True

        if kind == "play":
            ids = action.get("song_ids") or []
            rid = action.get("resource_id")
            if not ids and rid:
                ids = [rid]
            if not ids:
                self.log("warn", "播放任务没带歌曲 ID，跳过")
                return False
            # 任务要几条就播几条，play_count 只当作下限，别自己少播一半。
            # 随机抽不同的歌，避免反复播同一首导致服务端不重复计数。
            count = min(len(ids), max(need or 1, int(self.cfg.get("play_count") or 1)))
            for i, sid in enumerate(random.sample(ids, count)):
                client.play_once(sid)
                if i < count - 1:
                    self._sleep()
            return True

        return False

    def _like_notes(self, client, group_id, need) -> bool:
        liked = load_liked()
        try:
            records = (client.feed(group_id, size=min(50, max(10, need + 5)))
                       .get("data") or {}).get("records") or []
        except FansError as exc:
            raise FansError(f"拉推荐笔记失败：{exc}")
        targets = []
        for r in records:
            tid = str(r.get("threadId") or "")
            if not tid or tid in liked or r.get("owner"):
                continue
            # info.liked 已经赞过的再点一次不会涨进度，别浪费次数
            if (r.get("info") or {}).get("liked"):
                continue
            targets.append(tid)
        if not targets:
            self.log("warn", "推荐流里没有可点赞的新笔记")
            return False
        hit = 0
        for tid in targets[:need]:
            client.like_resource(tid, group_id)
            liked[tid] = int(time.time())
            hit += 1
            self._sleep()
        save_liked(liked)
        self.log("info", f"点赞 {hit} 条笔记")
        return hit > 0


def norm_cookie(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^(?i:cookie)\s*:\s*", "", text)
    text = text.replace("\r", ";").replace("\n", ";")
    return "; ".join(f"{k}={v}" for k, v in parse_cookie(text).items())


# ---------------------------------------------------------------- 快捷登录

def _save_login_cookie(cookie: str, who: str):
    """快捷登录拿到 Cookie 后由通用登录器回调，写进本模块配置。"""
    cfg = load_config()
    cfg["cookie"] = cookie
    save_config(cfg)


def _bind_login():
    from app import quick_login
    spec = quick_login.PLATFORMS.get("netease")
    if spec is not None:
        spec["save"] = _save_login_cookie


def login_start() -> bool:
    _bind_login()
    from app import quick_login
    return quick_login.get("netease").start()


def login_grab() -> dict:
    _bind_login()
    from app import quick_login
    return quick_login.get("netease").grab()


def login_snapshot() -> dict:
    from app import quick_login
    return quick_login.get("netease").snapshot()


_bind_login()


def code_mtime() -> float:
    try:
        return Path(__file__).stat().st_mtime
    except OSError:
        return 0.0
