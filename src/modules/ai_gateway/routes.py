from __future__ import annotations

import hmac
from itertools import chain
import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
import anyio

from .base import GatewayError
from .compat import anthropic_events, anthropic_to_openai, collect_completion, completion_to_anthropic
from .core import GATEWAY


def public_error(exc):
    if isinstance(exc, GatewayError):
        return {"message": str(exc), "type": "gateway_error", "code": exc.code}
    return {"message": "平台请求失败，请检查网络和账号状态", "type": "gateway_error", "code": "upstream_error"}


async def error_response(_request, exc):
    return JSONResponse({"error": public_error(exc)}, status_code=exc.status_code)


def local_management(request: Request):
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise GatewayError("账号管理仅允许从本机访问", 403, "local_only")
    hostname = request.url.hostname
    if hostname not in ("127.0.0.1", "::1", "localhost"):
        raise GatewayError("账号管理需要通过本机地址访问", 403, "local_only")
    origin = request.headers.get("origin")
    if origin and (urlsplit(origin).scheme not in ("http", "https") or
                   urlsplit(origin).netloc != request.url.netloc):
        raise GatewayError("拒绝其他网站发起的账号管理请求", 403, "invalid_origin")


def authenticate(request: Request):
    key = request.headers.get("x-api-key", "")
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        key = authorization[7:].strip()
    if not key or not hmac.compare_digest(key, GATEWAY.store.api_key()):
        raise GatewayError("网关 API Key 无效", 401, "invalid_api_key")


router = APIRouter(prefix="/ai-gateway", dependencies=[Depends(local_management)])
proxy_router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)])


@router.get("/", include_in_schema=False)
def page():
    return FileResponse(Path(__file__).resolve().parents[2] / "web_ui/modules/ai_gateway/ui.html",
                        headers={"Cache-Control": "no-store"})


@router.get("/api/state")
def state(request: Request):
    return {**GATEWAY.state(), "base_url": str(request.base_url).rstrip("/") + "/v1"}


@router.get("/api/key")
def key():
    return JSONResponse({"api_key": GATEWAY.store.api_key()}, headers={"Cache-Control": "no-store"})


@router.post("/api/key/rotate")
def rotate_key():
    return JSONResponse({"api_key": GATEWAY.store.api_key(rotate=True)}, headers={"Cache-Control": "no-store"})


def _operation(call):
    try:
        return call()
    except GatewayError:
        raise
    except Exception:
        raise GatewayError("平台请求失败，请检查网络和账号状态", 502, "upstream_error") from None


@router.post("/api/login/{provider_id}")
def start_login(provider_id: str, body: dict = Body(default={})):
    return _operation(lambda: GATEWAY.start_login(provider_id, body))


@router.get("/api/login/{session_id}")
def poll_login(session_id: str):
    return _operation(lambda: GATEWAY.poll_login(session_id))


@router.delete("/api/login/{session_id}")
def cancel_login(session_id: str):
    GATEWAY.cancel_login(session_id)
    return {"ok": True}


@router.post("/api/import/{provider_id}")
def import_local(provider_id: str):
    return _operation(lambda: GATEWAY.import_local(provider_id))


@router.patch("/api/accounts/{account_id}")
def update_account(account_id: str, body: dict = Body(...)):
    return GATEWAY.store.update(account_id, label=body.get("label"), enabled=body.get("enabled"))


@router.delete("/api/accounts/{account_id}")
def delete_account(account_id: str):
    GATEWAY.store.delete(account_id)
    return {"ok": True}


@router.get("/api/models")
def management_models():
    return {"object": "list", "data": _operation(GATEWAY.models)}


@proxy_router.get("/models")
def models():
    return {"object": "list", "data": _operation(GATEWAY.models)}


def _primed(payload, account_id):
    chunks = GATEWAY.chat(payload, account_id)
    try:
        first = _operation(lambda: next(chunks))
    except Exception:
        chunks.close()
        raise
    return chunks, chain([first], chunks)


def _sse(chunks, *, anthropic=False, owner=None):
    try:
        for value in chunks:
            if anthropic:
                event, data = value
                yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            else:
                yield "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"
        if not anthropic:
            yield "data: [DONE]\n\n"
    except Exception as exc:
        data = {"error": public_error(exc)}
        if anthropic:
            yield "event: error\ndata: " + json.dumps({"type": "error", **data}, ensure_ascii=False) + "\n\n"
        else:
            yield "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"
    finally:
        close = getattr(owner if owner is not None else chunks, "close", None)
        if close:
            close()


class OwnedStream(StreamingResponse):
    def __init__(self, *args, owner, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(self.owner.close)


@proxy_router.post("/chat/completions")
def chat(request: Request, body: dict = Body(...)):
    if body.get("n", 1) != 1:
        raise GatewayError("当前仅支持 n=1", 400, "unsupported_parameter")
    chunks, primed = _primed(body, request.headers.get("x-ai-account"))
    if body.get("stream"):
        return OwnedStream(_sse(primed, owner=chunks), owner=chunks, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                                 background=BackgroundTask(chunks.close))
    try:
        return _operation(lambda: collect_completion(primed, body["model"]))
    finally:
        chunks.close()


@proxy_router.post("/messages")
def messages(request: Request, body: dict = Body(...)):
    try:
        payload = anthropic_to_openai(body)
    except (KeyError, TypeError, AttributeError):
        raise GatewayError("Anthropic 消息或工具参数格式无效", 400, "invalid_request") from None
    chunks, primed = _primed(payload, request.headers.get("x-ai-account"))
    if body.get("stream"):
        return OwnedStream(_sse(anthropic_events(primed, body["model"]), anthropic=True, owner=chunks), owner=chunks,
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                                 background=BackgroundTask(chunks.close))
    try:
        return _operation(lambda: completion_to_anthropic(collect_completion(primed, body["model"])))
    finally:
        chunks.close()
