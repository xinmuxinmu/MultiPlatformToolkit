#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微博超话 / 吾爱破解 / 百度贴吧  一键签到

由原「自动签到工具」的单文件版拆出：业务逻辑原样保留，
HTTP 层改由统一应用的 FastAPI 路由承担（见 routes.py）。
"""
from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import json
import os
import random
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app.paths import SIGNIN_HOME

VERSION = "1.0.0"
APP_ID = "signin-tool"
STARTED_AT = time.time()

BASE_DIR = SIGNIN_HOME
CONFIG_PATH = BASE_DIR / "config.json"

UA_PC = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 用户任务中心：m.weibo.cn 独立会话，且服务端按 UA 判断是否渲染 App 版页面，
# UA 必须是真实微博 App webview 格式（Weibo (设备__weibo__版本...)）。
MTASK_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_2_1 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
            "Weibo (iPhone13,3__weibo__16.10.3__iphone__os18.2.1)")
MTASK_URL = ("https://m.weibo.cn/c/checkin?featurecode=from_sharingpage_to_mtask"
             "&luicode=20000301&lfid=task_mine2_signend")
MTASK_PLAIN_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_2_1 like Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 "
                  "Mobile/15E148 Safari/604.1")

# 这些任务只认微博 App 里的原生动作；网页端即使做了点赞/评论/转发也不会计入。
MTASK_APP_ONLY = {
    "shortplay", "liveshow", "daka", "daily_answer", "minigame", "widget",
    "watch_ads", "push", "mobile", "birthday", "fishery",
    "like", "act_like", "act_like_hot", "repost", "comment", "share",
}

# ---------------------------------------------------------------- 基础工具

class SignError(Exception):
    pass

def _decode(raw: bytes, charset: str | None = None) -> str:
    for enc in (charset, "utf-8", "gb18030"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "ignore")

def request(url, method="GET", headers=None, data=None, timeout=20):
    """极简 HTTP 客户端，返回 (status, headers, text)。自动处理 gzip 与 4xx/5xx。"""
    hdrs = {
        "User-Agent": UA_PC,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if headers:
        hdrs.update({k: v for k, v in headers.items() if v})

    body = None
    if data is not None:
        if isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode("utf-8")
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = data

    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc:
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            return resp.status, dict(resp.headers), _decode(raw, resp.headers.get_content_charset())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        enc = (exc.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        return exc.code, dict(exc.headers), _decode(raw, exc.headers.get_content_charset())

def parse_cookie(text: str) -> dict:
    """把浏览器里复制的 Cookie 字符串拆成 dict。"""
    out = {}
    for part in re.split(r"[;\r\n]", text or ""):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = key.strip()
        if key:
            out[key] = val.strip()
    return out

def norm_cookie(text: str) -> str:
    """规范化 Cookie 头（去掉多余空格、换行、'Cookie:' 前缀）。"""
    text = (text or "").strip()
    text = re.sub(r"^(?i:cookie)\s*:\s*", "", text)
    text = text.replace("\r", ";").replace("\n", ";")
    return "; ".join(f"{k}={v}" for k, v in parse_cookie(text).items())

def strip_tags(html: str) -> str:
    txt = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    txt = re.sub(r"<style[\s\S]*?</style>", " ", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = txt.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    txt = txt.replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", txt).strip()

def sleep_between(a=0.6, b=1.1):
    time.sleep(random.uniform(a, b))

# 转发/评论/点赞要的 CSRF 令牌不在 Cookie 里时，一次性把话说清楚，
# 别让用户对着满屏 "missing csrf token" 猜半天。
CSRF_HINT = ("Cookie 里缺少 XSRF-TOKEN，微博的转发/评论/点赞会全部被拒。"
             "请在浏览器里登录 weibo.com，F12 → 应用程序/Application → Cookie → "
             "https://weibo.com，确认有 XSRF-TOKEN 这一项后，连它一起重新复制 Cookie；"
             "如果列表里根本没有 XSRF-TOKEN，退出登录再登一次就会生成")

def current_weibo_user(cookie: str):
    """从微博首页的 $CONFIG 里读当前登录账号，返回 (uid, 昵称)。

    「你的 UID」这种配置填错过的坑：铁粉打卡、点赞时排除自己的帖子都要用它。
    首页 $CONFIG.user 是服务端直出的当前账号，比配置文件可靠；
    游客/未登录时没有 user 字段，返回 (None, None)。
    """
    try:
        _, _, home = request("https://weibo.com/", headers={
            "Cookie": cookie,
            "Referer": "https://weibo.com/",
            "Accept": "text/html,application/xhtml+xml,*/*",
        })
    except Exception:
        return None, None
    m = re.search(
        r'\$CONFIG\s*=\s*\{.{0,20000}?"user"\s*:\s*\{"id"\s*:\s*(\d+)'
        r'.{0,500}?"screen_name"\s*:\s*"([^"]*)"', home, re.S)
    return (m.group(1), m.group(2)) if m else (None, None)

# ---------------------------------------------------------------- 微博超话

class WeiboSenior:
    key = "weibo"
    name = "微博超话"

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log

    def run(self):
        cookie = norm_cookie(self.cfg.get("cookie", ""))
        if not cookie:
            raise SignError("还没填 Cookie，先去浏览器复制一份")
        ck = parse_cookie(cookie)
        if "SUB" not in ck:
            self.log("warn", "Cookie 里没有 SUB 字段，多半没登录，可能签不了")
        if "XSRF-TOKEN" not in ck and any(
                self.cfg.get(k) for k in ("repost_enable", "comment_enable", "like_enable")):
            self.log("warn", CSRF_HINT)

        # 「你的 UID」填错的坑：铁粉打卡的页面要用它。
        # 首页能拿到当前登录账号，发现配置对不上就顺手校正（写回 config）。
        my_uid, my_name = current_weibo_user(cookie)
        if my_uid and str(self.cfg.get("fans_fans_uid") or "").strip() != my_uid:
            self.log("info",
                     f"当前登录账号是 @{my_name or my_uid}，已按它校正「你的 UID」")
            self.cfg["fans_fans_uid"] = my_uid
            try:
                full = load_config()
                full["weibo"]["fans_fans_uid"] = my_uid
                save_config(full)
            except Exception:
                pass

        result = {"total": 0, "ok": 0, "already": 0, "fail": 0, "items": []}
        errors = []

        # 步骤顺序：轻的、接口类的先走，铁粉打卡会弹浏览器、最慢，固定放最后。
        steps = [
            ("sign", "超话签到", self.cfg.get("cookie") is not None,
             lambda: self._sign_topics(cookie, result)),
            ("mtask", "用户任务中心",
             bool(self.cfg.get("mtask_enable")) and bool(self.cfg.get("mtask_cookie")),
             lambda: self._do_user_task(result)),
            ("repost", "自动转发", bool(self.cfg.get("repost_enable")),
             lambda: self._do_repost(cookie, result)),
            ("comment", "自动评论", bool(self.cfg.get("comment_enable")),
             lambda: self._do_comment(cookie, result)),
            ("like", "博主点赞", bool(self.cfg.get("like_enable", True)),
             lambda: self._do_blogger_like(cookie, result)),
            ("dm", "私聊问候", bool(self.cfg.get("dm_enable")),
             lambda: self._do_dm_message(cookie, result)),
            ("group", "粉丝群消息", bool(self.cfg.get("group_enable")),
             lambda: self._do_group_message(cookie, result)),
            ("fans", "铁粉打卡", bool(self.cfg.get("fans_enable")),
             lambda: self._do_fans_task(cookie, result)),
        ]

        for key, label, enabled, fn in steps:
            if not enabled:
                continue
            # 今天已经成功跑过这一步就直接跳过；失败的不会记录，下次还能重试
            if daily_done(key):
                self.log("info", f"{label}：今天已经完成过，跳过")
                result["already"] += 1
                continue
            try:
                if fn():
                    mark_daily(key)
            except SignError as exc:
                self.log("error", f"{label}：{exc}")
                errors.append(str(exc))
            except Exception as exc:  # 单步异常不拖垮整轮
                self.log("error", f"{label}：{exc}")
                errors.append(str(exc))

        if errors and not result["ok"] and not result["already"]:
            result["error"] = "；".join(errors)
        return result

    def _sign_topics(self, cookie, result):
        self.log("head", "── 超话签到 ──")
        self.log("info", "正在拉取关注的超话列表…")
        topics = self._fetch_topics(cookie)
        if not topics:
            self.log("warn", "没读到任何超话，可能你还没关注超话，或 Cookie 已过期")
            return False

        self.log("info", f"共 {len(topics)} 个超话，开始签到")
        fail_cnt = 0
        for idx, t in enumerate(topics, 1):
            oid = str(t.get("oid") or "")
            tname = str(t.get("topic_name") or oid or "未知超话")
            # 签到接口要的是 oid 去掉前 5 位：
            #   老格式 100808xxxxxx      -> 8xxxxxx
            #   新格式 1022:100808xxxxxx -> 100808xxxxxx
            # 两种都靠 slice(5) 对齐，别按前缀判断（新格式不以 10080 开头）
            cid = oid[5:] if len(oid) > 5 else oid
            state, msg = self._sign_one(cookie, oid, cid)
            if state == "ok":
                result["ok"] += 1
                self.log("success", f"[{idx}/{len(topics)}] {tname} · {msg}")
            elif state == "already":
                result["already"] += 1
                self.log("info", f"[{idx}/{len(topics)}] {tname} · 今天已经签过了")
            else:
                result["fail"] += 1
                fail_cnt += 1
                self.log("error", f"[{idx}/{len(topics)}] {tname} · {msg}")
            result["items"].append({"name": tname, "state": state, "msg": msg})
            if idx < len(topics):
                sleep_between(0.8, 1.4)
        result["total"] = len(topics)
        # 全部成功/已签才算完成；有失败的留到下次重试
        return fail_cnt == 0

    def _actions(self, cookie):
        """拿互动客户端。缺 CSRF 令牌就别硬试了，服务端会一条条全拒。"""
        actions = WeiboActions(cookie, self.log)
        if actions.missing_csrf:
            raise SignError("Cookie 缺少 XSRF-TOKEN，这步做不了（原因见上方提示）")
        return actions

    # ---- 用户任务中心（m.weibo.cn 独立会话） ----

    def _mtask_headers(self, cookie_str):
        return {
            "Cookie": cookie_str,
            "User-Agent": MTASK_UA,
            "Referer": MTASK_URL,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        }

    def _mtask_json(self, url, cookie_str):
        status, _, text = request(url, headers=self._mtask_headers(cookie_str), timeout=30)
        if status != 200:
            raise SignError(f"任务中心接口异常（HTTP {status}）")
        try:
            d = json.loads(text)
        except ValueError:
            raise SignError("任务中心接口返回的不是 JSON，手机版 Cookie 可能已过期")
        if d.get("ok") != 1:
            raise SignError(f"任务中心接口：{d.get('msg') or '未知错误'}")
        return d.get("data") or {}

    def _do_user_task(self, result):
        self.log("head", "── 用户任务中心 ──")
        mtask_cookie = norm_cookie(self.cfg.get("mtask_cookie", ""))
        if not mtask_cookie:
            raise SignError("还没填手机版 Cookie（m.weibo.cn），这步做不了")
        ck = parse_cookie(mtask_cookie)
        if "SUB" not in ck:
            raise SignError("手机版 Cookie 里没有 SUB，多半没登录")
        # 渠道号必须伪装成 App webview（10GA393010），否则服务端只渲染
        # 「去 App 开宝箱」引导页，看不到任务中心。
        ck["WEIBOCN_FROM"] = "10GA393010"
        cookie_str = "; ".join(f"{k}={v}" for k, v in ck.items())

        # 1. 打开任务中心页：服务端在页面加载时完成「今日签到」
        self.log("info", "正在打开任务中心（加载页面即完成签到）…")
        status, _, text = request(MTASK_URL, headers={
            "Cookie": cookie_str, "User-Agent": MTASK_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }, timeout=30)
        if status != 200:
            raise SignError(f"任务中心页面打不开（HTTP {status}）")
        m = re.search(r"st\s*:\s*['\"]([^'\"]+)", text)
        st = m.group(1) if m else ""
        if not st:
            raise SignError("任务中心页面里没找到会话参数，手机版 Cookie 可能已过期")

        # 2. 查签到状态
        params = urllib.parse.urlencode({
            "v3revision": "1", "new_version": "202502",
            "ua": "iPhone13%2C3__weibo__16.10.3__iphone__os18.2.1",
            "from": "10GA393010", "hash": "sign",
            "luicode": "20000301", "lfid": "task_mine2_signend", "st": st,
        })
        data = self._mtask_json(
            f"https://m.weibo.cn/c/checkin/ug/v2/signin/signin?{params}", cookie_str)
        si = data.get("sign_in") or {}
        btn_show = si.get("show")
        campaign = si.get("signin_202512") or {}
        signed_days = campaign.get("signedDays") or si.get("tips") or ""
        camp_list = campaign.get("list") or []
        today = camp_list[0] if camp_list else {}
        is_signed = bool(today.get("is_signed")) or btn_show == 0

        if not is_signed:
            # 页面加载理论上已触发签到；没确认到就再刷一次页面复查
            sleep_between(1.2, 1.8)
            request(MTASK_URL, headers={
                "Cookie": cookie_str, "User-Agent": MTASK_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }, timeout=30)
            data = self._mtask_json(
                f"https://m.weibo.cn/c/checkin/ug/v2/signin/signin?{params}", cookie_str)
            si = data.get("sign_in") or {}
            campaign = si.get("signin_202512") or {}
            signed_days = campaign.get("signedDays") or si.get("tips") or ""
            camp_list = campaign.get("list") or []
            today = camp_list[0] if camp_list else {}
            is_signed = bool(today.get("is_signed")) or si.get("show") == 0

        if is_signed:
            result["ok"] += 1
            reward = today.get("reward") or ""
            self.log("success",
                     f"今日签到完成 · {signed_days}" + (f" · 今日奖励 {reward}" if reward else ""))
        else:
            result["fail"] += 1
            self.log("warn", "今天似乎还没签上，明天跑的时候再确认（页面加载会自动签到）")

        # 3. 拉任务列表
        try:
            tasks = self._mtask_tasks_data(st, cookie_str)
        except SignError as exc:
            self.log("warn", f"任务列表拉取失败：{exc}")
            return is_signed

        # 4. 关注任务（可选）：H5 关注推荐列表里的博主会真实计入任务进度
        if self.cfg.get("mtask_follow_enable"):
            follow = self._mtask_find(tasks, "follow")
            if follow and self._mtask_incomplete(follow):
                if self._do_follow_task(cookie_str, tasks):
                    sleep_between(1.0, 1.5)
                    try:
                        tasks = self._mtask_tasks_data(st, cookie_str)
                    except SignError:
                        pass

        # 5. 看热搜：H5 页面会带任务 token 上报，点一条热词即可完成
        if self.cfg.get("mtask_hot_search_enable", True):
            hot = self._mtask_find(tasks, "hot_search")
            if hot and self._mtask_incomplete(hot):
                if self._do_hot_search_task(cookie_str, tasks):
                    sleep_between(1.5, 2.0)
                    try:
                        tasks = self._mtask_tasks_data(st, cookie_str)
                    except SignError:
                        pass

        # 6. 领取已完成（status=2）的任务奖励
        self._mtask_claim(tasks, cookie_str)

        # 7. 汇总剩余任务，明确告知哪些只能在 App 内做
        try:
            tasks = self._mtask_tasks_data(st, cookie_str)
        except SignError:
            pass
        remaining = self._mtask_remaining(tasks)
        if remaining:
            shown = "、".join(remaining[:12]) + ("…" if len(remaining) > 12 else "")
            self.log("info", f"剩余 {len(remaining)} 个任务未完成：{shown}")
            self.log("info", "标（App）的只能在微博 App 内完成；其余可检查对应开关或稍后重试。")
        else:
            self.log("success", "今天的任务中心任务都已完成")
        return is_signed

    def _do_follow_task(self, cookie_str, tasks):
        """关注推荐列表里的博主，完成「关注」任务。

        验证结论：m.weibo.cn 的 H5 关注接口会真实计入任务进度；
        但内容接口要用普通手机 UA（App 格式 UA 会被服务端判「冒充 App」返回 -100）。
        """
        need = 0
        for group, items in (tasks.get("actions") or {}).items():
            for it in items or []:
                if it.get("ename") == "follow" and it.get("status") in (0, "0", 1, "1"):
                    need = int(it.get("num") or 0) - int(it.get("num_done") or 0)
        if need <= 0:
            return True
        ua_m = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_2_1 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1")
        m_hdr = {"Cookie": cookie_str, "User-Agent": ua_m, "Referer": "https://m.weibo.cn/",
                 "X-Requested-With": "XMLHttpRequest",
                 "Accept": "application/json, text/plain, */*"}
        status, _, text = request("https://m.weibo.cn/api/config", headers=m_hdr, timeout=30)
        st = ""
        try:
            st = (json.loads(text).get("data") or {}).get("st") or ""
        except ValueError:
            pass
        if not st:
            self.log("warn", "拿不到手机版会话参数，关注任务跳过")
            return False
        self.log("info", f"关注任务还需关注 {need} 人，正在关注推荐博主…")
        followed = 0
        for _ in range(need):
            status, _, text = request(
                "https://m.weibo.cn/api/container/getIndex"
                "?containerid=231093_-_selfrecomm", headers=m_hdr, timeout=30)
            uid = None
            try:
                d = json.loads(text)
                for c in ((d.get("data") or {}).get("cards") or []):
                    for g in (c.get("card_group") or []):
                        u = g.get("user") or {}
                        if u.get("id"):
                            uid = str(u["id"])
                            break
                    if uid:
                        break
            except ValueError:
                pass
            if not uid:
                self.log("warn", "推荐关注列表拿不到人，关注任务跳过")
                break
            status, _, text = request("https://m.weibo.cn/api/friendships/create",
                                      method="POST", headers=m_hdr,
                                      data=urllib.parse.urlencode({"uid": uid, "st": st}),
                                      timeout=30)
            try:
                ok = json.loads(text).get("ok")
            except ValueError:
                ok = None
            if ok == 1:
                followed += 1
                self.log("success", f"已关注推荐博主（UID {uid}）")
                sleep_between(1.2, 2.0)
            else:
                self.log("warn", f"关注失败：{text[:80]}")
                break
        return followed > 0

    def _mtask_tasks_data(self, st, cookie_str):
        return self._mtask_json(
            f"https://m.weibo.cn/c/checkin/getTask?v3revision=1&st={st}", cookie_str)

    @staticmethod
    def _mtask_flat(tasks):
        for group, items in (tasks.get("actions") or {}).items():
            for it in items or []:
                yield group, it

    @classmethod
    def _mtask_find(cls, tasks, ename):
        for _, it in cls._mtask_flat(tasks):
            if it.get("ename") == ename:
                return it
        return None

    @staticmethod
    def _mtask_incomplete(it):
        try:
            status = int(it.get("status") or 0)
            done = int(it.get("num_done") or 0)
            total = int(it.get("num") or 1)
        except (TypeError, ValueError):
            return True
        return status not in (2, 3) and done < total

    def _mtask_plain_headers(self, cookie_str):
        return {
            "Cookie": cookie_str,
            "User-Agent": MTASK_PLAIN_UA,
            "Referer": "https://m.weibo.cn/",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        }

    def _mtask_hot_words(self, containerid, extparam, cookie_str):
        url = ("https://m.weibo.cn/api/container/getIndex?containerid="
               + urllib.parse.quote(containerid, safe=""))
        if extparam:
            url += "&extparam=" + urllib.parse.quote(extparam, safe="")
        try:
            status, _, text = request(
                url, headers=self._mtask_plain_headers(cookie_str), timeout=30)
            data = json.loads(text)
        except Exception:
            return []
        words = []
        skip = ("更多", "实时热点，每分钟更新一次", "微博热搜")
        for card in (data.get("data") or {}).get("cards") or []:
            for group in card.get("card_group") or []:
                word = str(group.get("title_sub") or group.get("desc") or "").strip()
                if not word or len(word) > 40 or word in words:
                    continue
                if any(x in word for x in skip):
                    continue
                words.append(word)
        return words[:30]

    def _do_hot_search_task(self, cookie_str, tasks):
        hot = self._mtask_find(tasks, "hot_search")
        if not hot:
            return False
        scheme = str(hot.get("scheme") or "")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(scheme).query)
        containerid = (qs.get("containerid") or [""])[0]
        extparam = (qs.get("extparam") or [""])[0]
        if not containerid:
            self.log("warn", "热搜任务缺少目标参数，跳过")
            return False

        self.log("info", "正在打开热搜页完成「看热搜」任务…")
        words = self._mtask_hot_words(containerid, extparam, cookie_str)
        url = ("https://m.weibo.cn/p/index?containerid="
               + urllib.parse.quote(containerid, safe=""))
        # 任务 token 藏在 extparam 里，H5 页要原样带上才会把点击计入任务。
        if extparam:
            url += "&extparam=" + urllib.parse.quote(extparam, safe="")

        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            self.log("warn", f"Playwright 不可用，热搜任务跳过：{exc}")
            return False

        clicked = ""
        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.launch(channel="msedge", headless=True)
                except Exception:
                    browser = p.chromium.launch(headless=True)
                try:
                    ctx = browser.new_context(
                        viewport={"width": 420, "height": 900},
                        user_agent=MTASK_PLAIN_UA)
                    ctx.add_cookies([
                        {"name": k, "value": v, "domain": ".weibo.cn", "path": "/"}
                        for k, v in parse_cookie(cookie_str).items()
                    ])
                    page = ctx.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(5000)
                    for word in words:
                        try:
                            loc = page.get_by_text(word, exact=True).first
                            if loc.count():
                                loc.click(timeout=3000)
                                clicked = word
                                break
                        except Exception:
                            continue
                    if not clicked:
                        clicked = page.evaluate("""
                            () => {
                              const a = Array.from(document.querySelectorAll('a[href*="q="]'))
                                .find(x => (x.innerText || '').trim().length >= 2);
                              if (!a) return '';
                              const t = (a.innerText || '').trim().slice(0, 40);
                              a.click();
                              return t;
                            }
                        """)
                    page.wait_for_timeout(6000)
                finally:
                    browser.close()
        except Exception as exc:
            self.log("warn", f"热搜任务没做成：{exc}")
            return False

        if clicked:
            self.log("success", f"已点击热搜「{clicked}」")
            return True
        self.log("warn", "热搜页没找到可点击的词条")
        return False

    def _mtask_claim(self, tasks, cookie_str):
        claimed = 0
        seen = set()
        for _, it in self._mtask_flat(tasks):
            if str(it.get("status")) != "2":
                continue
            tid = str(it.get("id") or "")
            if not tid or tid in seen:
                continue
            if "已领取" in str(it.get("button_name") or ""):
                continue
            seen.add(tid)
            tname = it.get("name") or it.get("ename") or "未知任务"
            self.log("info", f"任务「{tname}」已完成，正在领奖励…")
            try:
                d = self._mtask_json(
                    "https://m.weibo.cn/c/checkin/ug/score/claim"
                    f"?action_id={tid}&is_android=0", cookie_str)
            except SignError as exc:
                self.log("warn", f"任务「{tname}」领取失败：{exc}")
                continue
            inner = d.get("data") or {}
            msg = inner.get("msg") or d.get("msg") or "领取成功"
            self.log("success", f"任务「{tname}」：{msg}")
            claimed += 1
            sleep_between(0.8, 1.2)
        if claimed:
            self.log("info", f"共领取 {claimed} 个任务的奖励")
        return claimed

    def _mtask_remaining(self, tasks):
        out = []
        for group, it in self._mtask_flat(tasks):
            if group == "external" or not self._mtask_incomplete(it):
                continue
            ename = str(it.get("ename") or "")
            name = str(it.get("name") or ename or "未知任务")
            if ename == "follow" and not self.cfg.get("mtask_follow_enable"):
                name += "（自动关注未开启）"
            elif ename == "hot_search" and not self.cfg.get("mtask_hot_search_enable", True):
                name += "（自动热搜未开启）"
            elif ename in MTASK_APP_ONLY or any(
                    x in name for x in ("答题", "短剧", "直播", "打卡", "小组件",
                                        "渔场", "广告", "通知", "绑定手机", "生日",
                                        "点赞", "转发", "评论", "分享")):
                name += "（App）"
            out.append(name)
        return out

    def _do_repost(self, cookie, result):
        count = max(1, int(self.cfg.get("repost_count") or 2))
        author = str(self.cfg.get("repost_author") or "").strip()
        uid = str(self.cfg.get("repost_uid") or "").strip()
        topic = str(self.cfg.get("repost_chaohua") or "").strip()
        texts = [x.strip() for x in str(self.cfg.get("repost_texts") or "").splitlines() if x.strip()]
        manual = [x.strip() for x in str(self.cfg.get("repost_links") or "").splitlines() if x.strip()]
        if not texts:
            texts = ["转发微博"]

        self.log("head", "── 自动转发 ──")
        actions = self._actions(cookie)

        targets = []
        if manual:
            self.log("info", f"用你填的 {len(manual)} 条微博链接")
            for link in manual:
                try:
                    targets.append(actions.resolve(link))
                except SignError as exc:
                    self.log("warn", f"跳过这条（{exc}）：{link[:40]}")
            targets = targets[:count]
        else:
            if not uid and not author:
                raise SignError("博主昵称和 UID 都没填，不知道转发谁")
            if not uid:
                self.log("info", f"正在搜索「{author}」的微博账号…")
                uid, real_name = actions.resolve_uid(author)
                self.log("info", f"找到 @{real_name}（UID {uid}）")
            else:
                real_name = author or uid

            self.log("info", f"正在读取 @{real_name} 的原创微博…")
            posts = actions.fetch_user_posts(uid, pages=2)
            if not posts:
                raise SignError("没读到微博，可能设置了仅粉丝可见，或账号受限")

            history = load_reposted()
            fresh = [p for p in posts if p["mblogid"] and p["mblogid"] not in history]
            skipped = len(posts) - len(fresh)
            if skipped:
                self.log("info", f"跳过 {skipped} 条之前转发过的")
            if not fresh:
                self.log("info", f"最近 {len(posts)} 条都转过了，等 TA 发新微博再来吧")
                return True

            if topic:
                hit = [p for p in fresh if topic in p["text"]]
                if hit:
                    fresh = hit
                    self.log("info", f"只挑带「{topic}」话题的，命中 {len(hit)} 条")
                else:
                    self.log("warn", f"最近的微博没有带「{topic}」的，改按最新原创来")

            # 带超话标签的排前面
            fresh.sort(key=lambda p: 0 if "超话" in p["text"] else 1)
            targets = fresh[:count]
            self.log("info", f"选中 {len(targets)} 条准备转发")

        if not targets:
            raise SignError("没有可转发的目标")

        done = 0
        history = load_reposted()
        for i, t in enumerate(targets, 1):
            text = random.choice(texts)
            mid = t.get("mid")
            if not mid and t.get("mblogid"):
                try:
                    mid = actions.resolve(t["mblogid"])["mid"]
                except SignError as exc:
                    self.log("warn", f"第 {i} 条拿不到 id：{exc}")
                    result["fail"] += 1
                    continue
            try:
                actions.repost(mid, text)
                done += 1
                result["ok"] += 1
                if t.get("mblogid"):
                    history[t["mblogid"]] = int(time.time())
                preview = (t.get("text") or "")[:26] or mid
                self.log("success", f"转发 {i}/{len(targets)}：{preview} · 附言「{text}」")
            except SignError as exc:
                result["fail"] += 1
                self.log("error", f"转发 {i}/{len(targets)} 失败：{exc}")
                if any(k in str(exc) for k in ("频繁", "风控", "验证", "异常")):
                    self.log("warn", "被拦了，先停手，过几个小时再试")
                    break
            if i < len(targets):
                sleep_between(3.0, 6.0)
        save_reposted(history)
        if done:
            self.log("done", f"转发完成 {done}/{len(targets)} 条")
        return done > 0

    def _js(self, ws, code, msg_id, want_value=True):
        params = {"expression": code}
        if want_value:
            params["returnByValue"] = True
        try:
            res = ws.call("Runtime.evaluate", params, msg_id=msg_id)
        except Exception:
            return None
        if not want_value:
            return None
        return (res.get("result", {}).get("result", {}) or {}).get("value")

    def _wait_weibo_session_list(self, ws, timeout=40.0):
        """等 api.weibo.com/chat 左侧会话列表渲染出来。

        这个 SPA 冷缓存首开经常二十秒起步，固定 sleep 等不准；等过半还没
        出来就整页重载一次再等——等价于用户手动「关掉浏览器重开」的效果。
        """
        deadline = time.time() + timeout
        reloaded = False
        while time.time() < deadline:
            cnt = self._js(ws, "document.querySelectorAll('.sessionlist').length", 316) or 0
            if cnt:
                return cnt
            if not reloaded and time.time() > deadline - timeout / 2:
                reloaded = True
                self.log("info", "会话列表还没渲染出来，重载一次页面…")
                self._js(ws, "location.reload()", 315, want_value=False)
            time.sleep(1)
        return 0

    def _weibo_open_session_and_send(self, ws, finder_js, text, what):
        """在微博私信页点开目标会话、输入文字并回车发送，返回是否发出。

        窗口打不开就整页重载再试（SPA 首开偶发渲染不全，相当于手动关掉
        重开）；发送后输入框还有内容说明回车没生效，清空重试一次。
        """
        opened = False
        for attempt in range(3):
            if attempt:
                self.log("info", f"{what}窗口没打开，重载聊天页再试…")
                self._js(ws, "location.reload()", 315, want_value=False)
                time.sleep(8)
                if not self._wait_weibo_session_list(ws, 20):
                    continue
            pos = click_element(ws, finder_js)
            if pos:
                self.log("info", f"点开「{(pos.get('txt') or what)[:14]}」，等右侧加载…")
            for _ in range(8):
                time.sleep(1)
                if self._js(ws, "!!document.getElementById('webchat-textarea')", 320):
                    opened = True
                    break
            if opened:
                break

        if not opened:
            raise SignError(f"{what}窗口没打开（可能点偏了或页面加载慢），过一会儿再试")

        for send_try in range(2):
            self._js(ws, "const t=document.getElementById('webchat-textarea');"
                         "t.focus(); t.click();", 330, want_value=False)
            time.sleep(0.6)
            ws.call("Input.insertText", {"text": text}, msg_id=331)
            time.sleep(1)
            for kind in ("keyDown", "keyUp"):
                ws.call("Input.dispatchKeyEvent", {
                    "type": kind, "key": "Enter", "code": "Enter", "text": "\r",
                    "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}, msg_id=333)
            time.sleep(4)
            left = self._js(ws, "document.getElementById('webchat-textarea').value", 334)
            # 输入框清空即视为发出（有些实现不会把输入同步进 value）
            if not (left or "").strip():
                return True
            if send_try == 0:
                self.log("warn", f"{what}消息好像没发出去（输入框还有内容），清空重试…")
                self._js(ws, "const t=document.getElementById('webchat-textarea');"
                             "t.value='';"
                             "t.dispatchEvent(new Event('input', {bubbles: true}));",
                         335, want_value=False)
                time.sleep(0.5)
        self.log("warn", f"{what}消息没发出去（输入框一直有内容），过一会儿再试")
        return False

    def _do_group_message(self, cookie, result):
        """在微博私信的粉丝群里发一条消息（消息互动任务）。"""
        group = str(self.cfg.get("group_name") or "").strip() or "囧菌今天发歌了吗"
        text = str(self.cfg.get("group_text") or "").strip() or "1"

        self.log("head", "── 粉丝群消息 ──")
        self.log("info", f"正在打开私信页，目标群「{group}」…")

        with browser_session(GROUP_PROFILE, "https://api.weibo.com/chat#/chat",
                             cookies=parse_cookie(cookie), mobile=False,
                             port=9336, wait=6) as ws:
            if not self._wait_weibo_session_list(ws):
                raise SignError("聊天页没加载出会话列表（Cookie 可能过期或页面开小差），稍后再试")
            ok = self._weibo_open_session_and_send(
                ws,
                f"() => [...document.querySelectorAll('.sessionlist')]"
                f".find(e => (e.innerText || '').includes('{group}'))",
                text, what="群")

        if ok:
            self.log("success", f"已在「{group}」发出「{text}」")
            result["ok"] += 1
        else:
            result["fail"] += 1
        return ok

    def _do_dm_message(self, cookie, result):
        """每天给博主的私聊发一次问候，按早/午/晚自动选文案。"""
        target = str(self.cfg.get("dm_target") or "").strip() or "封茗囧菌"
        morning = str(self.cfg.get("dm_morning") or "馒头早安！O(∩_∩)O").strip()
        noon = str(self.cfg.get("dm_noon") or "馒头午安！O(∩_∩)O").strip()
        night = str(self.cfg.get("dm_night") or "馒头晚安！O(∩_∩)O").strip()

        period, text = greeting_for_now(morning, noon, night)
        if not text:
            self.log("warn", f"{period}的问候语是空的，跳过")
            return False

        today = time.strftime("%Y-%m-%d")
        sent = load_dm_sent()
        if sent.get("date") == today:
            self.log("info", f"今天已经发过问候了（{sent.get('period', '')}），跳过")
            result["already"] += 1
            return True

        self.log("head", "── 每日私聊问候 ──")
        self.log("info", f"现在 {time.strftime('%H:%M')}，属于{period}时段")

        with browser_session(DM_PROFILE, "https://api.weibo.com/chat#/chat",
                             cookies=parse_cookie(cookie), mobile=False,
                             port=9337, wait=6) as ws:
            if not self._wait_weibo_session_list(ws):
                raise SignError("聊天页没加载出会话列表（Cookie 可能过期或页面开小差），稍后再试")
            ok = self._weibo_open_session_and_send(
                ws,
                f"() => [...document.querySelectorAll('.sessionlist')]"
                f".find(e => (e.innerText || '').trim().split('\\n')[0].trim() === '{target}')",
                text, what="私聊")

        if ok:
            self.log("success", f"已给 @{target} 发{period}问候：{text}")
            result["ok"] += 1
            save_dm_sent({"date": today, "period": period, "text": text})
            return True
        result["fail"] += 1
        return False

    def _do_fans_task(self, cookie, result):
        """手机版铁粉页打卡。页面是服务端直出的，只能靠浏览器渲染后点「今日」格子。"""
        blogger = str(self.cfg.get("fans_blogger_uid") or "").strip()
        fans = str(self.cfg.get("fans_fans_uid") or "").strip()
        if not blogger or not fans:
            raise SignError("铁粉任务的 blogger_uid / fans_uid 没填")

        url = ("https://page.star.weibo.com/z/bigFans?showmenu=0"
               f"&blogger_uid={blogger}&fans_uid={fans}")
        self.log("head", "── 铁粉打卡 ──")
        self.log("info", "正在打开手机版铁粉页面（会短暂弹浏览器）…")

        with browser_session(FANS_PROFILE, url, cookies=parse_cookie(cookie),
                             mobile=True, wait=16) as ws:
            text = page_text(ws)
            if "铁粉" not in text:
                raise SignError("铁粉页没加载出来，Cookie 可能过期了，重新复制一份")

            m = re.search(r"今日\s*(已领|待打卡|待领取|立即打卡|打卡)", text)
            state = m.group(1) if m else ""
            self.log("info", f"今日状态：{state or '没认出来'}")

            if state in ("已领", "打卡"):
                self.log("info", "今天已经打过卡了，奖励也领过了")
                result["already"] += 1
                ok_flag = True
            else:
                pos = click_element(
                    ws, "() => [...document.querySelectorAll('div.item')]"
                        ".find(e => (e.innerText || '').includes('今日'))")
                if not pos:
                    raise SignError("页面上没找到「今日」打卡格子")
                self.log("info", f"点了「{pos.get('txt', '今日')}」，等结果…")
                time.sleep(5)
                after = page_text(ws)
                m2 = re.search(r"今日\s*(已领|待打卡|待领取|立即打卡|打卡)", after)
                st2 = m2.group(1) if m2 else ""
                if st2 in ("已领", "打卡") or "打卡成功" in after:
                    self.log("success", "打卡成功，奖励已领")
                    result["ok"] += 1
                    ok_flag = True
                else:
                    self.log("warn", f"打卡后状态是「{st2 or '未知'}」，可能没点中或有验证码")
                    result["fail"] += 1
                    ok_flag = False

            # 每日任务状态
            daily = []
            for name in ("博文互动", "消息互动"):
                idx = text.find(name)
                if idx >= 0:
                    seg = text[idx: idx + 60]
                    done = "已完成" in seg
                    daily.append(f"{name}{'✓' if done else '✗'}")
            if daily:
                self.log("info", "每日任务：" + "  ".join(daily))
            return ok_flag

    def _do_comment(self, cookie, result):
        count = max(1, int(self.cfg.get("comment_count") or 5))
        url = str(self.cfg.get("comment_url") or "").strip()
        texts = [x.strip() for x in str(self.cfg.get("comment_texts") or "").splitlines() if x.strip()]
        if not url:
            raise SignError("没填水贴链接")
        if not texts:
            raise SignError("没填评论内容，一条也没有就没法评论")

        self.log("head", "── 自动评论 ──")
        actions = self._actions(cookie)
        info = actions.resolve(url)
        self.log("info", f"目标微博：{info.get('author') or '未知作者'} · {info.get('text') or info['mid']}")

        done = 0
        for i in range(1, count + 1):
            text = texts[(i - 1) % len(texts)] if len(texts) > 1 else texts[0]
            try:
                actions.comment(info["mid"], text)
                done += 1
                result["ok"] += 1
                self.log("success", f"评论 {i}/{count}：「{text}」")
            except SignError as exc:
                result["fail"] += 1
                self.log("error", f"评论 {i}/{count} 失败：{exc}")
                if any(k in str(exc) for k in ("频繁", "风控", "验证", "异常")):
                    self.log("warn", "被拦了，先停手，过几个小时再试")
                    break
            if i < count:
                sleep_between(4.0, 8.0)
        if done:
            self.log("done", f"评论完成 {done}/{count} 条")
        return done > 0

    def _do_blogger_like(self, cookie, result):
        """点赞博主本人的原创微博（不是超话里别人的帖子）。"""
        want = str(self.cfg.get("like_chaohua") or "").strip()
        self.log("head", "── 博主点赞 ──")

        actions = self._actions(cookie)

        # 点赞目标博主：指定了就用指定的，没指定跟转发目标走
        if not want:
            want = str(self.cfg.get("repost_author") or "").strip()
            if want:
                self.log("info", f"没指定点赞博主，用转发目标博主：「{want}」")

        uid = ""
        if want:
            if want.isdigit():
                uid = want
                want = ""
            elif want == str(self.cfg.get("repost_author") or "").strip():
                # 和转发目标是同一人时，直接复用已知的 UID，省一次搜索
                uid = str(self.cfg.get("repost_uid") or "").strip()
        else:
            uid = str(self.cfg.get("fans_blogger_uid") or "").strip()

        if not uid and not want:
            raise SignError("博主昵称和 UID 都没填，不知道点赞谁")
        if not uid:
            self.log("info", f"正在搜索「{want}」的微博账号…")
            uid, real_name = actions.resolve_uid(want)
            want = real_name
            self.log("info", f"找到 @{real_name}（UID {uid}）")

        self.log("info", f"正在读取博主（UID {uid}）的原创微博…")
        posts = actions.fetch_user_posts(uid, pages=2)
        if not posts:
            raise SignError("没读到博主的微博，可能设置了仅粉丝可见，或账号受限")

        history = load_liked()
        fresh = [p for p in posts if (p.get("mblogid") or p.get("mid")) not in history]
        if not fresh:
            self.log("info", f"博主最近 {len(posts)} 条微博都点过赞了，等 TA 发新微博再来")
            result["already"] += 1
            return True

        picked = fresh[0]
        mid = picked.get("mid")
        if not mid and picked.get("mblogid"):
            mid = actions.resolve(picked["mblogid"])["mid"]
        actions.like(mid)
        history[picked.get("mblogid") or mid] = int(time.time())
        save_liked(history)
        result["ok"] += 1
        preview = (picked.get("text") or "")[:26] or mid
        self.log("success", f"已点赞博主 @{picked.get('author') or want or uid} 的微博：{preview}")
        return True

    def _fetch_topics(self, cookie):
        headers = {
            "Cookie": cookie,
            "Referer": "https://weibo.com/",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        }
        topics, page, max_page = [], 1, 1
        while page <= max_page and page <= 20:
            url = ("https://weibo.com/ajax/profile/topicContent"
                   f"?tabid=231093_-_chaohua&page={page}")
            try:
                status, _, text = request(url, headers=headers)
            except Exception as exc:
                raise SignError(f"连不上微博：{exc}") from exc

            if status in (301, 302, 403, 418):
                raise SignError(f"微博返回 HTTP {status}，Cookie 大概失效了，重新复制一份")
            try:
                data = json.loads(text)
            except Exception:
                raise SignError(f"微博返回的不是 JSON（HTTP {status}），Cookie 可能已失效")
            if str(data.get("ok")) != "1":
                msg = data.get("msg") or data.get("message") or "接口拒绝"
                raise SignError(f"读取超话列表失败：{msg}（Cookie 可能已过期）")

            block = data.get("data") or {}
            lst = block.get("list") or []
            topics.extend(lst)
            try:
                max_page = int(block.get("max_page") or 1)
            except (TypeError, ValueError):
                max_page = 1
            if not lst:
                break
            page += 1
            sleep_between(0.4, 0.8)
        return topics

    def _sign_one(self, cookie, oid, cid):
        params = {
            "ajwvr": 6,
            "api": "http://i.huati.weibo.com/aj/super/checkin",
            "texta": "签到",
            "textb": "已签到",
            "status": 0,
            "id": cid,
            "__rnd": int(time.time() * 1000),
        }
        url = "https://weibo.com/p/aj/general/button?" + urllib.parse.urlencode(params)
        headers = {
            "Cookie": cookie,
            "Referer": f"https://weibo.com/p/{oid}" if oid else "https://weibo.com/",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        }
        try:
            status, _, text = request(url, headers=headers)
        except Exception as exc:
            return "fail", f"网络异常：{exc}"

        try:
            data = json.loads(text)
        except Exception:
            return "fail", f"返回异常（HTTP {status}），可能被风控拦截"

        code = str(data.get("code", ""))
        msg = str(data.get("msg") or "").strip()
        title = ""
        if isinstance(data.get("data"), dict):
            title = str(data["data"].get("alert_title") or "").strip()

        if code == "100000":
            return "ok", (title or msg or "签到成功")
        if code in ("382004", "100001"):
            return "already", "今天已经签过了"
        if code in ("100003",):
            return "already", "今天已经签过了"
        return "fail", (msg or f"code={code or '未知'}")

class WeiboActions:
    """微博互动：转发/点赞博主的原创微博、在指定微博下评论。

    转发/评论属于"发布内容"，微博风控比签到严得多，所以：
    · 每条之间随机停顿，不并发
    · 文案从配置里随机挑，不重复刷同一句话
    · 出错立即停手，不硬刚
    """

    def __init__(self, cookie: str, log):
        self.cookie = cookie
        self.log = log
        self.ck = parse_cookie(cookie)
        self.xsrf = urllib.parse.unquote(self.ck.get("XSRF-TOKEN", ""))
        # 转发/评论/点赞这几个"发布内容"的接口，微博服务端会校验
        # X-XSRF-TOKEN 头跟 Cookie 里的 XSRF-TOKEN 是否一致，缺一个就直接拒
        self.missing_csrf = not self.xsrf

    def _headers(self, referer="https://weibo.com/"):
        h = {
            "Cookie": self.cookie,
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if self.xsrf:
            h["X-XSRF-TOKEN"] = self.xsrf
        return h

    def _post(self, path, params, referer="https://weibo.com/"):
        url = "https://weibo.com" + path
        try:
            status, _, text = request(url, method="POST",
                                       headers=self._headers(referer),
                                       data=urllib.parse.urlencode(params))
        except Exception as exc:
            raise SignError(f"网络异常：{exc}") from exc
        try:
            return json.loads(text)
        except Exception:
            raise SignError(f"接口返回异常（HTTP {status}），大概率被风控拦了") from None

    def _get(self, url, referer="https://weibo.com/"):
        try:
            status, _, text = request(url, headers=self._headers(referer))
        except Exception as exc:
            raise SignError(f"网络异常：{exc}") from exc
        try:
            return json.loads(text)
        except Exception:
            raise SignError(f"接口返回异常（HTTP {status}）") from None

    # --- 按昵称找人
    def resolve_uid(self, name: str):
        """在微博搜索里按昵称找用户，返回 (uid, 实际昵称)。精确同名优先。"""
        url = "https://s.weibo.com/user?q=" + urllib.parse.quote(name)
        try:
            status, _, html = request(url, headers={
                "Cookie": self.cookie,
                "Referer": "https://weibo.com/",
                "Accept": "text/html,application/xhtml+xml,*/*",
            })
        except Exception as exc:
            raise SignError(f"搜索失败：{exc}") from exc

        cands = []
        for block in re.split(r'<div class="card card-user-b', html)[1:]:
            m_uid = re.search(r"weibo\.com/u/(\d{8,})", block)
            m_nm = re.search(r'class="name"[^>]*>\s*([^<]{1,30})', block)
            if not (m_uid and m_nm):
                continue
            cands.append((m_uid.group(1), m_nm.group(1).strip()))
        if not cands:
            raise SignError(f"微博搜索里没找到「{name}」，换个昵称或直接填 UID")

        for uid, nm in cands:
            if nm == name:
                return uid, nm
        for uid, nm in cands:
            if name in nm:
                return uid, nm
        raise SignError(f"没找到昵称正好是「{name}」的账号，可能被限流了，直接填 UID 更稳")

    # --- 读某人主页的原创微博
    def fetch_user_posts(self, uid: str, pages: int = 2):
        out = []
        for p in range(1, pages + 1):
            url = (f"https://weibo.com/ajax/statuses/mymblog"
                   f"?uid={uid}&page={p}&feature=1")
            data = self._get(url, referer=f"https://weibo.com/u/{uid}")
            block = data.get("data") or {}
            for x in (block.get("list") or []):
                u = x.get("user") or {}
                out.append({
                    "mid": str(x.get("id") or ""),
                    "mblogid": str(x.get("mblogid") or ""),
                    "text": strip_tags(x.get("text_raw") or x.get("text") or "")[:80],
                    "author": str(u.get("screen_name") or ""),
                    "created": str(x.get("created_at") or ""),
                })
            sleep_between(0.7, 1.2)
        return [x for x in out if x["mid"]]

    # --- 拿数字 mid
    def resolve(self, link_or_id: str) -> dict:
        s = (link_or_id or "").strip()
        if not s:
            raise SignError("链接是空的")
        if re.fullmatch(r"\d{15,}", s):
            return {"mid": s, "mblogid": "", "text": "", "author": ""}

        m = re.search(r"weibo\.com/\d+/([0-9A-Za-z]+)", s) or re.search(r"\b([0-9A-Za-z]{9})\b", s)
        if not m:
            raise SignError(f"认不出这个链接：{s[:40]}")
        mblogid = m.group(1)
        data = self._get("https://weibo.com/ajax/statuses/show?id=" + mblogid)
        mid = str(data.get("id") or "")
        if not mid:
            raise SignError("拿不到这条微博的 id，可能已删除或需要登录")
        return {
            "mid": mid,
            "mblogid": str(data.get("mblogid") or mblogid),
            "text": strip_tags(data.get("text_raw") or data.get("text") or "")[:60],
            "author": str((data.get("user") or {}).get("screen_name") or ""),
        }

    # --- 动作
    def repost(self, mid: str, text: str):
        data = self._post("/ajax/statuses/repost", {
            "id": mid,
            "comment": text,
            "comment_ori": "0",
            "is_repost": "0",
            "visible": "0",
        })
        if str(data.get("ok")) != "1":
            raise SignError(str(data.get("msg") or data.get("message") or "转发被拒绝"))
        return True

    def comment(self, mid: str, text: str):
        data = self._post("/ajax/comments/create", {
            "id": mid,
            "comment": text,
            "comment_ori": "0",
            "is_repost": "0",
            "is_comment": "0",
            "visible": "0",
        })
        if str(data.get("ok")) != "1":
            raise SignError(str(data.get("msg") or data.get("message") or "评论被拒绝"))
        return True

    def like(self, mid: str):
        """点赞。setLike 是「设为赞」，重复调用不会取消赞。"""
        data = self._post("/ajax/statuses/setLike", {"id": mid})
        if str(data.get("attitude") or "") == "heart" or str(data.get("ok")) == "1":
            return True
        msg = str(data.get("msg") or data.get("message") or "")
        raise SignError(msg or "点赞被拒绝")

# ---------------------------------------------------------------- 浏览器控制
# 52pojie 的任务页被加速乐 WAF 保护，纯 HTTP 过不去（要浏览器指纹）。
# 这里用标准库实现一个最小 CDP 客户端，直接驱动本机 Chrome，不装任何依赖。

class _WS:
    """够用的 WebSocket 客户端：只处理文本帧 + ping。"""

    def __init__(self, url, timeout=15):
        rest = url.split("://", 1)[1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手失败")
            buf += chunk
        if b"101" not in buf.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket 握手被拒绝")
        self._buf = b""

    def send(self, text):
        data = text.encode("utf-8")
        n = len(data)
        mask = os.urandom(4)
        head = bytearray([0x81])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        head += mask
        self.sock.sendall(bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("连接已断开")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self):
        b1, b2 = self._read(2)
        opcode = b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read(8))[0]
        payload = self._read(length) if length else b""
        if opcode == 0x8:
            raise RuntimeError("对端关闭连接")
        if opcode == 0x9:
            self.sock.sendall(b"\x8a\x00")
            return self.recv()
        return payload.decode("utf-8", "ignore")

    def call(self, method, params=None, msg_id=1, timeout=25):
        self.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.recv())
            if msg.get("id") == msg_id:
                return msg
        raise RuntimeError(f"{method} 超时")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

BROWSER_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

POJIE_PROFILE = BASE_DIR / "data" / "chrome-profile"
FANS_PROFILE = BASE_DIR / "data" / "chrome-profile-fans"
GROUP_PROFILE = BASE_DIR / "data" / "chrome-profile-group"
DM_PROFILE = BASE_DIR / "data" / "chrome-profile-dm"
WEIBO_QR_PROFILE = BASE_DIR / "data" / "chrome-profile-qr"
WEIBO_QR_PORT = 9340
WEIBO_QR_TIMEOUT = 300  # 秒；微博二维码大概几分钟一换，等超时就让用户重点一次

def find_browser():
    for p in BROWSER_PATHS:
        if os.path.exists(p):
            return p
    return None

def _http_json(url, timeout=2):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def launch_browser(browser, profile: Path, url: str, port=9333, keep_open=True):
    """起一个独立 profile 的浏览器窗口，返回 (进程, 端口)。"""
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [browser, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
         "--no-first-run", "--no-default-browser-check", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            if _http_json(f"http://127.0.0.1:{port}/json/list"):
                break
        except Exception:
            pass
        time.sleep(0.4)
    return proc

def kill_browser(proc):
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=10)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass

def read_page_text(port=9333, wait=16.0, stop_words=None):
    """连上刚起的浏览器，等页面稳定后读正文。"""
    targets = None
    for _ in range(40):
        try:
            targets = _http_json(f"http://127.0.0.1:{port}/json/list")
            if targets:
                break
        except Exception:
            pass
        time.sleep(0.3)
    if not targets:
        raise RuntimeError("浏览器调试端口没起来")

    page = next((t for t in targets
                 if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
    if not page:
        raise RuntimeError("没找到可用的标签页")

    ws = _WS(page["webSocketDebuggerUrl"])
    try:
        ws.call("Runtime.enable", msg_id=1)
        deadline = time.time() + wait
        text, prev = "", ""
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                res = ws.call("Runtime.evaluate", {
                    "expression": "document.body ? document.body.innerText : ''",
                    "returnByValue": True,
                }, msg_id=2)
                text = (res.get("result", {}).get("result", {}) or {}).get("value") or ""
            except Exception:
                continue
            if stop_words and any(w in text for w in stop_words):
                break
            if text and text == prev and len(text) > 80:
                break
            prev = text
        return text
    finally:
        ws.close()

@contextlib.contextmanager
def browser_session(profile: Path, url: str, cookies=None, mobile=False,
                    port=9335, wait=9.0):
    """起浏览器 → 注入 Cookie → 可选移动端模拟 → 打开页面，yield 出 CDP 连接。"""
    browser = find_browser()
    if not browser:
        raise SignError("没找到 Chrome 或 Edge，装一个浏览器再试")
    proc = launch_browser(browser, profile, "about:blank", port=port)
    ws = None
    try:
        targets = None
        for _ in range(40):
            try:
                targets = _http_json(f"http://127.0.0.1:{port}/json/list")
                if targets:
                    break
            except Exception:
                pass
            time.sleep(0.3)
        if not targets:
            raise SignError("浏览器没起来，可能被安全软件拦了")
        page = next((t for t in targets
                     if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
        if not page:
            raise SignError("浏览器没有可用标签页")

        ws = _WS(page["webSocketDebuggerUrl"])
        ws.call("Page.enable", msg_id=1)
        if cookies:
            ws.call("Network.enable", msg_id=2)
            for i, (k, v) in enumerate(cookies.items()):
                try:
                    ws.call("Network.setCookie", {
                        "name": k, "value": v, "domain": ".weibo.com",
                        "path": "/", "secure": True, "httpOnly": False,
                    }, msg_id=100 + i)
                except Exception:
                    pass
        if mobile:
            ws.call("Emulation.setUserAgentOverride", {
                "userAgent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                              "Mobile/15E148 Safari/604.1"),
                "platform": "iPhone"}, msg_id=3)
            ws.call("Emulation.setDeviceMetricsOverride",
                    {"width": 400, "height": 900, "deviceScaleFactor": 2, "mobile": True},
                    msg_id=4)
        ws.call("Page.navigate", {"url": url}, msg_id=5)
        time.sleep(wait)
        yield ws
    finally:
        if ws:
            ws.close()
        kill_browser(proc)
        # 等调试端口彻底释放，避免紧接着的下一个浏览器功能撞上单例锁
        for _ in range(20):
            try:
                _http_json(f"http://127.0.0.1:{port}/json/version", timeout=1)
                time.sleep(0.4)
            except Exception:
                break

def page_text(ws) -> str:
    try:
        res = ws.call("Runtime.evaluate", {
            "expression": "document.body ? document.body.innerText : ''",
            "returnByValue": True}, msg_id=900)
        return (res.get("result", {}).get("result", {}) or {}).get("value") or ""
    except Exception:
        return ""

def click_element(ws, finder_js):
    """在页面里找到元素并模拟真实鼠标点击（Vue 绑定的监听只认真实事件）。"""
    res = ws.call("Runtime.evaluate", {
        "expression": f"""(() => {{
            const el = ({finder_js})();
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {{x: r.x + r.width / 2, y: r.y + r.height / 2,
                     txt: (el.innerText || '').trim().slice(0, 20)}};
        }})()""",
        "returnByValue": True}, msg_id=901)
    pos = (res.get("result", {}).get("result", {}) or {}).get("value")
    if not pos:
        return None
    for kind in ("mousePressed", "mouseReleased"):
        ws.call("Input.dispatchMouseEvent", {
            "type": kind, "x": pos["x"], "y": pos["y"],
            "button": "left", "clickCount": 1}, msg_id=902)
    return pos

# ---------------------------------------------------------------- 吾爱破解

class PojieSenior:
    key = "pojie"
    name = "吾爱破解"
    BASE = "https://www.52pojie.cn"

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log

    def run(self):
        mode = str(self.cfg.get("pojie_mode") or "browser")
        if mode == "http":
            return self._run_http()
        return self._run_browser()

    def _run_browser(self):
        """用真实浏览器过 WAF 再签到。会短暂弹出浏览器窗口，签完自动关。"""
        browser = find_browser()
        if not browser:
            raise SignError("没找到 Chrome 或 Edge，装一个浏览器再试")

        self.log("info", "正在启动浏览器（网站有 JS 防护，纯请求过不去）…")
        url = f"{self.BASE}/home.php?mod=task&do=apply&id=2&referer=%2Fforum.php"
        proc = launch_browser(browser, POJIE_PROFILE, url)
        try:
            self.log("info", "等页面加载完（大概 10 秒）…")
            text = read_page_text(wait=20, stop_words=("任务", "签到", "请登录", "验证码"))
        finally:
            kill_browser(proc)

        if not text.strip():
            raise SignError("浏览器没读到页面内容，可能被网站拦了，过一会儿再试")

        if "请完成安全验证" in text or "安全验证" in text:
            raise SignError("被要求人工验证码了，用普通模式打开浏览器手动签一次，过几天再试自动")
        if "请登录" in text or "登录后使用" in text:
            raise SignError("这个浏览器还没登录过 52pojie。点上面「登录 52pojie」按钮，登录完再点签到")

        state = self._classify_text(text)
        hint = self._text_hint(text)

        if state == "ok":
            self.log("success", f"签到成功{' · ' + hint if hint else ''}")
            return {"total": 1, "ok": 1, "already": 0, "fail": 0,
                    "items": [{"name": "每日签到", "state": "ok", "msg": hint or "签到成功"}]}
        if state == "already":
            self.log("info", f"今天已经签过了{' · ' + hint if hint else ''}")
            return {"total": 1, "ok": 0, "already": 1, "fail": 0,
                    "items": [{"name": "每日签到", "state": "already", "msg": hint or "今日已签"}]}
        if state == "nologin":
            raise SignError("Cookie 失效了，点「登录 52pojie」重新登录一下")

        raise SignError(f"没认出结果：{(hint or text.strip())[:120]}")

    @staticmethod
    def _classify_text(text: str) -> str:
        if "请完成安全验证" in text:
            return "blocked"
        if "请先登录" in text or "请登录" in text or "登录后使用" in text:
            return "nologin"
        if "已经签到" in text or "已申请过此任务" in text:
            return "already"
        if "任务已完成" in text or "签到成功" in text or "申请成功" in text:
            return "ok"
        return "unknown"

    @staticmethod
    def _text_hint(text: str) -> str:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if any(k in line for k in ("完成", "签到", "申请", "奖励", "积分", "已签")):
                if len(line) <= 60:
                    return line
        return ""

    def _run_http(self):
        cookie = norm_cookie(self.cfg.get("cookie", ""))
        if not cookie:
            raise SignError("还没填 Cookie，先去浏览器复制一份")
        headers = {
            "Cookie": cookie,
            "Referer": self.BASE + "/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        self.log("info", "正在打开任务页面…")
        try:
            status, _, home = request(self.BASE + "/home.php?mod=task", headers=headers)
        except Exception as exc:
            raise SignError(f"连不上 52pojie：{exc}") from exc

        if self._is_challenge(home):
            raise SignError(
                "52pojie 的任务页开了 JS 防护（加速乐 WAF），纯请求过不去。"
                "办法：浏览器打开 https://www.52pojie.cn/home.php?mod=task ，"
                "等页面正常显示后重新复制 Cookie，确认里面有 wzws_cid 这一项，再粘进来"
            )
        if status == 403 or "403 Forbidden" in home:
            raise SignError("被网站拦截了（403），换个网络或稍后再试")

        formhash = ""
        m = re.search(r'formhash=([0-9a-zA-Z]{8})', home)
        if m:
            formhash = m.group(1)

        if not formhash and ("请先登录" in home or "您需要先登录" in home):
            raise SignError("Cookie 已失效，需要重新登录后复制")

        url = f"{self.BASE}/home.php?mod=task&do=apply&id=2"
        if formhash:
            url += f"&formhash={formhash}"
        url += "&referer=%2Fforum.php"

        self.log("info", "正在提交签到…")
        try:
            status, _, text = request(url, headers=headers)
        except Exception as exc:
            raise SignError(f"提交失败：{exc}") from exc

        state = self._classify(text)
        hint = self._hint(text)

        if state == "ok":
            self.log("success", f"签到成功{' · ' + hint if hint else ''}")
            return {"total": 1, "ok": 1, "already": 0, "fail": 0,
                    "items": [{"name": "每日签到", "state": "ok", "msg": hint or "签到成功"}]}
        if state == "already":
            self.log("info", f"今天已经签过了{' · ' + hint if hint else ''}")
            return {"total": 1, "ok": 0, "already": 1, "fail": 0,
                    "items": [{"name": "每日签到", "state": "already", "msg": hint or "今日已签"}]}
        if state == "nologin":
            raise SignError("Cookie 已失效，需要重新登录后复制")
        if state == "blocked":
            raise SignError("触发了网站的风控校验，稍等几分钟再试，或换网络")

        snippet = hint or strip_tags(text)[:120]
        raise SignError(f"没识别出签到结果：{snippet or '页面为空'}")

    @staticmethod
    def _is_challenge(text: str) -> bool:
        """加速乐 WAF 的 JS 挑战页，纯请求没法过。"""
        return ("Please enable JavaScript" in text
                or "请开启JavaScript" in text
                or "wzws_sid" in text
                or "WZWS-RAY" in text)

    @staticmethod
    def _classify(text: str) -> str:
        if ("403 Forbidden" in text or "请开启JavaScript" in text
                or "Please enable JavaScript" in text):
            return "blocked"
        if "需要先登录" in text or "请先登录" in text:
            return "nologin"
        if "已经签到" in text or "已申请过此任务" in text or "已签到" in text:
            return "already"
        if "任务已完成" in text or "签到成功" in text or "已完成" in text:
            return "ok"
        return "unknown"

    @staticmethod
    def _hint(text: str) -> str:
        m = re.search(r'<div class="alert_(?:info|error|right)[^"]*"[^>]*>([\s\S]*?)</div>', text)
        if m:
            return strip_tags(m.group(1))[:120]
        return ""

# ---------------------------------------------------------------- 百度贴吧

class TiebaSenior:
    key = "tieba"
    name = "百度贴吧"
    APP_VER = "11.8.8.0"
    APP_UA = "bdtb for Android " + APP_VER

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.cookie = ""
        self.bduss = ""

    def run(self):
        self.cookie = norm_cookie(self.cfg.get("cookie", ""))
        if not self.cookie:
            raise SignError("还没填 Cookie，先去浏览器复制一份")
        ck = parse_cookie(self.cookie)
        self.bduss = ck.get("BDUSS_BFESS") or ck.get("BDUSS") or ""

        self.log("info", "正在校验登录状态…")
        tbs = self._get_tbs()
        self.log("info", f"登录有效，tbs={tbs[:8]}…" if tbs else "登录有效")

        self.log("info", "正在拉取关注的贴吧…")
        forums = self._get_forums()
        unsign = [f for f in forums if str(f.get("is_sign", "0")) != "1"]
        if not unsign:
            self.log("success", f"关注的 {len(forums)} 个贴吧今天全部签过了")
            return {"total": len(forums), "ok": 0, "already": len(forums), "fail": 0, "items": []}

        self.log("info", f"关注 {len(forums)} 个吧，待签 {len(unsign)} 个")

        ok_names, fail_names = [], []
        done_ids = set()

        if self.bduss:
            self.log("info", "使用客户端协议批量签到（经验更高、速度更快）")
            done_ids = self._batch_sign(tbs, unsign, ok_names, fail_names)
        else:
            self.log("warn", "Cookie 里没有 BDUSS，只能用网页方式逐个签（稍慢）")

        rest = [f for f in unsign if str(f.get("forum_id")) not in done_ids]
        if rest:
            if done_ids:
                self.log("info", f"剩下 {len(rest)} 个吧用网页方式补签")
            for idx, f in enumerate(rest, 1):
                name = f.get("forum_name") or f.get("forum_id")
                state, msg = self._sign_web(tbs, name)
                if state == "ok":
                    ok_names.append(name)
                    self.log("success", f"[{idx}/{len(rest)}] {name} · {msg}")
                elif state == "already":
                    self.log("info", f"[{idx}/{len(rest)}] {name} · 今天已经签过了")
                else:
                    fail_names.append(name)
                    self.log("error", f"[{idx}/{len(rest)}] {name} · {msg}")
                if idx < len(rest):
                    sleep_between(0.6, 1.1)

        items = ([{"name": n, "state": "ok", "msg": "签到成功"} for n in ok_names]
                 + [{"name": n, "state": "fail", "msg": "签到失败"} for n in fail_names])
        return {
            "total": len(unsign),
            "ok": len(ok_names),
            "already": len(forums) - len(unsign),
            "fail": len(fail_names),
            "items": items,
        }

    # --- 接口层
    def _get_tbs(self):
        try:
            status, _, text = request("https://tieba.baidu.com/dc/common/tbs",
                                      headers={"Cookie": self.cookie,
                                               "Referer": "https://tieba.baidu.com/"})
            data = json.loads(text)
        except Exception as exc:
            raise SignError(f"获取登录状态失败：{exc}") from exc
        if str(data.get("is_login")) != "1":
            raise SignError("Cookie 已失效，重新登录贴吧后复制一份")
        return str(data.get("tbs") or "")

    def _get_forums(self):
        try:
            status, _, text = request(
                "https://tieba.baidu.com/mo/q/newmoindex",
                method="POST",
                headers={"Cookie": self.cookie,
                         "Referer": "https://tieba.baidu.com/",
                         "X-Requested-With": "XMLHttpRequest",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data="")
            data = json.loads(text)
        except Exception as exc:
            raise SignError(f"获取贴吧列表失败：{exc}") from exc
        if str(data.get("no")) != "0":
            raise SignError(f"获取贴吧列表失败：{data.get('error') or data.get('no')}")
        return ((data.get("data") or {}).get("like_forum") or [])

    def _batch_sign(self, tbs, forums, ok_names, fail_names):
        """走客户端协议 /c/c/forum/msign 批量签到，单次最多 200 个吧。"""
        id2name = {str(f.get("forum_id")): (f.get("forum_name") or "") for f in forums}
        ids = list(id2name.keys())
        finished = set()

        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            params = {
                "_client_type": "4",
                "_client_version": self.APP_VER,
                "_phone_imei": "0" * 15,
                "model": "HUAWEI P40",
                "net_type": "1",
                "stErrorNums": "1",
                "stMethod": "1",
                "stMode": "1",
                "stSize": "320",
                "stTime": "117",
                "stTimesNum": "1",
                "timestamp": str(int(time.time() * 1000)),
                "BDUSS": self.bduss,
                "tbs": tbs,
                "forum_ids": ",".join(chunk),
            }
            params["sign"] = self._app_sign(params)
            body = urllib.parse.urlencode(params)

            try:
                status, _, text = request(
                    "https://c.tieba.baidu.com/c/c/forum/msign",
                    method="POST",
                    headers={"User-Agent": self.APP_UA,
                             "Cookie": "ka=open",
                             "Content-Type": "application/x-www-form-urlencoded",
                             "Accept": ""},
                    data=body)
                data = json.loads(text)
            except Exception as exc:
                self.log("warn", f"批量签到请求失败：{exc}")
                continue

            if str(data.get("error_code")) != "0":
                err = data.get("error") or {}
                self.log("warn", f"批量签到被拒绝：{err.get('usermsg') or data.get('error_code')}")
                continue

            info = data.get("info") or []
            for row in info:
                fid = str(row.get("forum_id") or "")
                name = row.get("forum_name") or id2name.get(fid) or fid
                finished.add(fid)
                ok_names.append(name)
                score = row.get("cur_score")
                self.log("success", f"{name} · 签到成功" + (f"（+{score} 经验）" if score else ""))

            if not info:
                self.log("warn", "批量接口没有返回成功记录，改用网页方式补签")

        return finished

    def _sign_web(self, tbs, kw):
        data = {"ie": "utf-8", "tbs": tbs, "kw": kw}
        try:
            status, _, text = request(
                "https://tieba.baidu.com/sign/add",
                method="POST",
                headers={"Cookie": self.cookie,
                         "Referer": "https://tieba.baidu.com/f?kw=" + urllib.parse.quote(kw),
                         "X-Requested-With": "XMLHttpRequest",
                         "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                data=data)
            res = json.loads(text)
        except Exception as exc:
            return "fail", f"网络异常：{exc}"

        no = str(res.get("no", ""))
        if no == "0":
            body = res.get("data") or {}
            if str(body.get("errno")) == "0":
                gain = body.get("sign_bonus_point")
                return "ok", f"签到成功" + (f"（+{gain} 经验）" if gain else "")
            return "fail", str(body.get("errmsg") or "未知错误")
        if no == "1101":
            return "already", "今天已经签过了"
        if no == "1011":
            return "fail", "登录态失效"
        if no == "340006":
            return "fail", "需要验证码，歇一会儿再试"
        return "fail", str(res.get("error") or f"no={no}")

    def _app_sign(self, params):
        keys = sorted(params.keys())
        raw = "".join(f"{k}={params[k]}" for k in keys) + "tiebaclient!!!"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

# ---------------------------------------------------------------- 配置 & 任务

REPOSTED_PATH = BASE_DIR / "data" / "reposted.json"
DM_SENT_PATH = BASE_DIR / "data" / "dm-sent.json"
LIKED_PATH = BASE_DIR / "data" / "liked.json"
REPOST_KEEP_DAYS = 90

def load_liked() -> dict:
    """已点过赞的帖子 id，避免重复点同一条。"""
    try:
        data = json.loads(LIKED_PATH.read_text("utf-8"))
        if not isinstance(data, dict):
            return {}
    except Exception:
        return {}
    cutoff = int(time.time()) - REPOST_KEEP_DAYS * 86400
    return {k: v for k, v in data.items() if isinstance(v, int) and v >= cutoff}

def save_liked(data: dict):
    try:
        LIKED_PATH.parent.mkdir(parents=True, exist_ok=True)
        LIKED_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    except OSError:
        pass

DAILY_PATH = BASE_DIR / "data" / "daily-done.json"

def load_daily() -> dict:
    """今天已完成的任务，形如 {"2026-10-23": {"sign": 1792..., "fans": ...}}。
    只保留当天，跨天自动失效。"""
    try:
        data = json.loads(DAILY_PATH.read_text("utf-8"))
        if not isinstance(data, dict):
            return {}
    except Exception:
        return {}
    today = time.strftime("%Y-%m-%d")
    return {today: data[today]} if isinstance(data.get(today), dict) else {}

def save_daily(data: dict):
    try:
        DAILY_PATH.parent.mkdir(parents=True, exist_ok=True)
        DAILY_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    except OSError:
        pass

def daily_done(key: str) -> bool:
    today = time.strftime("%Y-%m-%d")
    return bool(load_daily().get(today, {}).get(key))

def mark_daily(key: str):
    today = time.strftime("%Y-%m-%d")
    data = load_daily()
    data.setdefault(today, {})[key] = int(time.time())
    save_daily(data)

def load_reposted() -> dict:
    """已转发过的微博 id，避免每天重复转同一条。"""
    try:
        data = json.loads(REPOSTED_PATH.read_text("utf-8"))
        if not isinstance(data, dict):
            return {}
    except Exception:
        return {}
    cutoff = int(time.time()) - REPOST_KEEP_DAYS * 86400
    return {k: v for k, v in data.items() if isinstance(v, int) and v >= cutoff}

def save_reposted(data: dict):
    try:
        REPOSTED_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPOSTED_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    except OSError:
        pass

def load_dm_sent() -> dict:
    try:
        data = json.loads(DM_SENT_PATH.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_dm_sent(data: dict):
    try:
        DM_SENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        DM_SENT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    except OSError:
        pass

def greeting_for_now(morning, noon, night):
    """按当前时间挑问候语。05-11 早 / 11-14 午 / 其余晚。"""
    h = time.localtime().tm_hour
    if 5 <= h < 11:
        return "早安", morning
    if 11 <= h < 14:
        return "午安", noon
    return "晚安", night

DEFAULT_CONFIG = {
    "weibo": {
        "cookie": "",
        "repost_enable": False,
        "repost_count": 2,
        "repost_author": "封茗囧菌",
        "repost_uid": "",
        "repost_chaohua": "",
        "repost_texts": "",
        "repost_links": "",
        "comment_enable": False,
        "comment_url": "",
        "comment_count": 5,
        "comment_texts": "",
        "like_enable": True,
        "like_chaohua": "",
        "mtask_enable": False,
        "mtask_cookie": "",
        "mtask_follow_enable": False,
        "mtask_hot_search_enable": True,
        "fans_enable": False,
        "fans_blogger_uid": "3056354293",
        "fans_fans_uid": "",
        "group_enable": False,
        "group_name": "囧菌今天发歌了吗",
        "group_text": "1",
        "dm_enable": False,
        "dm_target": "封茗囧菌",
        "dm_morning": "馒头早安！O(∩_∩)O",
        "dm_noon": "馒头午安！O(∩_∩)O",
        "dm_night": "馒头晚安！O(∩_∩)O",
    },
    "pojie": {"cookie": "", "pojie_mode": "browser"},
    "tieba": {"cookie": ""},
}

SIGNERS = {
    "weibo": (WeiboSenior, "微博超话"),
    "pojie": (PojieSenior, "吾爱破解"),
    "tieba": (TiebaSenior, "百度贴吧"),
}

_cfg_lock = threading.Lock()

def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        if CONFIG_PATH.exists():
            saved = json.loads(CONFIG_PATH.read_text("utf-8"))
            for k in cfg:
                if isinstance(saved.get(k), dict):
                    cfg[k].update(saved[k])
    except Exception:
        pass
    return cfg

def save_config(cfg: dict):
    with _cfg_lock:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")

class Task:
    """一次签到任务的运行状态 + 日志缓冲，前端轮询取增量。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.logs = []
        self.seq = 0
        self.running = False
        self.done = False
        self.summary = {}
        self.started_at = 0

    def log(self, platform, level, msg):
        with self.lock:
            self.seq += 1
            self.logs.append({
                "id": self.seq,
                "p": platform,
                "l": level,
                "m": msg,
                "t": time.strftime("%H:%M:%S"),
            })

    def start(self, targets, cfg):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.done = False
            self.logs = []
            self.seq = 0
            self.summary = {}
            self.started_at = time.time()
        threading.Thread(target=self._run, args=(targets, cfg), daemon=True).start()
        return True

    def _run(self, targets, cfg):
        results = {}
        try:
            for key in targets:
                cls, label = SIGNERS[key]
                self.log(key, "head", f"━━━ {label} ━━━")
                signer = cls(cfg.get(key, {}), lambda lv, m, k=key: self.log(k, lv, m))
                t0 = time.time()
                try:
                    res = signer.run()
                except SignError as exc:
                    self.log(key, "error", str(exc))
                    res = {"error": str(exc)}
                except Exception as exc:  # noqa: BLE001
                    self.log(key, "error", f"出了点意外：{exc}")
                    res = {"error": str(exc)}
                res["cost"] = round(time.time() - t0, 1)
                results[key] = res

                if "error" not in res:
                    parts = []
                    if res.get("ok"):
                        parts.append(f"成功 {res['ok']}")
                    if res.get("already"):
                        parts.append(f"已签 {res['already']}")
                    if res.get("fail"):
                        parts.append(f"失败 {res['fail']}")
                    self.log(key, "done", " · ".join(parts) if parts else "没有需要签到的项目")
        finally:
            with self.lock:
                self.summary = results
                self.running = False
                self.done = True
            self.log("", "finish", "全部完成")

