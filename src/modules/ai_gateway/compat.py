"""OpenAI result aggregation and Anthropic message/event conversion."""
from __future__ import annotations

import json
import time
import uuid

from .base import GatewayError


def collect_completion(chunks, model):
    result = dict(id="chatcmpl-" + uuid.uuid4().hex, object="chat.completion",
                  created=int(time.time()), model=model, choices=[])
    choices = {}
    for chunk in chunks:
        for key in ("id", "created", "usage", "system_fingerprint"):
            if key in chunk:
                result[key] = chunk[key]
        for part in chunk.get("choices", []):
            index = part.get("index", 0)
            choice = choices.setdefault(index, dict(index=index, message={"role": "assistant", "content": ""},
                                                    finish_reason=None))
            delta, message = part.get("delta") or {}, choice["message"]
            for field in ("content", "reasoning_content", "refusal"):
                if delta.get(field) is not None:
                    message[field] = message.get(field, "") + delta[field]
            for tool in delta.get("tool_calls", []):
                tools = message.setdefault("tool_calls", [])
                tool_index = tool.get("index", 0)
                if not isinstance(tool_index, int) or not 0 <= tool_index < 128:
                    raise GatewayError("平台返回无效工具索引", 502)
                while len(tools) <= tool_index:
                    tools.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                target = tools[tool_index]
                if tool.get("id"):
                    target["id"] = tool["id"]
                if tool.get("type"):
                    target["type"] = tool["type"]
                for field in ("name", "arguments"):
                    target["function"][field] += (tool.get("function") or {}).get(field) or ""
            if part.get("finish_reason") is not None:
                choice["finish_reason"] = part["finish_reason"]
    for choice in choices.values():
        if not choice["message"]["content"] and choice["message"].get("tool_calls"):
            choice["message"]["content"] = None
    result["choices"] = [choices[i] for i in sorted(choices)]
    return result


def _text_blocks(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(b, dict) and b.get("type") == "text" for b in value):
        return "\n".join(b.get("text", "") for b in value)
    raise GatewayError("当前网关仅支持文本 system 与文本 tool_result", 400, "unsupported_content")


