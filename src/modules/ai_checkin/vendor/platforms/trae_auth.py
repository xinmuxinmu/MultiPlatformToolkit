# -*- coding: utf-8 -*-
"""TraeWork multi-account credential storage and automatic token refresh.

The module reads existing platform credentials and rotates refresh tokens using
the device proof already stored with each account. Credential values are never
written to logs.
"""
import base64
import glob
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

try:
    from Crypto.PublicKey import ECC
    from Crypto.Signature import DSS
    from Crypto.Hash import SHA256
except ImportError:  # pragma: no cover
    ECC = DSS = SHA256 = None

# 项目根目录（本文件位于 <项目根>/platforms/ 下）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 凭据目录：默认 <项目根>/auths/，可用环境变量 WB_AUTHS_DIR 覆盖（与 wb_auth 共用）
_ENV_AUTHS_DIR = "WB_AUTHS_DIR"

# ---------- 协议常量（社区抓包固化，勿改） ----------

ACCOUNT_BASE = "https://api.trae.cn"        # 兑换端点兜底 host
EXCHANGE_PATH = "/trae/api/v3/oauth/ExchangeToken"

# SOLO 线（与本机 TRAE SOLO CN 客户端及生产 traework.py 口径一致）
CLIENT_ID = "en1oxy7wnw8j9n"
PLATFORM_CODE = "SOLO_PC"
DEFAULT_HOST = ACCOUNT_BASE

# 续期请求声明的客户端版本（与生产 traework.py 既有口径一致）
REFRESH_APP_VERSION = "1.107.1"
# 过期前预刷新余量（2 天；RT 为轮换制，不宜过期后再补救）
REFRESH_MARGIN_SEC = 2 * 86400

# HTTP 超时
HTTP_TIMEOUT = 30

# ---------- 目录与文件 ----------

def auths_dir() -> str:
    """凭据目录路径（默认 <项目根>/auths/，WB_AUTHS_DIR 可覆盖）"""
    override = os.environ.get(_ENV_AUTHS_DIR)
    if override:
        return override
    return os.path.join(_PROJECT_ROOT, "auths")

def list_account_files() -> list:
    """凭据目录下全部 traework-*.json（按文件名排序）"""
    files = sorted(glob.glob(os.path.join(auths_dir(), "traework-*.json")))
    return [f for f in files if not f.endswith(".tmp")]

def load_account(path: str) -> dict:
    """读取凭据文件；兼容嵌套形（本项目）与扁平形（外部导入）两种结构"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("凭据文件格式错误：根节点不是 JSON 对象")
    if isinstance(data.get("auth"), dict) and data["auth"].get("accessToken"):
        data["auth"]["apiHost"] = normalize_api_host(data["auth"].get("apiHost"))
        return data
    if data.get("accessToken"):
        return {
            "auth": {
                "accessToken": data.get("accessToken", ""),
                "refreshToken": data.get("refreshToken", ""),
                "expiresAt": data.get("expiresAt", 0),
                "apiHost": normalize_api_host(data.get("host", "")),
                "deviceId": data.get("deviceId", ""),
                "machineId": data.get("machineId", ""),
                "privateKeyPem": data.get("privateKeyPem", ""),
                "publicKeyPem": data.get("publicKeyPem", ""),
                "clientId": data.get("clientId", CLIENT_ID),
                "platform": data.get("platform", PLATFORM_CODE),
                "appVersion": data.get("appVersion", REFRESH_APP_VERSION),
            },
            "account": {
                "uid": data.get("uid", ""),
                "nickname": data.get("name", "") or data.get("nickname", ""),
            },
            "disabled": bool(data.get("disabled", False)),
            "source": data.get("source", ""),
        }
    raise ValueError("凭据文件缺少 accessToken")

def save_account(path: str, data: dict):
    """原子写回凭据文件（临时文件 + fsync + os.replace，防写坏）"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

# ---------- 账号一览（供调度器/CLI 展示，不含敏感字段） ----------

