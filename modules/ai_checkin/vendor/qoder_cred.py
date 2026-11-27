#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qoder 桌面端登录态读取（DPAPI + AES-GCM），供 checkin.py 在缺少 PAT 时兜底。

改编自 spirodelazz/ide-daily-checkin 的 scripts/qoder_extract.py（MIT），
上游又改编自 wallechfox/qoder-checkin。相对上游的改动：

  · 安装目录改为自动探测（Local\\Programs\\Qoder、注册表、Program Files、D:\\Qoder CN）
  · 不再写 qoder_config.json / qoder_accounts.json，改为项目内缓存 .qoder_session.json
  · 增加 refresh()（/api/v1/deviceToken/refresh），token 临期自动换新
  · DPAPI 必须在「登录 Qoder 的同一 Windows 用户」下运行，否则解不开

用法：
    python qoder_cred.py           # 查看状态（不打印令牌）
    python qoder_cred.py --json    # 输出完整凭据（含明文，勿外传）
    python qoder_cred.py --refresh # 强制用 refreshToken 换新
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import glob
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# 主项目适配：缓存文件可用 QODER_SESSION_FILE 覆盖（默认放模块旁）
CACHE_FILE = Path(os.environ.get("QODER_SESSION_FILE") or (BASE_DIR / ".qoder_session.json"))
UA = "Qoder/claim"
REFRESH_PATH = "/api/v1/deviceToken/refresh"

DEVICE_KEYS = (
    ("clientType", "Cosy-ClientType", "QODER_COSY_CLIENT_TYPE", "10"),
    ("machineOS", "Cosy-MachineOS", "QODER_COSY_MACHINE_OS", ""),
    ("machineHostname", "Cosy-MachineHostname", "QODER_COSY_MACHINE_HOSTNAME", ""),
    ("machineId", "Cosy-MachineId", "QODER_COSY_MACHINE_ID", ""),
    ("machineToken", "Cosy-MachineToken", "QODER_COSY_MACHINE_TOKEN", ""),
    ("machineCode", "Cosy-MachineCode", "QODER_COSY_MACHINE_CODE", ""),
    ("machineType", "Cosy-MachineType", "QODER_COSY_MACHINE_TYPE", ""),
    ("version", "Cosy-Version", "QODER_COSY_VERSION", ""),
)


# ── DPAPI / AES-GCM 解密（纯标准库）───────────────────────
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


class _AeadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.ULONG), ("dwInfoVersion", wt.ULONG),
        ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)), ("cbNonce", wt.ULONG),
        ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)), ("cbAuthData", wt.ULONG),
        ("pbTag", ctypes.POINTER(ctypes.c_ubyte)), ("cbTag", wt.ULONG),
        ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)), ("cbMacContext", wt.ULONG),
        ("cbAAD", wt.ULONG), ("cbData", ctypes.c_ulonglong), ("dwFlags", wt.ULONG),
    ]


