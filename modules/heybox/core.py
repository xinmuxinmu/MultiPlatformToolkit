#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小黑盒（Heybox）每日任务

签名算法移植自 heybox-daily 的 src/signature.ts（其注释标明改编自
XiaHouSheng/heybox-core，MIT）。网页版接口用这套 web 签名就够，
不需要 App 端那套从 native 还原出来的 SHA512 变体。

几点是拿匿名请求实测出来的，不是照抄：
- 签到 /task/sign_v3/sign 和状态 /task/sign_v4/get_sign_state 是 GET，
  用 web 签名就能访问（不带登录态返回 status:login，而不是「签名错误」）；
  POST 会直接 405。
- 任务完成判定靠重新拉 /task/list_v2/ 看 state 是否为 finish，不猜。
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_der_public_key

from app.paths import DATA_ROOT

VERSION = "1.0.0"
APP_ID = "heybox"

HOME = DATA_ROOT / "heybox"
HOME.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = HOME / "config.json"
STATE_PATH = HOME / "state.json"

WEB_HOST = "https://api.xiaoheihe.cn"
API_HOST = "https://api.xiaoheihe.cn"

ALPHABET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"
UA = ("Mozilla/5.0 (Linux; Android 14; wv) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Version/4.0 Chrome/143.0.7499.192 Mobile Safari/537.36")

# 与 heybox-daily 的 report.ts 保持一致：上报体用固定 RSA 公钥换 AES key
REPORT_PUBKEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDZgjVwAiKTjZ55nG+mW6r3TSU4"
    "ECvNYqDMIS/bhCj2QaH5GI/KZb2TBp+CBvUj9SLFnmJQ0kzHzHoGZCQ88VevCffF7"
    "JePGF9cmKQqotlfTKbV4oxV5iLz7JSG6b/Vg7AXtrTolNtWsa8HiB0tI0YClYaQl"
    "OXm4UxLeSxQwSFETwIDAQAB")

SHARE_IDS = ("1", "19", "31")
SHARE_NAMES = {"1": "分享帖子", "19": "分享游戏", "31": "分享游戏评价"}
SHARE_KEYS = {"1": "share_post", "19": "share_game", "31": "share_comment"}
# 轮询任务结算的间隔，和原项目一致
POLL_DELAYS = (2.0, 3.0, 5.0, 10.0)


class HeyboxError(Exception):
    pass


# ---------------------------------------------------------------- 签名

def _map_chars(text: str, chars: str) -> str:
    return "".join(chars[ord(c) % len(chars)] for c in text)


def _double(n: int) -> int:
    return ((n << 1) ^ 27) & 255 if n & 128 else (n << 1) & 255


def _triple(n: int) -> int:
    return _double(n) ^ n


def _k(n: int) -> int:
    return _triple(_double(n))


def _c(n: int) -> int:
    return _k(_triple(_double(n)))


def _s(n: int) -> int:
    return _c(n) ^ _k(n) ^ _triple(n)


def web_signature(path: str, now: int | None = None, nonce: str | None = None) -> dict:
    """算 hkey / _time / nonce。"""
    now = int(time.time()) if now is None else int(now)
    nonce = nonce or ("%032X" % random.getrandbits(128))
    norm = "/" + "/".join(p for p in path.split("/") if p) + "/"
    parts = [
        _map_chars(str(now + 1), ALPHABET[:-2]),
        _map_chars(norm, ALPHABET),
        _map_chars(nonce, ALPHABET),
    ]
    mixed = ""
    for i in range(max(len(p) for p in parts)):
        for p in parts:
            if i < len(p):
                mixed += p[i]
    digest = hashlib.md5(mixed[:20].encode()).hexdigest()
    nums = [ord(ch) for ch in digest[-6:]]
    transforms = (_s, _c, _k, _triple)
    total = sum(nums[4:])
    for row in range(4):
        acc = 0
        for col in range(4):
            acc ^= transforms[(col - row + 4) % 4](nums[col])
        total += acc
    return {
        "hkey": _map_chars(digest[:5], ALPHABET[:-4]) + str(total % 100).zfill(2),
        "_time": str(now),
        "nonce": nonce,
    }


# ---------------------------------------------------------------- 上报加密

