#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""豆包电脑版自动签到（HTTP + 本地客户端凭据解密，不启动软件或浏览器）。

CLI:
  python checkin.py run       # HTTP 签到并输出结果
  python checkin.py silent    # HTTP 签到并写入日志
  python checkin.py doctor    # 离线检查客户端凭据和数据目录

Windows 从豆包 Chromium Local State 解 DPAPI 密钥，再解密 Cookie SQLite；
macOS 从 Keychain 取密钥并解密 Cookie。凭据只读，只在内存中用于 HTTP 请求。
活动配置和签到接口依据豆包安装包中的官方前端 API 调用，签到成功以响应
``data.checked_in`` 为准。找不到本地登录态或接口不确认成功时如实返回失败。
"""

import argparse
import base64
import datetime
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
# 主项目适配：数据目录默认落在模块旁，可用 DOUBAO_CHECKIN_HOME 指向 data/ai_checkin/doubao
DATA_DIR = pathlib.Path(os.environ.get("DOUBAO_CHECKIN_HOME") or (pathlib.Path(__file__).resolve().parents[4] / "modules" / "ai_checkin" / "vendor" / "data")).resolve()
PROFILE_DIR = DATA_DIR / "profile"
SHOT_DIR = DATA_DIR / "shots"
LOG_FILE = DATA_DIR / "checkin.log"
# 客户端凭据加密缓存: 客户端运行时锁库读不到 Cookie, 用最近一次成功读取的凭据顶上
# (Windows 用 DPAPI 加密 / macOS 用钥匙串密钥派生 AES 密钥, 本机当前用户之外不可解)
CACHE_FILE = DATA_DIR / "client-cookies-cache.bin"
# 最近一次签到结果的明文 JSON (每次 emit 覆盖写): 供豆包定时任务「零命令纯读汇报」用
LAST_RESULT_FILE = DATA_DIR / "last-result.json"
# run 模式提权重跑的结果回传文件: 提权子进程把结果写到这里, 原进程轮询读取后转述
ELEVATED_RESULT_FILE = DATA_DIR / "elevated-run-result.json"

START_URL = "https://www.doubao.com/"                     # 登录用
ACTIVITY_URL = "https://www.doubao.com/pc/agent-explore"  # 签到活动页直链
API_BASE = "https://www.doubao.com"
API_TIMEOUT = 25

# 客户端标识 UA: 活动页前端以 /SamanthaDoubao/ 正则检测 navigator.userAgent,
# 版本号来自客户端内嵌浏览器 (可被环境变量覆盖, 格式 SamanthaDoubao/x.y.z)
CLIENT_UA_VERSION = os.environ.get("DOUBAO_CLIENT_UA_VERSION", "2.33.3")
_CLIENT_UA_BASE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    if sys.platform == "win32" else
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
CLIENT_UA = f"{_CLIENT_UA_BASE} SamanthaDoubao/{CLIENT_UA_VERSION}"
# 客户端数据目录: macOS 固定路径; Windows 在 %LOCALAPPDATA% 下自动探测
CLIENT_DATA_DIR = pathlib.Path.home() / "Library/Application Support/Doubao"
KEYCHAIN_SERVICES = ["Doubao Safe Storage", "Doubao Browser Safe Storage"]

# 签到按钮的候选文案 (按优先级; 豆包改版后在这里补充新文案)
CLICK_TEXTS = ["立即签到", "立即领取", "签到领额度", "今日签到", "签到"]
# 成功标志文案 (注意不要放「获得」这类页面常驻词, 会把已签状态误判成 success)
SUCCESS_TEXTS = ["签到成功", "已签到", "领取成功", "明日再来"]
# 已签过/无可签的标志
ALREADY_TEXTS = ["已签到", "明日再来", "今日已领"]

REPORTS = {
    "success": "豆包签到成功, 200 额度已到账",
    "already": "今日已签到",
    "unknown": "签到结果未能确认, 请人工查看运行截图",
    "AUTH_ERROR": "登录态失效: 客户端凭据模式需在豆包电脑版里重新登录; 网页模式重跑 login",
    "NETWORK": "网络不可达或页面打不开 (重试后仍失败), 稍后轮询会自动补",
    "BROWSER": "浏览器/playwright 环境异常, 先运行 doctor 自检",
    "TIMEOUT": "整体时间预算耗尽, 任务超时上限可能太小",
    "CLIENT_ONLY": "活动仅支持豆包电脑版客户端参与, 当前通道被平台拦截",
    "NO_CREDENTIAL": "未找到可用凭据: 安装豆包电脑版并登录, 或运行 login 保存网页登录态",
}

NEEDS_ATTENTION_CODES = {"unknown", "AUTH_ERROR", "ERROR", "BROWSER", "TIMEOUT", "CLIENT_ONLY", "NO_CREDENTIAL"}

# 最近一次 emit 的结果 (供提权重跑子进程写结果文件用)
_LAST_RESULT: dict | None = None


def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _env_int(name: str, default: int, lo: int = 1, hi: int = 86400) -> int:
    """读整型环境变量; 解析失败/越界一律回落默认值, 绝不抛异常。

    教训: 曾有环境变量写成非数字导致进程崩在异常保护生效之前,
    结果一条日志都没留下, 当天记录整条丢失。
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, val))


