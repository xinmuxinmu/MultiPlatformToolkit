#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 办公平台一键签到：Trae CN / Qoder / WorkBuddy / 豆包 / MiniMax / DuMate / LobsterAI。

实现来自三个 MIT 项目，按本项目的模块约定包了一层：

- mcvea/AIAutomator              → vendor/platforms（Trae、WorkBuddy、MiniMax、DuMate、Lobster）
- tututashu/doubao-auto-checkin  → vendor/doubao_checkin.py（豆包）
- spirodelazz/ide-daily-checkin  → vendor/qoder_cred.py（Qoder 桌面端凭据）+ 续期思路

数据目录：data/ai_checkin（配置、auths 多账号凭据、豆包截图与日志、Qoder 会话缓存）。
凭据只在本机读写，日志里不打印 token。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app.paths import AI_CHECKIN_HOME

VERSION = "1.0.0"
STARTED_AT = time.time()

BASE_DIR = AI_CHECKIN_HOME
CONFIG_PATH = BASE_DIR / "config.json"
VENDOR_DIR = Path(__file__).resolve().parent / "vendor"

# vendor 里的包名是 platforms（与上游一致）；项目内没有同名包，放最前面即可
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
QODER_TIMEOUT = 15

# 平台清单：id, 名称, 一句话说明, 是否需要额外凭据
PLATFORMS = [
    {"id": "trae", "name": "Trae CN", "desc": "本地读取/解密登录态，通过 HTTP 领取每日积分；token 临期自动续期", "cred": ""},
    {"id": "qoder_cn", "name": "Qoder 国内版", "desc": "本地读取登录态或填 PAT/Cookie，通过 HTTP 查询并领取签到活动", "cred": "token"},
    {"id": "qoder", "name": "Qoder 国际版", "desc": "本地读取登录态或填 PAT，通过 HTTP 查询并领取签到活动", "cred": "token"},
    {"id": "workbuddy", "name": "WorkBuddy", "desc": "本地读取/解密登录态，通过 HTTP 签到；支持多账号与临期续期", "cred": ""},
    {"id": "workbuddy_ai", "name": "WorkBuddy AI", "desc": "支持独立多账号授权与反代；国际版每日签到接口尚未确认", "cred": ""},
    {"id": "qwen", "name": "千问办公", "desc": "本地 DPAPI/AES 解密或扫码添加账号，查询 HTTP 签到活动；未开放时明确跳过", "cred": ""},
    {"id": "zhipu", "name": "智谱清言", "desc": "本地解密 Cookie 或扫码添加账号，通过 HTTP 领取每日积分", "cred": ""},
    {"id": "doubao", "name": "豆包", "desc": "本地读取/解密登录 Cookie，通过 HTTP 活动接口签到", "cred": ""},
    {"id": "minimax", "name": "MiniMax Code", "desc": "本地读取 OAuth 登录态，通过 HTTP 签到并自动续期", "cred": ""},
    {"id": "dumate", "name": "百度搭子 DuMate", "desc": "本地解密登录 Cookies，通过 HTTP 领取每日积分", "cred": ""},
    {"id": "lobster", "name": "LobsterAI", "desc": "本地读取 SQLite 登录态，通过 HTTP 接口签到", "cred": ""},
]

# 上游用「失败」状态表达「今天已经签过」，这些文案统一当 already 处理
ALREADY_HINTS = ("已签到", "已经签到", "今日已领", "已领取", "already")

AIAUTO_CLASSES = {
    "trae": ("platforms.traework", "TraeWorkCheckin"),
    "workbuddy": ("platforms.workbuddy", "WorkBuddyCheckin"),
    "minimax": ("platforms.minimax", "MiniMaxCheckin"),
    "dumate": ("platforms.dumate", "DuMateCheckin"),
    "lobster": ("platforms.lobster", "LobsterCheckin"),
    "qwen": ("platforms.qwenwork", "QwenWorkCheckin"),
}