def _android_base64(raw: bytes) -> str:
    """Android 那套 base64：每 76 字符换行，末尾补换行。"""
    text = base64.b64encode(raw).decode()
    return "\n".join(text[i:i + 76] for i in range(0, len(text), 76)) + "\n"


def encrypt_report(events: list, stamp: str | None = None) -> dict:
    stamp = stamp or str(int(time.time()))
    aes_key = "%016x" % random.getrandbits(64)
    padder = padding.PKCS7(128).padder()
    cipher = Cipher(algorithms.AES(aes_key.encode()), modes.CBC(b"abcdefghijklmnop"))
    body = gzip.compress(json.dumps({"events": events},
                                    separators=(",", ":"), ensure_ascii=False).encode())
    enc = cipher.encryptor()
    data = _android_base64(enc.update(body) + enc.finalize())

    pub = load_der_public_key(base64.b64decode(REPORT_PUBKEY_B64))
    key = _android_base64(pub.encrypt(aes_key.encode(), rsa_padding.PKCS1v15()))
    sid = (hashlib.md5((key + stamp).encode()).hexdigest()
           + hashlib.md5(data.encode()).hexdigest())
    return {"data": data, "key": key, "sid": sid, "time": stamp}


def share_event(action: str, content: dict, session_id: str, stamp: int | None = None) -> dict:
    if action not in ("show", "tap", "success"):
        raise HeyboxError(f"未知分享事件：{action}")
    stamp = int(time.time()) if stamp is None else int(stamp)
    addition = dict(content)
    addition["h_session_id"] = session_id
    if action != "show":
        addition["plat"] = "WechatSession"
    return {
        "type": "4" if action == "tap" else "3",
        "path": f"/share/behavior/{action}",
        "time": str(stamp),
        "addition": addition,
    }


# ---------------------------------------------------------------- 配置

DEFAULT_CONFIG = {
    "cookie": "",
    "heybox_id": "",
    "device_id": "",
    "sign_enable": True,
    "share_enable": True,
    "share_post": True,
    "share_game": True,
    "share_comment": True,
    "gap_min": 2.0,
    "gap_max": 4.0,
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
        cfg["device_id"] = "%016x" % random.getrandbits(64)
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


def norm_cookie(text: str) -> str:
    text = (text or "").strip()
    if text.lower().startswith("cookie:"):
        text = text[7:]
    return "; ".join(f"{k}={v}" for k, v in parse_cookie(text).items())


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


# ---------------------------------------------------------------- 客户端

class Client:
    def __init__(self, cookie: str, heybox_id: str = "", device_id: str = "", log=None):
        self.log = log or (lambda *_: None)
        ck = parse_cookie(cookie)
        if not ck.get("pkey"):
            raise HeyboxError(
                "Cookie 里没有 pkey，这份不是登录态。"
                "请在浏览器登录 xiaoheihe.cn 后重新复制整行 Cookie。")
        self.cookie = norm_cookie(cookie)
        self.heybox_id = str(heybox_id or ck.get("x_heybox_id")
                             or ck.get("heybox_id") or "").strip()
        if not self.heybox_id:
            raise HeyboxError("读不到 heybox_id，请在设置里手动填一下（纯数字）")
        self.device_id = device_id or ""

    # ---- 底层
    def _send(self, host: str, endpoint: str, extra: dict, body: dict | None, method="GET"):
        query = {
            "app": "heybox", "os_type": "web", "x_app": "heybox",
            "x_client_type": "web", "x_os_type": "Android",
            "x_client_version": "1.3.398", "version": "999.0.4",
            "heybox_id": self.heybox_id,
            **(extra or {}),
            **web_signature(endpoint),
        }
        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://web.xiaoheihe.cn",
            "Referer": "https://web.xiaoheihe.cn/",
            "X-Requested-With": "com.max.xiaoheihe",
            "Cookie": self.cookie,
        }
        data = None
        if body is not None:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
        url = host + endpoint + "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if exc.code == 405:
                raise HeyboxError(f"{endpoint} 不接受这个请求方法（接口可能改了）")
        except Exception as exc:  # noqa: BLE001
            raise HeyboxError(f"请求失败：{exc}")
        try:
            payload = json.loads(_decode(raw))
        except Exception:
            raise HeyboxError(f"接口返回的不是 JSON：{_decode(raw)[:150]}")
        status = str(payload.get("status") or "")
        if status in ("login", "relogin"):
            # relogin 是「这个接口不认这份凭据」，不是过期。任务中心是 App 端功能，
            # 网页登录拿到的 pkey 调不动它——实测过精简 4 个 cookie、整行 cookie、
            # 换 host 都一样，签名本身没问题（匿名请求返回的是 status:login）。
            raise HeyboxError(
                "小黑盒任务接口不认网页登录的凭据（status:relogin）。"
                "任务中心是 App 端功能，网页版 pkey 调不了，这块暂时跑不了。")
        if status == "lack_token":
            raise HeyboxError("设备令牌（x_xhh_tokenid）无效，重新快捷登录一次")
        if status == "failed":
            msg = payload.get("msg") or "接口返回失败"
            raise HeyboxError(f"{endpoint}：{msg}")
        return payload

    def web(self, endpoint: str, extra: dict | None = None, host=WEB_HOST, body=None):
        return self._send(host, endpoint, extra or {}, body)

    # ---- 业务
    def profile(self):
        """网页登录态校验接口。

        /task/list_v2/ 不认网页 pkey（返回 relogin），不能拿它判断有没有登录；
        restore_login 是网页自己恢复登录用的，登录态有效时返回 profile。
        """
        return self.web("/account/restore_login", {
            "app": "heybox", "heybox_id": self.heybox_id, "os_type": "web",
            "x_app": "heybox_website", "x_client_type": "web",
            "x_os_type": "Windows", "x_client_version": "",
            "client_type": "web", "web_version": "3.0", "version": "999.0.4",
        })

    def tasks(self):
        return self.web("/task/list_v2/")

    def sign_state(self):
        return self.web("/task/sign_v4/get_sign_state",
                        {"imei": self.device_id} if self.device_id else {})

    def sign(self):
        extra = {"imei": self.device_id} if self.device_id else {}
        return self.web("/task/sign_v3/sign", extra)

    def report(self, event: dict, session_id: str):
        payload = encrypt_report([event])
        return self.web(
            "/account/data_report/",
            {"type": "104", "time_": payload["time"], "session_id": session_id},
            host="https://data.xiaoheihe.cn",
            body={"data": payload["data"], "key": payload["key"], "sid": payload["sid"]},
        )


