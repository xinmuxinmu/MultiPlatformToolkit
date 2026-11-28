"""WorkBuddy device OAuth and SSE transport, based on the user's wb reference.
Protocol adaptation from wb2go, Copyright (c) 2026 wb2go contributors (MIT).
See LICENSE-wb2go in this directory.

Credentials stay inside the manager. This adapter never launches a desktop UI;
the optional native helper runs only in Electron's Node mode to decrypt a field.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Iterator
from urllib.parse import urlparse
import uuid

import httpx

from modules.ai_gateway.base import GatewayError


CN_BASE = "https://copilot.tencent.com"
INTL_BASE = "https://www.workbuddy.ai"
PENDING_CODES = {"11217", "11218", "1001"}
UA = "CLI/2.137.1 CodeBuddy/2.137.1"
GENERATION_TAGS = {"text-to-image", "image-to-image", "text-to-video", "image-to-video"}


def _flat(body: dict) -> dict:
    out = dict(body)
    for _ in range(3):
        inner = out.get("data")
        if not isinstance(inner, dict):
            break
        out.pop("data", None)
        out.update(inner)
    if isinstance(out.get("auth"), dict):
        out.update(out["auth"])
    return out


def _pick(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            return str(value).strip()
    return ""


def _expiry(auth: dict, token: str = "") -> float:
    try:
        value = float(auth.get("expires_at") or auth.get("expiresAt") or 0)
        if value:
            return value / 1000 if value > 1e12 else value
        duration = float(auth.get("expiresIn") or auth.get("expires_in") or 0)
        if duration:
            return time.time() + duration
        segment = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        return float(claims.get("exp") or 0)
    except (ValueError, TypeError, IndexError, KeyError):
        return 0


class WorkBuddyProvider:
    id = "workbuddy"
    name = "WorkBuddy"
    realm = "cn"
    capabilities = {"login": True, "local_import": True, "chat": True, "checkin": True}

    def __init__(self, client_factory=None):
        self._client_factory = client_factory or httpx.Client

    @staticmethod
    def _base(realm: str) -> str:
        if realm not in ("cn", "intl"):
            raise GatewayError("WorkBuddy 账号区域无效", status_code=400, code="invalid_realm")
        return INTL_BASE if realm == "intl" else CN_BASE

    def _headers(self, credential: dict | None = None, realm: str | None = None) -> dict:
        credential = credential or {}
        selected = realm or credential.get("realm") or self.realm
        origin = INTL_BASE if selected == "intl" else "https://www.codebuddy.cn"
        headers = {
            "User-Agent": UA, "X-Product": "CodeBuddy", "X-IDE-Name": "CLI",
            "X-IDE-Type": "CLI", "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest", "Origin": origin,
            "Referer": origin + "/", "X-Request-ID": str(uuid.uuid4()),
        }
        if credential.get("access_token"):
            headers["Authorization"] = "Bearer " + str(credential["access_token"])
        if credential.get("uid"):
            headers["X-User-Id"] = str(credential["uid"])
        if credential.get("enterprise_id"):
            headers["X-Enterprise-Id"] = str(credential["enterprise_id"])
        return headers

    def _json(self, method: str, realm: str, path: str, *, credential=None,
              params=None, payload=None, headers=None, allow_codes=(), billing=False) -> dict:
        outgoing = self._headers(credential, realm)
        outgoing.update(headers or {})
        try:
            with self._client_factory(timeout=30, follow_redirects=False) as client:
                base = "https://www.workbuddy.cn" if billing and realm == "cn" else self._base(realm)
                response = client.request(method, base + path, headers=outgoing,
                                          params=params, json=payload)
                body = response.json()
        except (httpx.HTTPError, ValueError, UnicodeError) as exc:
            raise GatewayError("WorkBuddy 接口连接失败或响应不是 JSON",
                               status_code=502, code="upstream_unavailable") from exc
        if not isinstance(body, dict):
            raise GatewayError("WorkBuddy 接口响应格式无效", status_code=502, code="invalid_response")
        business = str(body.get("code", "0"))
        if business in allow_codes:
            return body
        if not response.is_success or business not in ("0", "None"):
            status = response.status_code if response.status_code in (401, 403, 429) else 502
            safe_code = business if business.isdecimal() and len(business) < 12 else "未知"
            raise GatewayError(f"WorkBuddy 接口失败（HTTP {response.status_code}，业务码 {safe_code}）",
                               status_code=status, code="upstream_error")
        return body

    def start_login(self, options: dict | None = None) -> dict:
        realm = (options or {}).get("realm") or self.realm
        body = self._json("POST", realm, "/v2/plugin/auth/state", params={"platform": "CLI"}, payload={})
        flat = _flat(body)
        state = _pick(flat, "state", "auth_state", "oauthState")
        auth_url = _pick(flat, "authUrl", "auth_url", "authURL", "loginUrl", "login_url")
        parsed = urlparse(auth_url)
        host = (parsed.hostname or "").lower()
        official = ("codebuddy.cn", "workbuddy.cn", "workbuddy.ai", "copilot.tencent.com")
        if not state or parsed.scheme != "https" or not any(host == d or host.endswith("." + d) for d in official):
            raise GatewayError("WorkBuddy 没有返回有效的官方授权链接", status_code=502, code="invalid_auth_url")
        try:
            expires = min(1800, max(30, int(flat.get("expiresIn") or flat.get("expires_in") or 600)))
        except (ValueError, TypeError):
            expires = 600
        return {"auth_url": auth_url, "expires_in": expires, "expires_at": time.time() + expires,
                "state": state, "realm": realm}

    def poll_login(self, session: dict) -> dict:
        if float(session.get("expires_at") or 0) < time.time():
            return {"status": "expired"}
        state = session.get("state")
        if not isinstance(state, str) or not state:
            raise GatewayError("WorkBuddy 授权会话无效", status_code=400, code="invalid_session")
        realm = session.get("realm") or self.realm
        body = self._json("GET", realm, "/v2/plugin/auth/token", params={"state": state}, allow_codes=PENDING_CODES)
        if str(body.get("code")) in PENDING_CODES:
            return {"status": "pending"}
        flat = _flat(body)
        access = _pick(flat, "accessToken", "access_token", "token")
        if not access:
            raise GatewayError("WorkBuddy 授权响应缺少登录令牌", status_code=502, code="missing_token")
        credential = {"realm": realm, "access_token": access,
                      "refresh_token": _pick(flat, "refreshToken", "refresh_token"),
                      "expires_at": _expiry(flat, access)}
        profile = self._profile(credential)
        uid = profile.get("uid") or _pick(flat, "uid", "userId", "user_id")
        if not uid:
            raise GatewayError("WorkBuddy 授权成功但无法确认账号身份", status_code=502, code="missing_account_id")
        credential.update(profile, uid=uid)
        return self._account(credential, status="authorized")

    def _profile(self, credential: dict) -> dict:
        for path in ("/v2/plugin/login/account", "/v2/plugin/account"):
            try:
                flat = _flat(self._json("GET", credential["realm"], path, credential=credential))
            except GatewayError:
                continue
            profile = flat.get("account") or flat.get("userInfo") or flat
            if not isinstance(profile, dict):
                continue
            uid = _pick(profile, "uid", "userId", "user_id")
            if uid:
                return {"uid": uid, "nickname": _pick(profile, "nickname", "nickName", "userName", "username"),
                        "enterprise_id": _pick(profile, "enterpriseId", "enterprise_id")}
        return {}

    @staticmethod
    def _account(credential: dict, **extra) -> dict:
        uid = str(credential.get("uid") or "")
        return {"credential": credential, "external_id": uid,
                "label": credential.get("nickname") or ("账号 · " + uid[-6:]), **extra}

    def refresh(self, credential: dict) -> dict:
        refresh = credential.get("refresh_token")
        if not refresh:
            raise GatewayError("WorkBuddy 登录已过期，请重新扫码授权", status_code=401, code="login_required")
        realm = credential.get("realm") or self.realm
        body = self._json("POST", realm, "/v2/plugin/auth/token/refresh", credential=credential, payload={},
                          headers={"X-Refresh-Token": str(refresh),
                                   "X-Auth-Refresh-Source": "plugin" if realm == "intl" else "workbuddy"})
        flat = _flat(body)
        access = _pick(flat, "accessToken", "access_token", "token")
        if not access:
            raise GatewayError("WorkBuddy 续期响应缺少登录令牌", status_code=502, code="missing_token")
        credential["access_token"] = access
        credential["refresh_token"] = _pick(flat, "refreshToken", "refresh_token") or refresh
        credential["expires_at"] = _expiry(flat, access)
        return credential

    def _ensure_token(self, credential: dict) -> None:
        if not credential.get("access_token"):
            raise GatewayError("WorkBuddy 账号没有登录令牌", status_code=401, code="login_required")
        expiry = float(credential.get("expires_at") or 0)
        if expiry and expiry < time.time() + 120:
            self.refresh(credential)

    def models(self, credential: dict) -> list[dict]:
        self._ensure_token(credential)
        realm = credential.get("realm") or self.realm
        for path in ("/v3/config", "/v2/enterprises/personal/models"):
            try:
                body = self._json("GET", realm, path, credential=credential)
            except GatewayError:
                if path == "/v3/config":
                    continue
                raise
            flat = _flat(body)
            items = flat.get("models")
            if not isinstance(items, list) and isinstance(body.get("data"), list):
                items = body["data"]
            if not isinstance(items, list):
                continue
            result = []
            for item in items:
                # The same official catalog includes image/video generators,
                # whose API is not implemented by this chat-only gateway.
                tags = item.get("tags") if isinstance(item, dict) else None
                if isinstance(tags, list) and any(isinstance(tag, str) and tag in GENERATION_TAGS for tag in tags):
                    continue
                model = item if isinstance(item, str) else _pick(item, "id", "modelId", "model", "name") if isinstance(item, dict) else ""
                if model:
                    result.append({"id": model, "object": "model", "owned_by": self.id})
            if result:
                return result
        raise GatewayError("WorkBuddy 没有返回可用模型列表", status_code=502, code="missing_models")

    @staticmethod
    def _prepare(payload: dict) -> dict:
        out = copy.deepcopy({k: v for k, v in payload.items() if not k.startswith("_")})
        out["stream"] = True
        options = out.get("stream_options")
        out["stream_options"] = {**(options if isinstance(options, dict) else {}), "include_usage": True}
        if "max_completion_tokens" in out:
            out.setdefault("max_tokens", out.pop("max_completion_tokens"))
        messages = out.get("messages")
        if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages):
            raise GatewayError("messages 必须是非空消息列表", status_code=400, code="invalid_messages")
        for message in messages:
            if message.get("role") == "developer":
                message["role"] = "system"
        if messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": "You are a helpful assistant."})
        return out

    def _chat_headers(self, credential: dict) -> dict:
        headers = self._headers(credential)
        headers["Accept"] = "text/event-stream"
        uid = str(credential.get("uid") or "anonymous")
        for header, salt in (("X-Machine-ID", "machine"), ("X-Session-ID", "session")):
            headers[header] = hashlib.md5((salt + ":" + uid).encode()).hexdigest()
        headers["X-Device-Token"] = headers["X-Machine-ID"]
        headers["X-No-Department-Info"] = "1"
        if not credential.get("enterprise_id"):
            headers["X-No-Enterprise-Id"] = "1"
        return headers

    def chat(self, credential: dict, payload: dict) -> Iterator[dict]:
        self._ensure_token(credential)
        realm = credential.get("realm") or self.realm
        headers = self._chat_headers(credential)
        try:
            with self._client_factory(timeout=httpx.Timeout(180, connect=15), follow_redirects=False) as client:
                with client.stream("POST", self._base(realm) + "/v2/chat/completions",
                                   headers=headers, json=self._prepare(payload)) as response:
                    if not response.is_success:
                        status = response.status_code if response.status_code in (400, 401, 402, 403, 404, 429) else 502
                        # Preserve a useful business code without exposing the raw
                        # response, which may contain request or account details.
                        business = ""
                        response.read()
                        try:
                            body = response.json()
                            value = body.get("code") if isinstance(body, dict) else None
                            if isinstance(value, (str, int)) and not isinstance(value, bool):
                                value = str(value)
                                if value.isascii() and value.isdecimal() and len(value) < 12:
                                    business = value
                        except (ValueError, UnicodeError):
                            pass
                        detail = f"，业务码 {business}" if business else ""
                        code = "upstream_request_rejected" if business == "11128" else "upstream_error"
                        raise GatewayError(f"WorkBuddy 对话失败（HTTP {response.status_code}{detail}）",
                                           status_code=status, code=code)
                    saw_frame = False
                    data_lines = []
                    for line in response.iter_lines():
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                        elif not line and data_lines:
                            raw = "\n".join(data_lines)
                            data_lines = []
                            if raw == "[DONE]":
                                if not saw_frame:
                                    raise GatewayError("WorkBuddy 未返回有效的对话流", status_code=502, code="invalid_stream")
                                return
                            chunk = self._chunk(raw)
                            saw_frame = True
                            yield chunk
                    if data_lines:
                        raw = "\n".join(data_lines)
                        if raw != "[DONE]":
                            saw_frame = True
                            yield self._chunk(raw)
                    if not saw_frame:
                        raise GatewayError("WorkBuddy 未返回有效的对话流", status_code=502, code="invalid_stream")
        except httpx.HTTPError as exc:
            raise GatewayError("WorkBuddy 对话连接中断", status_code=502, code="upstream_unavailable") from exc

    @staticmethod
    def _chunk(raw: str) -> dict:
        try:
            chunk = json.loads(raw)
        except ValueError as exc:
            raise GatewayError("WorkBuddy 返回无效流事件", status_code=502, code="invalid_stream") from exc
        if not isinstance(chunk, dict) or chunk.get("error") or ("code" in chunk and str(chunk["code"]) not in ("0", "None")):
            raise GatewayError("WorkBuddy 返回对话业务错误", status_code=502, code="upstream_error")
        if not isinstance(chunk.get("choices"), list) and not isinstance(chunk.get("usage"), dict):
            raise GatewayError("WorkBuddy 返回无法识别的对话流", status_code=502, code="invalid_stream")
        return chunk

    def checkin(self, credential: dict, dry: bool = False) -> dict:
        if dry:
            return {"status": "skipped", "message": "只读模式跳过签到和令牌续期"}
        realm = credential.get("realm") or self.realm
        if realm != "cn":
            return {"status": "skipped", "message": "WorkBuddy 国际版没有国内版的签到活动"}
        self._ensure_token(credential)
        body = self._json("POST", realm, "/v2/billing/meter/checkin-activity-status",
                          credential=credential, payload={}, billing=True)
        flat = _flat(body)
        if any(flat.get(key) is True for key in ("today_checked_in", "checkedIn", "alreadyCheckedIn")):
            return {"status": "already", "message": "今日已签到，无需重复领取"}
        if flat.get("active") is False:
            return {"status": "skipped", "message": "当前没有进行中的签到活动"}
        if not any(flat.get(key) is False for key in ("today_checked_in", "checkedIn", "alreadyCheckedIn")):
            raise GatewayError("WorkBuddy 接口未明确今日签到状态，已停止领取", status_code=502, code="unknown_checkin_state")
        body = self._json("POST", realm, "/v2/billing/meter/daily-checkin", credential=credential,
                          payload={}, billing=True, allow_codes={"10001"})
        code = str(body.get("code"))
        if code == "10001":
            return {"status": "already", "message": "今日已签到，无需重复领取"}
        if code != "0":
            raise GatewayError("WorkBuddy 签到响应缺少明确成功状态", status_code=502, code="invalid_response")
        return {"status": "success", "message": "签到成功"}

    def _runtime(self) -> Path:
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        if self.realm == "intl":
            candidates = [os.environ.get("WORKBUDDY_AI_EXE"),
                          "D:/Program File/WorkbuddyAI/WorkBuddyAI.exe",
                          local / "Programs/WorkBuddyAI/WorkBuddyAI.exe", shutil.which("WorkBuddyAI.exe")]
        else:
            candidates = [os.environ.get("WORKBUDDY_EXE"), "D:/Program/WorkBuddy/WorkBuddy.exe",
                          local / "Programs/WorkBuddy/WorkBuddy.exe", shutil.which("WorkBuddy.exe")]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return Path(candidate)
        raise GatewayError("缺少对应版本的 WorkBuddy 本地解密运行时", status_code=400, code="decrypt_unavailable")

    def _field(self, raw, auth_path: Path, field: str) -> str:
        if isinstance(raw, str):
            return raw
        if raw is None:
            return ""
        if not isinstance(raw, dict) or raw.get("$wbEncrypted") != 1:
            raise GatewayError("WorkBuddy 本地凭据格式无法识别", status_code=400, code="invalid_credential")
        exe = self._runtime()
        helper = Path(__file__).resolve().parents[2] / "ai_checkin/vendor/platforms/wb_helper.js"
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("NODE_", "ELECTRON_", "WORKBUDDY_"))}
        env["ELECTRON_RUN_AS_NODE"] = "1"
        try:
            proc = subprocess.run([str(exe), str(helper), str(auth_path), field], stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=15, env=env, cwd=str(exe.parent),
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if proc.returncode:
                raise GatewayError("WorkBuddy 本地凭据解密失败", status_code=400, code="decrypt_failed")
            return proc.stdout.decode("utf-8").strip()
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
            raise GatewayError("WorkBuddy 本地凭据解密失败", status_code=400, code="decrypt_failed") from exc

    def import_local(self) -> list[dict]:
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        filename = "workbuddy-desktop-ai.info" if self.realm == "intl" else "workbuddy-desktop.info"
        paths = [local / "CodeBuddyExtension/Data/Public/auth" / filename, Path.home() / ".workbuddy/auth" / filename]
        reference = Path(os.environ.get("WB_REFERENCE_ACCOUNTS_DIR") or "D:/PythonApps/wb/accounts")
        if reference.is_dir():
            paths.extend(sorted(reference.glob("*.json")))
        results = {}
        failures = []
        for path in paths:
            if not path.is_file():
                continue
            try:
                body = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(body, dict) or body.get("disabled"):
                    continue
                auth = body.get("auth") if isinstance(body.get("auth"), dict) else body
                account = body.get("account") if isinstance(body.get("account"), dict) else body
                explicit_realm = body.get("realm")
                if explicit_realm and explicit_realm != self.realm:
                    continue
                domain = str(auth.get("domain") or "").lower()
                if domain and (("workbuddy.ai" in domain) != (self.realm == "intl")):
                    continue
                uid = _pick(account, "uid", "userId", "user_id")
                if not uid or not auth.get("accessToken"):
                    continue
                access = self._field(auth.get("accessToken"), path, "auth.accessToken")
                refresh = self._field(auth.get("refreshToken"), path, "auth.refreshToken")
                try:
                    nickname = self._field(account.get("nickname"), path, "account.nickname")
                except GatewayError:
                    nickname = ""
                if not access:
                    continue
                credential = {"access_token": access, "refresh_token": refresh,
                              "expires_at": _expiry(auth, access), "uid": uid, "nickname": nickname,
                              "enterprise_id": _pick(account, "enterpriseId", "enterprise_id"), "realm": self.realm}
                previous = results.get(uid)
                if not previous or credential["expires_at"] > previous["credential"].get("expires_at", 0):
                    results[uid] = self._account(credential)
            except (OSError, ValueError, GatewayError) as exc:
                failures.append(exc)
        if not results and failures:
            if isinstance(failures[0], GatewayError):
                raise failures[0]
            raise GatewayError("无法读取 WorkBuddy 本地登录态", status_code=400, code="import_failed") from failures[0]
        return list(results.values())


class WorkBuddyAIProvider(WorkBuddyProvider):
    id = "workbuddy_ai"
    name = "WorkBuddy AI"
    realm = "intl"
    capabilities = {"login": True, "local_import": True, "chat": True, "checkin": False,
                    "reason": "国际版每日签到接口尚未核实，当前支持扫码授权、本地导入和聊天反代"}

    def _chat_headers(self, credential: dict) -> dict:
        headers = super()._chat_headers(credential)
        # Official desktop 5.8.4 main/server.js: buildVlmGatewayHeaders
        # and applyGatewayHeaders. The international gateway rejects the
        # CLI identity without these conversation fields (business 11128).
        headers.update({
            "User-Agent": "WorkBuddy/5.8.4 WorkBuddy/5.8.4",
            "X-IDE-Type": "WorkBuddy", "X-IDE-Name": "WorkBuddy",
            "X-IDE-Version": "5.8.4", "X-Private-Data": "true",
            "X-Product": "SaaS", "X-Domain": "www.workbuddy.ai",
            "X-Agent-Intent": "craft", "X-Agent-Purpose": "conversation",
            "x-codebuddy-request": "1",
        })
        for field in ("X-Conversation-ID", "X-Conversation-Request-ID",
                      "X-Conversation-Message-ID", "acp-connection-id"):
            headers[field] = str(uuid.uuid4())
        if credential.get("enterprise_id"):
            headers["X-Tenant-Id"] = str(credential["enterprise_id"])
        return headers
