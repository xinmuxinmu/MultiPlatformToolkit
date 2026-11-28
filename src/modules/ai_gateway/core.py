from __future__ import annotations

import base64
import copy
import io
import threading
import time
import uuid

import qrcode

from .base import GatewayError, UnavailableProvider
from .store import AccountStore


def default_providers() -> dict:
    from .providers.workbuddy import WorkBuddyProvider, WorkBuddyAIProvider
    providers = [WorkBuddyProvider(), WorkBuddyAIProvider()]
    # Each adapter advertises only its verified capabilities; local import does
    # not imply that its chat transport is implemented.
    candidates = [("doubao", "豆包", "doubao", "DoubaoProvider"),
                  ("qwen", "千问办公", "qwen", "QwenProvider"),
                  ("kimi", "Kimi", "kimi", "KimiProvider"),
                  ("zhipu", "智谱清言", "zhipu", "ZhipuProvider"),
                  ("codearts", "CodeArts Agent", "codearts", "CodeArtsProvider"),
                  ("trae", "Trae CN", "trae", "TraeProvider"),
                  ("qoder", "Qoder 国际版", "qoder", "QoderProvider"),
                  ("qoder_cn", "Qoder 国内版", "qoder", "QoderCNProvider")]
    import importlib
    for pid, name, module, cls in candidates:
        try:
            adapter = importlib.import_module(f"{__package__}.providers.{module}")
        except ModuleNotFoundError as exc:
            if exc.name != f"{__package__}.providers.{module}":
                raise
            providers.append(UnavailableProvider(pid, name, "尚未核实该平台的独立授权与聊天协议"))
        else:
            providers.append(getattr(adapter, cls)())
    for pid, name in (("minimax", "MiniMax Code"), ("dumate", "百度搭子 DuMate"),
                      ("lobster", "LobsterAI")):
        providers.append(UnavailableProvider(
            pid, name, "签到功能在 AI 签到页；模型目录与聊天协议尚未核实，未接入统一反代"))
    return {p.id: p for p in providers}


def public_provider(provider) -> dict:
    caps = provider.capabilities
    return {"id": provider.id, "name": provider.name, "capabilities": caps}


