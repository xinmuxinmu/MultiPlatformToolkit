"""Public, side-effect-free connection details shared by gateway clients."""
from __future__ import annotations

from collections import Counter
from urllib.parse import urlsplit, urlunsplit


_SINGLE_USER_TEXT_PROVIDERS = frozenset({"trae", "zhipu"})
_OPENAI_FORMAT = "openai-chat-completions"
_ANTHROPIC_FORMAT = "anthropic-messages"


def _public_base_url(base_url):
    parts = urlsplit(str(base_url))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    # A connection URL must never propagate URL credentials, queries or fragments.
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    authority = host + (f":{parts.port}" if parts.port is not None else "")
    path = parts.path.rstrip("/")
    if not path.endswith("/v1"):
        path += "/v1"
    return urlunsplit((parts.scheme, authority, path, "", ""))


def _usage_note(chat_enabled, enabled_accounts, model_count, single_user_text):
    notes = []
    if not enabled_accounts:
        notes.append("当前没有启用账号，请先扫码或导入本机登录态。")
    if not model_count:
        notes.append("当前模型目录为空，请检查账号登录状态和网络后刷新。")
    if not chat_enabled:
        notes.append("该平台尚未接入统一网关聊天，当前没有可调用的聊天接口。")
        return "".join(notes)
    if single_user_text:
        notes.append("仅支持单轮纯文本：messages 只能包含一条 user 字符串消息；"
                     "不支持 system、历史消息、tools、图片、max_tokens、temperature。"
                     "IDE 默认 Agent 请求不兼容，请使用普通单轮聊天模式。")
    else:
        notes.append("支持 OpenAI Chat Completions 与 Anthropic Messages 接入；"
                     "tools 与 Agent 兼容性取决于上游模型，未逐项验证。")
    return "".join(notes)


def build_connection_info(base_url, providers, accounts, models):
    """Build copyable public configuration without reading a key or calling a provider.

    ``providers`` and ``accounts`` are the lists returned by ``Gateway.state()``;
    ``models`` is the current qualified catalog returned by ``Gateway.models()``.
    Only explicitly selected public fields leave this function.
    """
    base_url = _public_base_url(base_url)
    endpoints = {
        "models": base_url + "/models",
        "chat_completions": base_url + "/chat/completions",
        "messages": base_url + "/messages",
    }
    public_providers = []
    provider_by_id = {}
    enabled_accounts = Counter(
        row["provider"] for row in accounts
        if isinstance(row, dict) and row.get("enabled") is True
        and isinstance(row.get("provider"), str)
    )
    for row in providers:
        if not isinstance(row, dict):
            continue
        provider_id = row.get("id")
        if not isinstance(provider_id, str) or not provider_id or provider_id in provider_by_id:
            continue
        capabilities = row.get("capabilities")
        chat_enabled = isinstance(capabilities, dict) and capabilities.get("chat") is True
        single_user_text = provider_id in _SINGLE_USER_TEXT_PROVIDERS
        formats = [_OPENAI_FORMAT] if chat_enabled else []
        if chat_enabled and not single_user_text:
            formats.append(_ANTHROPIC_FORMAT)
        provider = {
            "id": provider_id,
            "name": row["name"] if isinstance(row.get("name"), str) else provider_id,
            "chat_enabled": chat_enabled,
            "enabled_accounts": enabled_accounts[provider_id],
            "model_count": 0,
            "api_formats": formats,
            "input_mode": "single_user_text" if single_user_text else "messages",
            "tools_supported": False if single_user_text else None,
            "agent_compatible": False if single_user_text else None,
        }
        provider_by_id[provider_id] = provider
        public_providers.append(provider)

    public_models = []
    seen = set()
    for row in models:
        model_id = row.get("id") if isinstance(row, dict) else None
        if not isinstance(model_id, str) or "/" not in model_id or model_id in seen:
            continue
        provider_id, model_name = model_id.split("/", 1)
        provider = provider_by_id.get(provider_id)
        if not provider or not provider["chat_enabled"] or not model_name.strip():
            continue
        seen.add(model_id)
        provider["model_count"] += 1
        public_models.append({
            "id": model_id, "provider": provider_id, "provider_name": provider["name"],
            "api_formats": list(provider["api_formats"]),
            "input_mode": provider["input_mode"],
            "tools_supported": provider["tools_supported"],
            "agent_compatible": provider["agent_compatible"],
        })
    for provider in public_providers:
        provider["usage_note"] = _usage_note(
            provider["chat_enabled"], provider["enabled_accounts"],
            provider["model_count"], provider["input_mode"] == "single_user_text")
    for model in public_models:
        model["usage_note"] = provider_by_id[model["provider"]]["usage_note"]

    preferred = next((row for row in public_models if row["id"] == "workbuddy/default"), None)
    if preferred is None:
        preferred = next((row for row in public_models if row["input_mode"] != "single_user_text"),
                         public_models[0] if public_models else None)
    example_model = preferred["id"] if preferred else "平台/模型"
    return {
        "base_url": base_url,
        "endpoints": endpoints,
        "authentication": {
            "header": "Authorization", "value_template": "Bearer <API_KEY>",
            "alternative_header": "x-api-key",
        },
        "client_fields": {
            "api_format": "OpenAI Chat Completions", "base_url": base_url,
            "full_url": endpoints["chat_completions"],
            "full_url_help": "仅在客户端要求完整接口地址时填写此地址；Base URL 字段使用上面的 /v1 地址，避免重复拼接路径。",
            "model_id_help": "从本页模型目录复制完整 ID（平台/模型），如 workbuddy/default；不能只填模型名称。",
            "api_key_help": "填写本工具中查看或复制的统一网关 API Key，不要填写平台登录 token。",
        },
        "summary": {
            "model_count": len(public_models),
            "chat_provider_count": sum(row["chat_enabled"] for row in public_providers),
            "enabled_account_count": sum(enabled_accounts.values()),
        },
        "models": public_models,
        "providers": public_providers,
        "client_guidance": [
            "模型字段必须复制目录中的完整 ID（平台/模型），如 workbuddy/default；不能只填裸模型名。",
            "普通 OpenAI 客户端使用 Base URL；仅完整接口地址字段使用 Full URL。",
            "API Key 使用本工具生成的统一网关 Key，可通过 Authorization: Bearer 或 x-api-key 发送。",
            "Trae CN 与智谱清言仅支持 OpenAI 单轮纯文本；不兼容 IDE 默认 Agent 请求。",
            "其他平台的 tools 和 Agent 兼容性取决于上游模型，未逐项验证。",
            "刷新接入信息只查询模型目录；需要手动发送请求才会发起聊天。",
        ],
        "example": {
            "method": "POST", "url": endpoints["chat_completions"],
            "headers": {"Authorization": "Bearer <API_KEY>", "Content-Type": "application/json"},
            "body": {"model": example_model,
                     "messages": [{"role": "user", "content": "Reply exactly OK"}], "stream": True},
        },
    }