# ---------------------------------------------------------------- 解析

def _is_today(stamp: float | None = None) -> int:
    """北京时间当天序号，和服务端 signed_date 的算法一致。"""
    stamp = time.time() if stamp is None else stamp
    return int((stamp / 1000 if stamp > 1e11 else stamp) / 86400 + 8 / 24)


def signed_today(state: dict | None, stamp: float | None = None) -> bool:
    state = state or {}
    if str(state.get("state") or "") not in ("ok", "ignore"):
        return False
    try:
        return int(state.get("signed_date")) == _is_today(stamp)
    except (TypeError, ValueError):
        return False


def parse_tasks(result: dict) -> dict:
    user = result.get("user") or {}
    groups = result.get("task_list") or []
    if not groups:
        raise HeyboxError("接口没返回任何任务分组（既不是登录问题，也不是空列表）")
    daily = next((g for g in groups if g.get("title") == "每日任务"), None)
    if not daily or not isinstance(daily.get("tasks"), list) or not daily["tasks"]:
        raise HeyboxError("没找到「每日任务」分组，接口结构可能变了")
    tasks = []
    for t in daily["tasks"]:
        tid = str((t.get("report_extra") or {}).get("task_id") or "sign")
        tasks.append({
            "id": tid,
            "title": t.get("title") or tid,
            "state": t.get("state") or "",
            "done": t.get("state") == "finish",
        })
    level = user.get("level_info") or {}
    return {
        "tasks": tasks,
        "exp": _num(level.get("exp")),
        "coin": _num(level.get("coin")),
        "level": _num(level.get("level")),
        "max_exp": _num(level.get("max_exp")),
        "battery": _num(user.get("battery")),
        "nickname": user.get("username") or "",
        "avatar": user.get("avatar") or "",
        "userid": str(user.get("userid") or ""),
    }


def _num(v):
    try:
        n = float(v)
        return int(n) if n.is_integer() else n
    except (TypeError, ValueError):
        return None