# 多账号是**可选功能**：默认由「凭据库里有没有凭据」决定——
# 有凭据则逐账号签到，没有则回退桌面客户端单账号模式。
# 需要强制单账号（或强制多账号）时用环境变量显式指定：
#   AI_CHECKIN_MULTI_ACCOUNT=0 → 强制单账号（忽略凭据库）
#   AI_CHECKIN_MULTI_ACCOUNT=1 → 强制多账号（凭据库为空时仍回退单账号）
MULTI_ACCOUNT_ENV = "AI_CHECKIN_MULTI_ACCOUNT"

def multi_account_enabled(default: bool = True) -> bool:
    """多账号是否启用；未设置环境变量时沿用 default"""
    raw = os.environ.get(MULTI_ACCOUNT_ENV, "").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    return default

def _uid_from_filename(path: str) -> str:
    m = re.search(r"traework-(.+)\.json$", os.path.basename(path))
    return m.group(1) if m else ""

def _label_for(uid: str, nickname: str, path: str) -> str:
    """账号展示名：昵称 > UID 尾号 > 文件名"""
    if nickname:
        return nickname
    if uid:
        return f"…{uid[-6:]}"
    stem = re.sub(r"^traework-?", "", os.path.splitext(os.path.basename(path))[0])
    return stem or "账号"

def list_accounts() -> list:
    """扫描凭据目录，返回 [{path, uid, nickname, label, disabled}]（不含 token）"""
    out = []
    for path in list_account_files():
        entry = {"path": path, "uid": "", "nickname": "", "label": "", "disabled": False}
        try:
            data = load_account(path)
            acct = data.get("account") or {}
            entry["uid"] = acct.get("uid") or _uid_from_filename(path)
            entry["nickname"] = acct.get("nickname") or ""
            entry["disabled"] = bool(data.get("disabled"))
        except Exception:  # noqa: BLE001 - 坏文件也列出，运行期会给出明确错误
            entry["uid"] = _uid_from_filename(path)
        entry["label"] = _label_for(entry["uid"], entry["nickname"], path)
        out.append(entry)

    # 去重展示名（同昵称时补 UID 尾号，避免日报行无法区分）
    seen = {}
    for e in out:
        seen[e["label"]] = seen.get(e["label"], 0) + 1
    for e in out:
        if seen[e["label"]] > 1 and e["uid"]:
            e["label"] = f"{e['label']}·{e['uid'][-4:]}"
    return out

# ---------- 时间与脱敏工具 ----------

def _expires_at_sec(data: dict) -> float:
    """归一化到期时间到「秒」：>=1e12 视为毫秒，否则为秒"""
    v = (data.get("auth") or {}).get("expiresAt")
    return _to_epoch(v)

def _to_epoch(value) -> float:
    """把秒/毫秒/ISO 字符串（如 2026-10-30T00:49:53.853Z）统一为秒"""
    if not value:
        return 0.0
    try:
        if isinstance(value, (int, float)):
            v = float(value)
            return v / 1000.0 if v >= 1e12 else v
        s = str(value).strip()
        if re.fullmatch(r"\d+(\.\d+)?", s):
            v = float(s)
            return v / 1000.0 if v >= 1e12 else v
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return 0.0

def is_expiring(data: dict, margin_sec: int | None = None) -> bool:
    """accessToken 是否临近/已经过期（默认 <2 天），需要预刷新"""
    exp = _expires_at_sec(data)
    if not exp:
        return False
    return time.time() + (margin_sec if margin_sec else REFRESH_MARGIN_SEC) > exp

def is_expired(data: dict) -> bool:
    exp = _expires_at_sec(data)
    return bool(exp) and time.time() > exp

def days_left(data: dict) -> int | None:
    """accessToken 剩余天数；无到期信息返回 None"""
    exp = _expires_at_sec(data)
    if not exp:
        return None
    return max(0, int((exp - time.time()) / 86400))

def refresh_days_left(data: dict) -> int | None:
    """refreshToken 剩余天数（refreshExpiresAt）；无信息返回 None"""
    v = (data.get("auth") or {}).get("refreshExpiresAt")
    exp = _to_epoch(v)
    if not exp:
        return None
    return max(0, int((exp - time.time()) / 86400))