BUDGET_SECONDS = _env_int("DOUBAO_BUDGET_SECONDS", 480, lo=60, hi=7200)
BUDGET_START = time.monotonic()


def budget_left() -> int:
    """剩余时间预算 (秒), 下限 0。"""
    return max(0, BUDGET_SECONDS - int(time.monotonic() - BUDGET_START))


def budget_exhausted() -> bool:
    return budget_left() <= 0


def rotate_log() -> None:
    """日志超过 512KB 时截半保留最新部分, 防长期轮询无限膨胀。失败静默。"""
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 512 * 1024:
            lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            LOG_FILE.write_text("".join(lines[len(lines) // 2:]), encoding="utf-8")
    except Exception:
        pass


def log(msg: str) -> None:
    """过程日志: 打印 + 落盘, 双通道, 保证不抛异常。"""
    line = f"[{now_str()}] {msg}"
    try:
        print(line)
    except Exception:
        pass  # pythonw 下 stdout 是 None, 打印会静默失败
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        rotate_log()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(title: str, body: str) -> None:
    """macOS 弹系统通知; 其他平台静默跳过 (结果以日志为准)。"""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{body}" with title "{title}"'],
            check=False, timeout=10,
        )
    except Exception:
        pass


def emit(out: dict, action: str) -> None:
    """结果输出: silent 类模式写日志文件, 其余打印到 stdout; 本函数保证不抛异常。

    判定用前缀而不是等号: silent-poll 同样是无窗口跑的, pythonw 下 sys.stdout
    是 None, print 会静默成功(不抛异常), 于是"打到 stdout"等于扔进黑洞——
    AUTH_ERROR、unknown 这类最需要人处理的结果会一条不剩地消失。

    非 silent 模式下 stdout 不可用(编码/管道问题)时退到日志, 至少不把结果丢掉。
    """
    out = dict(out)
    out.setdefault("needs_attention", out.get("result") in NEEDS_ATTENTION_CODES)
    global _LAST_RESULT
    _LAST_RESULT = out
    payload = json.dumps(out, ensure_ascii=False)
    # 明文结果文件: 覆盖写, 内容只有本次结果 (不含凭据), 供 AI 定时任务只读汇报
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        snapshot = dict(out)
        snapshot["saved_at"] = now_str()
        LAST_RESULT_FILE.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    if not str(action).startswith("silent"):
        try:
            print(payload)
            return
        except Exception:
            pass  # stdout 不可用时退到日志
    line = f"[{now_str()}] {payload}\n"
    custom = os.environ.get("DOUBAO_CHECKIN_LOG")
    for path in ([pathlib.Path(custom)] if custom else []) + [LOG_FILE]:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            rotate_log()
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            return
        except Exception:
            continue


def _already_signed_today() -> bool:
    """查今日日志里是否已有 success/already 结果。

    供静默模式秒退用: 当天签上后, 轮询再触发也只是空耗 (读凭据 + 启动浏览器
    + 打开页面要十几秒), 有这条守卫就在毫秒级结束。只认结果行
    (emit 写入的 JSON, 行首是当天日期), 过程日志 (凭据模式/打开页面) 不算数。
    """
    today = datetime.date.today().isoformat()
    paths = []
    custom = os.environ.get("DOUBAO_CHECKIN_LOG")
    if custom:
        paths.append(pathlib.Path(custom))
    paths.append(LOG_FILE)
    for path in paths:
        try:
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.startswith(f"[{today} "):
                    continue
                if '"result": "success"' in line or '"result": "already"' in line:
                    return True
        except Exception:
            continue
    return False


def goto_with_retry(page, url: str) -> bool:
    """导航 + 分桶退避重试。

    导航是只读操作 (页面本身幂等), 重试安全。退避序列 5/15/30 秒,
    专门覆盖「刚开机 WiFi 还没连上」的窗口; 受时间预算约束, 超预算即止。

    返回 True=页面已打开; False=预算内重试仍失败 (结果按 NETWORK 上报)。
    """
    backoffs = [5, 15, 30]
    for attempt in range(len(backoffs) + 1):
        if budget_exhausted():
            log("时间预算耗尽, 停止重试")
            return False
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return True
        except Exception as exc:
            kind = "超时" if "Timeout" in type(exc).__name__ else "网络错误"
            if attempt >= len(backoffs):
                log(f"导航失败 ({kind}), 重试额度已用完: {exc}")
                return False
            wait = min(backoffs[attempt], budget_left())
            log(f"导航失败 ({kind}), {wait}s 后第 {attempt + 1} 次重试")
            page.wait_for_timeout(wait * 1000)


def page_text(page) -> str:
    try:
        return page.inner_text("body")[:4000]
    except Exception:
        return ""


def find_and_click(page) -> bool:
    """按候选文案找签到按钮并点击, 返回是否点到。"""
    for text in CLICK_TEXTS:
        try:
            # 优先精确按钮, 再退到文本节点
            loc = page.get_by_role("button", name=text, exact=False)
            if loc.count() == 0:
                loc = page.get_by_text(text, exact=False)
            if loc.count() == 0:
                continue
            target = loc.first
            if target.is_visible():
                target.click(timeout=5000)
                log(f"已点击按钮: 「{text}」")
                return True
        except Exception:
            continue
    return False


def check_success(page) -> str:
    """判断签到结果: success / already / unknown

    「每日签到」任务的完成态判定 (实测): 任务卡片常驻, 未签时按钮区是
    「立即签到」, 已签后按钮消失变成对勾图标——所以「每日签到」在而
    「立即签到」不在即是已签, 这是比通用文案更可靠的幂等判据。
    """
    body = page_text(page)
    if "每日签到" in body and "立即签到" not in body:
        return "already"
    for t in ALREADY_TEXTS:
        if t in body:
            return "already"
    for t in SUCCESS_TEXTS:
        if t in body:
            return "success"
    return "unknown"


# ---------------------------------------------------------------------------
# 客户端凭据模式 (macOS): 钥匙串密钥 -> AES 解密客户端 Cookie 库
# ---------------------------------------------------------------------------

def _client_keychain_key() -> bytes | None:
    """从 macOS 钥匙串取豆包客户端的 Cookie 加密密钥。失败返回 None。

    只读取出密钥内容用于内存解密, 不写入任何文件。
    首次在终端使用时 macOS 可能弹一次钥匙串授权框, 选「始终允许」后不再弹。
    """
    if sys.platform != "darwin":
        return None
    for service in KEYCHAIN_SERVICES:
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-w", "-s", service],
                capture_output=True, timeout=15,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            continue
    return None


def _decrypt_client_cookie(blob: bytes, key_material: bytes) -> str | None:
    """解密豆包客户端 (macOS, 定制 Chromium) 的 v10 Cookie 密文, 返回明文文本。

    算法: PBKDF2-SHA1(salt='saltysalt', 1003 轮, 128 位) -> AES-128-CBC(IV=16 空格)
    -> PKCS7 去填充 -> 明文头部有 32 字节校验块 (豆包定制, 原版 Chromium 没有),
    跳过即得真实值。任何一步失败返回 None。
    """
    if len(blob) < 3 + 48 or blob[:3] not in (b"v10", b"v11"):
        return None
    try:
        from Crypto.Cipher import AES  # pycryptodome
    except Exception:
        return None
    try:
        aes_key = hashlib.pbkdf2_hmac("sha1", key_material, b"saltysalt", 1003, dklen=16)
        pt = AES.new(aes_key, AES.MODE_CBC, b" " * 16).decrypt(blob[3:])
        pad = pt[-1]
        if 1 <= pad <= 16 and pt[-pad:] == bytes([pad]) * pad:
            pt = pt[:-pad]
        value = pt[32:]  # 跳过头部 32 字节校验块
        if not value:
            return None
        return value.decode("utf-8", errors="replace")
    except Exception:
        return None


def _windows_dpapi_unprotect(data: bytes) -> bytes:
    """Windows DPAPI CryptUnprotectData (当前用户作用域), ctypes 实现, 免 pywin32。"""
    import ctypes
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _windows_client_roots() -> list[pathlib.Path]:
    """定位 Windows 豆包客户端的 Chromium 用户数据目录 (以含 Local State 为准)。"""
    out = []
    la = os.environ.get("LOCALAPPDATA")
    base = pathlib.Path(la) if la else None
    if not base or not base.exists():
        return out
    for p in sorted(base.iterdir()):
        if not (p.is_dir() and "doubao" in p.name.lower()):
            continue
        # 兼容两种布局: 数据直接在根目录 / 标准 Chromium 的 User Data 子目录
        for cand in (p, p / "User Data"):
            if (cand / "Local State").is_file():
                out.append(cand)
                break
    return out


def _windows_os_crypt_key(root: pathlib.Path) -> bytes | None:
    """从 Local State 取 Windows Chromium 的 Cookie 主密钥 (DPAPI 解包)。"""
    try:
        data = json.loads((root / "Local State").read_text(encoding="utf-8"))
        enc = base64.b64decode(data["os_crypt"]["encrypted_key"])
        if enc[:5] != b"DPAPI":
            return None
        return _windows_dpapi_unprotect(enc[5:])
    except Exception:
        return None


def _decrypt_cookie_windows(blob: bytes, key: bytes) -> str | None:
    """解密 Windows Chromium 格式 Cookie (v10 = AES-256-GCM), 返回明文文本。

    原版明文直接可用; 若豆包 Windows 版沿用了 mac 端的 32 字节校验头定制,
    标准解出会不可读, 此时尝试跳头重解。两路都以 UTF-8 可解码为准,
    防止把密文残渣当值。v20 (App-Bound Encryption) 与解密失败返回 None。
    """
    if blob[:3] not in (b"v10", b"v11"):
        return None
    if len(blob) < 3 + 12 + 16 + 16:
        return None
    try:
        from Crypto.Cipher import AES  # pycryptodome
    except Exception:
        return None
    data = blob[3:]
    try:
        cipher = AES.new(key, AES.MODE_GCM, nonce=data[:12])
        pt = cipher.decrypt(data[12:])[:-16]  # 末 16 字节是 GCM tag
    except Exception:
        return None
    for cand in (pt, pt[32:]):
        if not cand:
            continue
        try:
            return cand.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def _cache_protect(data: bytes) -> bytes | None:
    """把缓存字节加密到「本机当前用户」可解: Windows 用 DPAPI, macOS 用钥匙串密钥派生 AES-256-CBC。"""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class _BLOB(ctypes.Structure):
                _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

            buf = ctypes.create_string_buffer(data, len(data))
            blob_in = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
            blob_out = _BLOB()
            ok = ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(blob_in), "doubao-checkin-cache", None, None, None, 0, ctypes.byref(blob_out))
            if not ok:
                return None
            try:
                return ctypes.string_at(blob_out.pbData, blob_out.cbData)
            finally:
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        if sys.platform == "darwin":
            key_material = _client_keychain_key()
            if not key_material:
                return None
            from Crypto.Cipher import AES
            km = key_material if isinstance(key_material, bytes) else key_material.encode("utf-8")
            aes_key = hashlib.sha256(b"cache:" + km).digest()
            iv = os.urandom(16)
            pad = 16 - len(data) % 16
            ct = AES.new(aes_key, AES.MODE_CBC, iv).encrypt(data + bytes([pad]) * pad)
            return b"DCC1" + iv + ct
    except Exception:
        return None
    return None


def _cache_unprotect(data: bytes) -> bytes | None:
    """解密 _cache_protect 的产物; 任何失败返回 None。"""
    try:
        if sys.platform == "win32":
            return _windows_dpapi_unprotect(data)
        if sys.platform == "darwin":
            if len(data) < 36 or data[:4] != b"DCC1":
                return None
            key_material = _client_keychain_key()
            if not key_material:
                return None
            from Crypto.Cipher import AES
            km = key_material if isinstance(key_material, bytes) else key_material.encode("utf-8")
            aes_key = hashlib.sha256(b"cache:" + km).digest()
            iv, ct = data[4:20], data[20:]
            pt = AES.new(aes_key, AES.MODE_CBC, iv).decrypt(ct)
            pad = pt[-1]
            if not (1 <= pad <= 16) or pt[-pad:] != bytes([pad]) * pad:
                return None
            return pt[:-pad]
    except Exception:
        return None
    return None


def _save_cookie_cache(cookies: list[dict]) -> None:
    """客户端凭据成功读取后把解密结果加密落盘; 失败静默, 不影响主流程。"""
    try:
        blob = json.dumps({"v": 2, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), "cookies": cookies},
                          ensure_ascii=False).encode("utf-8")
        enc = _cache_protect(blob)
        if enc:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_bytes(enc)
    except Exception:
        pass