def _dpapi_unprotect(data: bytes) -> bytes:
    src = _DataBlob(len(data), ctypes.create_string_buffer(data, len(data)))
    dst = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(src), None, None, None, None, 0x01, ctypes.byref(dst)):
        raise OSError("DPAPI 解不开（%s）——需用登录 Qoder 的同一 Windows 用户运行"
                      % ctypes.WinError())
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def _decrypt_gcm_cng(key: bytes, nonce: bytes, blob: bytes) -> bytes:
    bcrypt = ctypes.WinDLL("bcrypt.dll")

    def chk(res, what):
        if res != 0:
            raise OSError("CNG %s 失败 NTSTATUS=0x%08x" % (what, res & 0xFFFFFFFF))

    ct, tag = blob[:-16], blob[-16:]
    h_alg = ctypes.c_void_p()
    chk(bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0), "OpenAlg")
    mode = "ChainingModeGCM"
    chk(bcrypt.BCryptSetProperty(h_alg, "ChainingMode", mode, (len(mode) + 1) * 2, 0), "SetProperty")
    h_key = ctypes.c_void_p()
    kbuf = ctypes.create_string_buffer(key, len(key))
    chk(bcrypt.BCryptGenerateSymmetricKey(h_alg, ctypes.byref(h_key), None, 0, kbuf, len(key), 0), "GenKey")
    nbuf = (ctypes.c_ubyte * len(nonce)).from_buffer_copy(nonce)
    tbuf = (ctypes.c_ubyte * len(tag)).from_buffer_copy(tag)
    info = _AeadInfo()
    info.cbSize = ctypes.sizeof(_AeadInfo)
    info.dwInfoVersion = 1
    info.pbNonce = ctypes.cast(nbuf, ctypes.POINTER(ctypes.c_ubyte))
    info.cbNonce = len(nonce)
    info.pbTag = ctypes.cast(tbuf, ctypes.POINTER(ctypes.c_ubyte))
    info.cbTag = len(tag)
    ibuf = ctypes.create_string_buffer(ct, len(ct))
    obuf = ctypes.create_string_buffer(len(ct))
    done = ctypes.c_ulong()
    chk(bcrypt.BCryptDecrypt(h_key, ibuf, len(ct), ctypes.byref(info), None, 0,
                             obuf, len(ct), ctypes.byref(done), 0), "Decrypt")
    return obuf.raw[:done.value]


def _decrypt_gcm(key: bytes, nonce: bytes, blob: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return _decrypt_gcm_cng(key, nonce, blob)
    return AESGCM(key).decrypt(nonce, blob, None)


def coerce_exp(value) -> int:
    """expiresAt → epoch 秒（ISO 字符串 / 秒 / 毫秒都兼容）。"""
    if value in (None, "", 0, "0") or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        n = float(value)
        return int(n / 1000) if n > 1e12 else int(n)
    text = str(value).strip()
    if text.isdigit():
        n = float(text)
        return int(n / 1000) if n > 1e12 else int(n)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return 0


# ── 客户端目录探测 ────────────────────────────────────────
def appdata_dirs() -> list[str]:
    """客户端数据目录：国际版 com.qoder.app.* / 国内版 com.qodercn.app.* 都找。"""
    env = os.environ.get("QODER_AUTH_DIR", "").strip()
    if env:
        return [env]
    appdata = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    found = []
    for pattern in ("com.qoder.app.*", "com.qodercn.app.*"):
        found += sorted(glob.glob(os.path.join(appdata, pattern)), key=os.path.getmtime, reverse=True)
    seen, result = set(), []
    for path in found:
        key = path.lower()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _reg_roots() -> list[str]:
    import winreg

    roots = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall")
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(key)[0]
        except OSError:
            winreg.CloseKey(key)
            continue
        for index in range(count):
            try:
                with winreg.OpenKey(key, winreg.EnumKey(key, index)) as sub:
                    name = str(winreg.QueryValueEx(sub, "DisplayName")[0]).lower()
                    if "qoder" in name:
                        loc = str(winreg.QueryValueEx(sub, "InstallLocation")[0]).strip()
                        if loc:
                            roots.append(os.path.abspath(loc))
            except OSError:
                continue
        winreg.CloseKey(key)
    return roots


def _install_roots() -> list[str]:
    local = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    roots = [
        os.path.join(local, "Programs", "Qoder"),
        os.path.join(local, "Programs", "Qoder CN"),
        os.path.join(pf, "Qoder"),
        os.path.join(pf, "Qoder CN"),
        os.path.join(pf86, "Qoder"),
        os.path.join(pf86, "Qoder CN"),
        r"D:\Qoder CN",
        r"D:\Qoder",
    ]
    for vers in glob.glob(os.path.join(r"D:\Qoder CN", ".qoder-versions", "*", "resources", "umid",
                                       "runtime-info.exe")):
        roots.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(vers))))
    return roots + _reg_roots()


