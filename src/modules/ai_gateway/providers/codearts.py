"""CodeArts credentials from the local VS Code secret store; no app process.

The installed Huawei authentication extension uses Chromium AES-GCM with a
current-user DPAPI key. OAuth renewal requires the persisted PKCE verifier and
DPoP key pair. See docs/modules/ai_checkin/codearts_gateway_protocol.md.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from modules.ai_gateway.base import GatewayError


STS = "https://sts.cn-north-4.myhuaweicloud.com"
SECRET_KEY = 'secret://{"extensionId":"huaweicloud.authentication","key":"HuaweiCloudSession"}'
CLIENT_ID = "codearts-agent"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _expiry(value) -> float:
    try:
        if isinstance(value, (int, float)):
            return value / 1000 if value > 1e12 else float(value)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0


def _dpop(context: dict, method: str, url: str) -> str:
    """ES256 DPoP matching the installed extension, with a fresh nonce per call."""
    try:
        pair = context["dpopKeyPair"]
        private = pair["privateKeyJwk"]
        public = pair["publicKeyJwk"]
        if private.get("crv") != "P-256" or private.get("kty") != "EC":
            raise ValueError()
        d = private["d"]
        scalar = int.from_bytes(base64.urlsafe_b64decode(d + "=" * (-len(d) % 4)), "big")
        key = ec.derive_private_key(scalar, ec.SECP256R1())
        # The header includes the public JWK only, even if a malformed local
        # record accidentally placed private fields in publicKeyJwk.
        jwk = {name: public[name] for name in ("kty", "crv", "x", "y")}
        derived = key.public_key().public_numbers()
        if jwk != dict(kty="EC", crv="P-256", x=_b64(derived.x.to_bytes(32, "big")),
                       y=_b64(derived.y.to_bytes(32, "big"))):
            raise ValueError()
        header = dict(alg="ES256", typ="dpop+jwt", jwk=jwk)
        claims = dict(htm=method.upper(), htu=url, iat=int(time.time()), jti=secrets.token_hex(32))
        parts = [_b64(json.dumps(item, separators=(",", ":")).encode()) for item in (header, claims)]
        message = ".".join(parts).encode("ascii")
        r, s = decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
        return ".".join([*parts, _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))])
    except (KeyError, ValueError, TypeError):
        raise GatewayError("CodeArts 本地 DPoP 登录上下文无效，请重新导入账号", 401, "invalid_credential") from None


def _read_session(profile: Path, decrypt_key=None) -> dict | None:
    local_state = profile / "Local State"
    database = profile / "User" / "globalStorage" / "state.vscdb"
    if not local_state.is_file() or not database.is_file():
        return None
    if decrypt_key is None:
        from modules.ai_gateway.store import _dpapi
        decrypt_key = lambda value: _dpapi(value, decrypt=True)
    try:
        state = json.loads(local_state.read_text(encoding="utf-8"))
        wrapped = base64.b64decode(state["os_crypt"]["encrypted_key"], validate=True)
        if not wrapped.startswith(b"DPAPI"):
            raise ValueError()
        key = decrypt_key(wrapped[5:])
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)
        try:
            row = connection.execute("SELECT value FROM ItemTable WHERE key=?", (SECRET_KEY,)).fetchone()
        finally:
            connection.close()
        if not row:
            return None
        encoded = json.loads(row[0])
        raw = bytes(encoded["data"])
        if raw[:3] not in (b"v10", b"v11") or len(raw) < 31:
            raise ValueError()
        value = json.loads(AESGCM(key).decrypt(raw[3:15], raw[15:], None))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except GatewayError:
        raise
    except Exception:
        # No raw database/crypto exception may expose credential material.
        raise GatewayError("CodeArts 本地登录凭据无法解密，请使用当前 Windows 用户重新登录后导入",
                           401, "local_import_failed") from None


class CodeArtsProvider:
    id = "codearts"
    name = "CodeArts Agent"
    capabilities = dict(login=False, local_import=True, chat=False, checkin=False,
                        reason="支持本地解密导入；手机扫码回调、聊天传输和每日签到接口仍待验证")

    def __init__(self, client_factory=None, profile_dirs=None):
        self._client_factory = client_factory or httpx.Client
        self._profile_dirs = profile_dirs

    def import_local(self) -> list[dict]:
        appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        profiles = self._profile_dirs or [appdata / "codearts-agent", appdata / "codearts"]
        results, seen = [], set()
        for profile in profiles:
            session = _read_session(Path(profile).resolve())
            if not session:
                continue
            account = session.get("account") or {}
            external_id = str(account.get("id") or session.get("id") or "")
            credential = dict(access_key_id=session.get("accessKey"), secret_access_key=session.get("secretKey"),
                              security_token=session.get("securityToken") or session.get("securitytoken"),
                              expires_at=_expiry(session.get("expires_at")), domain_id=session.get("domainId") or "",
                              user_id=external_id, refresh_token=session.get("refresh_token") or "",
                              login_context=copy.deepcopy(session.get("loginContext") or {}))
            if not external_id or external_id in seen:
                continue
            if not all(credential.get(k) for k in ("access_key_id", "secret_access_key", "security_token")):
                raise GatewayError("CodeArts 本地登录凭据不完整", 401, "local_import_failed")
            # The gateway immediately persists every returned credential in
            # its own DPAPI vault. Renewing here avoids exporting an expired
            # AK/SK pair and records the replacement refresh token atomically.
            if credential["expires_at"] <= time.time() + 120:
                self._refresh(credential)
            seen.add(external_id)
            results.append(dict(credential=credential, external_id=external_id,
                                label=str(account.get("label") or external_id)))
        return results

    def _refresh(self, credential: dict) -> None:
        if float(credential.get("expires_at") or 0) > time.time() + 120:
            return
        context = credential.get("login_context") or {}
        verifier = (context.get("pkcePair") or {}).get("codeVerifier")
        refresh = credential.get("refresh_token")
        if not verifier or not refresh:
            raise GatewayError("CodeArts 临时凭据已过期，请重新导入登录账号", 401, "login_expired")
        url = STS + "/v1/oauth2/tokens"
        proof = _dpop(context, "POST", url)
        try:
            with self._client_factory(timeout=60, follow_redirects=False) as client:
                response = client.post(url, data=dict(client_id=CLIENT_ID, code_verifier=verifier,
                                                      grant_type="refresh_token", refresh_token=refresh),
                                       headers={"DPoP": proof, "Content-Type": "application/x-www-form-urlencoded"})
            if response.status_code != 200:
                raise GatewayError("CodeArts 登录续期失败，请重新登录后导入", 401, "login_expired")
            data = response.json()
            tokens = data.get("credentials") or {}
            new_expiry = _expiry(tokens.get("expiration"))
            if not all(tokens.get(k) for k in ("access_key_id", "secret_access_key", "security_token")) or new_expiry <= time.time():
                raise GatewayError("CodeArts 续期响应不完整", 502, "upstream_error")
            credential.update(access_key_id=tokens["access_key_id"], secret_access_key=tokens["secret_access_key"],
                              security_token=tokens["security_token"], expires_at=new_expiry,
                              refresh_token=data.get("refresh_token") or refresh)
        except GatewayError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise GatewayError("CodeArts 登录续期请求失败", 502, "upstream_error") from None

    def models(self, credential):
        raise GatewayError(self.capabilities["reason"], 501, "unsupported_capability")

    def checkin(self, credential, dry=False):
        return dict(status="skipped", message="CodeArts 每日签到接口尚未验证")
