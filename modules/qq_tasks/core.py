#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 音乐每日签到 + QQ 日签卡 / 晚安卡。

两边是不同域名、不同 Cookie，不能混用：

- QQ 音乐：登录 y.qq.com 后拿 uin / qqmusic_key，请求
  u.y.qq.com/cgi-bin/musics.fcg 的 ActCenterSignNewSvr.DoSignIn。
  活动 ID 从当前积分中心页面确认仍是 PR-Config20200828-31525466015。
- QQ 打卡：登录 i.qq.com（ti.qq.com 同域）后拿 uin / skey / p_skey，
  请求 ti.qq.com/hybrid-h5/api/json/daily_attendance/SignIn。
  type=1 日签卡，type=2 晚安卡。

签到接口都只提交一次，不重试轰炸；重复签到按成功处理。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import random
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.paths import DATA_ROOT

VERSION = "1.0.0"
APP_ID = "qq-tasks"

HOME = DATA_ROOT / "qq_tasks"
HOME.mkdir(parents=True, exist_ok=True)
MUSIC_CONFIG = HOME / "music.json"
CARD_CONFIG = HOME / "card.json"
STATE_PATH = HOME / "state.json"

MUSIC_ACT_ID = "PR-Config20200828-31525466015"
MUSIC_URL = "https://u.y.qq.com/cgi-bin/musics.fcg"
CARD_URL = "https://ti.qq.com/hybrid-h5/api/json/daily_attendance/SignIn"

QQ_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36 "
         "QQJSSDK/1.3 /ANDROIDQQMUSIC/11050108 QQMusic/11.5.1.8 H5/1")

DEFAULT_MUSIC = {"cookie": "", "sign_enable": True}
DEFAULT_CARD = {"cookie": "", "day_enable": True, "night_enable": True}

_cfg_lock = threading.Lock()


class QQError(Exception):
    pass


# ---------------------------------------------------------------- 配置

def _load(path: Path, default: dict) -> dict:
    cfg = json.loads(json.dumps(default))
    try:
        if path.exists():
            saved = json.loads(path.read_text("utf-8"))
            if isinstance(saved, dict):
                cfg.update({k: v for k, v in saved.items() if k in cfg})
    except Exception:
        pass
    return cfg


def _save(path: Path, cfg: dict):
    with _cfg_lock:
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


def load_music() -> dict:
    return _load(MUSIC_CONFIG, DEFAULT_MUSIC)


def load_card() -> dict:
    return _load(CARD_CONFIG, DEFAULT_CARD)


def save_music(cfg: dict):
    _save(MUSIC_CONFIG, {k: cfg.get(k, v) for k, v in DEFAULT_MUSIC.items()})


def save_card(cfg: dict):
    _save(CARD_CONFIG, {k: cfg.get(k, v) for k, v in DEFAULT_CARD.items()})


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


# ---------------------------------------------------------------- Cookie

def parse_cookie(text: str) -> dict:
    out = {}
    for part in (text or "").replace("\r", ";").replace("\n", ";").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def norm_cookie(text: str) -> str:
    text = (text or "").strip()
    if text.lower().startswith("cookie:"):
        text = text[7:]
    return "; ".join(f"{k}={v}" for k, v in parse_cookie(text).items())


def _uin_of(ck: dict) -> str:
    return str(ck.get("uin") or ck.get("wxuin") or "").strip().lstrip("o")


def music_ready(cookie: str) -> bool:
    ck = parse_cookie(cookie)
    return bool((ck.get("qqmusic_key") or ck.get("qm_keyst")) and _uin_of(ck))


def card_ready(cookie: str) -> bool:
    ck = parse_cookie(cookie)
    return bool(ck.get("p_skey") and ck.get("skey") and _uin_of(ck))


def _g_tk(cookie: str) -> int:
    ck = parse_cookie(cookie)
    key = ck.get("qqmusic_key") or ck.get("qm_keyst") or ck.get("p_skey") or ck.get("skey") or ""
    h = 5381
    for ch in key:
        h = ((h << 5) + h + ord(ch)) & 0x7FFFFFFF
    return h


# ---------------------------------------------------------------- HTTP

