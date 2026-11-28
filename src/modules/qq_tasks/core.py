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
import urllib.parse
import urllib.request
from pathlib import Path

from app.paths import DATA_ROOT

VERSION = "1.6.0"
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
                 "medal_selected_ids": None, "card_pool_enable": True,
                 "auto_all_free": True, "card_draw_enable": False,
                 "schedule_enabled": False, "schedule_time": "09:30"}

# 与官方勋章 H5 的跳转逻辑一致：访问成功后，只有明确声明
# NeedReportCondition 的浏览任务才上报。广告、分享、听歌不能用空上报代替。
REPORT_TASK_TYPES = ("page_browsed", "homepage_browsed", "comm_jump",
                     "jump_external_app_singer")
AUTO_MEDAL_TASK_TYPES = (*REPORT_TASK_TYPES, "medal_praised_singer")
PAID_TYPE_RE = re.compile(
    r"(?:^buy_|^open_.*vip|supvip|supersubscr|super_subscr|paid|payment|"
    r"^mp_light_activity$|_light_activity_|^song_reward$|^gather_stars_|"
    r"^fan_club_open_|^ai_dou_tao_consumption|^open_.*lightsign)", re.I)
PAID_TITLE_RE = re.compile(r"购买|付费|付邮|充值|开通|续费|金币兑换|消耗金币|超级会员|开超会")

_cfg_lock = threading.Lock()
# 刷新与执行共享同一把锁，防止较早请求的旧进度晚返回后覆盖执行结果。
OPERATION_LOCK = threading.Lock()


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
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(path)

def load_music() -> dict:
    return _load(MUSIC_CONFIG, DEFAULT_MUSIC)

def save_music(cfg: dict):
    data = {k: cfg.get(k, v) for k, v in DEFAULT_MUSIC.items()}
    if not isinstance(data["cookie"], str):
        raise ValueError("Cookie 必须是文本")
    data["cookie"] = norm_cookie(data["cookie"])
    for key in ("sign_enable", "medal_enable", "medal_tasks_enable", "card_pool_enable",
                "auto_all_free", "card_draw_enable", "schedule_enabled"):
        if not isinstance(data[key], bool):
            raise ValueError(f"{key} 必须是布尔值")
    for key in ("medal_task_types", "medal_selected_ids"):
        if data[key] is not None:
            if not isinstance(data[key], list) or any(not isinstance(x, (str, int))
                                                     for x in data[key]):
                raise ValueError(f"{key} 必须是列表或 null")
            data[key] = list(dict.fromkeys(str(x).strip() for x in data[key] if str(x).strip()))
    from modules.qq_tasks import scheduler
    scheduler.validate_config(data)
    _save(MUSIC_CONFIG, data)

def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_state(data: dict):
    try:
        _save(STATE_PATH, data)
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