def _load_cookie_cache() -> tuple[list[dict], str]:
    """读取加密缓存并过滤已过期 Cookie; 不可用返回 ([], 原因)。"""
    try:
        if not CACHE_FILE.is_file():
            return [], "无凭据缓存"
        raw = _cache_unprotect(CACHE_FILE.read_bytes())
        if not raw:
            return [], "凭据缓存解密失败 (可能更换了用户或机器)"
        obj = json.loads(raw.decode("utf-8"))
        if obj.get("v") != 2:
            # v1 缓存是多 Profile 合并的数据, 可能包含非主账号的 Cookie, 不能再用;
            # 下次活读成功后会以 v2 格式自动重建。
            return [], "凭据缓存为旧版格式 (已作废), 等下次成功读取后自动重建"
        now = time.time()
        cookies = [c for c in obj.get("cookies", [])
                   if c.get("expires", -1) == -1 or c.get("expires", 0) > now]
        if not cookies:
            return [], "凭据缓存已全部过期"
        return cookies, (f"来自本机加密缓存 (缓存于 {obj.get('saved_at', '?')}; "
                         "客户端 Cookie 库当前不可读, 下次成功读取后自动刷新)")
    except Exception as exc:
        return [], f"凭据缓存读取失败: {exc}"


def _windows_esentutl_copy(db: pathlib.Path, tmp: pathlib.Path) -> bool:
    """esentutl 备份语义拷贝被锁定的 Cookie 库 (系统自带, 不引第三方依赖)。

    用绝对路径调用: 部分机器系统 PATH 被精简, 裸命令名会找不到 (System32 必在)。
    """
    esentutl = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "esentutl.exe"
    try:
        r = subprocess.run(
            [str(esentutl) if esentutl.is_file() else "esentutl",
             "/y", str(db), "/d", str(tmp), "/o"],
            capture_output=True, timeout=60,
        )
    except Exception:
        return False
    return r.returncode == 0 and tmp.is_file()