def _games_in(node, out=None):
    """推荐流里游戏节点藏得比较深，递归找带 appid + h_src 的对象。"""
    out = [] if out is None else out
    if isinstance(node, dict):
        item = node
        if item.get("appid") and item.get("h_src"):
            aid = str(item["appid"])
            if not any(g["app_id"] == aid for g in out):
                out.append({"app_id": aid, "h_src": item["h_src"]})
        for v in node.values():
            _games_in(v, out)
    elif isinstance(node, list):
        for v in node:
            _games_in(v, out)
    return out


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
            result = HeyboxDaily(cfg, self.log).run()
        except HeyboxError as exc:
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

class HeyboxDaily:
    def __init__(self, cfg, log):
        self.cfg = cfg or {}
        self.log = log

    def _client(self) -> Client:
        return Client(norm_cookie(self.cfg.get("cookie", "")),
                      self.cfg.get("heybox_id", ""),
                      self.cfg.get("device_id", ""), self.log)

    def _sleep(self):
        a = float(self.cfg.get("gap_min") or 2)
        b = float(self.cfg.get("gap_max") or 4)
        time.sleep(random.uniform(min(a, b), max(a, b)))

    # ---- 只读
    def refresh(self) -> dict:
        client = self._client()
        info = parse_tasks(client.tasks())
        state = client.sign_state()
        info["signed"] = signed_today(state)
        info["sign_raw"] = {k: state.get(k) for k in ("state", "signed_date") if k in state}
        info["fetched_at"] = int(time.time())
        save_state(info)
        return info

    # ---- 执行
    def run(self) -> dict:
        client = self._client()
        before = self.refresh()
        done = fail = skipped = 0

        # 1) 签到
        if self.cfg.get("sign_enable", True):
            if before.get("signed"):
                self.log("info", "今天已经签到过了")
                skipped += 1
            else:
                try:
                    client.sign()
                    self.log("info", "签到请求已提交，正在确认…")
                    if self._wait_sign(client):
                        self.log("success", "签到完成")
                        done += 1
                    else:
                        self.log("warn", "签到提交了但没确认到账，等下再刷新看看")
                        fail += 1
                except HeyboxError as exc:
                    self.log("error", f"签到失败：{exc}")
                    fail += 1
                self._sleep()
        else:
            self.log("info", "签到开关关着，跳过")

        # 2) 三个分享任务
        if not self.cfg.get("share_enable", True):
            self.log("info", "分享类任务整体关着，跳过")
        else:
            current = parse_tasks(client.tasks())
            for sid in SHARE_IDS:
                if not self.cfg.get(SHARE_KEYS[sid], True):
                    continue
                task = next((t for t in current["tasks"] if t["id"] == sid), None)
                if task is None:
                    self.log("warn", f"{SHARE_NAMES[sid]}：任务列表里没有这一项，跳过")
                    skipped += 1
                    continue
                if task["done"]:
                    self.log("info", f"{SHARE_NAMES[sid]}已完成，跳过")
                    skipped += 1
                    continue
                try:
                    if self._do_share(client, sid):
                        self.log("success", f"{SHARE_NAMES[sid]}完成")
                        done += 1
                    else:
                        self.log("warn", f"{SHARE_NAMES[sid]}上报后没结算")
                        fail += 1
                except HeyboxError as exc:
                    self.log("error", f"{SHARE_NAMES[sid]}失败：{exc}")
                    fail += 1
                current = parse_tasks(client.tasks())

        after = parse_tasks(client.tasks())
        after["signed"] = signed_today(client.sign_state())
        after["fetched_at"] = int(time.time())
        save_state(after)
        if before.get("exp") is not None and after.get("exp") is not None:
            gain = after["exp"] - before["exp"]
            if gain > 0:
                self.log("success", f"经验 +{gain}，盒币 {before.get('coin')} → {after.get('coin')}")
        self.log("info", f"完成 {done} · 失败 {fail} · 跳过 {skipped}")
        return {"ok": done, "fail": fail, "skipped": skipped}

    def _wait_sign(self, client) -> bool:
        for delay in POLL_DELAYS:
            time.sleep(delay)
            try:
                if signed_today(client.sign_state()):
                    return True
            except HeyboxError:
                continue
        return False

    def _do_share(self, client, sid: str) -> bool:
        content = self._select_share(client, sid)
        session_id = str(uuid.uuid4())
        # show → tap → success 三个事件都要发，服务端是按这套行为埋点结算的
        for action in ("show", "tap", "success"):
            client.report(share_event(action, content, session_id), session_id)
            if action == "tap":
                time.sleep(2)
            else:
                time.sleep(random.uniform(0.6, 1.2))
        for delay in POLL_DELAYS:
            time.sleep(delay)
            snapshot = parse_tasks(client.tasks())
            task = next((t for t in snapshot["tasks"] if t["id"] == sid), None)
            if task and task["done"]:
                return True
        return False

    def _select_share(self, client, sid: str) -> dict:
        if sid == "1":
            feed = client.web("/bbs/app/feeds", {
                "offset": "0", "limit": "20", "pull": "1", "last_pull": "1"})
            for item in (feed.get("links") or []):
                lid = item.get("linkid") or item.get("link_id")
                if lid and item.get("h_src"):
                    return {"src": "link", "link_id": str(lid), "h_src": item["h_src"]}
            raise HeyboxError("社区流里没有可分享的帖子")

        games = _games_in(client.web("/game/all_recommend/v2"))
        if not games:
            raise HeyboxError("推荐里没有可分享的游戏")
        if sid == "19":
            g = games[0]
            return {"src": "game_detail", "app_id": g["app_id"], "h_src": g["h_src"]}

        for game in games[:5]:
            comments = client.web("/bbs/app/link/game/comments", {
                "appid": game["app_id"], "api_version": "4",
                "offset": "0", "limit": "20"})
            review = next((x for x in (comments.get("links") or [])
                           if (x.get("linkid") or x.get("link_id")) and x.get("userid")), None)
            if not review:
                continue
            detail = client.web("/game/comment/share_data/", {
                "appid": game["app_id"], "userid": str(review["userid"])})
            lid = (detail.get("comment_detail") or {}).get("linkid")
            if lid:
                return {"src": "game_comment", "link_id": str(lid)}
        raise HeyboxError("没找到可分享的游戏评价")