DEFAULT_CONFIG = {
    "config_version": 2,
    "platforms": {
        "trae": True,
        "qoder": True,
        "qoder_cn": True,
        "workbuddy": True,
        "workbuddy_ai": True,
        "qwen": True,
        "zhipu": True,
        "doubao": True,
        "minimax": True,
        "dumate": True,
        "lobster": True,
    },
    "schedule": {"enabled": True, "time": "10:20"},
    "qoder_cn_token": "",
    "qoder_cn_cookie": "",
    "qoder_token": "",
    "keepalive": True,
}


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    cfg = {
        "platforms": dict(DEFAULT_CONFIG["platforms"]),
        "schedule": dict(DEFAULT_CONFIG["schedule"]),
        **{k: v for k, v in DEFAULT_CONFIG.items() if k not in ("platforms", "schedule")},
    }
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    # The old defaults left several AI clients unchecked because their check-in
    # paths were incomplete. The HTTP/local-credential implementation now covers
    # the full catalog, so migrate existing installations once to enable them.
    try:
        old_version = int(raw.get("config_version") or 1)
    except (TypeError, ValueError):
        old_version = 1
    migrate_platforms = old_version < DEFAULT_CONFIG["config_version"]
    if migrate_platforms:
        cfg["platforms"] = {pid: True for pid in cfg["platforms"]}
        cfg["config_version"] = DEFAULT_CONFIG["config_version"]
    for key, value in raw.items():
        if key == "platforms" and isinstance(value, dict):
            if migrate_platforms:
                continue
            for pid, enabled in value.items():
                if pid in cfg["platforms"]:
                    cfg["platforms"][pid] = bool(enabled)
        elif key == "schedule" and isinstance(value, dict):
            if "enabled" in value:
                cfg["schedule"]["enabled"] = bool(value["enabled"])
            text = str(value.get("time") or "").strip()
            if _valid_time(text):
                cfg["schedule"]["time"] = text
        elif key in cfg and key != "config_version":
            cfg[key] = value
    cfg["config_version"] = DEFAULT_CONFIG["config_version"]
    return cfg


def _valid_time(text: str) -> bool:
    try:
        hour, minute = text.split(":")
        return 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59
    except (ValueError, AttributeError):
        return False


def save_config(cfg: dict) -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def code_mtime() -> float:
    try:
        return Path(__file__).stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------- 运行状态（落盘）

STATE_PATH = BASE_DIR / "state.json"
RUN_LOG = BASE_DIR / "checkin.log"
_LOG_MAX = 256 * 1024


def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("results"), dict):
        data["results"] = {}
    data.setdefault("last_run_date", "")
    return data


def save_state(state: dict) -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_log(line: str) -> None:
    """计划任务模式没有控制台，统一落一份文本日志。"""
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        if RUN_LOG.exists() and RUN_LOG.stat().st_size > _LOG_MAX:
            keep = RUN_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
            RUN_LOG.write_text("\n".join(keep) + "\n", encoding="utf-8")
        with RUN_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- Qoder

def _find_claimable(node, found=None):
    if found is None:
        found = []
    if isinstance(node, dict):
        status = str(node.get("status") or "").upper()
        cid = next((node[k] for k in ("id", "campaignId", "campaign_id", "campaignID") if node.get(k)), None)
        if status == "CLAIMABLE" and cid is not None:
            kind = str(node.get("type") or node.get("campaignType") or "").upper()
            if not kind or "BENEFIT" in kind or "CHECK" in kind:
                found.append(str(cid))
            return found
        for value in node.values():
            _find_claimable(value, found)
    elif isinstance(node, list):
        for value in node:
            _find_claimable(value, found)
    return found


def _qoder_machine(base: str) -> dict:
    folder = ".qoder-cn" if "qoder.com.cn" in base else ".qoder"
    try:
        info = json.loads((Path.home() / folder / "umid-cache.json").read_text(encoding="utf-8")).get("info") or {}
    except (OSError, ValueError):
        return {}
    body = {}
    if info.get("machineCode"):
        body["machine_id"] = str(info["machineCode"])
    if info.get("machineToken"):
        body["machine_token"] = str(info["machineToken"])
    return body