def _sqlite_cookie_snapshot(db: pathlib.Path, tmp: pathlib.Path, timeout: float = 2) -> bool:
    """以 SQLite 只读备份 API 获取一致快照，包含尚未 checkpoint 的 WAL 数据。"""
    import sqlite3
    from contextlib import closing
    deadline = time.monotonic() + timeout
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("Cookie 数据库仍被锁定")
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=.5)) as source:
            with closing(sqlite3.connect(str(tmp), timeout=.5)) as destination:
                source.backup(destination, pages=64, progress=progress, sleep=.05)
        return tmp.is_file()
    except (OSError, sqlite3.Error, TimeoutError):
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _windows_shared_copy(db: pathlib.Path, tmp: pathlib.Path) -> tuple[bool, str]:
    """用 Win32 共享读句柄复制；尊重独占锁，不创建卷影、不终止客户端。"""
    if sys.platform != "win32":
        return False, "共享读取未尝试 (非 Windows)"
    import ctypes
    from ctypes import wintypes
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.ReadFile.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(db), 0x80000000, 0x7, None, 3, 0x08000000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            code = ctypes.get_last_error()
            return False, f"共享读取不可用 (Windows 错误 {code}); 文件可能被客户端独占锁定"
        try:
            buffer = ctypes.create_string_buffer(1024 * 1024)
            with tmp.open("wb") as destination:
                while True:
                    read = wintypes.DWORD()
                    if not kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(read), None):
                        return False, f"共享读取失败 (Windows 错误 {ctypes.get_last_error()})"
                    if not read.value:
                        break
                    destination.write(buffer.raw[:read.value])
            return True, "Win32 共享读取成功"
        finally:
            kernel.CloseHandle(handle)
    except Exception as exc:
        # 不把异常原文、路径或任何凭据内容透传到日志。
        return False, f"共享读取不可用 ({type(exc).__name__})"