# ---------- 主机归一化 ----------

def normalize_api_host(host: str) -> str:
    """签到 API 主机归一化：回调/授权域 api.trae.com.cn 不提供 v2 接口（404），
    统一映射到 api.trae.cn；空值回退 DEFAULT_HOST。"""
    h = str(host or "").strip().rstrip("/")
    if not h:
        return DEFAULT_HOST
    if h.endswith("trae.com.cn"):
        return DEFAULT_HOST
    return h





def machine_id_of(device: dict) -> str:
    """machineId 缺失时的兜底：由 device_id 派生一个稳定值（URL 与请求体同源）"""
    mid = (device.get("machine_id") or "").strip()
    if mid:
        return mid
    return hashlib.sha256(
        ("trae-oauth-machine:" + device["device_id"]).encode()).hexdigest()[:32]



_ECDSA_N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551

def _der_encode_signature(r: int, s: int) -> bytes:
    def _enc(x):
        b = x.to_bytes(32, "big")
        if b[0] & 0x80:
            b = b"\x00" + b
        return b
    rb, sb = _enc(r), _enc(s)
    body = b"\x02" + bytes([len(rb)]) + rb + b"\x02" + bytes([len(sb)]) + sb
    return b"\x30" + bytes([len(body)]) + body

def device_proof_sign(private_key_pem: str, data: bytes) -> str:
    """设备私钥 ECDSA P-256/SHA-256 签名，返回 base64(DER)，含低 s 归一化"""
    if ECC is None:
        raise RuntimeError("缺少 pycryptodome 依赖，请执行: pip install pycryptodome")
    key = ECC.import_key(private_key_pem)
    signer = DSS.new(key, "fips-186-3", encoding="der")
    der = signer.sign(SHA256.new(data))
    idx = 2
    if der[1] & 0x80:
        idx += 1
    if der[idx] != 0x02:
        raise ValueError("Missing r integer")
    len_r = der[idx + 1]
    r = int.from_bytes(der[idx + 2:idx + 2 + len_r], "big")
    idx += 2 + len_r
    if der[idx] != 0x02:
        raise ValueError("Missing s integer")
    len_s = der[idx + 1]
    s = int.from_bytes(der[idx + 2:idx + 2 + len_s], "big")
    if s > _ECDSA_N // 2:
        s = _ECDSA_N - s
    return base64.b64encode(_der_encode_signature(r, s)).decode("utf-8")

# ---------- HTTP 基础 ----------

def _post_json(url: str, headers: dict, payload: dict, timeout: int = HTTP_TIMEOUT):
    """POST JSON，返回 (status, text)；网络异常返回 (-1, 描述)"""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return -1, f"网络异常: {e}"

def _jwt_payload(token: str) -> dict:
    """解析 JWT payload（不校验签名，仅取字段）；失败返回 {}"""
    try:
        parts = str(token).split(".")
        if len(parts) < 2:
            return {}
        p = parts[1].replace("-", "+").replace("_", "/")
        p += "=" * ((4 - len(p) % 4) % 4)
        claims = json.loads(base64.b64decode(p))
        return claims if isinstance(claims, dict) else {}
    except Exception:  # noqa: BLE001
        return {}

def jwt_exp(token: str) -> float:
    """Trae JWT 的到期时间（epoch 秒）"""
    claims = _jwt_payload(token)
    try:
        return float(claims.get("exp") or 0)
    except (TypeError, ValueError):
        return 0.0

# ---------- Token 自动续期（设备证明版 ExchangeToken） ----------

