#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用快捷登录：起一个独立 profile 的浏览器打开登录页，用户扫码/输密码登完，
程序通过 CDP 把目标域名下的 Cookie 原样读出来并存好。

不用各平台自己的扫码协议——那玩意每个平台一套、还会变。
只认一件事：浏览器里这个域名最终有哪些 Cookie。登录成功与否由平台验证接口判定，
所以不会把「游客态残留」误存成登录态。

浏览器控制那套（CDP WebSocket、找 Chrome、起停进程）已经在签到模块里跑稳了，
这里直接复用，不再写第二份。
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from app.paths import DATA_ROOT
from modules.signin.core import (
    _WS, _http_json, find_browser, kill_browser, launch_browser)

PROFILE_ROOT = DATA_ROOT / "login_profiles"
PROFILE_ROOT.mkdir(parents=True, exist_ok=True)


def _kill_port(port: int):
    """按调试端口找到进程并整树杀掉。Browser.close 不响应时的兜底。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=10).stdout
        pids = {line.split()[-1] for line in out.splitlines()
                if f":{port} " in line and "LISTENING" in line}
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                           capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass

TIMEOUT = 300          # 二维码一般几分钟换一次，等太久没意义


# ---------------------------------------------------------------- 平台定义
# cookie_domains: 只取这些域下的 Cookie（子域 cookie 不会发给主域，
#                 混进来会污染登录态）
# need:           必须存在其中任意一个字段，才算「可能登录了」
# verify:         再多打一次接口确认，避免游客态被当成登录成功

def _verify_netease(cookie: str) -> tuple[bool, str]:
    """网易云的账号接口：未登录返回 code 200 但 account 为 null。"""
    try:
        req = urllib.request.Request(
            "https://music.163.com/api/nuser/account/get",
            headers={"Cookie": cookie, "User-Agent": "Mozilla/5.0",
                     "Referer": "https://music.163.com/"})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return False, ""
    account = data.get("account") or {}
    profile = data.get("profile") or {}
    if account.get("id"):
        name = profile.get("nickname") or account.get("userName") or str(account["id"])
        return True, name
    return False, ""


PLATFORMS = {
    "netease": {
        "name": "网易云音乐",
        "login_url": "https://music.163.com/#/login",
        "cookie_domains": ("music.163.com", ".music.163.com"),
        "need": ("MUSIC_U",),
        "port": 9350,
        "verify": _verify_netease,
    },
    # verify / save 由各模块导入时注册（签名与校验逻辑属于各自模块，
    # 放这里会形成循环依赖）
    "heybox": {
        "name": "小黑盒",
        "login_url": "https://www.xiaoheihe.cn/",
        "cookie_domains": ("xiaoheihe.cn", ".xiaoheihe.cn", "www.xiaoheihe.cn"),
        "need": ("pkey",),
        "port": 9351,
    },
    "kugou": {
        "name": "酷狗音乐",
        "login_url": "https://www.kugou.com/",
        "cookie_domains": ("kugou.com", ".kugou.com", "www.kugou.com"),
        "need": ("KuGoo", "token", "userid"),
        "port": 9352,
    },
}


class LoginSession:
    """单个平台的一次登录过程。

    交互是「两步」而不是自动轮询：
    1. start() 起浏览器，按钮变红表示还在等；
    2. 用户在里面登完，点一下按钮 -> grab() 立刻读 Cookie、校验、保存。
    这样用户知道东西是什么时候被拿走的，也不会有「登录了但页面没反应」的困惑。
    """

    def __init__(self, key: str):
        self.key = key
        self.spec = PLATFORMS[key]
        self.lock = threading.Lock()
        self.ws = None
        self._ws_lock = threading.Lock()
        self._mid = 100
        self._finished = threading.Event()
        self.state = {"stage": "idle", "msg": "", "qr": "", "cookie": "", "who": ""}

    # ---- 状态
    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    # ---- 入口
    def start(self) -> bool:
        with self.lock:
            busy = self.state.get("stage") in ("launching", "waiting")
        if busy:
            return False
        self._finished.clear()
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        spec = self.spec
        self._set(stage="launching", msg=f"正在打开{spec['name']}登录页…",
                  qr="", cookie="", who="")
        browser = find_browser()
        if not browser:
            self._set(stage="error", msg="没找到 Chrome 或 Edge，装一个浏览器再试")
            return
        port = spec["port"]
        profile = PROFILE_ROOT / self.key
        proc = ws = None
        reused = False
        try:
            try:
                _http_json(f"http://127.0.0.1:{port}/json/version", timeout=1)
                reused = True
                self._set(msg="登录窗口本来就开着，直接在里面登就行")
            except Exception:
                proc = launch_browser(browser, profile, spec["login_url"], port=port)
            ws = self._connect(port)
            self.ws = ws
            self._wait(port)
        except Exception as exc:  # noqa: BLE001
            self._set(stage="error", msg=f"登录没弄成：{exc}")
        finally:
            # 成功后关掉专用登录窗口。reused 表示这次连接的是上次留下的窗口，
            # 没有 proc 可杀，改用 CDP 的 Browser.close，再用端口兜底；
            # 新建进程仍由 kill_browser 兜底。
            if self.snapshot().get("stage") == "success":
                self._call("Browser.close", timeout=3)
                _kill_port(port)
            self.ws = None
            if ws:
                ws.close()
            if proc and not reused:
                kill_browser(proc)

    def _connect(self, port: int):
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
            raise RuntimeError("浏览器调试端口没起来，可能被安全软件拦了")
        page = next((t for t in targets
                     if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
        if not page:
            raise RuntimeError("浏览器没有可用标签页")
        ws = _WS(page["webSocketDebuggerUrl"])
        # 只开 Page 域。读 Cookie 不依赖 Network 域，
        # 开了反而要吞登录页那一串事件。
        ws.call("Page.enable", msg_id=1)
        return ws

    # ---- 和浏览器通信（会被 _wait 与 grab 同时用，得排队）
    def _call(self, method: str, params: dict | None = None, timeout: int = 30):
        ws = self.ws
        if not ws:
            return {}
        with self._ws_lock:
            self._mid += 1
            try:
                return ws.call(method, params or {}, msg_id=self._mid, timeout=timeout)
            except Exception:
                return {}

    def _cookies(self) -> list:
        return self._call("Network.getCookies").get("result", {}).get("cookies") or []

    def _wait(self, port: int):
        """保持等待连接，定时探一下浏览器是否还活着。不自动抓 Cookie。

        不再往状态里塞整页截图：一张截图 1MB 左右，前端每 1.5 秒拉一次状态，
        既拖慢页面，也容易让人误以为卡住。浏览器窗口本身就是可见的。
        """
        spec = self.spec
        self._set(stage="waiting",
                  msg=f"在弹出的窗口里登录{spec['name']}，登完回到这里点一下按钮")
        deadline = time.time() + TIMEOUT
        misses = 0
        while time.time() < deadline:
            if self._finished.wait(3):
                return
            try:
                _http_json(f"http://127.0.0.1:{port}/json/version", timeout=1)
                misses = 0
            except Exception:  # noqa: BLE001
                misses += 1
                if misses >= 2:
                    self._set(stage="error",
                              msg="登录窗口已经关闭了，重新点一次「快捷登录」")
                    return
        self._set(stage="error", msg="等太久了窗口已经关掉，重新点一次「快捷登录」")

    # ---- 用户点按钮时立刻抓一次
    def grab(self) -> dict:
        spec = self.spec
        if self.snapshot().get("stage") == "launching":
            self._set(msg="浏览器还在打开，等登录窗口出来后再点")
            return self.snapshot()
        if not self.ws:
            self._set(stage="error",
                      msg="登录窗口没开着，先点一次「快捷登录」把窗口打开")
            return self.snapshot()

        cookies = self._cookies()
        if not cookies:
            self._set(stage="error",
                      msg="读不到浏览器 Cookie：登录窗口已经断开，重新点一次「快捷登录」")
            return self.snapshot()

        mine = [c for c in cookies
                if str(c.get("domain") or "") in spec["cookie_domains"]]
        # 同名 Cookie 只留最后一次写入的（游客态残留 + 新登录撞名时，
        # 发两份服务端会取错值）
        uniq = {}
        for c in mine:
            uniq[c.get("name")] = c.get("value")
        names = set(uniq)
        cookie = "; ".join(f"{k}={v}" for k, v in uniq.items())

        if not any(n in names for n in spec["need"]):
            self._set(msg=f"还没登录：没找到 {' / '.join(spec['need'])}，登完再点一次")
            return self.snapshot()

        verifier = spec.get("verify")
        try:
            ok, who = verifier(cookie) if verifier else (True, "")
        except Exception as exc:  # noqa: BLE001
            self._set(stage="error", msg=f"平台校验中断：{exc}。重新点一次「快捷登录」")
            return self.snapshot()
        if not ok:
            if self.key == "kugou":
                self._set(stage="error",
                          msg="网页登录的 token 调不了酷狗任务接口，请关掉窗口，改用「扫码登录」")
            else:
                self._set(msg="读到 Cookie 了，但平台还没认这个登录，登完再点一次")
            return self.snapshot()

        if self._save(cookie, who):
            self._finished.set()
        return self.snapshot()

    def _save(self, cookie: str, who: str) -> bool:
        """把 Cookie 交给调用方写进自己的配置。"""
        saver = self.spec.get("save")
        if saver:
            try:
                saver(cookie, who)
            except Exception as exc:  # noqa: BLE001
                self._set(stage="error", msg=f"登录成功但保存失败：{exc}")
                return False
        self._set(stage="success", cookie=cookie, who=who,
                  msg=f"登录成功{f'（{who}）' if who else ''}，已经自动填好")
        return True


_sessions: dict[str, LoginSession] = {}
_sessions_lock = threading.Lock()


def get(key: str) -> LoginSession:
    with _sessions_lock:
        if key not in PLATFORMS:
            raise KeyError(key)
        if key not in _sessions:
            _sessions[key] = LoginSession(key)
        return _sessions[key]


def register(key: str, spec: dict):
    """各模块在导入时注册自己的平台登录参数。"""
    PLATFORMS[key] = spec


def profile_dir(key: str) -> Path:
    return PROFILE_ROOT / key