def _qoder_exchange_pat(base: str, pat: str) -> tuple[str, str]:
    payload = {"personal_token": pat, **_qoder_machine(base)}
    req = urllib.request.Request(
        base.rstrip("/") + "/api/v1/jobToken/exchange",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=QODER_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return "", f"PAT 兑换失败：HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return "", f"PAT 兑换失败：{exc}"
    token = data.get("token") or data.get("access_token") or data.get("device_token")
    return (str(token), "") if token else ("", "PAT 兑换失败：响应里没有令牌")


QODER_ALT_HOST = {
    "https://openapi.qoder.com.cn": "https://openapi.qoder.sh",
    "https://openapi.qoder.sh": "https://openapi.qoder.com.cn",
}


def _qoder_query(base: str, headers: dict) -> tuple[int, object]:
    endpoint = base.rstrip("/") + "/sash/api/v1/me/campaigns?clientType=10"
    req = urllib.request.Request(endpoint, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=QODER_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, str(exc)


def _qoder_flow(base: str, token: str, cookie: str, dry: bool) -> dict:
    """查 campaigns → 领取 CLAIMABLE。返回 {ok, status, message}。

    桌面端 token 只对「自己那个站点」有效（国际版 .sh / 国内版 .com.cn），
    拿错站点会 401。这里先按站点查，401 时用同一个 token 换另一个站点再试。
    """
    headers = {"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    if token:
        if token.startswith("pt-"):
            exchanged, error = _qoder_exchange_pat(base, token)
            if error:
                return {"ok": False, "status": "failed", "message": error}
            token = exchanged
        headers["Authorization"] = f"Bearer {token}"

    if not token and not cookie:
        try:
            import qoder_cred

            token, error = qoder_cred.get_token(base)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "failed", "message": f"读取 Qoder 桌面端凭据失败：{exc}"}
        if not token:
            return {"ok": False, "status": "failed", "message": error or "没有可用的 Qoder 令牌"}
        headers["Authorization"] = f"Bearer {token}"

    if dry:
        return {"ok": True, "status": "skipped", "message": "预览：将查询 CLAIMABLE 活动并领取"}

    bases = [base]
    alt = QODER_ALT_HOST.get(base.rstrip("/"))
    if alt:
        bases.append(alt)

    payload = None
    used = base
    status = 0
    for candidate in bases:
        status, payload = _qoder_query(candidate, headers)
        if status == 200 and isinstance(payload, dict):
            used = candidate
            break
        if status not in (401, 403):
            break
    if status != 200 or not isinstance(payload, dict):
        if status in (401, 403):
            return {"ok": False, "status": "failed",
                    "message": "令牌被拒绝（HTTP 401）：桌面端登录态可能已过期，"
                               "在 Qoder 客户端重新登录一次，或填 PAT（pt- 开头）"}
        return {"ok": False, "status": "failed",
                "message": f"查询活动失败：{payload if isinstance(payload, str) else f'HTTP {status}'}"}

    endpoint = used.rstrip("/") + "/sash/api/v1/me/campaigns"
    claimable = _find_claimable(payload)
    suffix = "" if used == base else f"（桌面端登录态属于 {used.split('//')[-1]}，已自动切过去）"
    if not claimable:
        return {"ok": True, "status": "skipped",
                "message": "当前没有可领取的签到活动（接口未提供已签到状态，无法确认今日是否已签）" + suffix}

    for cid in dict.fromkeys(claimable):
        req = urllib.request.Request(f"{endpoint}/{cid}/claim", data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=QODER_TIMEOUT) as resp:
                raw = resp.read()
            if raw:
                try:
                    claim_result = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    claim_result = None
                if isinstance(claim_result, dict):
                    code = claim_result.get("code")
                    message = str(claim_result.get("message") or claim_result.get("msg") or "")
                    if claim_result.get("success") is False or (
                            code is not None and str(code) not in ("0", "200")):
                        if any(hint in message.lower() for hint in ALREADY_HINTS):
                            return {"ok": True, "status": "already", "message": "今日已签到" + suffix}
                        return {"ok": False, "status": "failed",
                                "message": f"领取活动失败：{message or f'业务码 {code}'}" + suffix}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "status": "failed", "message": f"领取 {cid} 失败：HTTP {exc.code}"}
        except (urllib.error.URLError, OSError) as exc:
            return {"ok": False, "status": "failed", "message": f"领取 {cid} 失败：{exc}"}
    return {"ok": True, "status": "success", "message": f"已领取 {len(claimable)} 个活动" + suffix}


# ---------------------------------------------------------------- 平台执行

def _run_aiauto(key: str, dry: bool) -> dict:
    module_name, class_name = AIAUTO_CLASSES[key]
    try:
        module = __import__(module_name, fromlist=["_"])
        instance = getattr(module, class_name)(dry=dry)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "failed", "message": f"加载实现失败：{exc}"}

    result = instance.run()
    status = str(result.get("status") or "")
    message = " ".join(str(result.get("message") or "未知结果").split())
    credits = result.get("credits")
    if credits and "积分" not in message:
        message = f"{message}，积分 {credits}"
    # 上游把「设备今日已签」当失败返回（HTTP 200 + 文案），这里统一认成 already，
    # 否则用户每天看到的就是一片红。
    if status == "failed" and any(k in message for k in ALREADY_HINTS):
        return {"ok": True, "status": "already", "message": message}
    ok = bool(result.get("success")) and status in ("success", "already")
    if status == "skipped":
        return {"ok": True, "status": "skipped", "message": message}
    return {"ok": ok, "status": status or "failed", "message": message}