def candidate_umid_exes() -> list[str]:
    """runtime-info.exe 候选：用客户端自带工具生成设备标识。"""
    env = os.environ.get("QODER_UMID_EXE", "").strip()
    if env:
        return [env]
    out, seen = [], set()
    for root in _install_roots():
        path = os.path.join(root, "resources", "umid", "runtime-info.exe")
        key = path.lower()
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def _run_umid(exe: str) -> dict:
    try:
        proc = subprocess.run([exe, "--account-stdin"], input=b"", capture_output=True,
                              timeout=40, cwd=os.path.dirname(exe))
        lines = [line for line in proc.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
        return json.loads(lines[-1]) if lines else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def _arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(
        machine, "x86_64")


def device() -> tuple[dict, str]:
    exe = next((p for p in candidate_umid_exes() if os.path.isfile(p)), "")
    info = _run_umid(exe) if exe else {}
    dev = {
        "clientType": "10",
        "machineOS": _arch() + "_windows",
        "machineHostname": socket.gethostname(),
        "machineId": "",
        "machineToken": info.get("machineToken") or "",
        "machineCode": info.get("machineCode") or "",
        "machineType": info.get("machineType") or "",
        "version": "",
    }
    for root in appdata_dirs():
        try:
            value = Path(root, "auth.machine-id").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            dev["machineId"] = value
            break
    if exe:
        manifest = os.path.join(os.path.dirname(os.path.dirname(exe)), "build-manifest.json")
        try:
            dev["version"] = str(json.loads(Path(manifest).read_text(encoding="utf-8"))
                                 .get("productVersion") or "")
        except (OSError, ValueError):
            pass
    return dev, exe


def device_headers(dev: dict | None = None) -> dict[str, str]:
    dev = dev if dev is not None else (read_cache().get("device") or {})
    headers: dict[str, str] = {}
    for cfg_key, header, env, default in DEVICE_KEYS:
        value = os.getenv(env, "").strip() or str(dev.get(cfg_key) or "").strip() or str(default or "")
        if value:
            headers[header] = value
    headers.setdefault("Cosy-ClientType", "10")
    return headers


# ── 登录态读取 ────────────────────────────────────────────
def _read_store(root: str) -> dict | None:
    raw_path = os.path.join(root, "auth.v1.dat")
    state_path = os.path.join(root, "Local State")
    if not (os.path.isfile(raw_path) and os.path.isfile(state_path)):
        return None
    raw = Path(raw_path).read_bytes()
    if raw[:3] != b"v10":
        return None
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    key_blob = base64.b64decode(state["os_crypt"]["encrypted_key"])
    plain = _decrypt_gcm(_dpapi_unprotect(key_blob[5:]), raw[3:15], raw[15:])
    session = json.loads(plain.decode("utf-8"))
    if not session.get("token"):
        return None
    return session


def extract() -> dict | None:
    """直接从客户端 auth.v1.dat 读出 token（不依赖缓存）。"""
    for root in appdata_dirs():
        try:
            session = _read_store(root)
        except Exception:  # noqa: BLE001 - 单个目录坏了继续找下一个
            continue
        if not session:
            continue
        token = {
            "accessToken": str(session.get("token") or ""),
            "refreshToken": str(session.get("refreshToken") or ""),
        }
        expires = coerce_exp(session.get("expiresAt"))
        if expires:
            token["expiresAt"] = expires
        return token
    return None


# ── 缓存 / 续期 ───────────────────────────────────────────
def read_cache() -> dict:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(data: dict) -> None:
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CACHE_FILE)