def _copy_cookie_database(db: pathlib.Path, tmp: pathlib.Path) -> tuple[bool, str]:
    """按只读 SQLite、普通复制、共享句柄、系统备份依次尝试。"""
    import shutil
    if _sqlite_cookie_snapshot(db, tmp):
        return True, "SQLite 只读快照成功"
    try:
        shutil.copy(db, tmp)
        copied = True
    except OSError:
        copied = False
    note = ""
    if not copied and sys.platform == "win32":
        copied, note = _windows_shared_copy(db, tmp)
    if not copied and sys.platform == "win32":
        copied = _windows_esentutl_copy(db, tmp)
    if not copied:
        return False, (note + "; " if note else "") + "未创建 VSS 卷影; 请退出豆包后重试，或使用本机加密缓存"
    # 普通文件复制不能遗漏 WAL；读取不到时宁可回落缓存，避免使用过时或不完整的库。
    wal = pathlib.Path(str(db) + "-wal")
    if wal.is_file():
        try:
            shutil.copy(wal, pathlib.Path(str(tmp) + "-wal"))
        except OSError:
            if sys.platform != "win32" or not _windows_shared_copy(wal, pathlib.Path(str(tmp) + "-wal"))[0]:
                return False, "Cookie WAL 文件仍被锁定; 请退出豆包后重试，或使用本机加密缓存"
    return True, "Cookie 数据库只读复制成功"


def read_client_cookies() -> tuple[list[dict], str]:
    """客户端凭据读取入口: 先活读客户端库, 失败回落本机加密缓存。"""
    cookies, reason = _read_client_cookies_live()
    if cookies:
        _save_cookie_cache(cookies)
        return cookies, reason
    cached, cache_note = _load_cookie_cache()
    if cached:
        return cached, cache_note
    return [], reason