def _run_doubao(dry: bool) -> dict:
    try:
        import doubao_checkin
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "failed",
                "message": f"加载豆包 HTTP 签到实现失败：{exc}"}
    try:
        return doubao_checkin.checkin_http(dry=dry)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "failed",
                "message": f"豆包 HTTP 签到异常：{type(exc).__name__}: {exc}"}


def run_platform(key: str, cfg: dict, dry: bool = False) -> dict:
    if key in ("workbuddy", "workbuddy_ai", "qwen", "zhipu"):
        from modules.ai_gateway.core import GATEWAY
        from modules.ai_gateway.base import GatewayError
        try:
            provider = GATEWAY.provider(key)
            if not provider.capabilities.get("checkin"):
                return {"ok": True, "status": "skipped", "message": provider.capabilities.get("reason") or "该版本签到接口尚未确认"}
            accounts = GATEWAY.store.accounts(key)
            if accounts:
                if not any(a["enabled"] for a in accounts):
                    return {"ok": True, "status": "skipped", "message": "此平台的账号均已停用"}
                result = GATEWAY.checkin(key, dry=dry)
                return {**result, "ok": result["status"] != "failed"}
            if key == "zhipu":
                rows = provider.import_local()
                if not rows:
                    return {"ok": False, "status": "failed", "message": "未找到清言本地登录态，可在 AI 账号与反代页面扫码添加"}
                result = provider.checkin(rows[0]["credential"], dry=dry)
                return {**result, "ok": result.get("status") != "failed"}
        except GatewayError as exc:
            return {"ok": False, "status": "failed", "message": str(exc)}
    if key in AIAUTO_CLASSES:
        return _run_aiauto(key, dry)
    if key == "doubao":
        return _run_doubao(dry)
    if key == "qoder_cn":
        return _qoder_flow("https://openapi.qoder.com.cn",
                           str(cfg.get("qoder_cn_token") or ""),
                           str(cfg.get("qoder_cn_cookie") or ""), dry)
    if key == "qoder":
        return _qoder_flow("https://openapi.qoder.sh",
                           str(cfg.get("qoder_token") or ""), "", dry)
    return {"ok": False, "status": "failed", "message": f"未知平台：{key}"}


def keepalive(dry: bool = False, margin_days: int = 5) -> list[str]:
    """Trae / WorkBuddy 临期账号续期；返回发生动作的说明行。"""
    messages: list[str] = []
    for label, module_name in (("Trae CN", "trae_auth"), ("WorkBuddy", "wb_auth")):
        try:
            module = __import__(f"platforms.{module_name}", fromlist=["_"])
            accounts = module.list_accounts()
        except Exception as exc:  # noqa: BLE001
            messages.append(f"{label}：续期模块不可用（{exc}）")
            continue
        if not accounts:
            continue
        for entry in accounts:
            try:
                data = module.load_account(entry["path"])
            except Exception as exc:  # noqa: BLE001
                messages.append(f"{label} {entry['label']}：凭据读取失败（{exc}）")
                continue
            if data.get("disabled"):
                messages.append(f"{label} {entry['label']}：需要重新登录")
                continue
            if not module.is_expiring(data, margin_days * 86400):
                continue
            ok, text = module.refresh_account(entry["path"], data, dry=dry)
            messages.append(f"{label} {entry['label']} {'续期成功' if ok else '续期失败'}：{text}")
    return messages