def exchange_token(host: str, refresh_token: str, device: dict,
                   client_id: str = CLIENT_ID, platform_code: str = PLATFORM_CODE,
                   app_version: str | None = None):
    """用 refreshToken + 设备私钥续期，返回 (result_dict, error)。

    result_dict: {"token": ..., "refresh_token": ..., "expires_at": <秒>}。
    error 为空字符串表示成功；否则为人类可读错误。
    """
    if ECC is None:
        return None, "缺少 pycryptodome 依赖，请执行: pip install pycryptodome"
    private_key = (device.get("private_key_pem") or "").strip()
    if not (refresh_token and private_key and device.get("device_id")):
        return None, "缺少 refreshToken 或设备密钥，无法自动续期"

    app_version = app_version or REFRESH_APP_VERSION
    ts = int(time.time())
    nonce = secrets.token_hex(16)
    canonical = "\n".join(["POST", EXCHANGE_PATH, client_id,
                           refresh_token, str(ts), nonce])
    try:
        signature = device_proof_sign(private_key, canonical.encode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return None, f"设备私钥签名失败：{e}"

    device_info = {
        "DeviceID": device["device_id"],
        "MachineID": machine_id_of(device),
        "PlatformCode": platform_code,
        "DeviceType": "PC",
        "DeviceName": os.environ.get("USERNAME", "user"),
        "DeviceModel": "",
        "ClientVersion": app_version,
        "DevicePublicKey": device.get("public_key_pem") or "",
        "DeviceBrand": "",
        "DeviceCPU": "",
        "OSInfo": "Windows",
        "OSVersion": platform.release(),
    }
    body = {
        "ClientID": client_id,
        "ClientSecret": "",
        "RefreshToken": refresh_token,
        "DeviceInfo": device_info,
        "DeviceProof": {"Timestamp": ts, "Nonce": nonce, "Signature": signature},
        "IDEVersion": app_version,
    }
    headers = {"Content-Type": "application/json", "x-cloudide-token": ""}
    url = normalize_api_host(host) + EXCHANGE_PATH
    status, text = _post_json(url, headers, body)
    if status == -1:
        return None, text
    try:
        j = json.loads(text)
    except json.JSONDecodeError:
        return None, f"非 JSON 响应（HTTP {status}）：{text[:200]}"

    err = ((j.get("ResponseMetadata") or {}).get("Error") or {})
    code = err.get("Code")
    if code and str(code) != "0":
        return None, (f"code={code}/{err.get('StandardCode') or ''} "
                      f"{err.get('Message') or ''}").strip()
    res = j.get("Result") or {}
    new_token = res.get("Token") or ""
    if not new_token:
        return None, f"响应中未找到 Token（HTTP {status}）"
    return {
        "token": new_token,
        "refresh_token": res.get("RefreshToken") or "",
        "expires_at": jwt_exp(new_token),
    }, ""

def refresh_account(path: str, data: dict | None = None, dry: bool = False):
    """刷新账号 Token（refreshToken 轮换并原子写回）。返回 (ok, message)。

    dry=True 时跳过（只读模式零副作用）。
    """
    if dry:
        return False, "只读模式跳过续期"
    if data is None:
        data = load_account(path)
    auth = data.get("auth") or {}
    rt = (auth.get("refreshToken") or "").strip()
    if not rt:
        return False, "凭据缺少 refreshToken，无法自动续期"

    device = {
        "device_id": auth.get("deviceId") or "",
        "machine_id": auth.get("machineId") or "",
        "private_key_pem": auth.get("privateKeyPem") or "",
        "public_key_pem": auth.get("publicKeyPem") or "",
    }
    result, err = exchange_token(
        auth.get("apiHost") or DEFAULT_HOST, rt, device,
        client_id=auth.get("clientId") or CLIENT_ID,
        platform_code=auth.get("platform") or PLATFORM_CODE,
        app_version=auth.get("appVersion") or REFRESH_APP_VERSION,
    )
    if not result:
        return False, f"续期失败：{err}"

    auth["accessToken"] = result["token"]
    if result.get("refresh_token"):
        auth["refreshToken"] = result["refresh_token"]
    if result.get("expires_at"):
        auth["expiresAt"] = int(result["expires_at"])
    data["auth"] = auth
    data["disabled"] = False
    data.pop("note", None)
    save_account(path, data)
    return True, "Token 已自动续期"

# ---------- 凭据组装 ----------



