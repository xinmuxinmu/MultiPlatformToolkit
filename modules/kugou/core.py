#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""酷狗音乐 · 每日领 VIP / 乐迷团

签名与参数拼装移植自 MakcRe/KuGouMusicApi（MIT）的 util/helper.js 与 util/request.js。
三套签名都在这里，别混用：

- android：md5(salt + 排序好的 k=v + body + salt)，salt = OIlwieks28dk2k092lksi2UIkp
  网关（gateway.kugou.com）绝大多数接口用这个。
- web：同上但 salt 换成 NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt，乐迷团那组 /youth/v1/ut/*
  用这个，且 token 要先做一次 RSA 无填充加密再拼 "h5" 前缀。
- register：设备注册专用，本轮没用到。

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

VERSION = "1.0.0"

HOME = DATA_ROOT / "kugou"
HOME.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = HOME / "config.json"
STATE_PATH = HOME / "state.json"

BASE = "https://gateway.kugou.com"

SALT_ANDROID = "OIlwieks28dk2k092lksi2UIkp"
SALT_WEB = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
SALT_REGISTER = "1014"

APPID_STD, CLIENTVER_STD = 1005, 20489
APPID_LITE, CLIENTVER_LITE = 3116, 11440
SRCAPPID = 2919

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
}


class KugouError(Exception):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- 签名

def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _pairs(params: dict, with_equal: bool = True) -> str:
    """按键名字典序拼接。酷狗这边是 k=v 再 join，register 那套只要值。"""
    keys = sorted(params)
    if with_equal:
        return "".join(f"{k}={params[k]}" for k in keys)
    return "".join(str(params[k]) for k in keys)


def sign_android(params: dict, data: str = "") -> str:
    return md5(f"{SALT_ANDROID}{_pairs(params)}{data}{SALT_ANDROID}")


def sign_web(params: dict, data: str = "") -> str:
    return md5(f"{SALT_WEB}{_pairs(params)}{data}{SALT_WEB}")


def sign_register(params: dict) -> str:
    return md5(f"{SALT_REGISTER}{_pairs(params, with_equal=False)}{SALT_REGISTER}")


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
            self.log("finish", "全部完成")


TASK = Task()


# ---------------------------------------------------------------- 主流程

class KugouDaily:
    def __init__(self, cfg, log):
        self.cfg = cfg or {}
        self.log = log

    def _client(self) -> Client:
        c = self.cfg
        return Client(norm_cookie(c.get("cookie", "")), c.get("userid", ""),
                      c.get("token", ""), c.get("mid", ""), c.get("dfid", "-"),
                      c.get("platform", "standard"), self.log)

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
                pid = _str((team["period"] or {}).get("period_id")
                           or (team["period"] or {}).get("id"))
                if pid:
                    st = client.user_status(pid)
                    if _int(st.get("status")) == 1:
                        team["status"] = st.get("data") or {}
                    tm = client.team_info(pid)
                    if _int(tm.get("status")) == 1:
                        team["team"] = tm.get("data") or {}
                    team["period_id"] = pid
        except KugouError as exc:
            team = {"error": str(exc)}

        info = {
            **prof,
            "listen_done": bool(prog.get("listen")),
            "ad_done": _int(prog.get("ad")) or 0,
            "ad_max": _int(self.cfg.get("ad_times")) or 8,
            "team": team,
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

        after = load_state()
        after.update({"listen_done": bool(prog.get("listen")),
                      "ad_done": _int(prog.get("ad")) or 0,
                      "progress": prog,
                      "fetched_at": int(time.time())})
        try:
            prof2 = profile_of(client.my_info())
            if prof2["nickname"]:
                after.update(prof2)
        except KugouError:
            pass
        save_state(after)
        self.log("info", f"完成 {done} · 失败 {fail} · 跳过 {skipped}")
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


def _login_get(path: str, extra: dict, mid: str) -> dict:
    now = int(time.time())
    p = {"dfid": "-", "mid": mid, "uuid": "-", "appid": APPID_STD,
         "clientver": CLIENTVER_STD, "clienttime": now, **extra}
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


def qr_start() -> dict:
    """取二维码。图是接口直接给的 base64，不用自己画。"""
    cfg = load_config()
    data = _login_get("/v2/qrcode", {
        "appid": 1001, "type": 1, "plat": 4,       # 拿 key 用 1001，校验时才用 1005
        "qrcode_txt": QR_TEXT, "srcappid": SRCAPPID}, cfg["mid"])
    body = data.get("data") or {}
    if not body.get("qrcode"):
        raise KugouError(f"没拿到二维码（error_code={data.get('error_code')}）")
    return {"key": body["qrcode"], "img": body.get("qrcode_img") or ""}


def qr_poll(key: str) -> dict:
    cfg = load_config()
    data = _login_get("/v2/get_userinfo_qrcode", {
        "plat": 4, "appid": APPID_STD, "srcappid": SRCAPPID,
        "qrcode": key, "dev": "-"}, cfg["mid"])
    body = data.get("data") or {}
    state = _int(body.get("status"))
    if state == 4:
        token = str(body.get("token") or "")
        userid = str(body.get("userid") or "")
        if not token or not userid:
            return {"stage": "error", "msg": "扫码通过了但没拿到 token，再扫一次试试"}
        cfg["token"] = token
        cfg["userid"] = userid
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
    """用账号接口确认 token 真的能用，别把游客 Cookie 当成登录态。"""
    fields = kugoo_fields(cookie)
    if not fields["token"] or not fields["userid"].isdigit():
        return False, ""
    cfg = load_config()
    try:
        info = Client(norm_cookie(cookie), fields["userid"], fields["token"],
                      fields["mid"] or cfg.get("mid", ""),
                      fields["dfid"] or cfg.get("dfid", "-"),
                      cfg.get("platform", "standard")).my_info()
    except Exception:  # noqa: BLE001
        return False, ""
    prof = profile_of(info)
    if prof["nickname"]:
        return True, prof["nickname"]
    return False, ""


def _save_login_cookie(cookie: str, who: str):
    cfg = load_config()
    field = kugoo_fields(cookie)
    cfg["cookie"] = cookie
    if field["token"]:
        cfg["token"] = field["token"]
    if field["userid"].isdigit():
        cfg["userid"] = field["userid"]
    if field["mid"]:
        cfg["mid"] = field["mid"]
    if field["dfid"]:
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


def login_snapshot() -> dict:
    from app import quick_login
    return quick_login.get("kugou").snapshot()


_bind_login()


def code_mtime() -> float:
    try:
        return Path(__file__).stat().st_mtime
    except OSError:
        return 0.0
