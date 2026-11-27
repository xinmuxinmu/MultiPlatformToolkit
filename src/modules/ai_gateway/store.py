"""Atomic local account storage; credentials use Windows current-user DPAPI."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import secrets
import threading
import time
import uuid

from app.paths import DATA_ROOT
from .base import GatewayError


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise GatewayError("账号加密存储需要 Windows 当前用户 DPAPI", 503, "vault_unavailable")

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), "MultiPlatformToolkit AI", None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise GatewayError("无法解密账号库，请使用保存账号时的 Windows 用户" if decrypt else "Windows 账号凭据加密失败",
                           503, "vault_unavailable")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(output.pbData)


def seal(value: dict) -> str:
    return base64.b64encode(_dpapi(json.dumps(value, ensure_ascii=False).encode())).decode()


def unseal(value: str) -> dict:
    try:
        result = json.loads(_dpapi(base64.b64decode(value, validate=True), decrypt=True))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except GatewayError:
        raise
    except Exception:
        raise GatewayError("账号加密数据损坏", 503, "vault_unavailable") from None


PUBLIC_FIELDS = ("id", "provider", "external_id", "label", "enabled", "created_at", "updated_at")


def public_account(account: dict) -> dict:
    return {key: account.get(key) for key in PUBLIC_FIELDS}


class AccountStore:
    def __init__(self, path: Path | None = None):
        self.path = path or DATA_ROOT / "ai_gateway" / "accounts.json"
        self.lock = threading.RLock()

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "accounts": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
                raise ValueError()
            return data
        except (OSError, ValueError):
            raise GatewayError("账号库无法读取，请检查 accounts.json", 503, "store_unavailable") from None

    def _write(self, data: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def accounts(self, provider: str | None = None) -> list[dict]:
        with self.lock:
            return [public_account(a) for a in self._read()["accounts"]
                    if provider is None or a["provider"] == provider]

    def upsert(self, provider: str, credential: dict, external_id: str, label: str = "") -> dict:
        if not credential or not external_id:
            raise GatewayError("平台未返回完整账号身份", 502, "invalid_identity")
        encrypted = seal(credential)
        with self.lock:
            data = self._read()
            account = next((a for a in data["accounts"]
                            if a["provider"] == provider and a["external_id"] == str(external_id)), None)
            now = int(time.time())
            if account is None:
                account = dict(id=uuid.uuid4().hex, provider=provider, external_id=str(external_id),
                               enabled=True, created_at=now)
                data["accounts"].append(account)
            account.update(label=str(label or external_id), credential=encrypted, updated_at=now)
            self._write(data)
            return public_account(account)

    def credential(self, account_id: str) -> dict:
        with self.lock:
            account = next((a for a in self._read()["accounts"] if a["id"] == account_id), None)
            if account is None:
                raise GatewayError("账号不存在", 404, "account_not_found")
            return unseal(account["credential"])

    def save_credential(self, account_id: str, credential: dict):
        with self.lock:
            data = self._read()
            account = next((a for a in data["accounts"] if a["id"] == account_id), None)
            if account is not None:  # Deleting an in-flight account must not recreate it.
                account.update(credential=seal(credential), updated_at=int(time.time()))
                self._write(data)

    def update(self, account_id: str, *, label=None, enabled=None) -> dict:
        with self.lock:
            data = self._read()
            account = next((a for a in data["accounts"] if a["id"] == account_id), None)
            if account is None:
                raise GatewayError("账号不存在", 404, "account_not_found")
            if label is not None:
                account["label"] = str(label).strip()[:100] or account["label"]
            if enabled is not None:
                if not isinstance(enabled, bool):
                    raise GatewayError("enabled 必须是布尔值")
                account["enabled"] = enabled
            account["updated_at"] = int(time.time())
            self._write(data)
            return public_account(account)

    def delete(self, account_id: str):
        with self.lock:
            data = self._read()
            data["accounts"] = [a for a in data["accounts"] if a["id"] != account_id]
            self._write(data)

    def api_key(self, *, rotate=False) -> str:
        with self.lock:
            data = self._read()
            if not data.get("api_key") or rotate:
                key = "mpt-" + secrets.token_urlsafe(32)
                data["api_key"] = seal({"key": key})
                self._write(data)
                return key
            return unseal(data["api_key"])["key"]