# ---------------------------------------------------------------- 任务与日志

class Task:
    """一次签到任务的状态 + 增量日志，前端轮询 /api/logs。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.logs: list[dict] = []
        self.seq = 0
        self.running = False
        self.done = False
        self.summary: dict = {}
        self.started_at = 0.0

    def log(self, platform: str, level: str, message: str) -> None:
        with self.lock:
            self.seq += 1
            self.logs.append({"id": self.seq, "p": platform, "l": level,
                              "m": message, "t": time.strftime("%H:%M:%S")})
        _append_log(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{platform}] {message}")

    def start(self, targets: list[str], cfg: dict, dry: bool = False) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.done = False
            self.logs = []
            self.seq = 0
            self.summary = {}
            self.started_at = time.time()
        threading.Thread(target=self._run, args=(targets, cfg, dry), daemon=True).start()
        return True

    def _run(self, targets: list[str], cfg: dict, dry: bool) -> None:
        summary = {"ok": 0, "fail": 0, "skip": 0}
        results: dict[str, dict] = {}
        try:
            if cfg.get("keepalive") and not dry:
                for line in keepalive():
                    self.log("续期", "info", line)
            for key in targets:
                name = next((p["name"] for p in PLATFORMS if p["id"] == key), key)
                self.log(name, "info", "开始")
                try:
                    result = run_platform(key, cfg, dry=dry)
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "status": "failed", "message": f"{type(exc).__name__}: {exc}"}
                level = "success" if result["ok"] and result["status"] != "skipped" else (
                    "warn" if result["status"] == "skipped" else "error")
                if result["status"] == "skipped":
                    summary["skip"] += 1
                elif result["ok"]:
                    summary["ok"] += 1
                else:
                    summary["fail"] += 1
                self.log(name, level, result["message"])
                results[key] = {"status": result["status"], "message": result["message"],
                                "at": time.strftime("%Y-%m-%d %H:%M")}
        finally:
            with self.lock:
                self.summary = summary
                self.running = False
                self.done = True
            self.log("汇总", "info",
                     f"成功 {summary['ok']}，失败 {summary['fail']}，跳过 {summary['skip']}")
            if not dry and targets:
                state = load_state()
                state["results"].update(results)
                state["last_run_date"] = time.strftime("%Y-%m-%d")
                save_state(state)


TASK = Task()


# ---------------------------------------------------------------- 定时（应用内）

def enabled_platforms(cfg: dict) -> list[str]:
    return [p["id"] for p in PLATFORMS if cfg.get("platforms", {}).get(p["id"])]


def due(cfg: dict, state: dict | None = None) -> bool:
    """今天到了设定的时间、且今天还没跑过，就算到期（启动晚了也能补跑）。"""
    sched = cfg.get("schedule") or {}
    if not sched.get("enabled"):
        return False
    hhmm = str(sched.get("time") or "")
    if not _valid_time(hhmm):
        return False
    state = state if state is not None else load_state()
    if state.get("last_run_date") == time.strftime("%Y-%m-%d"):
        return False
    return time.strftime("%H:%M") >= hhmm


_scheduler_stop = threading.Event()
_scheduler_thread: threading.Thread | None = None


def start_scheduler() -> None:
    """应用启动后挂一个轻量线程：到点自动跑一次，重复 import 也只启一次。"""
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()

    def loop() -> None:
        while not _scheduler_stop.is_set():
            try:
                if not TASK.running:
                    cfg = load_config()
                    if due(cfg):
                        targets = enabled_platforms(cfg)
                        if targets:
                            TASK.start(targets, cfg)
            except Exception as exc:  # noqa: BLE001 - 定时线程不能死
                _append_log(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [定时] 检查失败：{exc}")
            _scheduler_stop.wait(20)

    _scheduler_thread = threading.Thread(target=loop, name="ai-checkin-scheduler", daemon=True)
    _scheduler_thread.start()


def stop_scheduler() -> None:
    _scheduler_stop.set()
