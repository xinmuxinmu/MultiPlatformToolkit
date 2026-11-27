#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""酷狗音乐 · 每日领 VIP / 乐迷团

签名与参数拼装移植自 MakcRe/KuGouMusicApi（MIT）的 util/helper.js 与 util/request.js。
三套签名都在这里，别混用：

- android：md5(salt + 排序好的 k=v + body + salt)，salt = OIlwieks28dk2k092lksi2UIkp
  网关（gateway.kugou.com）绝大多数接口用这个。
- web：同上但 salt 换成 NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt，乐迷团那组 /youth/v1/ut/*
  用这个，且 token 要先做一次 RSA 无填充加密再拼 "h5" 前缀。

实测口径（照 develop202/GeXyu 两个在跑的签到脚本对过）：
- /youth/v2/report/listen_song 返回 status=1 表示领到，error_code=130012 表示今天已领。
- /youth/v1/ad/play_report 返回 status=1 表示领到，error_code=30002 表示次数用光。
- 只要 Cookie 里有 token + userid 就能调通，dfid 用 "-" 也行，
  mid 由 calculateMid(guid) 自己算（十进制的大整数，不是 hex）。
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.serialization import load_pem_public_key

from app.paths import DATA_ROOT

VERSION = "1.2.0"

HOME = DATA_ROOT / "kugou"
HOME.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = HOME / "config.json"
STATE_PATH = HOME / "state.json"

BASE = "https://gateway.kugou.com"

SALT_ANDROID = "OIlwieks28dk2k092lksi2UIkp"
SALT_WEB = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"

APPID_STD, CLIENTVER_STD = 1005, 20489
APPID_LITE, CLIENTVER_LITE = 3116, 11440
SRCAPPID = 2919

# 乐迷勋章（守护值）接口。和乐迷团 /youth/v1/ut/* 是两个系统：
# 勋章走 singerdiscussgo.kugou.com，appid 用账号自己的（标准版 1005），
# token 直传（不用 h5+RSA 包装），签名是 web 盐、clienttime 毫秒、body 参与签名。
MEDAL_HOST = "https://singerdiscussgo.kugou.com"
MEDAL_APPID_STD, MEDAL_APPID_LITE = 1005, 3116
MEDAL_CLIENTVER = "20000"

UA = "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi"
UA_LISTEN = "Android13-1070-10566-201-0-ReportPlaySongToServerProtocol-wifi"

KG_HEADERS = {"kg-rc": "1", "kg-thash": "5d816a0", "kg-rec": "1",
              "kg-rf": "B9EDA08A64250DEFFBCADDEE00F8F25F"}

# 乐迷团接口把 token 再包一层：明文 "moc.uoguk.59::" + token，补零到 128 字节，
# 用公钥做无填充 RSA（pow(m, e, n)）后转大写 hex，前面加 "h5"。
TOKEN_PREFIX = "moc.uoguk.59::"
# 账号接口（/v3/get_my_info）用标准版公钥，乐迷团 /youth/v1/ut/* 用概念版公钥。
# 两个公钥对应两套密钥，用混了会拿到 error_code 20006「参数错误」。
STD_PUBKEY = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDIAG7QOELSYoIJvTFJhMpe1s/g"
    "bjDJX51HBNnEl5HXqTW6lQ7LC8jr9fWZTwusknp+sVGzwd40MwP6U5yDE27M/X1+"
    "UR4tvOGOqp94TJtQ1EPnWGWXngpeIW5GxoQGao1rmYWAu6oi1z9XkChrsUdC6DJE"
    "5E221wf/4WLFxwAtRQIDAQAB\n"
    "-----END PUBLIC KEY-----"
)
LITE_PUBKEY = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDECi0Np2UR87scwrvTr72L6oO0"
    "1rBbbBPriSDFPxr3Z5syug0O24QyQO8bg27+0+4kBzTBTBOZ/WWU0WryL1JSXRTX"
    "LgFVxtzIY41Pe7lPOgsfTCn5kZcvKhYKJesKnnJDNr5/abvTGf+rHG3YRwsCHcQ0"
    "8/q6ifSioBszvb3QiwIDAQAB\n"
    "-----END PUBLIC KEY-----"
)

# 未登录类错误码（GeXyu/kgcheckin README 的已知错误码表）
LOGIN_CODES = {51002, 20018}

DEFAULT_CONFIG = {
    "cookie": "",
    "web_cookie": "",             # 网页登录态，勋章 H5 用这一份
    "userid": "",
    "token": "",
    "mid": "",
    "dfid": "-",
    "platform": "standard",       # standard | lite
    "listen_enable": True,        # 听歌领 VIP
    "ad_enable": True,            # 看广告领 VIP
    "ad_times": 8,
    "ad_gap": 30,                 # 每次领 VIP 之间等多久（秒）
    "gap_min": 2.0,
    "gap_max": 4.0,
    "medal_enable": True,          # 乐迷勋章 · 每日守护
    "medal_tasks_enable": True,    # 勋章页任务（分享/日历/点赞）
    "medal_task_ids": None,        # 手动勾选要执行的勋章任务
    "auto_all_nonpaid": True,      # 自动尝试全部非付费任务（购买/付费/金币兑换除外）
    "medal_singer_id": "188254",  # 守护的歌手 id（默认封茗囧菌）
}

class KugouError(Exception):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code

# ---------------------------------------------------------------- 签名

def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def _pairs(params: dict) -> str:
    """按键名字典序拼接 k=v。"""
    return "".join(f"{key}={params[key]}" for key in sorted(params))

def sign_android(params: dict, data: str = "") -> str:
    return md5(f"{SALT_ANDROID}{_pairs(params)}{data}{SALT_ANDROID}")

def sign_web(params: dict, data: str = "") -> str:
    return md5(f"{SALT_WEB}{_pairs(params)}{data}{SALT_WEB}")

def calc_mid(guid: str) -> str:
    """mid 不是 hex，是 md5(guid) 当 16 进制大整数转成十进制。"""
    return str(int(md5(guid), 16))

def rsa_raw_hex(text: str, pem: str = "") -> str:
    """无填充 RSA：明文补零到密钥长度后直接 pow(m, e, n)。"""
    key = load_pem_public_key((pem or STD_PUBKEY).encode())
    nums = key.public_numbers()
    size = key.key_size // 8
    raw = text.encode("utf-8")
    if len(raw) > size:
        raise KugouError("token 太长，RSA 装不下")
    m = int.from_bytes(raw.ljust(size, b"\x00"), "big")
    return format(pow(m, nums.e, nums.n), "x").zfill(size * 2)

# ---------------------------------------------------------------- 配置

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
    if not cfg.get("mid"):
        cfg["mid"] = calc_mid(str(uuid.uuid4()))
        save_config(cfg)
    return cfg

def save_config(cfg: dict):
    with _cfg_lock:
        HOME.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")

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

def parse_cookie(text: str) -> dict:
    out = {}
    for part in (text or "").replace("\r", ";").replace("\n", ";").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out

def kugoo_fields(cookie: str) -> dict:
    """网页登录后凭据塞在 KuGoo 这个 cookie 里，格式是 a=b&c=d，不是分号。

    KugooID 就是 userid，t 是网页 token。注意这个 token 调 App 接口会 20006，
    只在不方便扫码时用来兜底显示，真正能用的还是扫码拿的那个。
    """
    ck = parse_cookie(cookie)
    inner = {}
    for part in (ck.get("KuGoo") or "").split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            inner[k.strip()] = v.strip()
    return {
        "userid": inner.get("KugooID") or ck.get("KugooID") or "",
        "token": inner.get("t") or ck.get("t") or "",
        "mid": ck.get("mid") or inner.get("mid") or "",
        "dfid": ck.get("dfid") or inner.get("dfid") or "",
    }

def norm_cookie(text: str) -> str:
    text = (text or "").strip()
    if text.lower().startswith("cookie:"):
        text = text[7:]
    return "; ".join(f"{k}={v}" for k, v in parse_cookie(text).items())

def today_str() -> str:
    return time.strftime("%Y-%m-%d")

def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")

# ---------------------------------------------------------------- 客户端

class Client:
    def __init__(self, cookie: str, userid: str = "", token: str = "",
                 mid: str = "", dfid: str = "-", platform: str = "standard",
                 log=None):
        self.log = log or (lambda *_: None)
        ck = parse_cookie(cookie)
        kg = kugoo_fields(cookie)
        self.token = str(token or ck.get("token") or kg["token"] or "").strip()
        self.userid = str(userid or ck.get("userid") or kg["userid"] or "").strip()
        if not self.token:
            raise KugouError(
                "Cookie 里没有 token，这份不是登录态。"
                "请在浏览器登录 kugou.com 后重新复制整行 Cookie，或直接用快捷登录。")
        if not self.userid.isdigit():
            raise KugouError("读不到 userid，请在设置里手动填一下（纯数字）")
        self.cookie = norm_cookie(cookie)
        self.mid = str(mid or "").strip() or kg["mid"] or calc_mid(str(uuid.uuid4()))
        self.dfid = str(dfid or "-").strip() or kg["dfid"] or "-"
        lite = str(platform or "standard").lower() == "lite"
        self.appid = APPID_LITE if lite else APPID_STD
        self.clientver = CLIENTVER_LITE if lite else CLIENTVER_STD

    # ---- 底层
    def _send(self, path, params=None, data=None, method="GET", headers=None,
              sign="android", base=BASE):
        now = int(time.time())
        p = {
            "dfid": self.dfid, "mid": self.mid, "uuid": "-",
            "appid": self.appid, "clientver": self.clientver,
            "clienttime": now,
        }
        if self.token:
            p["token"] = self.token
        if self.userid:
            p["userid"] = self.userid
        p.update(params or {})

        body = None
        if data is not None:
            body = data if isinstance(data, str) else json.dumps(
                data, separators=(",", ":"), ensure_ascii=False)

        hdrs = {
            "User-Agent": UA,
            "dfid": str(p["dfid"]),
            "clienttime": str(p["clienttime"]),
            "mid": str(p["mid"]),
            "Cookie": self.cookie,
            **KG_HEADERS,
        }
        if body is not None:
            hdrs["Content-Type"] = "application/json; charset=utf-8"
        hdrs.update(headers or {})

        if sign == "android":
            p["signature"] = sign_android(p, body or "")
        elif sign == "web":
            p["signature"] = sign_web(p, body or "")

        url = base + path + "?" + urllib.parse.urlencode(p)
        req = urllib.request.Request(
            url, data=body.encode("utf-8") if body is not None else None,
            headers=hdrs, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if not raw:
                raise KugouError(f"{path} 返回 HTTP {exc.code}")
        except Exception as exc:  # noqa: BLE001
            raise KugouError(f"请求失败：{exc}")
        try:
            payload = json.loads(_decode(raw))
        except Exception:
            raise KugouError(f"接口返回的不是 JSON：{_decode(raw)[:150]}")
        code = _int(payload.get("error_code"))
        if code in LOGIN_CODES:
            raise KugouError("登录态失效了，重新快捷登录一次", code)
        return payload

    # ---- 账号
    def my_info(self) -> dict:
        pk = rsa_raw_hex(json.dumps(
            {"token": self.token, "clienttime": int(time.time())},
            separators=(",", ":"))).upper()
        return self._send(
            "/v3/get_my_info", params={"plat": 1},
            data={"visit_time": int(time.time()), "usertype": 1,
                  "p": pk, "userid": int(self.userid)},
            method="POST", headers={"x-router": "usercenter.kugou.com"})

    # ---- 领 VIP
    def listen_song(self) -> dict:
        return self._send(
            "/youth/v2/report/listen_song",
            params={"clientver": 10566},
            data={"mixsongid": 666075191},
            method="POST", headers={"User-Agent": UA_LISTEN})

    def ad_vip(self) -> dict:
        now = int(time.time() * 1000)
        return self._send(
            "/youth/v1/ad/play_report",
            data={"ad_id": 12307537187,
                  "play_end": now, "play_start": now - 30000},
            method="POST")

    # ---- 乐迷团（web 签名 + h5 token）
    def _send_team(self, path: str, with_period: bool = False,
                   period_id: str = "") -> dict:
        now_ms = int(time.time() * 1000)
        p = {
            "srcappid": SRCAPPID, "clientver": self.clientver,
            "clienttime": now_ms, "mid": self.mid, "uuid": "-",
            "dfid": self.dfid, "appid": self.appid,
            "userid": int(self.userid),
            "token": "h5" + rsa_raw_hex(TOKEN_PREFIX + self.token, LITE_PUBKEY).upper(),
        }
        if with_period:
            p["period_id"] = period_id
        p["signature"] = sign_web(p)
        url = BASE + path + "?" + urllib.parse.urlencode(p)
        hdrs = {
            "User-Agent": UA,
            "Host": "gateway.kugou.com",
            "dfid": self.dfid,
            "clienttime": str(now_ms),
            "mid": self.mid,
            "Cookie": self.cookie,
            **KG_HEADERS,
        }
        req = urllib.request.Request(url, headers=hdrs, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if not raw:
                raise KugouError(f"乐迷团接口返回 HTTP {exc.code}")
        except Exception as exc:  # noqa: BLE001
            raise KugouError(f"乐迷团接口请求失败：{exc}")
        try:
            return json.loads(_decode(raw))
        except Exception:
            raise KugouError(f"乐迷团接口返回的不是 JSON：{_decode(raw)[:150]}")

    def period_info(self) -> dict:
        return self._send_team("/youth/v1/ut/get_period_info")

    def team_info(self, period_id: str = "") -> dict:
        return self._send_team("/youth/v1/ut/get_my_team_info",
                               with_period=bool(period_id), period_id=period_id)

    def user_status(self, period_id: str = "") -> dict:
        return self._send_team("/youth/v1/ut/get_user_status",
                               with_period=bool(period_id), period_id=period_id)

# ---------------------------------------------------------------- 乐迷勋章（守护值）
#
# 勋章系统和乐迷团（/youth/v1/ut/*）完全独立：
#   GET  /v1/task/progress_list   任务列表 + 等级/守护值/排名（classify_id = 歌手 id）
#   POST /v1/task/finish/sign_in  每日守护，body {"singer_id": <歌手id>}
#   POST /v1/task/finish/sign_in_additional  追加守护（App 里要"看视频"），
#        body {"singer_id": id, "auth_sign": md5(f"{userid}_{singer_id}_{clienttimems}")}
# 签名规则：web 盐、clienttime 用毫秒、uuid=mid、POST 时 JSON body 参与 md5。
# 20217 = token 不属于这个 appid（要换回 1005）；30008 = auth_sign 算法不对。

class MedalClient:
    def __init__(self, userid: str, token: str, mid: str = "",
                 dfid: str = "-", log=None):
        self.log = log or (lambda *_: None)
        self.userid = str(userid or "").strip()
        self.token = str(token or "").strip()
        if not self.token or not self.userid.isdigit():
            raise KugouError("勋章接口需要 userid + token（扫码登录拿的那种）")
        self.mid = str(mid or "").strip() or calc_mid(str(uuid.uuid4()))
        self.dfid = str(dfid or "-").strip() or "-"
        # 实测：勋章接口只认账号自己的 appid（标准版 1005），
        # 传 1058/1014 会报 20017 invalid signature，概念版 3116 没验证过。
        self.appid = MEDAL_APPID_STD

    def _call(self, path: str, params: dict | None = None,
              data: dict | None = None, host: str | None = None) -> dict:
        now_ms = int(time.time() * 1000)
        p = {
            "appid": self.appid, "clientver": MEDAL_CLIENTVER,
            "clienttime": now_ms, "mid": self.mid, "uuid": self.mid,
            "dfid": self.dfid, "srcappid": SRCAPPID,
            "userid": self.userid, "token": self.token,
        }
        p.update(params or {})
        body = ""
        if data is not None:
            body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        p["signature"] = sign_web(p, body)
        base = (host or MEDAL_HOST).rstrip("/")
        url = base + "/" + path.lstrip("/") + "?" + urllib.parse.urlencode(p)
        req = urllib.request.Request(
            url, data=body.encode("utf-8") if data is not None else None,
            method="POST" if data is not None else "GET",
            headers={
                "User-Agent": UA,
                "Content-Type": "application/json; charset=utf-8",
                "Cookie": f"token={self.token}; userid={self.userid}",
            })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if exc.code == 404:
                raise KugouError(f"该任务没有可用接口（HTTP 404）：{path}", 404)
            if not raw:
                raise KugouError(f"勋章接口返回 HTTP {exc.code}", exc.code)
        except Exception as exc:  # noqa: BLE001
            raise KugouError(f"勋章接口请求失败：{exc}")
        try:
            payload = json.loads(_decode(raw))
        except Exception:
            raise KugouError(f"勋章接口返回的不是 JSON：{_decode(raw)[:150]}")
        code = _int(payload.get("errcode"))
        if code == 20017:
            raise KugouError("勋章接口说 token 校验失败（20017）。"
                             "多半是 token 过期了，重新扫码登录一次", code)
        if code in LOGIN_CODES:
            raise KugouError("登录态失效了，重新快捷登录一次", code)
        return payload

    # ---- 只读
    def progress_list(self, classify_id: str) -> dict:
        return self._call("/v1/task/progress_list",
                          {"type": "singer_energy", "classify_id": str(classify_id)})

    # ---- 执行
    def sign_in(self, singer_id: int) -> dict:
        return self._call("/v1/task/finish/sign_in",
                          data={"singer_id": int(singer_id)})

    def sign_in_additional(self, singer_id: int) -> dict:
        now_ms = int(time.time() * 1000)
        auth = md5(f"{self.userid}_{int(singer_id)}_{now_ms}")
        return self._call("/v1/task/finish/sign_in_additional",
                          data={"singer_id": int(singer_id), "auth_sign": auth})

    # ---- 勋章页任务
    def task_finish(self, task_type: str, data: dict) -> dict:
        return self._call("/v1/task/finish/" + task_type, data=data)

    def fans_rank(self, singer_id: int) -> dict:
        return self._call("/v1/task/fans_integral_rank", params={
            "singer_id": int(singer_id), "classify_id": int(singer_id),
            "type": "singer_energy", "page": 1, "page_size": 20})

    def like_fan(self, singer_id: int, liked_user_id: int, user_id: int) -> dict:
        return self._call("/v1/task/finish/fans_like", data={
            "singer_id": int(singer_id), "liked_user_id": int(liked_user_id),
            "user_id": int(user_id), "like": 1})

    def outside_finish(self, task_id: str, singer_id: int,
                       event_id: str = "") -> dict:
        """通用任务上报（H5/App 外部动作）。实测每次 +1 次完成次数。"""
        body = {
            "task_id": str(task_id),
            "singer_id": int(singer_id),
            "classify_id": int(singer_id),
            "event_id": event_id or f"{int(time.time() * 1000)}_{task_id}_{self.userid}",
        }
        return self._call("/v2/task/outside_finish", data=body)

def medal_of(payload: dict) -> dict:
    """progress_list 的返回压成 UI 好用的形状。"""
    data = (payload or {}).get("data") or {}
    uls = data.get("user_level_status") or {}
    tasks = ((data.get("medal_task_data") or {}).get("task_list")) or []
    rows = [
        {"name": _str(t.get("name")), "task_id": _str(t.get("task_id")),
         "finish": _int(t.get("finish_times")) or 0,
         "need": _int(t.get("times")) or 1,
         "integral": _int(t.get("integral")) or 0}
        for t in tasks
    ]
    done = sum(1 for t in rows if t["finish"] >= t["need"])
    return {
        "level": _int(uls.get("level")) or 0,
        "integral": _int(uls.get("cur_integral")) or 0,
        "today": _int(uls.get("today_integral")) or 0,
        "signed": _int(uls.get("has_singer_sign")) == 1,
        "follow_day": _int(uls.get("follow_day")) or 0,
        "rank": _str(uls.get("rank") or "—"),
        "task_done": done,
        "task_total": len(rows),
        "tasks": rows,
    }

# ---------------------------------------------------------------- 小工具

def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

def _str(v):
    return "" if v is None else str(v)

def profile_of(info: dict) -> dict:
    """/v3/get_my_info 的结构：data 里放昵称、头像、busi_vip 到期时间。"""
    data = info.get("data") or {}
    vip_end = ""
    for item in (data.get("busi_vip") or []):
        end = item.get("vip_end_time") or item.get("end_time") or ""
        if end and end > vip_end:
            vip_end = end
    return {
        "nickname": _str(data.get("nickname")),
        "userid": _str(data.get("userid") or data.get("kugouid")),
        "avatar": _str(data.get("pic") or data.get("avatar")),
        "vip_end": vip_end,
        "vip_type": _int(data.get("vip_type")),
    }

def claim_result(payload: dict) -> tuple[bool, str]:
    """把接口返回翻译成「领到了 / 今天已领 / 用光了 / 失败」。"""
    code = _int(payload.get("error_code"))
    if _int(payload.get("status")) == 1:
        return True, "领取成功"
    if code == 130012:
        return True, "今天已经领过了"
    if code == 30002:
        return False, "今天次数已经用光"
    return False, _str(payload.get("error_msg") or payload.get("msg")
                       or payload.get("error") or f"失败（error_code={code}）")

def progress_of(state: dict) -> dict:
    """当天进度。接口没有「查今天领没领」的读接口，所以只能自己记账。"""
    p = state.get("progress") or {}
    if p.get("date") != today_str():
        p = {"date": today_str(), "listen": False, "ad": 0}
    return p

# ---------------------------------------------------------------- 日志任务

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
            self.logs.append({"id": self.seq, "l": level, "m": msg,
                              "t": time.strftime("%H:%M:%S")})

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
            result = KugouDaily(cfg, self.log).run()
        except KugouError as exc:
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
            self.log("finish", "已结束（有错误）" if result.get("error") else "已结束")

TASK = Task()

# ---------------------------------------------------------------- 主流程

AUTO_MEDAL_TASKS = ("fans_medal_share", "calendar_reminder", "fans_like")
# 这三个走 /v1/task/finish/*；其余非付费任务走 /v2/task/outside_finish
TASK_FINISH_TYPES = ("fans_medal_share", "calendar_reminder")

# 涉及真实花钱/货币兑换的任务，永远不自动执行
PAID_TASK_ID_MARKERS = ("purchase", "pay_", "buy_", "coin_",
                        "battle_pass", "svip", "vip")
PAID_TASK_NAME_MARKERS = ("购买", "付费", "开通", "金币", "超会", "会员")

def is_paid_task(task: dict) -> bool:
    """购买/付费/金币兑换类任务 → 不自动勾选、不自动执行。"""
    tid = str(task.get("task_id") or "").lower()
    name = str(task.get("name") or "")
    return (any(m in tid for m in PAID_TASK_ID_MARKERS)
            or any(m in name for m in PAID_TASK_NAME_MARKERS))

class KugouDaily:
    def __init__(self, cfg, log):
        self.cfg = cfg or {}
        self.log = log

    def _client(self) -> Client:
        c = self.cfg
        return Client(norm_cookie(c.get("cookie", "")), c.get("userid", ""),
                      c.get("token", ""), c.get("mid", ""), c.get("dfid", "-"),
                      c.get("platform", "standard"), self.log)

    def _medal_client(self) -> MedalClient:
        c = self.cfg
        return MedalClient(c.get("userid", ""), c.get("token", ""),
                           c.get("mid", ""), c.get("dfid", "-"), self.log)

    def _medal_singer(self) -> int:
        return _int(self.cfg.get("medal_singer_id")) or 188254

    def _fetch_medal(self) -> dict:
        """只读勋章状态。读不到就返回 error 字符串，不拖垮整个 refresh。"""
        try:
            mc = self._medal_client()
            payload = mc.progress_list(self._medal_singer())
            if _int(payload.get("errcode")) != 0:
                return {"error": f"勋章接口 errcode {payload.get('errcode')}：{payload.get('errmsg')}"}
            return medal_of(payload)
        except KugouError as exc:
            return {"error": str(exc)}

    def _selected_medal_tasks(self, tasks: dict | None = None) -> set[str] | None:
        if self.cfg.get("auto_all_nonpaid", True) and tasks is not None:
            return {tid for tid, t in tasks.items()
                    if tid != "sign_in" and not is_paid_task(t)}
        raw = self.cfg.get("medal_task_ids")
        if not isinstance(raw, (list, tuple, set)):
            return set(AUTO_MEDAL_TASKS)
        return {str(x) for x in raw if str(x or "").strip()}

    def _run_medal_tasks(self, mc, sid: int) -> tuple[dict, int, int, int]:
        """遍历账号当前的全部勋章任务，只执行设置里勾选了的那几项。"""
        info = self._fetch_medal()
        if info.get("error"):
            raise KugouError(info["error"])
        tasks = {t["task_id"]: t for t in info.get("tasks", [])}
        auto_all = bool(self.cfg.get("auto_all_nonpaid", True))
        selected = self._selected_medal_tasks(tasks)
        done = fail = skipped = 0
        if auto_all:
            paid = [t for tid, t in tasks.items()
                    if tid != "sign_in" and is_paid_task(t)]
            self.log("info", "勋章任务：自动尝试全部非付费任务"
                             f"（{len(selected)} 项；购买/付费/金币兑换 {len(paid)} 项已排除）")

        missing = sorted(t for t in selected
                         if t != "sign_in" and t not in tasks)
        if missing:
            self.log("info", "已选任务不在当前列表（可能已下线/换期）："
                     + "、".join(missing[:6])
                     + (" 等" if len(missing) > 6 else ""))
        if not selected:
            self.log("info", "勋章页任务：没有可自动尝试的非付费任务，跳过"
                     if auto_all else "勋章页任务：没有勾选任何任务，跳过")
            return info, 0, 0, skipped
        selected_pending = [t for tid, t in tasks.items()
                            if tid != "sign_in" and tid in selected
                            and (t.get("finish") or 0) < (t.get("need") or 1)]
        if not selected_pending:
            self.log("info", "勋章页任务：已选任务今天都已完成，无需重复执行")
            unselected = [x for tid, x in tasks.items()
                          if tid != "sign_in"
                          and x["finish"] < x["need"] and tid not in selected]
            skipped += len(unselected)
            return info, 0, 0, skipped

        for tid, t in tasks.items():
            if tid == "sign_in":
                continue
            if tid not in selected:
                continue
            need = max(0, (t.get("need") or 1) - (t.get("finish") or 0))
            if need <= 0:
                continue
            label = t.get("name") or tid
            if tid == "fans_like":
                try:
                    rank = mc.fans_rank(int(sid))
                    users = (rank.get("data") or {}).get("list") or []
                    uid = _int(self.cfg.get("userid")) or 0
                    got = 0
                    for u in users:
                        if got >= need:
                            break
                        if u.get("liked_today"):
                            continue
                        r = mc.like_fan(int(sid), u.get("user_id"), uid)
                        if _int(r.get("errcode")) == 0:
                            got += 1
                            self.log("success", f"点赞乐迷 {u.get('nickname') or u.get('user_id')}：{got}/{need}")
                        else:
                            self.log("warn", f"点赞乐迷失败：{r.get('errmsg')}（errcode {r.get('errcode')}）")
                            fail += 1
                            break
                        time.sleep(random.uniform(1.2, 2.0))
                    done += got
                    if got < need:
                        skipped += 1
                        self.log("info", f"{label}还差 {need - got} 次，可用乐迷不足或接口限制")
                except KugouError as exc:
                    if self._log_task_error(label, exc):
                        skipped += 1
                    else:
                        fail += 1
            elif tid in TASK_FINISH_TYPES:
                try:
                    r = mc.task_finish(tid, {"singer_id": int(sid)})
                except KugouError as exc:
                    if self._log_task_error(label, exc):
                        skipped += 1
                    else:
                        fail += 1
                    continue
                if _int(r.get("errcode")) == 0:
                    self.log("success", f"{label}：已完成")
                    done += 1
                elif _int(r.get("errcode")) == 20010:
                    self.log("info", f"{label}：需要在酷狗 App 内完成，跳过")
                    skipped += 1
                else:
                    self.log("warn", f"{label}：{r.get('errmsg')}（errcode {r.get('errcode')}）")
                    fail += 1
                time.sleep(random.uniform(1.2, 2.0))
            else:
                # 其余任务统一走 H5/App 外部动作上报接口，每调一次完成次数 +1
                got = 0
                try:
                    for n in range(need):
                        r = mc.outside_finish(
                            tid, int(sid),
                            event_id=f"{int(time.time() * 1000)}_{tid}_"
                                     f"{self.cfg.get('userid', '')}_{n}")
                        if (_int(r.get("errcode")) == 0
                                and _int(r.get("status")) == 1):
                            got += 1
                        else:
                            self.log("warn",
                                     f"{label}：{r.get('errmsg') or '上报失败'}"
                                     f"（errcode {r.get('errcode')}）")
                            break
                        if n < need - 1:
                            time.sleep(random.uniform(0.5, 1.0))
                except KugouError as exc:
                    if self._log_task_error(label, exc):
                        skipped += 1
                    else:
                        fail += 1
                    continue
                if got >= need:
                    self.log("success", f"{label}：已完成 {got}/{need}")
                    done += 1
                elif got:
                    self.log("info", f"{label}：完成 {got}/{need}，剩余下次继续")
                    skipped += 1
                else:
                    self.log("warn", f"{label}：本次没有完成")
                    fail += 1

        after = self._fetch_medal()
        if after.get("error"):
            return info, done, fail, skipped
        after_tasks = {x["task_id"]: x for x in after.get("tasks", [])}
        selected_left = [x.get("name") or tid for tid, x in after_tasks.items()
                         if tid != "sign_in" and x["finish"] < x["need"] and tid in selected]
        unselected = [tid for tid, x in after_tasks.items()
                      if tid != "sign_in" and x["finish"] < x["need"] and tid not in selected]
        skipped += len(unselected)
        self.log("info",
                 f"勋章任务：{after.get('task_done')}/{after.get('task_total')} 已完成"
                 f"（本次执行 {done} 项，已选未完成 {len(selected_left)} 项，"
                 f"未勾选/付费排除 {len(unselected)} 项）")
        if selected_left:
            self.log("info", "已选任务未完成：" + "、".join(selected_left[:8])
                     + (" 等" if len(selected_left) > 8 else ""))
        return after, done, fail, skipped

    def _log_task_error(self, label: str, exc: KugouError) -> bool:
        """任务接口报错：404 = 平台没有该任务接口（跳过），其余算失败。"""
        if getattr(exc, "code", None) == 404:
            self.log("info", f"{label}：平台没有对应接口，跳过")
            return True
        self.log("warn", f"{label}：{exc}")
        return False

    def _sleep(self):
        a = float(self.cfg.get("gap_min") or 2)
        b = float(self.cfg.get("gap_max") or 4)
        time.sleep(random.uniform(min(a, b), max(a, b)))

    # ---- 只读
    def refresh(self) -> dict:
        client = self._client()
        payload = client.my_info()
        prof = profile_of(payload)
        if not prof["nickname"]:
            if _int(payload.get("error_code")) == 20006:
                raise KugouError(
                    "这份 token 调不了接口（error_code 20006）。"
                    "网页登录拿到的 t 只能在网页上用，请点「扫码登录」拿一个真 token。")
            raise KugouError("账号接口没返回昵称，这个 token 可能已经过期")

        prev = load_state()
        prog = progress_of(prev)
        team = {}
        try:
            period = client.period_info()
            if _int(period.get("status")) == 1:
                team = {"period": period.get("data") or {}}
                current = (team["period"] or {}).get("current_period_info") or {}
                pid = _str(current.get("id")
                           or (team["period"] or {}).get("period_id")
                           or (team["period"] or {}).get("id"))
                if pid:
                    st = client.user_status(pid)
                    if _int(st.get("status")) == 1:
                        team["status"] = st.get("data") or {}
                    elif _int(st.get("error_code")) == 51002:
                        team["error"] = "乐迷团接口返回 51002：当前 token 不被概念版乐迷团认可，需要概念版登录"
                    tm = client.team_info(pid)
                    if _int(tm.get("status")) == 1:
                        team["team"] = tm.get("data") or {}
                    team["period_id"] = pid
        except KugouError as exc:
            team = {"error": str(exc)}

        medal = self._fetch_medal()

        info = {
            **prof,
            "listen_done": bool(prog.get("listen")),
            "ad_done": _int(prog.get("ad")) or 0,
            "ad_max": _int(self.cfg.get("ad_times")) or 8,
            "team": team,
            "medal": medal,
            "medal_singer_id": self._medal_singer(),
            "fetched_at": int(time.time()),
            "progress": prog,
        }
        save_state(info)
        return info

    # ---- 执行
    def run(self) -> dict:
        client = self._client()
        prev = load_state()
        prog = progress_of(prev)
        done = fail = skipped = 0

        prof = profile_of(client.my_info())
        if prof["nickname"]:
            self.log("info", f"账号：{prof['nickname']}")

        # 1) 听歌领 VIP
        if not self.cfg.get("listen_enable", True):
            self.log("info", "听歌领 VIP 关着，跳过")
        elif prog.get("listen"):
            self.log("info", "听歌领 VIP 今天已经领过，跳过")
            skipped += 1
        else:
            try:
                ok, msg = claim_result(client.listen_song())
                self.log("success" if ok else "warn", f"听歌领 VIP：{msg}")
                prog["listen"] = True if ok else False
                done += 1 if ok else 0
                fail += 0 if ok else 1
            except KugouError as exc:
                self.log("error", f"听歌领 VIP 失败：{exc}")
                fail += 1
            self._save_prog(prog)
            self._sleep()

        # 2) 看广告领 VIP
        if not self.cfg.get("ad_enable", True):
            self.log("info", "广告领 VIP 关着，跳过")
        else:
            times = _int(self.cfg.get("ad_times")) or 8
            gap = max(1, _int(self.cfg.get("ad_gap")) or 30)
            got = _int(prog.get("ad")) or 0
            if got >= times:
                self.log("info", f"广告领 VIP 今天已经领满 {times} 次，跳过")
                skipped += 1
            else:
                for i in range(got + 1, times + 1):
                    try:
                        ok, msg = claim_result(client.ad_vip())
                    except KugouError as exc:
                        self.log("error", f"第 {i} 次领 VIP 失败：{exc}")
                        fail += 1
                        break
                    if ok:
                        self.log("success", f"第 {i} 次领 VIP：{msg}")
                        got = i
                        prog["ad"] = got
                        done += 1
                        self._save_prog(prog)
                    else:
                        self.log("warn", f"第 {i} 次领 VIP：{msg}")
                        fail += 1
                        break
                    if i < times:
                        self.log("info", f"等 {gap} 秒再领下一次")
                        time.sleep(gap)

        # 3) 乐迷勋章：每日守护 + 追加守护（实测每次各 +10 守护值）
        medal = {}
        if not self.cfg.get("medal_enable", True):
            self.log("info", "乐迷勋章关着，跳过")
        else:
            try:
                mc = self._medal_client()
                sid = self._medal_singer()
                info = self._fetch_medal()
                if info.get("error"):
                    self.log("error", f"乐迷勋章读不到状态：{info['error']}")
                    fail += 1
                else:
                    medal = info
                    tasks = {t["task_id"]: t for t in info.get("tasks", [])}
                    sign = tasks.get("sign_in") or {}
                    if sign.get("finish", 0) >= (sign.get("need") or 1):
                        self.log("info", "每日守护今天已经做过了")
                        skipped += 1
                    else:
                        r = mc.sign_in(sid)
                        d = r.get("data") or {}
                        if _int(r.get("errcode")) == 0:
                            self.log("success", f"每日守护：守护值 +{d.get('incr_score', '?')}，"
                                                f"累计 {d.get('total_score', '?')}，排名 {d.get('rank_num', '?')}")
                            done += 1
                        else:
                            self.log("warn", f"每日守护：{r.get('errmsg')}（errcode {r.get('errcode')}）")
                            fail += 1
                        self._sleep()
                    # 追加守护：App 里要求先看视频，接口只校验 auth_sign = md5(userid_singerid_clienttime)
                    # today 里包含签到给的分数，追加一次后再查一次判断有没有用
                    before = info.get("today") or 0
                    r2 = mc.sign_in_additional(sid)
                    if _int(r2.get("errcode")) == 0:
                        time.sleep(1)
                        after_info = self._fetch_medal()
                        gained = (after_info.get("today") or 0) - before
                        if gained > 0:
                            self.log("success", f"追加守护：守护值 +{gained}，今日合计 +{after_info.get('today')}")
                            done += 1
                            medal = after_info
                        else:
                            self.log("info", "追加守护接口说成功，但今天追加次数已用完")
                            skipped += 1
                    else:
                        # 30001 = 当天追加次数已用完，不算失败
                        if _int(r2.get("errcode")) == 30001:
                            self.log("info", "追加守护：今天已经追加过了，跳过")
                            skipped += 1
                        else:
                            self.log("warn", f"追加守护：{r2.get('errmsg')}（errcode {r2.get('errcode')}）")
                            fail += 1
                    if self.cfg.get("medal_tasks_enable", True):
                        after_medal, td, tf, ts = self._run_medal_tasks(mc, sid)
                        medal = after_medal
                        done += td
                        fail += tf
                        skipped += ts
                    else:
                        self.log("info", "勋章页任务关着，跳过")
            except KugouError as exc:
                self.log("error", f"乐迷勋章失败：{exc}")
                fail += 1

        after = load_state()
        after.update({"listen_done": bool(prog.get("listen")),
                      "ad_done": _int(prog.get("ad")) or 0,
                      "progress": prog,
                      "fetched_at": int(time.time())})
        if medal:
            after["medal"] = medal
        try:
            prof2 = profile_of(client.my_info())
            if prof2["nickname"]:
                after.update(prof2)
        except KugouError:
            pass
        save_state(after)
        self.log("info", f"完成 {done} · 失败 {fail} · 跳过 {skipped}"
                         "（跳过含今天已完成、次数用尽和未勾选的任务）")
        return {"ok": done, "fail": fail, "skipped": skipped}

    def _save_prog(self, prog: dict):
        state = load_state()
        state["progress"] = prog
        save_state(state)

# ---------------------------------------------------------------- 扫码登录
#
# 酷狗网页版登录后 cookie 里的 t / KuGoo 只够网页自己用，调 /v3、/youth 这些
# App 接口会直接返回 20006/20018。真正能用的 token 只能从扫码登录拿：
#   /v2/qrcode               拿 key + 二维码图片（base64 直接给前端显示）
#   /v2/get_userinfo_qrcode  轮询，data.status: 0 过期 / 1 待扫 / 2 待确认 / 4 成功
# status=4 时返回 token + userid，这才是能调接口的那一对。

LOGIN_HOST = "https://login-user.kugou.com"
QR_TEXT = f"https://h5.kugou.com/apps/loginQRCode/html/index.html?appid={APPID_STD}&"

def _platform_ids(platform: str) -> tuple[int, int]:
    if str(platform or "").lower() == "lite":
        return APPID_LITE, CLIENTVER_LITE
    return APPID_STD, CLIENTVER_STD

def _login_get(path: str, extra: dict, mid: str, platform: str = "standard") -> dict:
    now = int(time.time())
    appid, clientver = _platform_ids(platform)
    p = {"dfid": "-", "mid": mid, "uuid": "-", "appid": appid,
         "clientver": clientver, "clienttime": now, **extra}
    p["signature"] = sign_web(p)
    url = LOGIN_HOST + path + "?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "dfid": "-", "mid": mid,
        "clienttime": str(now), "Cookie": f"kg_mid={mid}", **KG_HEADERS})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except Exception as exc:  # noqa: BLE001
        raise KugouError(f"登录接口请求失败：{exc}")
    try:
        return json.loads(_decode(raw))
    except Exception:
        raise KugouError(f"登录接口返回的不是 JSON：{_decode(raw)[:150]}")

def qr_start(platform: str = "standard") -> dict:
    """取二维码。图是接口直接给的 base64，不用自己画。"""
    cfg = load_config()
    appid, _ = _platform_ids(platform)
    data = _login_get("/v2/qrcode", {
        "appid": 1001, "type": 1, "plat": 4,       # 拿 key 用 1001，校验时才用目标 appid
        "qrcode_txt": f"https://h5.kugou.com/apps/loginQRCode/html/index.html?appid={appid}&",
        "srcappid": SRCAPPID}, cfg["mid"], platform)
    body = data.get("data") or {}
    if not body.get("qrcode"):
        raise KugouError(f"没拿到二维码（error_code={data.get('error_code')}）")
    return {"key": body["qrcode"], "img": body.get("qrcode_img") or ""}

def qr_poll(key: str, platform: str = "standard") -> dict:
    cfg = load_config()
    appid, clientver = _platform_ids(platform)
    data = _login_get("/v2/get_userinfo_qrcode", {
        "plat": 4, "appid": appid, "clientver": clientver, "srcappid": SRCAPPID,
        "qrcode": key, "dev": "-"}, cfg["mid"], platform)
    body = data.get("data") or {}
    state = _int(body.get("status"))
    if state == 4:
        token = str(body.get("token") or "")
        userid = str(body.get("userid") or "")
        if not token or not userid:
            return {"stage": "error", "msg": "扫码通过了但没拿到 token，再扫一次试试"}
        cfg["token"] = token
        cfg["userid"] = userid
        cfg["platform"] = "lite" if str(platform).lower() == "lite" else "standard"
        cfg["cookie"] = f"token={token}; userid={userid}; kg_mid={cfg['mid']}"
        save_config(cfg)
        return {"stage": "success", "msg": "登录成功，token 已保存", "userid": userid}
    if state == 2:
        return {"stage": "confirm", "msg": "已扫码，在手机上点一下确认"}
    if state == 0:
        return {"stage": "expired", "msg": "二维码过期了，点「换一张」刷新"}
    return {"stage": "waiting", "msg": "用手机酷狗 App 扫码"}

# ---------------------------------------------------------------- 快捷登录

def _verify_kugou(cookie: str) -> tuple[bool, str]:
    """网页登录的 token 只给勋章 / H5 用，调不了 App 接口（20006/20018）。

    以前这里硬要用 my_info 校验，结果网页登录永远被判失败、逼着用户去扫码，
    而扫码拿到的 App token 又调不了勋章接口——两边互相堵死。现在只要有
    token + userid 就收下，存进 web_cookie，徽章接口用它。
    """
    fields = kugoo_fields(cookie)
    if not fields["token"] or not fields["userid"].isdigit():
        return False, ""
    nickname = ""
    cfg = load_config()
    try:
        info = Client(norm_cookie(cookie), fields["userid"], fields["token"],
                      fields["mid"] or cfg.get("mid", ""),
                      fields["dfid"] or cfg.get("dfid", "-"),
                      cfg.get("platform", "standard")).my_info()
        nickname = profile_of(info)["nickname"]
    except Exception:  # noqa: BLE001
        pass
    return True, nickname or fields["userid"]

def _save_login_cookie(cookie: str, who: str):
    """网页登录的 Cookie 单独存 web_cookie。

    别覆盖扫码拿到的 App token：听歌 / 广告领 VIP 走 App 接口，
    网页 token 上去会直接 20006；反过来勋章 H5 只认网页这套。
    """
    cfg = load_config()
    field = kugoo_fields(cookie)
    cfg["web_cookie"] = cookie
    if not cfg.get("token") and field["token"]:
        cfg["token"] = field["token"]
    if not str(cfg.get("userid") or "").isdigit() and field["userid"].isdigit():
        cfg["userid"] = field["userid"]
    if not cfg.get("mid") and field["mid"]:
        cfg["mid"] = field["mid"]
    if not cfg.get("dfid") and field["dfid"]:
        cfg["dfid"] = field["dfid"]
    save_config(cfg)

def _bind_login():
    from app import quick_login
    spec = quick_login.PLATFORMS.get("kugou")
    if spec is not None:
        spec["verify"] = _verify_kugou
        spec["save"] = _save_login_cookie

def login_start() -> bool:
    _bind_login()
    from app import quick_login
    return quick_login.get("kugou").start()

def login_grab() -> dict:
    _bind_login()
    from app import quick_login
    return quick_login.get("kugou").grab()

def login_snapshot() -> dict:
    from app import quick_login
    return quick_login.get("kugou").snapshot()

_bind_login()

def code_mtime() -> float:
    try:
        return Path(__file__).stat().st_mtime
    except OSError:
        return 0.0