def refresh(base: str, refresh_token: str, dev: dict | None = None) -> dict | None:
    """用 refreshToken 换新 token（/api/v1/deviceToken/refresh）。失败返回 None。"""
    if not refresh_token:
        return None
    url = base.rstrip("/") + REFRESH_PATH
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": UA}
    headers.update(device_headers(dev))
    body = json.dumps({"refresh_token": refresh_token}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    token = (data.get("token") or data.get("accessToken") or data.get("access_token")
             or data.get("device_token"))
    if not token:
        return None
    return {
        "accessToken": str(token),
        "refreshToken": str(data.get("refreshToken") or data.get("refresh_token") or refresh_token),
        "expiresAt": coerce_exp(data.get("expires_at") or data.get("expiresAt")) or int(time.time()) + 7200,
    }


def _cache_key(base: str) -> str:
    return base.rstrip("/").lower()


def get_token(base: str, force: bool = False) -> tuple[str, str]:
    """返回 (accessToken, 错误)。命中缓存或续期成功都算命中。"""
    key = _cache_key(base)
    cache = read_cache()
    entry = cache.get(key) if isinstance(cache.get(key), dict) else None

    if entry and not force:
        token = str(entry.get("accessToken") or "")
        expires = int(entry.get("expiresAt") or 0)
        if token and (not expires or expires - time.time() > 300):
            return token, ""

    dev = (entry or {}).get("device") or {}
    refresh_token = str((entry or {}).get("refreshToken") or "")

    if refresh_token and not force:
        renewed = refresh(base, refresh_token, dev)
        if renewed:
            cache[key] = {**renewed, "device": dev, "base": base}
            _write_cache(cache)
            return renewed["accessToken"], ""

    token = extract()
    if not token:
        return "", ("未读取到 Qoder 桌面端登录态：请确认 Qoder 客户端已登录，"
                    "并用「登录 Qoder 的同一 Windows 用户」运行；也可直接填 PAT")
    dev, _exe = device()
    cache[key] = {**token, "device": dev, "base": base}
    _write_cache(cache)
    return token["accessToken"], ""


def _mask(value: str) -> str:
    return f"{value[:6]}…（长度 {len(value)}）" if value else "空"


def main() -> int:
    parser = argparse.ArgumentParser(description="Qoder 桌面端登录态检查")
    parser.add_argument("--json", action="store_true", help="输出完整凭据（含明文，勿外传）")
    parser.add_argument("--refresh", action="store_true", help="强制用 refreshToken 换新")
    parser.add_argument("--base", default="https://openapi.qoder.com.cn", help="要续期的 API 基址")
    args = parser.parse_args()

    dev, exe = device()
    token = read_cache().get(_cache_key(args.base)) or extract()

    if args.json:
        print(json.dumps({"device": dev, "token": token}, ensure_ascii=False, indent=2))
        return 0

    print("Qoder 桌面端登录态")
    print("-" * 40)
    print("数据目录：", "、".join(appdata_dirs()) or "未找到 com.qoder* 目录")
    print("设备工具：", exe or "未找到 runtime-info.exe（可设 QODER_UMID_EXE）")
    print("machineToken：", "已读取" if dev.get("machineToken") else "空")
    print("版本：", dev.get("version") or "未知")

    if args.refresh:
        entry = read_cache().get(_cache_key(args.base)) or {}
        renewed = refresh(args.base, str(entry.get("refreshToken") or ""), dev)
        if renewed:
            cache = read_cache()
            cache[_cache_key(args.base)] = {**renewed, "device": dev, "base": args.base}
            _write_cache(cache)
            token = renewed
            print("续期：成功")
        else:
            print("续期：失败（refreshToken 不可用或已失效，重新登录客户端即可）")

    if token:
        expires = int(token.get("expiresAt") or 0)
        when = (datetime.fromtimestamp(expires).strftime("%Y-%m-%d %H:%M") if expires else "未知")
        left = f"（剩余 {round((expires - time.time()) / 3600, 1)} 小时）" if expires else ""
        print("accessToken：", _mask(str(token.get("accessToken") or "")))
        print("过期时间：", when, left)
    else:
        print("accessToken：未读取到（客户端未登录，或当前用户不是登录 Qoder 的用户）")
    return 0


if __name__ == "__main__":
    sys.exit(main())