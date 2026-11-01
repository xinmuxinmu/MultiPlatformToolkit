#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B站大会员大积分 · 每日任务

接口与任务编排队列移植自 RayWangQvQ/BiliBiliToolPro（MIT），端点见
VipBigPointDomainService / VipBigPointAppService / IApiApi.cs。

几条是拿本机真实登录态实测出来的，不是照抄注释：

1. 公共 App 参数里 `build` 必须带。缺了或太旧，receive/v2 之类的接口会统一
   回 6007000「请将客户端更新至最新版本」。当前用 9120300 / 9.12.0。
2. `task_code` 字段名就是下划线形式。上游 C# 用 UrlEncoded 序列化时把属性名
   TaskCode 原样当 key 发出去，这本身是个 bug（issue #1195 里有提）。
3. 浏览类任务实测返回 code 0 且积分真的到账：
   - 浏览追番频道  position=jp_channel
   - 浏览影视频道  position=tv_channel
   - 浏览会员购    走 show.bilibili.com 的 event dispatch，字段是 eventId（驼峰）
4. 走不通的两个（已在界面上标注，不假装成功）：
   - 浏览装扮商城主页：complete/v2 稳定 -400，该任务靠 H5 页面埋点判定。
   - 观看剧集内容：receive/v2 需要 App 端 access_key（Cookie 里没有），
     且要求真实播放满 10 分钟，HTTP 层面做不掉。