TASK = Task()

class WeiboQrLogin:
    """微博扫码登录：起一个独立 profile 的浏览器打开微博登录页，用户用微博 App 扫码，
    登录成功后通过 CDP 把 weibo.com 的 Cookie（含 XSRF-TOKEN）原样读出来，存进 config。

    比手动复制靠谱在这：Cookie 是浏览器真实登录态，一个字段都不缺；
    存之前还会打一个接口验证会话有效，免得把过期残留写进去。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"stage": "idle", "msg": "", "qr": "", "cookie": ""}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def start(self):
        with self.lock:
            busy = self.state.get("stage") in ("launching", "waiting")
        if busy:
            return False
        threading.Thread(target=self._run, daemon=True).start()
        return True

    # ---- 主流程
    def _run(self):
        self._set(stage="launching", msg="正在打开微博登录页…", qr="", cookie="")
        browser = find_browser()
        if not browser:
            self._set(stage="error", msg="没找到 Chrome 或 Edge，装一个浏览器再试")
            return
        proc = ws = None
        reused = False
        try:
            try:
                _http_json(f"http://127.0.0.1:{WEIBO_QR_PORT}/json/version", timeout=1)
                reused = True
                self._set(msg="检测到登录窗口已经开着，直接在里面扫码就行")
            except Exception:
                proc = launch_browser(browser, WEIBO_QR_PROFILE,
                                      "https://weibo.com/login.php",
                                      port=WEIBO_QR_PORT)
            ws = self._connect()
            self._wait_scan(ws)
        except Exception as exc:
            self._set(stage="error", msg=f"扫码登录没弄成：{exc}")
        finally:
            if ws:
                ws.close()
            if proc and not reused:
                kill_browser(proc)

    @staticmethod
    def _connect():
        targets = None
        for _ in range(40):
            try:
                targets = _http_json(f"http://127.0.0.1:{WEIBO_QR_PORT}/json/list")
                if targets:
                    break
            except Exception:
                pass
            time.sleep(0.3)
        if not targets:
            raise RuntimeError("浏览器调试端口没起来，可能被安全软件拦了")
        page = next((t for t in targets
                     if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
        if not page:
            raise RuntimeError("浏览器没有可用标签页")
        ws = _WS(page["webSocketDebuggerUrl"])
        # 只需要 Page 域（截图）；读 Cookie 不依赖 Network 域，
        # 不启用它可以避开登录页疯狂的事件洪流
        ws.call("Page.enable", msg_id=1)
        return ws

    def _wait_scan(self, ws):
        self._set(stage="waiting",
                  msg="请用微博 App 扫页面上的二维码，并在手机上点确认")
        deadline = time.time() + WEIBO_QR_TIMEOUT
        mid = 10
        next_shot = time.time() + 6
        while time.time() < deadline:
            time.sleep(2)
            try:
                cookies = (ws.call("Network.getCookies", {}, msg_id=mid)
                           .get("result", {}).get("cookies") or [])
            except Exception:
                raise RuntimeError("浏览器窗口被关掉了，再点一次「扫码登录」重新来")
            mid += 1
            # 只取浏览器自己会发给 weibo.com 的 Cookie。
            # 注意域名要卡死：passport.weibo.com 这些子域名的 Cookie 浏览器根本不会
            # 发给 weibo.com，必须排除；而 weibo.com 登录页一跳转就会产生一个
            # "游客 SUB"，看着像登录了其实没有——所以拿到 SUB 后还要打接口验一把。
            mine = [c for c in cookies
                    if str(c.get("domain") or "") in ("weibo.com", ".weibo.com")]
            # 同名 Cookie 只留最后一次设置的（正常登录态不会重名，
            # 但游客态残留 + 新登录写入撞名时，发两份会让服务端取错值）
            uniq = {}
            for c in mine:
                uniq[c.get("name")] = c.get("value")
            names = set(uniq)
            if "SUB" in names:
                if self._save_cookie("; ".join(
                        f"{k}={v}" for k, v in uniq.items())):
                    return
                # 没通过验证（多半是游客会话，用户还没扫码），继续等
            if time.time() >= next_shot:
                # 把页面截图同步到控制台弹窗，扫码不用去找浏览器窗口
                next_shot = time.time() + 15
                try:
                    shot = (ws.call("Page.captureScreenshot",
                                    {"format": "png", "fromSurface": False},
                                    msg_id=mid, timeout=60)
                            .get("result", {}).get("data") or "")
                    if shot:
                        self._set(qr="data:image/png;base64," + shot)
                except Exception:
                    pass
                mid += 1
        self._set(stage="error",
                  msg="等超时了（二维码大概已过期），再点一次「扫码登录」")

    def _save_cookie(self, cookie: str) -> bool:
        """验证是真的登录态再落盘。

        拿「某人主页的微博列表」当探针：这个接口未登录/游客会话会直接 403，
        真登录才返回 JSON。别用公开资料类接口（游客也能看），也不要用
        关注超话列表（游客会拿到 ok:1 的空列表，会把没扫码误判成登录成功）。
        """
        uid = ""
        try:
            uid = str((load_config().get("weibo") or {}).get("fans_blogger_uid") or "").strip()
        except Exception:
            pass
        probe = ("https://weibo.com/ajax/statuses/mymblog?uid="
                 + (uid or "3056354293") + "&page=1&feature=1")
        try:
            status, _, text = request(probe, headers={
                "Cookie": cookie, "Referer": "https://weibo.com/"})
            data = json.loads(text)
        except Exception:
            return False
        if status != 200 or not isinstance(data.get("data"), dict):
            return False
        cfg = load_config()
        cfg["weibo"]["cookie"] = cookie
        # 顺手校正「你的 UID」：填错的话，铁粉打卡和点赞时排除自己的帖子都会失效
        my_uid, my_name = current_weibo_user(cookie)
        if my_uid:
            cfg["weibo"]["fans_fans_uid"] = my_uid
        save_config(cfg)
        has_csrf = "XSRF-TOKEN=" in cookie
        who = f"（@{my_name}）" if my_name else ""
        TASK.log("weibo", "success",
                 "扫码登录成功，Cookie 已自动保存"
                 + (f"，当前账号 {who}" if who else "")
                 + ("" if has_csrf else
                    "（注意：没读到 XSRF-TOKEN，转发/评论/点赞可能仍不行）"))
        self._set(stage="success",
                  msg="登录成功，Cookie 已自动保存"
                      + (f"，当前账号 {who}" if who else "")
                      + ("" if has_csrf else
                         "；但这份 Cookie 里没有 XSRF-TOKEN，转发/评论/点赞仍需手动补"),
                  cookie=cookie)
        return True

QR = WeiboQrLogin()

def code_mtime() -> float:
    """代码文件的修改时间，用来判断跑的是不是最新版本。"""
    try:
        p = Path(sys.executable) if getattr(sys, "frozen", False) else Path(__file__)
        return p.stat().st_mtime
    except OSError:
        return 0.0