def _number(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default

def _enabled(value) -> bool:
    return value is True or str(value).lower() in ("1", "true", "yes")

def _service_result(payload: dict) -> tuple[int, object]:
    """缺 req/code 不能按成功处理，否则限流/登录页会变成假完成。"""
    if "code" not in payload or _number(payload["code"], -1) != 0:
        raise QQError(f"QQ 音乐返回错误：{payload.get('code', '缺少状态码')}")
    req = payload.get("req_0")
    if not isinstance(req, dict) or "code" not in req:
        raise QQError("QQ 音乐接口返回结构异常（缺少任务响应）")
    code = _number(req["code"], -1)
    if code in (1000, 500003):
        raise QQError("QQ 音乐 Cookie 已失效，请重新快捷登录")
    return code, req.get("data")

def _task_identity(task: dict) -> str:
    return str(task.get("TaskID") or hashlib.sha256(
        json.dumps([task.get("TaskType"), task.get("Title"), task.get("ExtInfoStr"),
                    task.get("BtnJumpURL")], ensure_ascii=False).encode()).hexdigest()[:20])

def _flatten_tasks(data: dict) -> list[dict]:
    """兼容旧 Detail[]、当前 Detail.TaskList 和顶层 TaskList。"""
    tasks = []
    seen = set()

    def collect(node):
        if isinstance(node, list):
            for item in node:
                collect(item)
        elif isinstance(node, dict):
            if "TaskType" in node or "TaskID" in node:
                key = _task_identity(node)
                if key not in seen:
                    tasks.append(node)
                    seen.add(key)
                return
            for key in ("Detail", "TaskList"):
                collect(node.get(key))

    collect(data)
    return tasks

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
        return _service_result(data)

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
        except QQError as exc:
            return {"signed": None, "already": False, "verified": False,
                    "msg": "签到已提交，状态待核对", "verify_error": str(exc)}
        if not st["signed"]:
            return {**st, "already": False, "verified": True,
                    "msg": "签到已提交，服务端尚未确认完成"}
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
        return _flatten_tasks(data)

    def report_task(self, medal_id: str, task: dict) -> tuple[bool, int]:
        code, _ = self._medal_call(
            "music.medalHall.MedalTaskSrv", "CgiReportCondition",
            {"TaskType": str(task.get("TaskType") or ""),
             "MedalID": str(medal_id),
             "ExtInfoStr": str(task.get("ExtInfoStr") or "")})
        return code == 0, code

    def visit_task(self, task: dict) -> None:
        """真实加载浏览类任务入口；不执行页面中的支付/抽卡/领奖按钮。"""
        self._open_task_page(task)

    def play_browser_game(self, task: dict) -> dict:
        """在已登录的官方页面实际游玩；由勋章任务回读确认奖励。"""
        from modules.qq_tasks.browser_games import (BrowserGameError, play_browser_game,
                                                    supports_browser_game)
        if not supports_browser_game(_task_url(task)):
            raise QQError("此小游戏尚无可验证的电脑端自动流程")
        def action(page):
            try:
                return play_browser_game(page)
            except BrowserGameError as exc:
                raise QQError(str(exc)) from None
        return self._open_task_page(task, action=action)

    def set_free_companion(self, task: dict) -> dict:
        from modules.qq_tasks.free_actions import ActionError, select_free_companion
        try:
            return select_free_companion(self, task)
        except ActionError as exc:
            raise QQError(str(exc)) from exc

    def _open_task_page(self, task: dict, *, action=None):
        url = _task_url(task)
        if not url:
            raise QQError("任务没有可在电脑浏览器打开的入口")
        try:
            from playwright.sync_api import sync_playwright
            from modules.signin.core import find_browser
            from app.ui_settings import is_headless
            with sync_playwright() as pw:
                options = {"headless": is_headless()}
                browser_path = find_browser()
                if browser_path:
                    options["executable_path"] = str(browser_path)
                browser = pw.chromium.launch(**options)
                try:
                    context = browser.new_context(
                        user_agent=QQ_UA, viewport={"width": 430, "height": 900},
                        is_mobile=True, has_touch=True)
                    context.add_cookies([{"name": k, "value": v, "domain": ".y.qq.com",
                                          "path": "/"} for k, v in parse_cookie(self.cookie).items()])
                    page = context.new_page()
                    response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    if response is None or response.status >= 400:
                        raise QQError("浏览任务页面未能成功加载")
                    if action is not None:
                        return action(page)
                    page.wait_for_timeout(3000)
                finally:
                    browser.close()
        except QQError:
            raise
        except Exception:
            # Browser exceptions may include the authenticated task URL and
            # its query parameters. Never copy them into task errors or logs.
            raise QQError("浏览任务页面失败，请刷新任务进度确认状态") from None

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
            if _enabled(item.get("IsPraised")):
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

    def draw_card(self, pool_id: str, *, allow_existing_chances: bool = False) -> dict:
        """显式启用后，普通卡池只使用一张已有抽卡次数，不购买或兑换。"""
        if allow_existing_chances is not True:
            raise QQError("使用已有抽卡次数的开关未开启")
        pool = str(pool_id)
        if not re.fullmatch(r"[0-9A-Za-z-]+", pool):
            raise QQError("抽卡任务缺少有效卡池")
        module = "music.medalCard.MedalCardSrv"
        code, home = self._medal_call(module, "GetCardHomepage", {"PoolID": pool})
        if code != 0 or not isinstance(home, dict):
            raise QQError("无法确认普通卡池状态，未抽卡")
        vip = home.get("SVipPoolInfo")
        status = home.get("StatusInfo")
        if (not isinstance(vip, dict) or vip.get("SVipPool") is not False
                or not isinstance(status, dict) or _number(status.get("Status"), -1) != 0):
            raise QQError("卡池付费属性或开放状态不明确，未抽卡")
        if _number(vip.get("SinglePrice"), -1) != 0 or _number(vip.get("TenPrice"), -1) != 0:
            raise QQError("卡池含付费抽取配置，未抽卡")
        balance = _number(home.get("RemainDraws"))
        if balance < 1:
            raise QQError("没有可用的普通抽卡次数，不自动购买")
        code, order = self._medal_call(module, "GenerateOrderID", {"PoolID": pool})
        order_id = (order or {}).get("OrderID") if isinstance(order, dict) else None
        if code != 0 or not isinstance(order_id, str) or not order_id or len(order_id) > 256:
            raise QQError("无法生成单次抽卡流水，未抽卡")
        code, data = self._medal_call(module, "DrawCard", {
            "PoolID": pool, "Num": 1, "OrderID": order_id, "InviterEncUin": ""})
        if code != 0:
            raise QQError(f"单次抽卡未成功（code={code}），不重试或购买")
        if not isinstance(data, dict) or not isinstance(data.get("Cards"), list) or not data["Cards"]:
            raise QQError("抽卡已提交但响应缺少卡片，需刷新确认，不重复抽取")
        remaining = _number(data.get("RemainDraws"), -1)
        if remaining != balance - 1:
            raise QQError("抽卡已提交但次数变化待核对，不重复抽取")
        return {"ok": True, "drawn": 1, "remaining": remaining,
                "msg": "已使用1次普通抽卡次数，等待勋章任务核对"}

    def card_pool_signin(self, pool_id: str) -> dict:
        """抽卡页每日签到。

        GetDailySignInTask 只是读取阶梯；真正签到要按当前阶梯的
        ExtInfoStr 调 MedalCardTaskSrv.CgiTaskReport（网页端同款）。
        """
        code, d = self._medal_call("music.medalCard.MedalCardTaskSrv",
                                   "GetDailySignInTask", {"PoolID": str(pool_id)})
        if code != 0:
            raise QQError(f"抽卡页签到状态拉取失败（code={code}）")
        if not isinstance(d, dict):
            raise QQError("抽卡页签到状态结构异常")
        data = d
        if int(data.get("HasSignIn") or 0) == 1:
            return {"ok": True, "signed": True, "already": True, "msg": "今天已经签到过了"}
        tasks = data.get("SignInTask") or []
        if not tasks:
            raise QQError("抽卡页没有可签到的阶梯任务")
        try:
            idx = int(data.get("CurrLadderIndex") or 0)
        except (TypeError, ValueError):
            idx = 0
        if not isinstance(tasks, list) or not 0 <= idx < len(tasks):
            raise QQError("抽卡页签到阶梯已变化，请在官方页面查看")
        task = tasks[idx]
        if not isinstance(task, dict) or not task.get("ExtInfoStr"):
            raise QQError("抽卡页当前签到阶梯缺少上报凭证")
        code, _ = self._medal_call(
            "music.medalCard.MedalCardTaskSrv", "CgiTaskReport",
            {"GroupKeyType": 1, "GroupKey": str(pool_id),
             "ExtInfoStr": str(task.get("ExtInfoStr") or ""),
             "CustomExt": {}})
        if code != 0:
            raise QQError(f"抽卡页签到上报被拒绝（code={code}）")
        code, d = self._medal_call("music.medalCard.MedalCardTaskSrv",
                                   "GetDailySignInTask", {"PoolID": str(pool_id)})
        if code != 0 or int((d or {}).get("HasSignIn") or 0) != 1:
            raise QQError("抽卡页签到上报后状态仍未生效")
        return {"ok": True, "signed": True, "already": False, "msg": "签到完成"}

    def flip_card_challenge(self, issue: str, biz: str = "medalcard",
                            max_games: int = 3) -> dict:
        """游戏必须实际游玩；不编造配对结果、步数或耗时换取奖励。"""
        raise QQError("翻卡挑战需要在官方页面实际完成，请打开任务入口游玩后刷新状态")

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
        code, result = _service_result(data)
        if code != 0:
            raise QQError(f"QQ 音乐签到失败（code={code}）")
        if not isinstance(result, dict):
            raise QQError("QQ 音乐签到响应缺少数据")
        ret = _number(result.get("retCode"), 0)
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
    allow_card_draw = load_music().get("card_draw_enable", False) is True
    medals = client.medals()
    for m in medals:
        try:
            m.update(client.medal_status(m["id"]))
        except QQError as exc:
            m.update({"signed": None, "error": str(exc)})
        try:
            tasks = client.task_list(m["id"])
            views = [_task_view(t, allow_card_draw=allow_card_draw) for t in tasks]
            m["task_total"] = len(views)
            m["task_done"] = sum(1 for v in views if v["done"])
            m["tasks"] = views
        except QQError as exc:
            m["task_error"] = str(exc)
        if not m.get("name"):
            m["name"] = f"勋章 {m['id']}"
    signed = sum(1 for m in medals if m.get("signed"))
    return {"ok": True, "signed": signed, "total": len(medals),
            "medals": medals, "at": int(time.time())}

def _task_done(t: dict) -> bool:
    total = _number(t.get("TotalTimes"))
    achieved = _number(t.get("AchievedTimes"))
    return _number(t.get("Status")) == 2 or (total > 0 and achieved >= total)

def _pool_id(task: dict) -> str:
    # 先解析官方入口的嵌套网页地址；直接 unquote 整条应用链接会把
    # 内层查询参数误作外层参数，也会漏掉位于内层首项的 pool_id。
    url = _task_url(task)
    value = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("pool_id", [""])[0]
    return value if re.fullmatch(r"[0-9A-Za-z-]+", value) else ""

def _task_url(task: dict) -> str:
    """只允许官方活动网页；应用协议/本地文件交给用户手动处理。"""
    typ = str(task.get("TaskType") or "").strip()
    if typ == "jump_external_app_singer":
        ext = task.get("Ext") or {}
        if isinstance(ext, dict):
            container = str(ext.get("containerID") or "")
            if re.fullmatch(r"[A-Za-z0-9_-]+", container):
                return "https://weibo.com/p/" + container
    url = str(task.get("BtnJumpURL") or "").strip().replace("&amp;", "&")
    if typ == "mini_game_singer" and url.lower() in ("", "none"):
        try:
            ext_info = json.loads(task.get("ExtInfoStr") or "{}")
        except (TypeError, ValueError):
            ext_info = {}
        if isinstance(ext_info, dict):
            url = str(ext_info.get("BtnJumpURL") or "").strip()
    for _ in range(3):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme in ("http", "https"):
            host = (parsed.hostname or "").lower()
            if parsed.username or parsed.password:
                return ""
            if any(host == d or host.endswith("." + d)
                   for d in ("qq.com", "weibo.com", "weibo.cn")):
                return urllib.parse.urlunparse(parsed._replace(scheme="https"))
            return ""
        query = urllib.parse.parse_qs(parsed.query)
        nested = query.get("url") or query.get("weburl") or []
        if not nested:
            return ""
        url = nested[0]
    return ""


def _task_capability(task: dict, *, allow_card_draw: bool = False) -> tuple[str, str]:
    typ = str(task.get("TaskType") or "").strip()
    title = str(task.get("Title") or task.get("TaskName") or "")
    if PAID_TYPE_RE.search(typ) or PAID_TITLE_RE.search(title):
        return "paid", "付费、购买或兑换类任务不自动执行"
    if _number(task.get("Status")) in (3, 4):
        return "unavailable", "服务端已隐藏或结束此任务"
    if _number(task.get("BtnState")) == 1 and not _task_done(task):
        return "unavailable", "服务端暂未开放此任务，请查看官方页面的前置条件"
    if typ == "sign_in":
        return "auto", "由每日勋章签到处理"
    if typ == "medal_praised_singer":
        return "auto", "对排行榜中尚未点赞的乐迷实际点赞"
    if typ == "medal_card_draw_card":
        if allow_card_draw and _pool_id(task):
            return "auto", "仅普通卡池使用1次已有抽卡次数；付费卡池或余额不明时跳过"
        return "manual", "需开启“允许使用已有普通抽卡次数”，每任务最多1次，不自动购买"
    if "watch_ad" in typ or typ.startswith(("mp_reward_light", "second_click_ads")):
        return "manual", "广告奖励需要 QQ 音乐客户端实际播放与 SDK 凭证"
    if "listen" in typ or typ.startswith("song_listening"):
        return "manual", "需要实际播放歌曲，平台验证收听记录"
    if "share" in typ or typ == "calendar_reminder":
        return "manual", "需要在 QQ 音乐客户端完成分享或日历操作"
    if typ == "ai_sing_singer":
        return "manual", "需要在官方客户端实际选歌并完成演唱制作，仅浏览或上报不算完成"
    if typ == "set_medal_role_ai_player_singer":
        from modules.qq_tasks.free_actions import ActionError, parse_companion_target
        try:
            parse_companion_target(task)
        except ActionError:
            return "manual", "播放器任务入口缺少有效的免费角色信息，请在官方客户端处理"
        return "auto", "仅设置免费已解锁、未绑定其他艺人的播放器，随后核对任务进度"
    if "game" in typ or "farm" in typ or typ in ("play_bounce", "play_guess"):
        from modules.qq_tasks.browser_games import supports_browser_game
        if supports_browser_game(_task_url(task)):
            if "starlight_game" in _task_url(task):
                return "auto", "在官方翻卡页面实际翻牌；不看广告，结算后核对任务进度"
            return "auto", "在官方游戏页面实际操作，不看广告或购买资源，完成后核对任务进度"
        return "manual", "需要在官方页面实际游玩，不能用虚构进度替代"
    if "skin_blind_box" in str(task.get("BtnJumpURL") or ""):
        return "manual", "盲盒页面还需实际参与活动，仅浏览不足以完成"
    if typ == "jump_external_app_singer" and _task_url(task):
        return "auto", "访问官方支持的微博网页入口后上报，再核对进度"
    if _enabled(task.get("NeedReportCondition")) and _task_url(task) and typ:
        return "auto", "按服务端声明访问任务网页后上报，再核对进度"
    if typ in REPORT_TASK_TYPES:
        return "manual", "服务端未提供可上报的网页任务条件，请在官方客户端完成"
    return "manual", "此任务尚无可验证的电脑端自动流程，可打开官方入口处理"


def _task_view(task: dict, *, verified: bool = True, allow_card_draw: bool = False) -> dict:
    typ = str(task.get("TaskType") or "").strip()
    capability, reason = _task_capability(task, allow_card_draw=allow_card_draw)
    return {
        "id": _task_identity(task),
        "name": str(task.get("Title") or task.get("TaskName") or typ or "未命名任务"),
        "type": typ,
        "finish": _number(task.get("AchievedTimes")),
        "need": _number(task.get("TotalTimes")),
        "done": _task_done(task),
        "auto": capability == "auto",
        "capability": capability,
        "reason": reason,
        "url": _task_url(task),
        "verified": verified,
    }

def _selected_task_types(selected) -> set[str] | None:
    if not isinstance(selected, (list, tuple, set)):
        return None  # 默认按当前服务端任务的能力动态选择，兼容新增免费浏览任务。
    return {str(x).strip() for x in selected if str(x or "").strip()}


def complete_medal_tasks(client: MusicClient, medal_id: str, selected=None, *,
                         allow_card_draw: bool = False) -> dict:
    """执行真实动作并以服务端回读为准；写请求不盲目重试。"""
    try:
        tasks = client.task_list(medal_id)
    except QQError:
        time.sleep(1.0)
        tasks = client.task_list(medal_id)
    before = {_task_identity(t): _task_done(t) for t in tasks}
    last_confirmed_tasks = tasks
    picked = _selected_task_types(selected)
    fail = 0
    failures, manual, paid, unavailable, attempted = {}, [], [], [], set()
    card_pools = {_pool_id(t) for t in tasks if _pool_id(t)}
    for task in tasks:
        if _task_done(task):
            continue
        typ = str(task.get("TaskType") or "").strip()
        if typ == "sign_in" or (picked is not None and typ not in picked):
            continue
        capability, reason = _task_capability(task, allow_card_draw=allow_card_draw)
        title = str(task.get("Title") or task.get("TaskName") or typ)
        if capability != "auto":
            row = f"{title}（{reason}）"
            if capability == "paid":
                paid.append(row)
            elif capability == "unavailable":
                unavailable.append(row)
            else:
                manual.append(row)
            continue
        need = max(0, _number(task.get("TotalTimes"), 1)
                   - _number(task.get("AchievedTimes")))
        if not need:
            continue
        try:
            attempted.add(_task_identity(task))
            if typ == "medal_praised_singer":
                got, _bad = client.praise_rank(medal_id, min(need, 20))
                if got < min(need, 20):
                    raise QQError(f"本次点赞 {got}/{need}，可能没有可点赞对象")
            elif typ == "set_medal_role_ai_player_singer":
                client.set_free_companion(task)
            elif typ == "medal_card_draw_card":
                client.draw_card(_pool_id(task), allow_existing_chances=allow_card_draw)
            elif "game" in typ or "farm" in typ or typ in ("play_bounce", "play_guess"):
                client.play_browser_game(task)
            else:
                # 每次拿新 TaskID / ExtInfoStr；阶梯式任务完成一次后参数可能变化。
                current = task
                for n in range(min(need, 20)):
                    client.visit_task(current)
                    ok, code = client.report_task(medal_id, current)
                    if not ok:
                        raise QQError(f"上报被拒绝（code={code}）")
                    if n + 1 < min(need, 20):
                        time.sleep(1.0)
                        refreshed = client.task_list(medal_id)
                        last_confirmed_tasks = refreshed
                        current = next((t for t in refreshed
                                        if _task_identity(t) == _task_identity(task)), None)
                        if current is None or _task_done(current):
                            break
                        # 新阶梯的能力/类型可能已变化，不能沿用上一阶梯的可执行判断。
                        if (str(current.get("TaskType") or "").strip() != typ
                                or _task_capability(current, allow_card_draw=allow_card_draw)[0] != "auto"):
                            break
                        # 没有确认进度增长时不重复提交同一条件。
                        if _number(current.get("AchievedTimes")) <= _number(task.get("AchievedTimes")):
                            break
                        task = current
        except QQError as exc:
            failures[_task_identity(task)] = f"{title}（{exc}）"
    verify_error = ""
    try:
        after = client.task_list(medal_id)
    except QQError:
        time.sleep(1.0)
        try:
            after = client.task_list(medal_id)
        except QQError as exc:
            verify_error = str(exc)
            after = last_confirmed_tasks  # 保留上次确认进度，绝不能补造 Status=2。
    views = [_task_view(t, verified=not bool(verify_error), allow_card_draw=allow_card_draw)
             for t in after]
    if not verify_error:
        for task in after:
            if _task_done(task):
                failures.pop(_task_identity(task), None)
    failed = list(failures.values())
    fail = len(failed)
    auto_done = sum(1 for t in after if _task_identity(t) in attempted
                    and not before.get(_task_identity(t), False) and _task_done(t)) if not verify_error else 0
    selected_views = [v for v in views if v["type"] != "sign_in"
                      and (picked is None or v["type"] in picked)]
    pending = [v["name"] for v in views if v["id"] in attempted and not v["done"]]
    unfinished = sum(1 for v in views if not v["done"])
    # “跳过”只统计本次没有实际尝试的未完成任务。已执行但尚未被任务列表
    # 确认的项目属于 pending，不能再重复算作跳过。
    skipped = sum(1 for v in views if not v["done"] and v["id"] not in attempted
                  and v["type"] != "sign_in")
    return {
        "total": len(views), "done": sum(1 for v in views if v["done"]),
        "auto": auto_done, "fail": fail,
        "skipped": skipped, "unfinished": unfinished,
        "unselected": sum(1 for v in views if not v["done"] and v["type"] != "sign_in"
                          and picked is not None and v["type"] not in picked),
        "remaining": [v["name"] for v in selected_views if not v["done"] and v["auto"]],
        "manual": manual, "paid": paid, "unavailable": unavailable,
        "failed": failed, "pending": pending,
        "verify_error": verify_error, "tasks": views, "card_pools": sorted(card_pools),
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
        with OPERATION_LOCK:
            self._run_locked()

    def _run_locked(self):
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
            discovered_pools, pool_errors = [], []
            if not mc.get("medal_enable", True):
                self.log("info", "乐迷勋章开关关着，跳过")
                skipped += 1
                if mc.get("card_pool_enable", True) and cookie:
                    # 歌词卡开关独立于勋章签到；这里只读发现已选歌手的卡池。
                    try:
                        discovered_pools, pool_errors = self._discover_card_pools(
                            cookie, mc.get("medal_selected_ids"))
                    except QQError as exc:
                        pool_errors.append(str(exc))
            elif not cookie:
                self.log("warn", "QQ 音乐还没登录，乐迷勋章跳过")
                skipped += 1
            else:
                medal = self._run_medals(cookie, mc.get("medal_tasks_enable", True),
                                         None if mc.get("auto_all_free", True) else mc.get("medal_task_types"),
                                         mc.get("medal_selected_ids"),
                                         allow_card_draw=mc.get("card_draw_enable", False) is True)
                if medal:
                    result["medal"] = medal
                    done += medal.get("done", 0)
                    fail += medal.get("fail", 0)
                    skipped += medal.get("skipped", 0)

            # 歌词卡页只签到；受支持的小游戏由任务入口真实操作。
            pools = sorted(set(((result.get("medal") or {}).get("card_pools") or [])
                               + discovered_pools))
            if not mc.get("card_pool_enable", True):
                if pools:
                    self.log("info", "抽卡页任务开关关着，跳过")
                skipped += 1 if pools else 0
            elif not cookie:
                self.log("warn", "QQ 音乐还没登录，抽卡页跳过")
                skipped += 1 if pools else 0
            elif pools or pool_errors:
                card = self._run_card_pools(cookie, pools, discovery_errors=pool_errors)
                result["card_pool"] = card
                done += card.get("done", 0)
                fail += card.get("fail", 0)
                skipped += card.get("skipped", 0)
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"出了点意外：{exc}")
            result["error"] = str(exc)
        finally:
            save_state({**load_state(), **result, "fetched_at": int(time.time())})
            self.log("info", f"完成 {done} · 失败 {fail} · 未处理 {skipped}")
            with self.lock:
                self.summary = result
                self.running = False
                self.done = True

    def _run_medals(self, cookie: str, tasks_enable: bool = True, selected=None,
                    medal_ids=None, *, allow_card_draw: bool = False) -> dict:
        try:
            client = MusicClient(cookie)
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
                    elif st.get("signed"):
                        self.log("success", f"{name}：{st['msg']}")
                        done += 1
                    else:
                        self.log("warn", f"{name}：{st['msg']}")
                        skipped += 1
                    if i < len(medals) - 1:
                        time.sleep(random.uniform(1.2, 2.4))
            except QQError as exc:
                self.log("warn", f"{name}：{exc}")
                m["msg"] = str(exc)
                m["error"] = str(exc)
                fail += 1

            if tasks_enable:
                try:
                    tr = complete_medal_tasks(client, mid, selected,
                                              allow_card_draw=allow_card_draw)
                    m.update({"task_total": tr["total"], "task_done": tr["done"],
                              "task_auto": tr["auto"], "task_skipped": tr["skipped"],
                              "tasks": tr["tasks"], "task_verified": not bool(tr["verify_error"]),
                              "task_pending": tr["pending"], "task_error": tr["verify_error"]})
                    done += tr["auto"]
                    fail += tr["fail"]
                    skipped += tr["skipped"]
                    card_pools.update(tr.get("card_pools") or [])
                    tail = f"，本次自动完成 {tr['auto']} 项" if tr["auto"] else ""
                    rest = (f"（未完成 {tr['unfinished']} 项，其中本次未处理 {tr['skipped']} 项）"
                            if tr["unfinished"] else "")
                    self.log("info", f"{name}：任务 {tr['done']}/{tr['total']}{tail}{rest}")
                    if tr["remaining"]:
                        names = "、".join(tr["remaining"][:6])
                        more = " 等" if len(tr["remaining"]) > 6 else ""
                        self.log("info", f"{name}：已选任务未完成 {names}{more}")
                    if tr.get("failed"):
                        self.log("warn", f"{name}：处理失败 {('、'.join(tr['failed'][:6]))}")
                    if tr.get("verify_error"):
                        self.log("warn", f"{name}：任务状态核对失败 {tr['verify_error']}"
                                         f"（保留上次确认进度，不记为完成，请稍后刷新）")
                    if tr.get("pending"):
                        self.log("info", f"{name}：已执行但尚未确认到账 "
                                         f"{('、'.join(tr['pending'][:6]))}")
                    if tr.get("manual"):
                        names = "、".join(tr["manual"][:6])
                        more = " 等" if len(tr["manual"]) > 6 else ""
                        self.log("info", f"{name}：需要手动完成 {names}{more}")
                    if tr.get("paid"):
                        names = "、".join(tr["paid"][:6])
                        more = " 等" if len(tr["paid"]) > 6 else ""
                        self.log("info", f"{name}：付费任务已跳过 {names}{more}")
                    if tr.get("unavailable"):
                        names = "、".join(tr["unavailable"][:6])
                        more = " 等" if len(tr["unavailable"]) > 6 else ""
                        self.log("info", f"{name}：任务暂未开放 {names}{more}")
                    if tr["unselected"]:
                        self.log("info", f"{name}：未勾选 {tr['unselected']} 项")
                except QQError as exc:
                    self.log("warn", f"{name}：任务处理失败 {exc}")
                    fail += 1

            elif load_music().get("card_pool_enable", True):
                # 抽卡页签到开关独立于勋章任务开关，仍只读发现对应卡池。
                try:
                    card_pools.update(_pool_id(t) for t in client.task_list(mid) if _pool_id(t))
                except QQError as exc:
                    self.log("warn", f"{name}：卡池入口读取失败 {exc}")

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

    def _discover_card_pools(self, cookie: str, medal_ids=None) -> tuple[list[str], list[str]]:
        """勋章签到关闭时，只读发现所选歌手卡池，不发签到或任务写请求。"""
        client = MusicClient(cookie)
        medals = client.medals()
        wanted = ({str(x) for x in medal_ids} if isinstance(medal_ids, (list, tuple, set))
                  else None)
        pools, errors = set(), []
        for medal in medals:
            mid = str(medal.get("id") or "")
            if not mid or (wanted is not None and mid not in wanted):
                continue
            try:
                pools.update(_pool_id(t) for t in client.task_list(mid) if _pool_id(t))
            except QQError:
                errors.append(f"{medal.get('name') or '所选歌手'}：歌词卡入口读取失败")
        return sorted(pools), errors

    def _run_card_pools(self, cookie: str, pools: list[str], *, discovery_errors=None) -> dict:
        """歌词卡卡池只做每日签到；游戏入口交由用户实际游玩。"""
        client = MusicClient(cookie)
        errors = list(discovery_errors or [])
        result = {"pools": [], "done": 0, "fail": len(errors), "skipped": 0,
                  "discovery_errors": errors}
        for error in errors:
            self.log("warn", error)
        for pool in pools:
            row = {"pool": pool, "signin": None, "flip": None}
            try:
                row["signin"] = client.card_pool_signin(pool)
                if row["signin"].get("already"):
                    self.log("info", f"抽卡页 {pool}：今天已签到")
                    result["skipped"] += 1
                else:
                    self.log("success", f"抽卡页 {pool}："
                                        f"{row['signin'].get('msg') or '签到完成'}")
                    result["done"] += 1
                time.sleep(random.uniform(1.0, 2.0))
            except QQError as exc:
                row["signin"] = {"ok": False, "error": str(exc)}
                self.log("warn", f"抽卡页 {pool}：签到失败 {exc}")
                result["fail"] += 1
            row["flip"] = {"ok": False, "manual": True,
                           "reason": "需在官方页面实际完成翻卡挑战后刷新状态"}
            self.log("info", f"抽卡页 {pool}：翻卡挑战需实际游玩，已保留手动入口")
            result["pools"].append(row)
        result["at"] = int(time.time())
        return result

TASK = Task()

# ---------------------------------------------------------------- 快捷登录

def _verify_music(cookie: str) -> tuple[bool, str]:
    ck = parse_cookie(cookie)
    if not music_ready(cookie):
        return False, ""
    try:
        MusicClient(cookie).medals()  # 只读验证；字段齐全不代表登录态尚未过期。
    except QQError:
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