登录态直接复用 bilibili_chat 已经登好的账号，不另外扫码。
"""
from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app.paths import BILI_CHAT_HOME, DATA_ROOT

VERSION = "1.0.0"
APP_ID = "bilibili_vip"

HOME = DATA_ROOT / "bilibili_vip"
HOME.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = HOME / "config.json"
STATE_PATH = HOME / "state.json"

API = "https://api.bilibili.com"
REF_TASK = "https://big.bilibili.com/mobile/bigPoint/task"
REF_INDEX = "https://big.bilibili.com/mobile/index"

# App 端参数，缺 build 会被判「客户端过旧」
BUILD = "9120300"
APP_VER = "9.12.0"
UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-A5560 Build/TP1A.220624.014; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/120.0.0.0 Mobile Safari/537.36"
)

# task_code -> 处理方式
#   view    : 走 deliver/task/complete，靠 position 区分频道
#   vipmall : 走 show.bilibili.com 的埋点事件
#   v2      : 走 score/task/complete/v2
#   json    : 走 score/task/complete（JSON 体）
VIEW_POSITION = {"animatetab": "jp_channel", "filmtab": "tv_channel"}

# 需要花钱的任务，一律不碰
PAID_TASKS = {"tvodbuy", "vipmallbuy", "dressbuyamount", "offlinetask"}

# 名称兜底：接口没给 title 时用这个
FALLBACK_NAMES = {
    "animatetab": "浏览追番频道页10秒",
    "filmtab": "浏览影视频道页10秒",
    "vipmallview": "浏览会员购页面10秒",
    "dress-view": "浏览装扮商城主页",
    "ogvwatchnew": "观看剧集内容",
    "bonus": "大会员福利大积分",
    "privilege": "浏览大会员权益页面",
}


class BiliError(Exception):
    pass


# ---------------------------------------------------------------- 配置

DEFAULT_CONFIG = {
    # bili_chat：复用 B 站聊天模块的登录态；manual：用下面手填的 Cookie
    "cookie_source": "bili_chat",
    "cookie": "",
    "sign_enable": True,
    "express_enable": True,   # 大会员等级加速包（看视频 1 分钟换 10 经验）
    "bonus_enable": True,     # 福利任务 / 体验任务，一次性
    "view_enable": True,      # 浏览类日常：追番 / 影视 / 会员购
    "dress_enable": True,     # 装扮商城主页（大概率失败，界面会标红）
    "ogv_enable": False,      # 观看剧集，需要真看 10 分钟，默认关
    "view_wait": 10,          # 浏览类任务的等待秒数
    "gap_min": 1.5,
    "gap_max": 3.5,
}

_cfg_lock = threading.Lock()


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        if CONFIG_PATH.exists():
            saved = json.loads(CONFIG_PATH.read_text("utf-8"))
            for k in cfg:
                if k in saved:
                    cfg[k] = saved[k]
    except Exception:
        pass
    return cfg


def save_config(cfg: dict):
    with _cfg_lock:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text("utf-8"))
    except Exception:
        return {}


def save_state(data: dict):
    try:
        STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 登录态

def _accounts_dir() -> Path:
    return BILI_CHAT_HOME / "accounts"


def bili_chat_accounts() -> list[dict]:
    """列出 bilibili_chat 那边已经登录的账号。"""
    base = _accounts_dir()
    if not base.exists():
        return []
    try:
        index = json.loads((base / "index.json").read_text("utf-8"))
    except Exception:
        index = {}
    active = str(index.get("active") or "")
    metas = index.get("accounts") or {}
    out = []
    for aid, meta in metas.items():
        state_file = base / aid / "state.json"
        if not state_file.exists():
            continue
        out.append({
            "id": aid,
            "name": (meta or {}).get("name") or aid,
            "nickname": (meta or {}).get("nickname") or "",
            "uid": (meta or {}).get("uid") or "",
            "active": aid == active,
        })
    # 没有 index 但有 state 的兜底
    if not out:
        for d in base.iterdir():
            if (d / "state.json").exists():
                out.append({"id": d.name, "name": d.name, "nickname": "",
                            "uid": "", "active": True})
    return out


def cookie_from_bili_chat(account_id: str = "") -> str:
    """从 bilibili_chat 的 Playwright state.json 里抽登录 Cookie。"""
    base = _accounts_dir()
    if not account_id:
        accounts = bili_chat_accounts()
        if not accounts:
            raise BiliError(
                "没找到 B 站登录态。先去「B站聊天记录」页扫码登录一次，"
                "或者在设置里改成手填 Cookie。")
        account_id = next((a["id"] for a in accounts if a["active"]), accounts[0]["id"])
    state_file = base / account_id / "state.json"
    if not state_file.exists():
        raise BiliError(f"账号 {account_id} 没有登录态文件")
    try:
        data = json.loads(state_file.read_text("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise BiliError(f"登录态文件读不了：{exc}")
    cookies = {}
    for c in data.get("cookies") or []:
        name = c.get("name")
        if name:
            cookies[name] = c.get("value") or ""
    if not cookies.get("SESSDATA"):
        raise BiliError("登录态里没有 SESSDATA，去 B站聊天记录页重新登录一次")
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def parse_cookie(text: str) -> dict:
    out = {}
    for part in (text or "").replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    return out


def norm_cookie(text: str) -> str:
    return "; ".join(f"{k}={v}" for k, v in parse_cookie(text).items())


# ---------------------------------------------------------------- 请求

# B 站是境内站点，绕开系统代理直连更稳
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Client:
    def __init__(self, cookie: str, log=None):
        self.log = log or (lambda *_: None)
        self.cookie = norm_cookie(cookie)
        ck = parse_cookie(self.cookie)
        if not ck.get("SESSDATA"):
            raise BiliError("Cookie 里没有 SESSDATA，这份不是登录态")
        self.bili_jct = ck.get("bili_jct", "")
        self.buvid = ck.get("buvid3") or ck.get("buvid4") or ""
        self.uid = ck.get("DedeUserID", "")

    # ---- 底层
    def _base_query(self) -> dict:
        ms = int(time.time() * 1000)
        return {
            "build": BUILD,
            "disable_rcmd": 0,
            "mobi_app": "android",
            "platform": "android",
            "statistics": json.dumps(
                {"appId": 1, "platform": 3, "version": APP_VER, "abtest": ""},
                separators=(",", ":")),
            "t": ms,
            "ts": ms // 1000,
        }

    def _send(self, url, ref=REF_TASK, form=None, js=None, method="GET"):
        headers = {
            "User-Agent": UA,
            "Cookie": self.cookie,
            "Referer": ref,
            "Accept": "application/json, text/plain, */*",
        }
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif js is not None:
            data = json.dumps(js).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with _OPENER.open(req, timeout=20) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if not raw:
                raise BiliError(f"{url} 返回 HTTP {exc.code}")
        except Exception as exc:  # noqa: BLE001
            raise BiliError(f"请求失败：{exc}")
        try:
            return json.loads(raw.decode("utf-8", "ignore"))
        except Exception:
            raise BiliError(f"接口返回的不是 JSON：{raw[:120]!r}")

    def get(self, path, query=None, ref=REF_TASK):
        q = self._base_query()
        q.update(query or {})
        return self._send(f"{API}{path}?{urllib.parse.urlencode(q)}", ref=ref, method="GET")

    def post_form(self, path, form, ref=REF_TASK, query=None):
        url = f"{API}{path}"
        if query:
            q = self._base_query()
            q.update(query)
            url += "?" + urllib.parse.urlencode(q)
        return self._send(url, ref=ref, form=form, method="POST")

    def post_json(self, url, js, ref=REF_TASK):
        return self._send(url, ref=ref, js=js, method="POST")

    # ---- 业务
    def nav(self):
        d = self._send(f"{API}/x/web-interface/nav", ref="https://www.bilibili.com/")
        return d

    def combine(self):
        """任务列表 + 当前积分。接口返回的 task_info / point_info 是小写键。"""
        d = self.get("/x/vip_point/task/combine",
                     {"buvid": self.buvid, "csrf": self.bili_jct,
                      "brand": "Samsung", "channel": "bili",
                      "containerName": "AbstractWebActivity", "device": "phone"})
        if d.get("code") != 0 or not d.get("data"):
            raise BiliError(f"拿任务列表失败：{d.get('code')} {d.get('message')}")
        return d["data"]

    def sign_status(self):
        return self.get("/x/vip/vip_center/sign_in/three_days_sign",
                        {"csrf": self.bili_jct})

    def do_sign(self):
        return self.post_form(
            "/pgc/activity/score/task/sign2",
            {"device": "phone", "t": int(time.time() * 1000), "ts": int(time.time())},
            ref=REF_INDEX,
            query={"mobi_app": "android", "csrf": self.bili_jct, "platform": "android"})

    def privileges(self):
        return self._send(f"{API}/x/vip/privilege/my", ref=REF_TASK)

    def add_experience(self):
        return self.post_form("/x/vip/experience/add", {"csrf": self.bili_jct})

    def receive_task(self, code):
        return self.post_form("/pgc/activity/score/task/receive/v2",
                              {"task_code": code, "csrf": self.bili_jct})

    def complete_json(self, code):
        return self.post_json(f"{API}/pgc/activity/score/task/complete",
                              {"task_code": code})

    def complete_v2(self, code):
        return self.post_form("/pgc/activity/score/task/complete/v2",
                              {"task_code": code, "csrf": self.bili_jct})

    def complete_view(self, position):
        return self.post_form("/pgc/activity/deliver/task/complete",
                              {"position": position, "c_locale": "zh_CN",
                               "channel": "bili", "s_locale": "zh_CN"})

    def view_vipmall(self):
        # 字段是 eventId（驼峰），写成 event_id 会被判「eventId不能为空」
        return self.post_json(
            "https://show.bilibili.com/api/activity/fire/common/event/dispatch",
            {"csrf": self.bili_jct, "eventId": "hevent_oy4b7h3epeb"},
            ref="https://show.bilibili.com/")


# ---------------------------------------------------------------- 解析

def _tasks_of(combine: dict) -> list[dict]:
    """把各模块的任务摊平成一维列表，附上所属模块。"""
    out = []
    ti = (combine or {}).get("task_info") or (combine or {}).get("Task_info") or {}
    for module in ti.get("modules") or []:
        title = module.get("module_title") or ""
        for item in module.get("common_task_item") or []:
            row = dict(item)
            row["_module"] = title
            out.append(row)
    return out


def _task_by_code(combine: dict, code: str) -> dict | None:
    return next((t for t in _tasks_of(combine) if t.get("task_code") == code), None)


def task_state_text(state, max_times=None) -> str:
    if state == 3:
        return "已完成"
    if state == 1:
        return "待完成"
    if state == 0:
        return "未领取"
    return "—"


def build_overview(combine: dict) -> dict:
    """给界面用的精简任务列表。"""
    ti = (combine or {}).get("task_info") or {}
    pi = (combine or {}).get("point_info") or {}
    rows = []
    for t in _tasks_of(combine):
        code = t.get("task_code") or ""
        state = t.get("state")
        rows.append({
            "code": code,
            "title": t.get("title") or FALLBACK_NAMES.get(code) or code,
            "module": t.get("_module") or "",
            "state": state,
            "done": state == 3,
            "times": t.get("complete_times") or 0,
            "max": t.get("max_times") or 0,
            "paid": code in PAID_TASKS,
            "auto": code in VIEW_POSITION or code in ("vipmallview", "dress-view", "bonus", "privilege"),
        })
    return {
        "point": pi.get("point"),
        "expire_point": pi.get("expire_point"),
        "expire_days": pi.get("expire_days"),
        "score_month": ti.get("score_month"),
        "score_limit": ti.get("score_limit"),
        "tasks": rows,
        "fetched_at": int(time.time()),
    }


def _profile_fields(nav_data: dict | None) -> dict:
    """把 nav 里的账号信息摘出来给界面用。"""
    d = nav_data or {}
    return {
        "nickname": d.get("uname") or "",
        "avatar": d.get("face") or "",
        "uid": d.get("mid") or 0,
        "vip_type": d.get("vipType") or 0,
    }


# ---------------------------------------------------------------- 任务运行

class Task:
    """一次运行的状态 + 日志缓冲，前端轮询取增量。"""

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
            result = VipBigPoint(cfg, self.log).run()
        except BiliError as exc:
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


class VipBigPoint:
    def __init__(self, cfg, log):
        self.cfg = cfg or {}
        self.log = log
        # 统计
        self.ok = 0
        self.already = 0
        self.skip = 0
        self.fail = 0

    # ---- 工具
    def _gap(self):
        lo = float(self.cfg.get("gap_min") or 1.5)
        hi = float(self.cfg.get("gap_max") or 3.5)
        if hi < lo:
            lo, hi = hi, lo
        time.sleep(random.uniform(lo, hi))

    def _client(self) -> Client:
        source = (self.cfg.get("cookie_source") or "bili_chat").strip()
        if source == "manual":
            cookie = self.cfg.get("cookie") or ""
            if not parse_cookie(cookie).get("SESSDATA"):
                raise BiliError("设置里填的 Cookie 里没有 SESSDATA")
        else:
            cookie = cookie_from_bili_chat(self.cfg.get("bili_chat_account") or "")
        return Client(cookie, self.log)

    def _mark(self, kind):
        if kind == "ok":
            self.ok += 1
        elif kind == "already":
            self.already += 1
        elif kind == "skip":
            self.skip += 1
        else:
            self.fail += 1

    # ---- 各步骤
    def _check_vip(self, client):
        nav = client.nav()
        if nav.get("code") != 0:
            raise BiliError("登录态失效了（nav 返回 %s），去 B站聊天记录页重新登录"
                            % nav.get("code"))
        data = nav.get("data") or {}
        if not data.get("isLogin"):
            raise BiliError("Cookie 已过期，去 B站聊天记录页重新登录一次")
        name = data.get("uname") or ""
        vip_status = data.get("vipStatus")
        vip_type = data.get("vipType")
        if not vip_status:
            raise BiliError(f"账号「{name}」不是大会员，没有大积分任务")
        kind = "年度大会员" if vip_type == 2 else "大会员"
        self.log("info", f"账号：{name}（{kind}）")
        return data

    def _sign(self, client):
        self.log("head", "签到")
        d = client.sign_status()
        if d.get("code") != 0:
            self.log("warn", f"签到状态拿不到：{d.get('code')} {d.get('message')}")
            self._mark("fail")
            return
        info = (d.get("data") or {}).get("three_day_sign") or {}
        if info.get("signed"):
            score = info.get("score")
            self.log("info", f"今天已经签过了" + (f"（+{score} 大积分）" if score else ""))
            self._mark("already")
            return
        r = client.do_sign()
        if r.get("code") == 0:
            self.log("ok", "签到成功")
            self._mark("ok")
        else:
            self.log("error", f"签到失败：{r.get('code')} {r.get('message')}")
            self._mark("fail")

    def _express(self, client):
        """大会员等级加速包：看完视频后换 10 经验。"""
        self.log("head", "大会员经验加速包")
        d = client.privileges()
        if d.get("code") != 0:
            self.log("warn", f"读不到特权列表：{d.get('code')}")
            self._mark("fail")
            return
        item = next((x for x in (d.get("data") or {}).get("list") or []
                     if x.get("type") == 9), None)
        if item is None:
            self.log("info", "当前账号没有这一项，跳过")
            self._mark("skip")
            return
        state = item.get("state")
        if state == 1:
            self.log("info", "经验已经兑换过了")
            self._mark("already")
            return
        if state == 2:
            # 需要先看视频，HTTP 层做不掉，交给用户自己
            self.log("skip", "要先看完 1 分钟视频才能兑换，这步留给你手动点")
            self._mark("skip")
            return
        r = client.add_experience()
        if r.get("code") == 0:
            self.log("ok", "经验 +10 已领取")
            self._mark("ok")
        else:
            self.log("error", f"领取失败：{r.get('code')} {r.get('message')}")
            self._mark("fail")

    def _one_time(self, client, combine, code, label):
        """福利任务 / 体验任务：领取 → 完成 → 复查。"""
        task = _task_by_code(combine, code)
        if task is None:
            self.log("info", f"{label}：任务已下线，跳过")
            self._mark("skip")
            return
        if task.get("state") == 3:
            self.log("info", f"{label}：已经完成过了")
            self._mark("already")
            return
        if task.get("state") == 0:
            rv = client.receive_task(code)
            if rv.get("code") == 0:
                self.log("info", f"{label}：领取成功")
            else:
                self.log("warn", f"{label}：领取返回 {rv.get('code')} {rv.get('message')}")
            self._gap()
        r = client.complete_json(code)
        if r.get("code") == 0:
            self.log("ok", f"{label}：完成")
            self._mark("ok")
        else:
            self.log("error", f"{label}：失败 {r.get('code')} {r.get('message')}")
            self._mark("fail")

    def _view_task(self, client, code, name):
        """浏览类：追番 / 影视。服务端只认 position，本地等够时间再报。"""
        wait = max(1, int(self.cfg.get("view_wait") or 10))
        self.log("info", f"{name}：模拟浏览 {wait} 秒")
        time.sleep(wait)
        r = client.complete_view(VIEW_POSITION[code])
        if r.get("code") == 0:
            self.log("ok", f"{name}：完成")
            self._mark("ok")
        else:
            self.log("error", f"{name}：失败 {r.get('code')} {r.get('message')}")
            self._mark("fail")

    def _vipmall_task(self, client, name):
        wait = max(1, int(self.cfg.get("view_wait") or 10))
        self.log("info", f"{name}：模拟浏览 {wait} 秒")
        time.sleep(wait)
        r = client.view_vipmall()
        if r.get("code") == 0:
            self.log("ok", f"{name}：完成")
            self._mark("ok")
        else:
            self.log("error", f"{name}：失败 {r.get('code')} {r.get('message')}")
            self._mark("fail")

    def _dress_task(self, client, name):
        r = client.complete_v2("dress-view")
        if r.get("code") == 0:
            self.log("ok", f"{name}：完成")
            self._mark("ok")
        else:
            self.log("skip", f"{name}：接口回 {r.get('code')}，这条只认真实打开装扮商城页，"
                              f"HTTP 走不通，需要的话手动点一次")
            self._mark("skip")

    def _ogv_task(self, client, name):
        self.log("skip", f"{name}：要真实播放满 10 分钟，且领取接口需要 App 端 access_key，"
                          f"做不掉，留给你手动看")
        self._mark("skip")

    # ---- 主流程
    def run(self) -> dict:
        client = self._client()
        self.profile = self._check_vip(client)

        combine = client.combine()
        self.log("info", f"当前大积分：{(combine.get('point_info') or {}).get('point')}")

        if self.cfg.get("sign_enable", True):
            self._sign(client)
            self._gap()

        if self.cfg.get("express_enable", True):
            self._express(client)
            self._gap()

        if self.cfg.get("bonus_enable", True):
            self.log("head", "一次性任务")
            for code, label in (("bonus", "大会员福利"), ("privilege", "大会员权益页")):
                self._one_time(client, combine, code, label)
                self._gap()

        # 日常任务：重新拉一次，前面几步可能已经改变了状态
        combine = client.combine()
        self.log("head", "日常任务")

        for code, name in (("animatetab", "浏览追番频道"), ("filmtab", "浏览影视频道")):
            task = _task_by_code(combine, code)
            if task is None:
                continue
            if task.get("state") == 3:
                self.log("info", f"{name}：已经完成")
                self._mark("already")
                continue
            if not self.cfg.get("view_enable", True):
                self.log("info", f"{name}：开关关着，跳过")
                self._mark("skip")
                continue
            self._view_task(client, code, name)
            self._gap()

        task = _task_by_code(combine, "vipmallview")
        if task is not None:
            if task.get("state") == 3:
                self.log("info", "浏览会员购页面：已经完成")
                self._mark("already")
            elif not self.cfg.get("view_enable", True):
                self.log("info", "浏览会员购页面：开关关着，跳过")
                self._mark("skip")
            else:
                self._vipmall_task(client, "浏览会员购页面")
                self._gap()

        task = _task_by_code(combine, "dress-view")
        if task is not None:
            if task.get("state") == 3:
                self.log("info", "浏览装扮商城主页：已经完成")
                self._mark("already")
            elif not self.cfg.get("dress_enable", True):
                self.log("info", "浏览装扮商城主页：开关关着，跳过")
                self._mark("skip")
            else:
                self._dress_task(client, "浏览装扮商城主页")
                self._gap()

        task = _task_by_code(combine, "ogvwatchnew")
        if task is not None and task.get("state") != 3:
            if self.cfg.get("ogv_enable", False):
                self._ogv_task(client, "观看剧集内容")
            else:
                self.log("info", "观看剧集内容：开关关着，跳过（要真看 10 分钟）")
                self._mark("skip")

        if any(t.get("task_code") in PAID_TASKS and t.get("state") != 3
               for t in _tasks_of(combine)):
            self.log("info", "购买类任务要花钱，不碰")

        # 收尾复查
        self.log("head", "结果")
        try:
            final = client.combine()
            pi = final.get("point_info") or {}
            ti = final.get("task_info") or {}
            self.log("info", f"当前大积分：{pi.get('point')}"
                             f"（本月已得 {ti.get('score_month')}/{ti.get('score_limit')}）")
            overview = build_overview(final)
            overview.update(_profile_fields(getattr(self, "profile", None)))
            save_state(overview)
        except Exception as exc:  # noqa: BLE001
            self.log("warn", f"复查失败：{exc}")

        self.log("done", f"成功 {self.ok} · 已完成 {self.already} · "
                         f"跳过 {self.skip} · 失败 {self.fail}")
        return {"ok": self.ok, "already": self.already,
                "skip": self.skip, "fail": self.fail}

    # ---- 只刷新不执行
    def refresh(self) -> dict:
        client = self._client()
        profile = self._check_vip(client)
        combine = client.combine()
        overview = build_overview(combine)
        overview.update(_profile_fields(profile))
        save_state(overview)
        return overview


def refresh_state(cfg: dict | None = None) -> dict:
    return VipBigPoint(cfg or load_config(), lambda *_: None).refresh()


def code_mtime() -> float:
    try:
        return Path(__file__).resolve().stat().st_mtime
    except Exception:
        return 0.0