def _post_json(url: str, body: str, headers: dict, timeout=20) -> dict:
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if "gzip" in (resp.headers.get("Content-Encoding") or "").lower():
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        if not raw:
            raise QQError(f"接口返回 HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        raise QQError(f"请求失败：{exc}")
    text = raw.decode("utf-8", "ignore")
    try:
        payload = json.loads(text)
    except Exception:
        raise QQError(f"接口返回的不是 JSON：{text[:160]}")
    if not isinstance(payload, dict):
        raise QQError("接口返回结构异常")
    return payload


# ---------------------------------------------------------------- QQ 音乐

class MusicClient:
    def __init__(self, cookie: str):
        self.cookie = norm_cookie(cookie)
        ck = parse_cookie(self.cookie)
        self.uin = _uin_of(ck)
        if not music_ready(self.cookie):
            raise QQError("QQ 音乐 Cookie 里缺少 uin 或 qqmusic_key，请重新登录")
        self.uid = str(ck.get("uid") or "")

    def sign(self) -> tuple[bool, str]:
        payload = {
            "comm": {
                "g_tk": _g_tk(self.cookie), "uin": self.uin, "format": "json",
                "inCharset": "utf-8", "outCharset": "utf-8", "notice": 0,
                "platform": "h5", "needNewCode": 1, "ct": 23, "cv": 0,
                "uid": self.uid,
            },
            "req_0": {
                "module": "music.actCenter.ActCenterSignNewSvr",
                "method": "DoSignIn", "param": {"ActID": MUSIC_ACT_ID},
            },
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        sign = ("zza" + "".join(random.choice("0123456789abcdef") for _ in range(10))
                + hashlib.md5(("CJBPACrRuNy7" + body).encode()).hexdigest())
        url = f"{MUSIC_URL}?_webcgikey=DoSignIn&_={int(time.time() * 1000)}&sign={sign}"
        data = _post_json(url, body, {
            "User-Agent": QQ_UA, "Content-Type": "application/json",
            "Origin": "https://i.y.qq.com",
            "Referer": "https://i.y.qq.com/n2/m/client/actcenter/index.html?_hdct=2",
            "Cookie": self.cookie,
        })
        if int(data.get("code") or 0) != 0:
            raise QQError(f"QQ 音乐返回错误：{data.get('code')}")
        req = data.get("req_0") or {}
        code = int(req.get("code") or 0)
        if code in (1000, 500003):
            raise QQError("QQ 音乐 Cookie 已失效，请重新快捷登录")
        if code == 2000:
            raise QQError("QQ 音乐签名被拒绝，活动接口可能改版")
        if code == 200002:
            return True, "今天已经签到过了"
        if code != 0:
            raise QQError(f"QQ 音乐签到失败（code={code}）")
        result = req.get("data") or {}
        ret = int(result.get("retCode") or 0)
        if ret == 0:
            days = result.get("totalDays")
            score = result.get("todayScore")
            tail = []
            if days is not None:
                tail.append(f"累计 {days} 天")
            if score is not None:
                tail.append(f"今日 +{score} 积分")
            return True, "签到成功" + (f"（{'，'.join(tail)}）" if tail else "")
        if ret == 40001:
            return True, "今天已经签到过了"
        raise QQError(f"QQ 音乐签到失败：{result.get('errMsg') or ret}")


# ---------------------------------------------------------------- QQ 打卡

class CardClient:
    def __init__(self, cookie: str):
        self.cookie = norm_cookie(cookie)
        ck = parse_cookie(self.cookie)
        self.uin = _uin_of(ck)
        if not card_ready(self.cookie):
            raise QQError("QQ 打卡 Cookie 里缺少 uin / skey / p_skey，请重新登录")

    def sign(self, kind: str) -> tuple[bool, str]:
        if kind not in ("day", "night"):
            raise QQError(f"未知打卡类型：{kind}")
        typ = 1 if kind == "day" else 2
        payload = {"uin": self.uin, "type": typ, "qua": "V1_AND_SQ_8.9.25_3640_YYB_D"}
        if kind == "day":
            payload.update({
                "sId": "",
                "mpExtend": {
                    "tianshuAdsReq": json.dumps({
                        "app": "QQ", "os": "AND", "version": "8.8.83.7540", "imei": "",
                    }, separators=(",", ":")),
                },
            })
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        data = _post_json(CARD_URL, body, {
            "User-Agent": ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Version/4.0 Chrome/120.0 Mobile Safari/537.36"),
            "Content-Type": "application/json",
            "Origin": "https://ti.qq.com", "Referer": "https://ti.qq.com/",
            "Cookie": self.cookie,
        })
        code = data.get("ret")
        if code is None:
            code = data.get("retcode", data.get("code"))
        msg = str(data.get("msg") or data.get("message") or data.get("errMsg") or "")
        if (code in (0, "0", 200, "200", None)
                and not any(x in msg for x in ("失败", "错误", "过期", "登录"))):
            label = "日签卡" if kind == "day" else "晚安卡"
            return True, f"{label}打卡成功" + (f"（{msg}）" if msg else "")
        if "已" in msg and ("签" in msg or "打卡" in msg):
            return True, msg
        if code in (-3000, "-3000"):
            raise QQError("QQ 打卡 Cookie 已失效，请重新快捷登录")
        raise QQError(msg or f"QQ 打卡失败（{data}）"[:160])


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

    def start(self):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.done = False
            self.logs = []
            self.seq = 0
            self.summary = {}
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        result = {"music": {}, "card": {}}
        done = fail = skipped = 0
        try:
            mc = load_music()
            if not mc.get("sign_enable", True):
                self.log("info", "QQ 音乐签到开关关着，跳过")
                skipped += 1
            elif not mc.get("cookie"):
                self.log("warn", "QQ 音乐还没登录，跳过")
                skipped += 1
            else:
                try:
                    ok, msg = MusicClient(mc["cookie"]).sign()
                    self.log("success" if ok else "warn", f"QQ 音乐：{msg}")
                    result["music"] = {"ok": ok, "msg": msg, "at": int(time.time())}
                    done += 1 if ok else 0
                    fail += 0 if ok else 1
                except QQError as exc:
                    self.log("error", f"QQ 音乐：{exc}")
                    result["music"] = {"ok": False, "msg": str(exc), "at": int(time.time())}
                    fail += 1

            cc = load_card()
            if not cc.get("cookie"):
                self.log("warn", "QQ 打卡还没登录，跳过")
                skipped += 1
            else:
                tasks = []
                if cc.get("day_enable", True):
                    tasks.append(("day", "日签卡"))
                if cc.get("night_enable", True):
                    tasks.append(("night", "晚安卡"))
                if not tasks:
                    self.log("info", "QQ 日签卡和晚安卡都关着，跳过")
                    skipped += 1
                client = CardClient(cc["cookie"])
                for kind, label in tasks:
                    try:
                        ok, msg = client.sign(kind)
                        self.log("success" if ok else "warn", f"QQ {label}：{msg}")
                        result["card"][kind] = {"ok": ok, "msg": msg, "at": int(time.time())}
                        done += 1 if ok else 0
                        fail += 0 if ok else 1
                    except QQError as exc:
                        self.log("error", f"QQ {label}：{exc}")
                        result["card"][kind] = {"ok": False, "msg": str(exc), "at": int(time.time())}
                        fail += 1
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"出了点意外：{exc}")
            result["error"] = str(exc)
        finally:
            save_state({**load_state(), **result, "fetched_at": int(time.time())})
            self.log("info", f"完成 {done} · 失败 {fail} · 跳过 {skipped}")
            with self.lock:
                self.summary = result
                self.running = False
                self.done = True


TASK = Task()


# ---------------------------------------------------------------- 快捷登录

def _verify_music(cookie: str) -> tuple[bool, str]:
    ck = parse_cookie(cookie)
    if not music_ready(cookie):
        return False, ""
    return True, str(ck.get("uin") or ck.get("wxuin") or "")


def _verify_card(cookie: str) -> tuple[bool, str]:
    if not card_ready(cookie):
        return False, ""
    return True, str(parse_cookie(cookie).get("uin") or "")


def _save_music_login(cookie: str, who: str):
    cfg = load_music()
    cfg["cookie"] = cookie
    save_music(cfg)


def _save_card_login(cookie: str, who: str):
    cfg = load_card()
    cfg["cookie"] = cookie
    save_card(cfg)


def _bind_login():
    from app import quick_login
    ms = quick_login.PLATFORMS.get("qq_music")
    if ms is not None:
        ms["verify"] = _verify_music
        ms["save"] = _save_music_login
    cs = quick_login.PLATFORMS.get("qq_card")
    if cs is not None:
        cs["verify"] = _verify_card
        cs["save"] = _save_card_login


def _session(which: str):
    _bind_login()
    from app import quick_login
    key = "qq_music" if which == "music" else "qq_card"
    return quick_login.get(key)


def login_start(which: str) -> bool:
    return _session(which).start()


def login_grab(which: str) -> dict:
    return _session(which).grab()


def login_snapshot(which: str) -> dict:
    return _session(which).snapshot()


_bind_login()
