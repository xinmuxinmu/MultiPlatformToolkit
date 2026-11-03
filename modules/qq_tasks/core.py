#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 音乐每日签到 + 乐迷勋章每日签到。

- 日常签到：登录 y.qq.com 后拿 uin / qqmusic_key，请求
  u6.y.qq.com/cgi-bin/musics.fcg 的 ActCenterSignNewSvr.SignIn。
  当前活动 ID 是 Z25hHGi（积分中心页实际调用，ScenesID=2）。
- 乐迷勋章：musicu.fcg 的 medalHall 系列接口。
  GetUserAllMedalByModuleID 拿账号全部勋章，SignIn 逐枚签到。
  MedalID 必须传字符串，传数字会返回 10006。

签到接口只提交一次，不重试轰炸；重复签到按成功处理。

（QQ 日签卡 / 晚安卡已移除：ti.qq.com 那套登录页是手机体验推广页，
桌面扫码授权拿不到 p_skey，点「完成登录」也不会回填，留着只会误导。）
"""
from __future__ import annotations

import gzip
import hashlib
import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.paths import DATA_ROOT

VERSION = "1.3.0"
APP_ID = "qq-tasks"

HOME = DATA_ROOT / "qq_tasks"
HOME.mkdir(parents=True, exist_ok=True)
MUSIC_CONFIG = HOME / "music.json"
STATE_PATH = HOME / "state.json"

MUSIC_ACT_ID = "Z25hHGi"
MUSIC_SCENES_ID = "2"
MUSIC_URL = "https://u6.y.qq.com/cgi-bin/musics.fcg"
MEDAL_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
MEDAL_MODULE_ID = 2   # 乐迷勋章所在的勋章模块

QQ_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36 "
         "QQJSSDK/1.3 /ANDROIDQQMUSIC/11050108 QQMusic/11.5.1.8 H5/1")

DEFAULT_MUSIC = {"cookie": "", "sign_enable": True, "medal_enable": True,
                 "medal_tasks_enable": True, "medal_task_types": None,
                 "medal_selected_ids": None, "card_pool_enable": True}

# 这几类任务官方客户端也是「做完动作后上报」，可以直接调上报接口；
# 新广告、听歌、付费、游戏类会被服务端拒绝，不硬试。
REPORT_TASK_TYPES = ("watch_ads", "share_song", "share_medal",
                     "page_browsed", "calendar_reminder")
AUTO_MEDAL_TASK_TYPES = (*REPORT_TASK_TYPES, "medal_praised_singer",
                         "medal_card_draw_card")
POOL_ID_RE = re.compile(r"[?&]pool_id=([0-9A-Za-z\-]+)")

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


def save_music(cfg: dict):
    _save(MUSIC_CONFIG, {k: cfg.get(k, v) for k, v in DEFAULT_MUSIC.items()})


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

    def _comm(self) -> dict:
        return {
            "g_tk": _g_tk(self.cookie), "uin": self.uin, "format": "json",
            "inCharset": "utf-8", "outCharset": "utf-8", "notice": 0,
            "platform": "h5", "needNewCode": 1,
        }

    def _medal_call(self, module: str, method: str, param: dict):
        """勋章接口统一走 musicu.fcg，req_0 单个模块。"""
        payload = {
            "comm": self._comm(),
            "req_0": {"module": module, "method": method, "param": param},
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        sign = ("zza" + "".join(random.choice("0123456789abcdef") for _ in range(10))
                + hashlib.md5(("CJBPACrRuNy7" + body).encode()).hexdigest())
        data = _post_json(f"{MEDAL_URL}?sign={sign}", body, {
            "User-Agent": QQ_UA, "Content-Type": "application/json",
            "Origin": "https://i.y.qq.com", "Referer": "https://y.qq.com/",
            "Cookie": self.cookie,
        })
        if int(data.get("code") or 0) != 0:
            raise QQError(f"QQ 音乐返回错误：{data.get('code')}")
        req = data.get("req_0") or {}
        code = int(req.get("code") or 0)
        if code in (1000, 500003):
            raise QQError("QQ 音乐 Cookie 已失效，请重新快捷登录")
        return code, req.get("data")

    def medals(self) -> list[dict]:
        code, data = self._medal_call(
            "music.medalHall.MedalHallBaseDataSrv", "GetUserAllMedalByModuleID",
            {"ModuleID": MEDAL_MODULE_ID, "SortType": 2,
             "FirstDisplayPageSize": 20, "Limit": 0})
        if code != 0 or not isinstance(data, dict):
            raise QQError(f"拉取勋章列表失败（code={code}）")
        shown = {str(x.get("MedalID")): x for x in (data.get("DisplayList") or [])}
        out = []
        for item in (data.get("InfoList") or []):
            mid = str(item.get("MedalID") or "").strip()
            if not mid:
                continue
            info = shown.get(mid) or {}
            out.append({
                "id": mid,
                "name": str(info.get("Name") or ""),
                "level": int(item.get("Level") or 0),
                "pic": str(info.get("PicURL") or ""),
            })
        return out

    def medal_status(self, medal_id: str) -> dict:
        code, data = self._medal_call(
            "music.medalHall.MedalSignInSrv", "GetSignInDetailPage",
            {"MedalID": str(medal_id)})
        if code != 0 or not isinstance(data, dict):
            raise QQError(f"查询勋章状态失败（code={code}）")
        return {
            "signed": int(data.get("HasSignIn") or 0) == 1,
            "continue_days": int(data.get("ContinueDays") or 0),
            "sign_number": int(data.get("SignInNumber") or 0),
        }

    def medal_sign(self, medal_id: str) -> dict:
        """单枚勋章签到；返回签到后的状态，失败抛 QQError。

        SignInMode=1 / NeedMedalFanValue=1 是勋章详情页实际发的参数，
        少了这两个会返回 10000。
        """
        code, _ = self._medal_call(
            "music.medalHall.MedalSignInSrv", "SignIn",
            {"MedalID": str(medal_id), "NeedMedalFanValue": 1, "SignInMode": 1})
        if code in (30001, 40001):
            return {"already": True, "signed": True,
                    "continue_days": 0, "sign_number": 0,
                    "msg": "今天已经签到过了"}
        if code != 0:
            raise QQError(f"签到失败（code={code}）")
        try:
            st = self.medal_status(medal_id)
        except QQError:
            st = {"signed": True, "continue_days": 0, "sign_number": 0}
        st["already"] = False
        extra = []
        if st.get("continue_days"):
            extra.append(f"连续 {st['continue_days']} 天")
        if st.get("sign_number"):
            extra.append(f"累计 {st['sign_number']} 次")
        st["msg"] = "签到成功" + (f"（{'，'.join(extra)}）" if extra else "")
        return st

    def task_list(self, medal_id: str) -> list[dict]:
        code, data = self._medal_call(
            "music.medalHall.MedalTaskSrv", "GetTaskList",
            {"From": 1, "MedalID": str(medal_id)})
        if code != 0 or not isinstance(data, dict):
            raise QQError(f"拉取勋章任务失败（code={code}）")
        out = []
        for mod in (data.get("Detail") or []):
            out.extend(mod.get("TaskList") or [])
        return out

    def report_task(self, medal_id: str, task: dict) -> tuple[bool, int]:
        code, _ = self._medal_call(
            "music.medalHall.MedalTaskSrv", "CgiReportCondition",
            {"TaskType": str(task.get("TaskType") or ""),
             "MedalID": str(medal_id),
             "ExtInfoStr": str(task.get("ExtInfoStr") or "")})
        return code == 0, code

    def praise_rank(self, medal_id: str, need: int) -> tuple[int, int]:
        """给排行榜上未点赞的乐迷点赞，返回 (成功数, 失败数)。"""
        code, data = self._medal_call(
            "music.medalHall.MedalRankListSrv", "GetRankModule",
            {"MedalID": str(medal_id), "FirstPageSize": 20,
             "From": 1, "Target": ""})
        if code != 0 or not isinstance(data, dict):
            raise QQError(f"拉取勋章排行失败（code={code}）")
        items = ((data.get("FirstTabDetail") or {}).get("ItemList")) or []
        got = bad = 0
        for item in items:
            if got >= need:
                break
            if item.get("IsPraised"):
                continue
            enc = str(item.get("ItemID") or "").strip()
            if not enc:
                continue
            code, _ = self._medal_call(
                "music.medalHall.MedalAppreciationSrv", "PraiseMedal",
                {"From": 1, "Oper": 1, "MedalID": str(medal_id), "EncUin": enc})
            if code == 0:
                got += 1
                time.sleep(random.uniform(0.6, 1.1))
            else:
                bad += 1
        return got, bad

    # ---------------------------------------------------------------- 歌词卡/抽卡页

    def draw_card(self, pool_id: str) -> dict:
        """抽 1 次歌词卡（消耗账号已有的抽卡次数，不花钱）。"""
        code, d = self._medal_call("music.medalCard.MedalCardSrv", "GenerateOrderID",
                                   {"PoolID": str(pool_id)})
        if code != 0 or not (d or {}).get("OrderID"):
            raise QQError("生成抽卡订单失败")
        order = str(d["OrderID"])
        code, d = self._medal_call(
            "music.medalCard.MedalCardSrv", "DrawCard",
            {"PoolID": str(pool_id), "Num": 1, "OrderID": order, "InviterEncUin": ""})
        if code != 0:
            raise QQError(f"抽卡失败（code={code}）")
        cards = (d or {}).get("CardList") or (d or {}).get("Cards") or []
        first = cards[0] if cards else {}
        return {"ok": True, "order": order,
                "card": str(first.get("Name") or ""),
                "rarity": int(first.get("Rarity") or 0)}

    def card_pool_signin(self, pool_id: str) -> dict:
        """抽卡页每日签到（GetDailySignInTask 拉取即触发当日签到）。"""
        code, d = self._medal_call("music.medalCard.MedalCardTaskSrv",
                                   "GetDailySignInTask", {"PoolID": str(pool_id)})
        if code != 0:
            raise QQError(f"抽卡页签到失败（code={code}）")
        signed = int((d or {}).get("HasSignIn") or 0) == 1
        return {"ok": True, "signed": signed}

    def flip_card_challenge(self, pool_id: str, max_games: int = 3) -> dict:
        """抽卡页「翻卡挑战」：把当天免费次数玩完（每次都真打完上报）。"""
        issue, biz = str(pool_id), "medalcard"
        code, d = self._medal_call("music.singercard.gamesvr", "GetFlipGameConfig",
                                   {"issue": issue, "biz": biz})
        if code != 0:
            raise QQError(f"翻卡挑战配置拉取失败（code={code}）")
        remain = int((((d or {}).get("userState") or {}).get("todayRemainCount")) or 0)
        games = debris = 0
        for _ in range(min(remain, max_games)):
            code, d = self._medal_call("music.singercard.gamesvr", "StartFlipGame",
                                       {"issue": issue, "biz": biz, "difficulty": 1})
            gid = (d or {}).get("gameID")
            if code != 0 or not gid:
                break
            perfect = int((d or {}).get("perfectStep") or 7)
            total = int((d or {}).get("totalTimeSec") or 30)
            pairs = max(1, perfect - 1)
            for pct, matched in ((25, pairs // 3), (60, pairs * 2 // 3), (100, pairs)):
                self._medal_call("music.singercard.gamesvr", "ReportFlipGameProgress",
                                 {"issue": issue, "gameID": gid, "difficulty": 1,
                                  "progressPercent": pct, "matchedPairNum": matched,
                                  "biz": biz})
                time.sleep(random.uniform(1.5, 2.5))
            code, d = self._medal_call("music.singercard.gamesvr", "FinishFlipGame",
                                       {"issue": issue, "gameID": gid,
                                        "usedSteps": perfect,
                                        "usedTimeMS": int(total * 1000 * 0.7),
                                        "biz": biz})
            if code != 0:
                break
            games += 1
            debris += int((d or {}).get("debrisRewardFinal") or 0)
            time.sleep(random.uniform(1.0, 2.0))
        return {"ok": True, "remain": remain, "games": games, "debris": debris}

    def _act_call(self, method: str, param: dict):
        payload = {
            "comm": {**self._comm(), "ct": 23, "cv": 0, "mesh_devops": "DevopsBase"},
            "req_0": {
                "module": "music.actCenter.ActCenterSignNewSvr",
                "method": method, "param": param,
            },
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        sign = ("zza" + "".join(random.choice("0123456789abcdef") for _ in range(10))
                + hashlib.md5(("CJBPACrRuNy7" + body).encode()).hexdigest())
        url = (f"{MUSIC_URL}?_webcgikey={method}"
               f"&_={int(time.time() * 1000)}&sign={sign}")
        data = _post_json(url, body, {
            "User-Agent": QQ_UA, "Content-Type": "application/json",
            "Origin": "https://i.y.qq.com",
            "Referer": "https://i2.y.qq.com/n3/client/pages/points_center/index.html",
            "Cookie": self.cookie,
        })
        if int(data.get("code") or 0) != 0:
            raise QQError(f"QQ 音乐返回错误：{data.get('code')}")
        req = data.get("req_0") or {}
        code = int(req.get("code") or 0)
        if code in (1000, 500003):
            raise QQError("QQ 音乐 Cookie 已失效，请重新快捷登录")
        if code != 0:
            raise QQError(f"QQ 音乐签到失败（code={code}）")
        result = req.get("data") or {}
        ret = int(result.get("retCode") or 0)
        if ret != 0:
            raise QQError(f"QQ 音乐签到失败：{result.get('retMsg') or ret}")
        return result

    def sign_status(self) -> dict:
        result = self._act_call("GetSignInSummary", {"ActID": MUSIC_ACT_ID})
        info = result.get("Info") or {}
        return {
            "signed": int(info.get("IsSignIn") or 0) == 1,
            "continue_days": int(info.get("ContinueSignInCount") or 0),
            "total_days": int(info.get("AccSignInCount") or 0),
        }

    def sign(self) -> tuple[bool, str]:
        result = self._act_call(
            "SignIn", {"ActID": MUSIC_ACT_ID, "ScenesID": MUSIC_SCENES_ID})
        info = result.get("Info") or {}
        if int(info.get("IsSignIn") or 0) != 1:
            raise QQError("QQ 音乐签到接口没有返回已签到状态")
        days = int(info.get("ContinueSignInCount") or 0)
        total = int(info.get("AccSignInCount") or 0)
        tail = []
        if days:
            tail.append(f"连续 {days} 天")
        if total:
            tail.append(f"累计 {total} 天")
        return True, "签到成功" + (f"（{'，'.join(tail)}）" if tail else "")


# ---------------------------------------------------------------- 乐迷勋章

def refresh_medals(cookie: str) -> dict:
    """只读拉取账号全部乐迷勋章的今日签到状态。"""
    client = MusicClient(cookie)
    medals = client.medals()
    for m in medals:
        st = client.medal_status(m["id"])
        m.update(st)
        try:
            tasks = client.task_list(m["id"])
            views = [_task_view(t) for t in tasks]
            m["task_total"] = len(views)
            m["task_done"] = sum(1 for v in views if v["done"])
            m["tasks"] = views
        except QQError:
            pass
        if not m.get("name"):
            m["name"] = f"勋章 {m['id']}"
    signed = sum(1 for m in medals if m.get("signed"))
    return {"ok": True, "signed": signed, "total": len(medals),
            "medals": medals, "at": int(time.time())}


def _task_done(t: dict) -> bool:
    total = int(t.get("TotalTimes") or 0)
    achieved = int(t.get("AchievedTimes") or 0)
    return int(t.get("Status") or 0) == 2 or (total > 0 and achieved >= total)


def _task_view(t: dict) -> dict:
    typ = str(t.get("TaskType") or "").strip()
    return {
        "name": str(t.get("Title") or t.get("TaskName") or typ or "未命名任务"),
        "type": typ,
        "finish": int(t.get("AchievedTimes") or 0),
        "need": int(t.get("TotalTimes") or 0),
        "done": _task_done(t),
        "auto": typ in REPORT_TASK_TYPES or typ in ("sign_in", "medal_praised_singer"),
    }


def _selected_task_types(selected) -> set[str]:
    if not isinstance(selected, (list, tuple, set)):
        return set(AUTO_MEDAL_TASK_TYPES)
    return {str(x) for x in selected if str(x or "").strip()}


def complete_medal_tasks(client: MusicClient, medal_id: str, selected=None) -> dict:
    """遍历该勋章当前的全部任务，只执行设置里勾选了的任务类型。"""
    tasks = client.task_list(medal_id)
    before = sum(1 for t in tasks if _task_done(t))
    picked = _selected_task_types(selected)
    fail = 0
    card_pools = set()
    for t in tasks:
        if str(t.get("TaskType") or "").strip() == "medal_card_draw_card":
            m = POOL_ID_RE.search(str(t.get("BtnJumpURL") or ""))
            if m:
                card_pools.add(m.group(1))
    for t in tasks:
        if _task_done(t):
            continue
        typ = str(t.get("TaskType") or "").strip()
        if typ == "sign_in" or typ not in picked:
            continue
        total = int(t.get("TotalTimes") or 1)
        achieved = int(t.get("AchievedTimes") or 0)
        need = max(0, total - achieved)
        if need <= 0:
            continue
        if typ == "medal_praised_singer":
            try:
                _got, bad = client.praise_rank(medal_id, need)
                fail += bad
            except QQError:
                fail += 1
        elif typ == "medal_card_draw_card":
            if not card_pools:
                fail += 1
                continue
            pool = next(iter(card_pools))
            for _ in range(need):
                try:
                    client.draw_card(pool)
                except QQError:
                    fail += 1
                    break
                time.sleep(random.uniform(1.0, 1.8))
        else:
            for _ in range(need):
                ok, _code = client.report_task(medal_id, t)
                if not ok:
                    time.sleep(2.5)
                    ok, _code = client.report_task(medal_id, t)
                if not ok:
                    fail += 1
                    break
                time.sleep(random.uniform(0.8, 1.4))
    after = client.task_list(medal_id)
    views = [_task_view(t) for t in after]
    done = sum(1 for v in views if v["done"])
    remaining = [v["name"] for v in views
                 if not v["done"] and v["type"] != "sign_in" and v["type"] in picked]
    unselected = [v for v in views
                  if not v["done"] and v["type"] != "sign_in" and v["type"] not in picked]
    return {
        "total": len(views),
        "done": done,
        "auto": max(0, done - before),
        "fail": fail,
        "skipped": sum(1 for v in views if not v["done"]),
        "unselected": len(unselected),
        "remaining": remaining,
        "tasks": views,
        "card_pools": sorted(card_pools),
    }


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
        result = {"music": {}}
        done = fail = skipped = 0
        try:
            mc = load_music()
            cookie = mc.get("cookie") or ""
            if not mc.get("sign_enable", True):
                self.log("info", "QQ 音乐签到开关关着，跳过")
                skipped += 1
            elif not cookie:
                self.log("warn", "QQ 音乐还没登录，跳过")
                skipped += 1
            else:
                try:
                    client = MusicClient(cookie)
                    st = client.sign_status()
                    if st["signed"]:
                        msg = f"今天已经签到过了（连续 {st['continue_days']} 天）"
                        self.log("info", f"QQ 音乐：{msg}")
                        result["music"] = {"ok": True, "msg": msg, **st,
                                           "at": int(time.time())}
                        skipped += 1
                    else:
                        ok, msg = client.sign()
                        self.log("success" if ok else "warn", f"QQ 音乐：{msg}")
                        result["music"] = {"ok": ok, "msg": msg, **st,
                                           "signed": ok, "at": int(time.time())}
                        done += 1 if ok else 0
                        fail += 0 if ok else 1
                except QQError as exc:
                    self.log("error", f"QQ 音乐：{exc}")
                    result["music"] = {"ok": False, "msg": str(exc), "at": int(time.time())}
                    fail += 1

            # 乐迷勋章：逐枚签到，单枚失败不影响其他勋章
            if not mc.get("medal_enable", True):
                self.log("info", "乐迷勋章开关关着，跳过")
                skipped += 1
            elif not cookie:
                self.log("warn", "QQ 音乐还没登录，乐迷勋章跳过")
                skipped += 1
            else:
                medal = self._run_medals(cookie, mc.get("medal_tasks_enable", True),
                                         mc.get("medal_task_types"),
                                         mc.get("medal_selected_ids"))
                if medal:
                    result["medal"] = medal
                    done += medal.get("done", 0)
                    fail += medal.get("fail", 0)
                    skipped += medal.get("skipped", 0)

            # 抽卡页（歌词卡）：每日签到 + 翻卡挑战免费次数
            pools = (result.get("medal") or {}).get("card_pools") or []
            if not mc.get("card_pool_enable", True):
                if pools:
                    self.log("info", "抽卡页任务开关关着，跳过")
                skipped += 1 if pools else 0
            elif not cookie:
                self.log("warn", "QQ 音乐还没登录，抽卡页跳过")
                skipped += 1 if pools else 0
            elif pools:
                card = self._run_card_pools(cookie, pools)
                result["card_pool"] = card
                done += card.get("done", 0)
                fail += card.get("fail", 0)
                skipped += card.get("skipped", 0)
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

    def _run_medals(self, cookie: str, tasks_enable: bool = True, selected=None,
                    medal_ids=None) -> dict:
        client = MusicClient(cookie)
        try:
            medals = client.medals()
        except QQError as exc:
            self.log("error", f"乐迷勋章：{exc}")
            return {"ok": False, "error": str(exc), "medals": [],
                    "done": 0, "fail": 1, "skipped": 0, "at": int(time.time())}
        if not medals:
            self.log("warn", "乐迷勋章：账号下没有勋章")
            return {"ok": True, "signed": 0, "total": 0, "medals": [],
                    "done": 0, "fail": 0, "skipped": 1, "at": int(time.time())}

        all_total = len(medals)
        all_medals = list(medals)
        prev_medals = {str(x.get("id")): x
                       for x in ((load_state().get("medal") or {}).get("medals") or [])
                       if x.get("id") is not None}

        def unselected_view(m: dict) -> dict:
            """未勾选的勋章：沿用上次刷新到的状态，只标记「未选」，不发任何请求。"""
            row = dict(m)
            old = prev_medals.get(str(row.get("id"))) or {}
            for k in ("signed", "continue_days", "sign_number", "task_total",
                      "task_done", "task_auto", "task_skipped", "tasks", "msg"):
                if k in old:
                    row[k] = old[k]
            row["unselected"] = True
            return row

        wanted = None
        if isinstance(medal_ids, (list, tuple, set)):
            wanted = {str(x) for x in medal_ids if str(x or "").strip()}
            picked = [m for m in all_medals if str(m.get("id")) in wanted]
            if not picked:
                self.log("info", "乐迷勋章：没有勾选任何勋章/歌手，跳过")
                out = [unselected_view(m) for m in all_medals]
                return {"ok": True, "signed": sum(1 for m in out if m.get("signed")),
                        "total": len(out), "medals": out, "all_total": all_total,
                        "done": 0, "fail": 0, "skipped": 1,
                        "at": int(time.time())}
            if len(picked) < all_total:
                self.log("info", f"乐迷勋章：按选择只处理 {len(picked)}/{all_total} 枚")
            medals = picked

        done = fail = skipped = 0
        card_pools = set()
        for i, m in enumerate(medals):
            mid = m["id"]
            name = m.get("name") or f"勋章 {mid}"
            try:
                st = client.medal_status(mid)
                m.update(st)
                if st["signed"]:
                    self.log("info", f"{name}：今天已经签到过了")
                    skipped += 1
                    m["msg"] = "今天已经签到过了"
                else:
                    st = client.medal_sign(mid)
                    m.update(st)
                    if st.get("already"):
                        self.log("info", f"{name}：{st['msg']}")
                        skipped += 1
                    else:
                        self.log("success", f"{name}：{st['msg']}")
                        done += 1
                    if i < len(medals) - 1:
                        time.sleep(random.uniform(1.2, 2.4))
            except QQError as exc:
                self.log("warn", f"{name}：{exc}")
                m["msg"] = str(exc)
                m["error"] = str(exc)
                fail += 1

            if tasks_enable:
                try:
                    tr = complete_medal_tasks(client, mid, selected)
                    m.update({"task_total": tr["total"], "task_done": tr["done"],
                              "task_auto": tr["auto"], "task_skipped": tr["skipped"],
                              "tasks": tr["tasks"]})
                    done += tr["auto"]
                    fail += tr["fail"]
                    skipped += tr["skipped"]
                    card_pools.update(tr.get("card_pools") or [])
                    tail = f"，本次自动完成 {tr['auto']} 项" if tr["auto"] else ""
                    rest = f"（其余 {tr['skipped']} 项未完成）" if tr["skipped"] else ""
                    self.log("info", f"{name}：任务 {tr['done']}/{tr['total']}{tail}{rest}")
                    if tr["remaining"]:
                        names = "、".join(tr["remaining"][:6])
                        more = " 等" if len(tr["remaining"]) > 6 else ""
                        self.log("info", f"{name}：已选任务未完成 {names}{more}")
                    if tr["unselected"]:
                        self.log("info", f"{name}：未勾选 {tr['unselected']} 项")
                except QQError as exc:
                    self.log("warn", f"{name}：任务处理失败 {exc}")
                    fail += 1

            if not m.get("name"):
                m["name"] = f"勋章 {mid}"

        picked_ids = {str(m.get("id")) for m in medals}
        out = [m if str(m.get("id")) in picked_ids else unselected_view(m)
               for m in all_medals]
        signed = sum(1 for m in out if m.get("signed"))
        total = len(out)
        self.log("info", f"乐迷勋章：今天已签到 {signed}/{total} 枚"
                         + (f"（本次处理 {len(medals)} 枚）" if wanted is not None else ""))
        return {"ok": True, "signed": signed, "total": total, "medals": out,
                "all_total": all_total, "card_pools": sorted(card_pools),
                "done": done, "fail": fail, "skipped": skipped,
                "at": int(time.time())}

    def _run_card_pools(self, cookie: str, pools: list[str]) -> dict:
        """抽卡页（歌词卡）任务：每日签到 + 翻卡挑战免费次数。"""
        client = MusicClient(cookie)
        result = {"pools": [], "done": 0, "fail": 0, "skipped": 0}
        for pool in pools:
            row = {"pool": pool, "signin": None, "flip": None}
            try:
                row["signin"] = client.card_pool_signin(pool)
                if row["signin"]["signed"]:
                    self.log("info", f"抽卡页 {pool}：今天已签到")
                    result["skipped"] += 1
                else:
                    self.log("success", f"抽卡页 {pool}：签到完成")
                    result["done"] += 1
                time.sleep(random.uniform(1.0, 2.0))
            except QQError as exc:
                row["signin"] = {"ok": False, "error": str(exc)}
                self.log("warn", f"抽卡页 {pool}：签到失败 {exc}")
                result["fail"] += 1
            try:
                row["flip"] = client.flip_card_challenge(pool)
                if row["flip"]["games"]:
                    self.log("success", f"抽卡页 {pool}：翻卡挑战 {row['flip']['games']} 局"
                                       f"，得碎片 {row['flip']['debris']}")
                    result["done"] += row["flip"]["games"]
                else:
                    self.log("info", f"抽卡页 {pool}：翻卡挑战今天没有免费次数了")
                    result["skipped"] += 1
            except QQError as exc:
                row["flip"] = {"ok": False, "error": str(exc)}
                self.log("warn", f"抽卡页 {pool}：翻卡挑战失败 {exc}")
                result["fail"] += 1
            result["pools"].append(row)
        result["at"] = int(time.time())
        return result


TASK = Task()


# ---------------------------------------------------------------- 快捷登录

def _verify_music(cookie: str) -> tuple[bool, str]:
    ck = parse_cookie(cookie)
    if not music_ready(cookie):
        return False, ""
    return True, str(ck.get("uin") or ck.get("wxuin") or "")


def _save_music_login(cookie: str, who: str):
    cfg = load_music()
    cfg["cookie"] = cookie
    save_music(cfg)


def _bind_login():
    from app import quick_login
    ms = quick_login.PLATFORMS.get("qq_music")
    if ms is not None:
        ms["verify"] = _verify_music
        ms["save"] = _save_music_login


def _session(which: str = "music"):
    _bind_login()
    from app import quick_login
    return quick_login.get("qq_music")


def login_start(which: str) -> bool:
    return _session(which).start()


def login_grab(which: str) -> dict:
    return _session(which).grab()


def login_snapshot(which: str) -> dict:
    return _session(which).snapshot()


_bind_login()