# ---------------------------------------------------------------- 快捷登录

def _verify_heybox(cookie: str) -> tuple[bool, str]:
    """用网页自己的 restore_login 验登录态。

    不能再用 /task/list_v2/ 验：那个接口不认网页 pkey，登录成功也返回
    relogin，会把正常账号永远判成“还没登录”。restore_login 才代表网页登录态。
    """
    cookie = norm_cookie(cookie)
    ck = parse_cookie(cookie)
    if not ck.get("pkey"):
        return False, ""
    hid = (ck.get("x_heybox_id") or ck.get("heybox_id")
           or ck.get("user_heybox_id") or "")
    if not str(hid).isdigit():
        return False, ""
    try:
        data = Client(cookie, str(hid)).profile()
    except Exception:  # noqa: BLE001
        return False, ""
    prof = (data.get("result") or {}).get("profile") or {}
    if prof.get("heybox_id"):
        return True, str(prof.get("nickname") or prof["heybox_id"])
    return False, ""


def _save_login_cookie(cookie: str, who: str):
    cfg = load_config()
    cfg["cookie"] = cookie
    ck = parse_cookie(cookie)
    hid = ck.get("x_heybox_id") or ck.get("heybox_id") or ck.get("user_heybox_id")
    if hid and str(hid).isdigit():
        cfg["heybox_id"] = str(hid)
    save_config(cfg)


def _bind_login():
    from app import quick_login
    spec = quick_login.PLATFORMS.get("heybox")
    if spec is not None:
        spec["verify"] = _verify_heybox
        spec["save"] = _save_login_cookie


def login_start() -> bool:
    _bind_login()
    from app import quick_login
    return quick_login.get("heybox").start()


def login_grab() -> dict:
    _bind_login()
    from app import quick_login
    return quick_login.get("heybox").grab()


def login_snapshot() -> dict:
    from app import quick_login
    return quick_login.get("heybox").snapshot()


_bind_login()


def code_mtime() -> float:
    try:
        return Path(__file__).stat().st_mtime
    except OSError:
        return 0.0