class Gateway:
    def __init__(self, store=None, providers=None):
        self.store = store or AccountStore()
        self._providers = providers
        self.sessions = {}
        self.lock = threading.RLock()
        self.account_locks = {}
        self.cursors = {}

    @property
    def providers(self):
        if self._providers is None:
            with self.lock:
                if self._providers is None:
                    self._providers = default_providers()
        return self._providers

    def provider(self, provider_id, capability=None):
        provider = self.providers.get(provider_id)
        if provider is None:
            raise GatewayError("未知平台", 404, "provider_not_found")
        if capability and not provider.capabilities.get(capability):
            raise GatewayError(provider.capabilities.get("reason") or "该平台尚未支持此能力",
                               422, "capability_unavailable")
        return provider

    def state(self):
        return dict(providers=[public_provider(p) for p in self.providers.values()],
                    accounts=self.store.accounts())

    def _purge_sessions(self):
        now = time.time()
        for sid, session in list(self.sessions.items()):
            if session["expires_at"] < now - 60:
                del self.sessions[sid]

    def start_login(self, provider_id, options=None):
        provider = self.provider(provider_id, "login")
        with self.lock:
            self._purge_sessions()
            if len(self.sessions) >= 20:
                raise GatewayError("待完成的授权过多，请先完成或取消扫码", 429)
        private = provider.start_login(options or {})
        url = str(private.get("auth_url") or private.get("qr_url") or "")
        from urllib.parse import urlsplit
        if urlsplit(url).scheme != "https" or len(url) > 4096:
            raise GatewayError("平台未返回有效 HTTPS 授权链接", 502, "invalid_login_response")
        expires = min(int(private.get("expires_in") or 300), 900)
        expires_at = min(float(private.get("expires_at") or time.time() + expires), time.time() + 900)
        sid = uuid.uuid4().hex
        qr_data = private.get("qr_image")
        if qr_data:
            try:
                prefix, encoded = str(qr_data).split(",", 1)
                image_bytes = base64.b64decode(encoded, validate=True)
                if (prefix != "data:image/png;base64" or len(image_bytes) > 1024 * 1024 or
                        not image_bytes.startswith(b"\x89PNG\r\n\x1a\n")):
                    raise ValueError("invalid QR image")
            except (ValueError, TypeError):
                raise GatewayError("平台返回的二维码图片无效", 502, "invalid_login_response") from None
        else:
            png = io.BytesIO()
            qrcode.make(url).save(png, format="PNG")
            qr_data = "data:image/png;base64," + base64.b64encode(png.getvalue()).decode()
        with self.lock:
            self.sessions[sid] = dict(provider=provider_id, private=private, expires_at=expires_at,
                                      status="pending", lock=threading.Lock())
        return dict(id=sid, provider=provider_id, status="pending", auth_url=url, expires_at=expires_at,
                    qr_data=qr_data)

    def poll_login(self, session_id):
        with self.lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise GatewayError("扫码会话不存在或已取消", 404, "session_not_found")
        if not session["lock"].acquire(blocking=False):
            return {"status": "pending"}
        try:
            if session["status"] == "authorized":
                return {"status": "authorized", "account": session["account"]}
            if session["expires_at"] <= time.time():
                session["private"] = {}
                return {"status": "expired"}
            result = self.provider(session["provider"], "login").poll_login(session["private"])
            status = result.get("status", "pending")
            if status in ("authorized", "completed"):
                profile = result.get("profile") or {}
                account = self.store.upsert(session["provider"], result["credential"],
                                            str(result.get("external_id") or profile.get("id") or ""),
                                            result.get("label") or profile.get("name") or "")
                session.update(status="authorized", account=account, private={})
                return {"status": "authorized", "account": account}
            if status not in ("pending", "scanned", "expired", "denied"):
                raise GatewayError("平台返回未知授权状态", 502)
            if status in ("expired", "denied"):
                session["expires_at"] = 0
                session["private"] = {}
            return {"status": status}
        finally:
            session["lock"].release()

    def cancel_login(self, session_id):
        with self.lock:
            session = self.sessions.get(session_id)
        if session:
            with session["lock"]:
                with self.lock:
                    self.sessions.pop(session_id, None)

    def import_local(self, provider_id):
        provider = self.provider(provider_id, "local_import")
        rows = provider.import_local()
        accounts = [self.store.upsert(provider_id, row["credential"], str(row["external_id"]), row.get("label", ""))
                    for row in rows]
        return {"accounts": accounts, "count": len(accounts)}

    def import_all_local(self):
        """Import every provider's local session independently; one missing client must not block others."""
        results = []
        for provider_id, provider in self.providers.items():
            if not provider.capabilities.get("local_import"):
                results.append({"provider": provider_id, "name": provider.name,
                                "status": "unsupported", "count": 0,
                                "message": "未提供本机登录态导入"})
                continue
            try:
                imported = self.import_local(provider_id)
            except GatewayError as exc:
                results.append({"provider": provider_id, "name": provider.name,
                                "status": "failed", "count": 0,
                                "message": str(exc)})
            except Exception:
                results.append({"provider": provider_id, "name": provider.name,
                                "status": "failed", "count": 0,
                                "message": "本机登录态导入失败，请检查平台客户端登录状态"})
            else:
                count = imported["count"]
                results.append({"provider": provider_id, "name": provider.name,
                                "status": "imported" if count else "empty",
                                "count": count,
                                "message": f"导入或更新 {count} 个账号" if count else "未发现本机登录态"})
        return {"results": results,
                "imported": sum(row["count"] for row in results),
                "failed": sum(row["status"] == "failed" for row in results),
                "chat_providers": [p.id for p in self.providers.values()
                                   if p.capabilities.get("chat")]}

    def account_lock(self, account_id):
        with self.lock:
            # StreamingResponse may advance the iterator on different workers.
            return self.account_locks.setdefault(account_id, threading.Lock())

    def models(self):
        result = []
        seen = set()
        failures = 0
        local_cache = {}

        def local_credential_for(provider, account):
            """Recover one unreadable vault entry from the matching desktop login state."""
            if not provider.capabilities.get("local_import"):
                return None
            if provider.id not in local_cache:
                try:
                    local_cache[provider.id] = provider.import_local()
                except Exception:
                    local_cache[provider.id] = []
            match = next((item for item in local_cache[provider.id]
                          if isinstance(item, dict) and
                          str(item.get("external_id") or "") ==
                          str(account.get("external_id") or "")), None)
            credential = match.get("credential") if isinstance(match, dict) else None
            if not isinstance(credential, dict) or not credential:
                return None
            self.store.save_credential(account["id"], credential)
            return copy.deepcopy(credential)

        for account in self.store.accounts():
            provider = self.provider(account["provider"])
            if not account["enabled"] or not provider.capabilities.get("chat"):
                continue
            with self.account_lock(account["id"]):
                try:
                    credential = self.store.credential(account["id"])
                except GatewayError as exc:
                    if exc.code != "vault_unavailable":
                        failures += 1
                        continue
                    credential = local_credential_for(provider, account)
                    if credential is None:
                        failures += 1
                        continue
                before = copy.deepcopy(credential)
                try:
                    models = provider.models(credential)
                except Exception:
                    failures += 1
                    continue
                finally:
                    if before != credential:
                        self.store.save_credential(account["id"], credential)
            for model in models:
                model_id = str(model.get("id") if isinstance(model, dict) else model)
                if not model_id or model_id == "None":
                    continue
                name = f"{provider.id}/{model_id}"
                if name not in seen:
                    seen.add(name)
                    result.append(dict(id=name, object="model", created=0, owned_by=provider.id))
        if failures and not result:
            raise GatewayError("无法从已启用账号获取模型，请检查登录状态和网络", 502, "models_unavailable")
        return result

    def select_account(self, provider_id, account_id=None):
        accounts = [a for a in self.store.accounts(provider_id) if a["enabled"]]
        if account_id:
            account = next((a for a in accounts if a["id"] == account_id), None)
            if not account:
                raise GatewayError("指定账号不存在、已停用或属于其他平台", 404, "account_not_found")
            return account
        if not accounts:
            raise GatewayError("该平台没有启用的账号，请先扫码或导入本机登录态", 409, "no_accounts")
        with self.lock:
            cursor = self.cursors.get(provider_id, 0)
            self.cursors[provider_id] = cursor + 1
            return accounts[cursor % len(accounts)]

    def chat(self, payload, account_id=None):
        if not isinstance(payload.get("model"), str) or "/" not in payload["model"]:
            raise GatewayError("model 需要使用 平台/模型 格式，可通过 /v1/models 查询", 400, "invalid_model")
        provider_id, model = payload["model"].split("/", 1)
        if not model:
            raise GatewayError("模型名不能为空")
        provider = self.provider(provider_id, "chat")
        if not isinstance(payload.get("messages"), list) or not payload["messages"]:
            raise GatewayError("messages 必须是非空数组")
        account = self.select_account(provider_id, account_id)
        request = {**payload, "model": model, "stream": True}
        # Pin the chosen account for the entire request, including tool-call chunks.
        with self.account_lock(account["id"]):
            try:
                credential = self.store.credential(account["id"])
            except GatewayError as exc:
                if exc.code != "vault_unavailable" or not provider.capabilities.get("local_import"):
                    raise
                try:
                    local_rows = provider.import_local()
                except Exception:
                    raise exc
                match = next((item for item in local_rows
                              if isinstance(item, dict) and
                              str(item.get("external_id") or "") ==
                              str(account.get("external_id") or "")), None)
                credential = match.get("credential") if isinstance(match, dict) else None
                if not isinstance(credential, dict) or not credential:
                    raise exc
                self.store.save_credential(account["id"], credential)
            before = copy.deepcopy(credential)
            chunks = None
            finished = False
            try:
                chunks = provider.chat(credential, request)
                for chunk in chunks:
                    if not isinstance(chunk, dict):
                        raise GatewayError("平台返回非 JSON 聊天数据", 502)
                    if chunk.get("error"):
                        raise GatewayError("平台聊天接口返回错误", 502, "upstream_error")
                    for choice in chunk.get("choices", []):
                        # Some desktop transports emit an empty string on
                        # intermediate chunks; this is not a terminal status.
                        finished = finished or bool(choice.get("finish_reason"))
                    yield {**chunk, "model": payload["model"]}
                if not finished:
                    raise GatewayError("平台聊天流中断，未返回结束状态", 502, "incomplete_stream")
            finally:
                close = getattr(chunks, "close", None)
                if close:
                    close()
                if before != credential:
                    self.store.save_credential(account["id"], credential)

    def checkin(self, provider_id, dry=False):
        provider = self.provider(provider_id, "checkin")
        results = []
        local_credentials = None
        local_import_attempted = False

        def local_credential_for(account):
            nonlocal local_credentials, local_import_attempted
            if not local_import_attempted:
                local_import_attempted = True
                try:
                    local_credentials = provider.import_local()
                except Exception:  # Keep the original account-store/auth error actionable.
                    local_credentials = []
            match = next((item for item in (local_credentials or [])
                          if str(item.get("external_id") or "") ==
                          str(account.get("external_id") or "")), None)
            value = match.get("credential") if isinstance(match, dict) else None
            return copy.deepcopy(value) if isinstance(value, dict) else None

        for account in self.store.accounts(provider_id):
            if not account["enabled"]:
                continue
            with self.account_lock(account["id"]):
                try:
                    credential = self.store.credential(account["id"])
                    before = copy.deepcopy(credential)
                except GatewayError as exc:
                    if (provider_id == "workbuddy" and not dry and
                            exc.code == "vault_unavailable"):
                        credential = local_credential_for(account)
                        if credential is None:
                            results.append({"account": account["label"], "status": "failed",
                                            "message": str(exc)})
                            continue
                        # The local import confirms the same server-side account ID;
                        # save it under the current user after the check-in attempt.
                        before = None
                    else:
                        results.append({"account": account["label"], "status": "failed",
                                        "message": str(exc)})
                        continue
                try:
                    result = provider.checkin(credential, dry=dry)
                except GatewayError as exc:
                    # A stale Zhipu vault entry can hide a newer desktop cookie.
                    # Import it only after an explicit expired-credential response,
                    # and replace the saved credential only when the provider
                    # confirms that it belongs to this exact account.
                    if (provider_id == "zhipu" and not dry and
                            exc.code == "credential_expired"):
                        refreshed = local_credential_for(account)
                        if refreshed is not None:
                            credential = refreshed
                            try:
                                result = provider.checkin(credential, dry=False)
                            except GatewayError as retry_error:
                                result = {"status": "failed", "message": str(retry_error)}
                        else:
                            result = {"status": "failed", "message": str(exc)}
                    else:
                        result = {"status": "failed", "message": str(exc)}
                finally:
                    if before != credential:
                        self.store.save_credential(account["id"], credential)
                results.append({"account": account["label"], **result})
        if not results:
            raise GatewayError("该平台没有启用的账号", 409, "no_accounts")
        statuses = [r["status"] for r in results]
        status = ("failed" if "failed" in statuses else "success" if "success" in statuses else
                  "already" if all(s == "already" for s in statuses) else "skipped")
        return {"status": status, "message": "；".join(f"{r['account']}：{r['message']}" for r in results),
                "accounts": results}


GATEWAY = Gateway()