def _read_client_cookies_live() -> tuple[list[dict], str]:
    """读取豆包电脑版客户端的登录 Cookie (macOS / Windows 均已实测)。

    返回 (cookies, reason): cookies 为空列表时 reason 说明原因。
    只读取, 解密在内存完成, Cookie 值不打印、不落盘。
    """
    import shutil
    import sqlite3
    import tempfile

    if sys.platform not in ("darwin", "win32"):
        return [], "客户端凭据模式目前仅支持 macOS / Windows (其他平台仍走 login 模式)"
    try:
        from Crypto.Cipher import AES  # noqa: F401  探测依赖, 缺了给出安装提示
    except Exception:
        return [], "缺少 pycryptodome, 安装: python -m pip install pycryptodome"

    # 收集 (Profile 标识, Cookie 库路径, 解密函数) 任务列表。
    # 多账号机器上各 Profile 各有一个账号, 同名 Cookie 合并注入会互相覆盖、签错账号——
    # 因此只取「第一个有登录态的 Profile」。Default (主账号所在) 按字典序排在
    # Profile 1 等之前, 顺序天然正确; 可用环境变量 DOUBAO_PROFILE 强制指定。
    jobs: list[tuple[str, pathlib.Path, object]] = []
    notes: list[str] = []
    if sys.platform == "darwin":
        if not CLIENT_DATA_DIR.exists():
            return [], f"未找到豆包客户端数据目录: {CLIENT_DATA_DIR}"
        key = _client_keychain_key()
        if not key:
            return [], ("钥匙串取密钥失败 (需安装豆包电脑版并登录过; "
                        "终端首次使用请在弹窗里选「始终允许」)")
        for profile in sorted(CLIENT_DATA_DIR.glob("*")):
            db = profile / "Cookies"
            if db.is_file():
                jobs.append((profile.name, db, lambda blob, k=key: _decrypt_client_cookie(blob, k)))
        if not jobs:
            return [], "客户端目录里没有可读的 Cookie 库 (客户端可能未登录或已清空数据)"
    else:  # win32
        roots = _windows_client_roots()
        if not roots:
            return [], "未在 %LOCALAPPDATA% 下找到豆包客户端的用户数据目录 (确认已安装并登录过客户端)"
        for root in roots:
            key = _windows_os_crypt_key(root)
            if not key:
                notes.append(f"{root.name}: Local State 密钥解密失败")
                continue
            for db in sorted(root.glob("*/Cookies")) + sorted(root.glob("*/Network/Cookies")):
                label = db.parent.name if db.parent.name != "Network" else db.parent.parent.name
                jobs.append((label, db, lambda blob, k=key: _decrypt_cookie_windows(blob, k)))
        if not jobs:
            return [], ("Windows 客户端凭据不可用: " + ("; ".join(notes) or "无可读 Cookie 库")
                        + " (若客户端为 v20/App-Bound 加密则暂不支持, 请用 login 模式)")

    forced = os.environ.get("DOUBAO_PROFILE", "").strip()
    if forced:
        matched = [j for j in jobs if j[0].lower() == forced.lower()]
        if matched:
            jobs = matched
            notes.append(f"已按 DOUBAO_PROFILE 指定 Profile「{forced}」")
        else:
            names = ", ".join(sorted({j[0] for j in jobs}))
            notes.append(f"DOUBAO_PROFILE={forced} 未匹配 (可用: {names}), 按默认顺序选取")

    cookies: list[dict] = []
    used_profile: str | None = None
    for label, db, decrypt in jobs:
        if used_profile is not None:
            break  # 单账号模式: 第一个有登录态的 Profile 生效, 其余一律不碰
        work = None
        try:
            work = pathlib.Path(tempfile.mkdtemp(prefix="doubao-ck-"))
            tmp = work / "Cookies"
            copied, copy_note = _copy_cookie_database(db, tmp)
            if not copied:
                notes.append(f"{label}: {copy_note}")
                continue
            con = sqlite3.connect(str(tmp))
            try:
                rows = con.execute(
                    "SELECT host_key, name, encrypted_value, path, is_secure, is_httponly, expires_utc "
                    "FROM cookies WHERE host_key LIKE '%doubao%'"
                ).fetchall()
            finally:
                con.close()  # Windows 上不先关连接, unlink 会报 WinError 32 并吞掉整库数据
            tmp.unlink(missing_ok=True)
        except Exception:
            continue
        finally:
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)
        profile_cookies: list[dict] = []
        for host, name, blob, path, secure, httponly, exp in rows:
            value = decrypt(blob)
            if not value:
                continue
            expires = -1  # -1 = 会话 cookie
            if exp and exp > 0:
                expires = int(exp / 1_000_000 - 11644473600)  # chromium 纪元 -> unix
                if expires < 0:
                    expires = -1
            profile_cookies.append({
                "name": name, "value": value, "domain": host, "path": path or "/",
                "secure": bool(secure), "httpOnly": bool(httponly), "expires": expires,
            })
        if profile_cookies:
            cookies = profile_cookies
            used_profile = label
    if not cookies:
        base = "客户端 Cookie 解密后为空 (客户端可能未登录或已清空数据)"
        if notes:
            base += "; " + "; ".join(notes)
        return [], base
    prof = f"; 单账号 Profile「{used_profile}」" if used_profile else ""
    return cookies, f"解密成功 {len(cookies)} 条{prof}"