def anthropic_to_openai(body):
    unsupported = [k for k in ("thinking", "betas", "mcp_servers", "container") if body.get(k)]
    if unsupported:
        raise GatewayError("尚未支持 Anthropic 参数：" + ", ".join(unsupported), 400, "unsupported_parameter")
    if not isinstance(body.get("max_tokens"), int) or isinstance(body["max_tokens"], bool) or body["max_tokens"] <= 0:
        raise GatewayError("max_tokens 必须是正整数")
    messages = []
    if body.get("system"):
        messages.append({"role": "system", "content": _text_blocks(body["system"])})
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        raise GatewayError("messages 必须是非空数组")
    for item in body["messages"]:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
            raise GatewayError("Anthropic messages.role 必须是 user 或 assistant")
        content = item.get("content", "")
        if isinstance(content, str):
            messages.append({"role": item["role"], "content": content})
            continue
        if not isinstance(content, list):
            raise GatewayError("消息内容必须是字符串或内容块数组")
        normal, tools, tool_results = [], [], []
        for block in content:
            kind = block.get("type") if isinstance(block, dict) else None
            if kind == "text":
                normal.append({"type": "text", "text": block.get("text", "")})
            elif kind == "image":
                source = block.get("source") or {}
                if source.get("type") != "base64" or not str(source.get("media_type", "")).startswith("image/"):
                    raise GatewayError("图片仅支持 base64 image source", 400, "unsupported_content")
                normal.append({"type": "image_url", "image_url": {"url": f"data:{source['media_type']};base64,{source.get('data', '')}"}})
            elif kind == "tool_use" and item["role"] == "assistant":
                tools.append({"id": block["id"], "type": "function", "function": {"name": block["name"],
                              "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False)}})
            elif kind == "tool_result" and item["role"] == "user":
                text = _text_blocks(block.get("content", ""))
                if block.get("is_error"):
                    text = "Tool error: " + text
                tool_results.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": text})
            else:
                raise GatewayError("尚未支持的消息内容块：" + str(kind), 400, "unsupported_content")
        messages.extend(tool_results)
        if normal or tools:
            row = {"role": item["role"], "content": normal or None}
            if tools:
                row["tool_calls"] = tools
            messages.append(row)
    result = {"model": body.get("model"), "messages": messages, "max_tokens": body["max_tokens"],
              "stream": bool(body.get("stream"))}
    for key in ("temperature", "top_p"):
        if key in body:
            result[key] = body[key]
    if body.get("stop_sequences"):
        result["stop"] = body["stop_sequences"]
    if body.get("tools"):
        result["tools"] = [{"type": "function", "function": {"name": t["name"],
                            "description": t.get("description", ""), "parameters": t.get("input_schema", {})}}
                           for t in body["tools"]]
    if body.get("tool_choice"):
        choice = body["tool_choice"]
        kind = choice.get("type")
        if kind == "tool":
            result["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
        elif kind in ("auto", "any", "none"):
            result["tool_choice"] = {"any": "required"}.get(kind, kind)
        else:
            raise GatewayError("不支持的 tool_choice")
        if "disable_parallel_tool_use" in choice:
            result["parallel_tool_calls"] = not choice["disable_parallel_tool_use"]
    return result


def _stop_reason(value):
    return {"tool_calls": "tool_use", "length": "max_tokens", "stop": "end_turn",
            "content_filter": "refusal"}.get(value, "end_turn")


def completion_to_anthropic(completion):
    choices = completion.get("choices") or []
    if not choices:
        raise GatewayError("平台未返回聊天内容", 502)
    choice, blocks = choices[0], []
    message = choice["message"]
    if message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    for tool in message.get("tool_calls", []):
        try:
            args = json.loads(tool["function"]["arguments"] or "{}")
        except ValueError:
            raise GatewayError("平台返回的工具参数不是完整 JSON", 502) from None
        if not isinstance(args, dict):
            raise GatewayError("平台返回的工具参数必须是 JSON 对象", 502)
        blocks.append({"type": "tool_use", "id": tool["id"], "name": tool["function"]["name"], "input": args})
    usage = completion.get("usage") or {}
    return dict(id="msg_" + completion["id"], type="message", role="assistant", model=completion["model"],
                content=blocks, stop_reason=_stop_reason(choice.get("finish_reason")), stop_sequence=None,
                usage={"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)})


def anthropic_events(chunks, model):
    mid = "msg_" + uuid.uuid4().hex
    yield ("message_start", {"type": "message_start", "message": {"id": mid, "type": "message",
           "role": "assistant", "model": model, "content": [], "stop_reason": None, "stop_sequence": None,
           "usage": {"input_tokens": 0, "output_tokens": 0}}})
    tool_states, next_index, reason, usage = {}, 0, "stop", {}
    text_index = None
    for chunk in chunks:
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            text = delta.get("content") or delta.get("refusal")
            if text:
                if text_index is None:
                    text_index, next_index = next_index, next_index + 1
                    yield ("content_block_start", {"type": "content_block_start", "index": text_index,
                           "content_block": {"type": "text", "text": ""}})
                yield ("content_block_delta", {"type": "content_block_delta", "index": text_index,
                       "delta": {"type": "text_delta", "text": text}})
            for tool in delta.get("tool_calls", []):
                state = tool_states.setdefault(tool.get("index", 0), {"id": "", "name": "", "arguments": []})
                state["id"] = tool.get("id") or state["id"]
                fn = tool.get("function") or {}
                state["name"] += fn.get("name") or ""
                if fn.get("arguments"):
                    state["arguments"].append(fn["arguments"])
            if choice.get("finish_reason") is not None:
                reason = choice["finish_reason"]
    if text_index is not None:
        yield ("content_block_stop", {"type": "content_block_stop", "index": text_index})
    # OpenAI may fragment function names. Wait for the complete name, then emit
    # each Anthropic tool block in sequence, retaining its argument fragments.
    for key in sorted(tool_states):
        state = tool_states[key]
        if not state["id"] or not state["name"]:
            raise GatewayError("平台返回的工具调用缺少 id 或名称", 502)
        try:
            args = json.loads("".join(state["arguments"]) or "{}")
        except ValueError:
            raise GatewayError("平台返回的工具参数不是完整 JSON", 502) from None
        if not isinstance(args, dict):
            raise GatewayError("平台返回的工具参数必须是 JSON 对象", 502)
        index, next_index = next_index, next_index + 1
        yield ("content_block_start", {"type": "content_block_start", "index": index,
               "content_block": {"type": "tool_use", "id": state["id"], "name": state["name"], "input": {}}})
        for fragment in state["arguments"] or ["{}"]:
            yield ("content_block_delta", {"type": "content_block_delta", "index": index,
                   "delta": {"type": "input_json_delta", "partial_json": fragment}})
        yield ("content_block_stop", {"type": "content_block_stop", "index": index})
    yield ("message_delta", {"type": "message_delta", "delta": {"stop_reason": _stop_reason(reason),
           "stop_sequence": None}, "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                                                   "output_tokens": usage.get("completion_tokens", 0)}})
    yield ("message_stop", {"type": "message_stop"})