def _cookie_header(cookies: list[dict]) -> str:
    """Build an HTTP Cookie header from the locally decrypted client jar."""
    parts = []
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "").strip()
        domain = str(cookie.get("domain") or "").lower()
        if name and value and "doubao.com" in domain:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _json_response(raw: bytes) -> object:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return {"_text": raw[:500].decode("utf-8", "replace")}


def _doubao_post(path: str, payload: dict | None, cookie: str) -> tuple[int, object]:
    """Call a documented Doubao client API endpoint without a browser."""
    url = API_BASE.rstrip("/") + path
    headers = {
        "User-Agent": CLIENT_UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": API_BASE,
        "Referer": ACTIVITY_URL,
        "Cookie": cookie,
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if payload is not None else b""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
            return response.status, _json_response(response.read())
    except urllib.error.HTTPError as exc:
        try:
            payload = _json_response(exc.read())
        except OSError:
            payload = {}
        return exc.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return 0, {"_error": str(reason)[:300]}


def _response_data(response: object) -> object:
    return response.get("data") if isinstance(response, dict) else None


def _response_error(response: object) -> str:
    if not isinstance(response, dict):
        return "响应格式不是 JSON 对象"
    return str(response.get("msg") or response.get("message") or response.get("_error") or "")[:300]


def _response_ok(status: int, response: object) -> bool:
    if not 200 <= status < 300 or not isinstance(response, dict):
        return False
    code = response.get("code")
    return code is None or str(code) in ("0", "200")


def _campaign_group_code(node: object) -> str:
    """Read the campaign scene used by the bundled agent-explore check-in flow."""
    code_keys = {"campaign_group_code", "campaigngroupcode"}
    if isinstance(node, dict):
        for key, value in node.items():
            normalized = str(key).replace("_", "").lower()
            if normalized in code_keys and isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
            if normalized == "ug_campaign_info" and isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except ValueError:
                    decoded = value
                found = _campaign_group_code(decoded)
                if found:
                    return found
            found = _campaign_group_code(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _campaign_group_code(value)
            if found:
                return found
    return ""


def _is_explicitly_checked_in(node: object) -> bool:
    """Only honor explicit server check-in flags; unrelated campaign status is ignored."""
    if isinstance(node, dict):
        for key in ("checked_in", "is_checked_in", "signed_in", "already_checked_in"):
            value = node.get(key)
            if value is True or (isinstance(value, (int, str)) and str(value).lower() in ("1", "true", "yes")):
                return True
        return any(_is_explicitly_checked_in(value) for value in node.values())
    if isinstance(node, list):
        return any(_is_explicitly_checked_in(value) for value in node)
    return False


def _already_message(message: str) -> bool:
    lower = message.lower()
    return any(hint.lower() in lower for hint in ("已签到", "已经签到", "今日已领", "已领取", "already", "checked in"))


def checkin_http(dry: bool = False) -> dict:
    """Sign in through Doubao HTTP APIs using locally decrypted desktop cookies.

    API contract was checked against the installed Doubao web bundle: basic launch
    provides ``ug_campaign_info.campaign_group_code``; landing info and check-in
    are POST requests whose ``scene`` is that campaign group code. The check-in
    response's ``data.checked_in`` is the success signal used by Doubao itself.
    """
    try:
        cookies, credential_note = read_client_cookies()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "failed", "message": f"读取豆包客户端凭据失败：{type(exc).__name__}：{exc}"}
    cookie = _cookie_header(cookies)
    if not cookie:
        return {
            "ok": False,
            "status": "failed",
            "message": f"没有可用于 HTTP 签到的豆包客户端 Cookie：{credential_note}",
        }
    if dry:
        return {"ok": True, "status": "skipped", "message": "预览：读取本机凭据并通过豆包 HTTP 接口查询签到活动后领取"}

    status, basic = _doubao_post("/alice/basic/launch", None, cookie)
    if status == 401 or status == 403:
        return {"ok": False, "status": "failed", "message": f"豆包登录态被拒绝（HTTP {status}），请在客户端重新登录"}
    if not _response_ok(status, basic):
        message = _response_error(basic) or (f"HTTP {status}" if status else "网络连接失败")
        return {"ok": False, "status": "failed", "message": f"读取豆包签到活动配置失败：{message}"}

    code = _campaign_group_code(_response_data(basic))
    if not code:
        return {"ok": False, "status": "failed", "message": "豆包接口未返回 ug_campaign_info.campaign_group_code，暂时无法确定签到活动"}

    # Match the client flow's read-only landing query before submitting its
    # DailyCheckIn operation; this also lets an explicit server state short-circuit.
    status, landing = _doubao_post("/alice/ug/campaign/landing/get", {"scene": code}, cookie)
    if status == 401 or status == 403:
        return {"ok": False, "status": "failed", "message": f"豆包活动接口拒绝登录态（HTTP {status}）"}
    if not _response_ok(status, landing):
        message = _response_error(landing) or (f"HTTP {status}" if status else "网络连接失败")
        return {"ok": False, "status": "failed", "message": f"读取豆包签到状态失败：{message}"}
    if _is_explicitly_checked_in(_response_data(landing)):
        return {"ok": True, "status": "already", "message": "今日已签到"}

    status, result = _doubao_post("/alice/ug/campaign/landing/checkin", {"scene": code}, cookie)
    message = _response_error(result)
    if status == 401 or status == 403:
        return {"ok": False, "status": "failed", "message": f"豆包签到接口拒绝登录态（HTTP {status}），请在客户端重新登录"}
    if _already_message(message):
        return {"ok": True, "status": "already", "message": message or "今日已签到"}
    if not _response_ok(status, result):
        message = message or (f"HTTP {status}" if status else "网络连接失败")
        return {"ok": False, "status": "failed", "message": f"豆包签到请求失败：{message}"}

    data = _response_data(result)
    if isinstance(data, dict) and (data.get("checked_in") is True or str(data.get("checked_in")).lower() in ("1", "true", "yes")):
        return {"ok": True, "status": "success", "message": "豆包签到成功"}
    if _already_message(message):
        return {"ok": True, "status": "already", "message": message}
    return {"ok": False, "status": "failed", "message": message or "豆包未确认签到成功（缺少 data.checked_in=true）"}


def checkin(start_url: str, action: str) -> int:
    """Compatibility entry point; all automated sign-in work uses direct HTTP."""
    result = checkin_http()
    code = {"success": "success", "already": "already", "skipped": "skipped"}.get(result.get("status"), "ERROR")
    emit({"result": code, "report": result.get("message") or "签到结果未知",
          "needs_attention": not bool(result.get("ok"))}, action)
    return 0 if result.get("ok") else 1


def login() -> int:
    """Interactive browser login is intentionally unsupported by this module."""
    emit({"result": "NO_CREDENTIAL", "report": "豆包签到仅使用客户端本地凭据；请先在豆包电脑版登录后重试",
          "needs_attention": True}, "login")
    return 1


def doctor() -> int:
    """Offline credential and data-directory check; never opens a browser."""
    cookies, reason = read_client_cookies()
    checks = [{"check": "client-credential", "ok": bool(_cookie_header(cookies)), "detail": reason}]
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append({"check": "data-writable", "ok": True, "detail": f"{DATA_DIR} 可写"})
    except Exception as exc:  # noqa: BLE001
        checks.append({"check": "data-writable", "ok": False, "detail": f"不可写：{exc}"})
    ok = all(item["ok"] for item in checks)
    out = {"result": "ok" if ok else "FAIL", "report": "离线自检通过" if ok else "自检发现问题，见 checks",
           "credential_mode": "client" if checks[0]["ok"] else "none",
           "needs_attention": not ok, "checks": checks}
    emit(out, "doctor")
    for check in checks:
        print(f"  {'✓' if check['ok'] else '✗'} {check['check']}: {check['detail']}")
    return 0 if ok else 1


def main() -> int:
    # argparse 之前不放任何可能抛异常的逻辑; 之后所有路径都收敛到受保护的分支
    ap = argparse.ArgumentParser(description="豆包自动签到")
    ap.add_argument(
        "action",
        choices=["login", "run", "silent", "silent-poll", "doctor"],
        help="run=签到(stdout), silent=签到(写日志), silent-poll=轮询补签(空跑不落盘), "
             "doctor=离线自检, login=手动登录(备用)",
    )
    ap.add_argument("--url", default=ACTIVITY_URL, help="签到活动页直链 (默认 agent-explore)")
    ap.add_argument("--elevated-child", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.action == "login":
        return login()
    if args.action == "doctor":
        try:
            return doctor()
        except Exception as exc:
            log(f"未预期异常: {type(exc).__name__}: {exc}")
            emit({"result": "ERROR", "report": f"运行异常: {type(exc).__name__}: {exc}",
                  "needs_attention": True}, "doctor")
            return 1
    try:
        if args.elevated_child:
            # 提权重跑子进程: 无窗口跑 silent, 结果写回回传文件给原进程转述
            try:
                return checkin(args.url, "silent")
            finally:
                try:
                    DATA_DIR.mkdir(parents=True, exist_ok=True)
                    payload = _LAST_RESULT or {
                        "result": "ERROR", "report": "提权重跑未产生结果",
                        "needs_attention": True}
                    ELEVATED_RESULT_FILE.write_text(
                        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
        return checkin(args.url, args.action)
    except Exception as exc:
        # 兜底: 任何未预期异常也要留痕, 不能无声消失
        log(f"未预期异常: {type(exc).__name__}: {exc}")
        emit({"result": "ERROR", "report": f"运行异常: {type(exc).__name__}: {exc}",
              "needs_attention": True}, args.action)
        return 1


if __name__ == "__main__":
    sys.exit(main())
