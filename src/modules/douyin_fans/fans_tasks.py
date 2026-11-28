"""Read the official live-fans task list without executing any task.

The caller binds credentials to the intended account. Shared Douyin cookies
are passed explicitly in the Cookie header; a domain-scoped jar is never
silently re-scoped. No server object, schema URL or credential is returned.
"""
from __future__ import annotations

import re
import time

import httpx


BASE_URL = "https://webcast.amemv.com"
TASK_PATH = "/webcast/fansclub/homepage/"
_TASKS = {
    9997: ("heat_checkin", "checkin"),
    1: ("light", "paid"),
    3: ("gift", "paid"),
    2: ("live", "playback"),
    11000: ("publish", "manual"),
    11001: ("discussion", "manual"),
    11002: ("invite", "manual"),
}
_FORBIDDEN_HEADERS = {"host", "proxy-authorization", "content-length", "connection", "transfer-encoding"}


def _integer(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,20}", value):
        return int(value)
    return None


def _text(value, limit=600, secrets=()):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[已隐藏]")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text).strip()[:limit]


def _secrets(headers, cookies):
    values = set()
    for name, value in headers.items():
        lowered = name.lower()
        if lowered == "cookie":
            values.add(value)
            values.update(part.split("=", 1)[1].strip() for part in value.split(";") if "=" in part)
        elif lowered in {"authorization", "x-tt-token", "x-tma-host-sessionid"}:
            values.add(value)
    for cookie in cookies if isinstance(cookies, (list, tuple)) else ():
        if isinstance(cookie, dict) and isinstance(cookie.get("value"), str):
            values.add(cookie["value"])
    return sorted((value for value in values if len(value) >= 4), key=len, reverse=True)


def _target_cookies(value):
    jar = httpx.Cookies()
    for cookie in value if isinstance(value, (list, tuple)) else ():
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        if domain not in {"webcast.amemv.com", "amemv.com"}:
            continue
        name, content = cookie.get("name"), cookie.get("value")
        if not isinstance(name, str) or not name or not isinstance(content, str):
            continue
        expires = cookie.get("expires", cookie.get("expirationDate", -1))
        if isinstance(expires, (int, float)) and expires > 0 and expires <= time.time():
            continue
        path = cookie.get("path")
        jar.set(name, content, domain=str(cookie["domain"]),
                path=path if isinstance(path, str) and path.startswith("/") else "/")
    return jar


def _task(item, index, secrets):
    if not isinstance(item, dict):
        return None
    task_type = _integer(item.get("task_type"))
    task_id, kind = _TASKS.get(task_type, (f"http_task_{task_type}" if task_type is not None else f"http_task_unknown_{index}", "manual"))
    title = _text(item.get("title"), 180, secrets) or (f"官方任务 {task_type}" if task_type is not None else "官方任务")
    description = _text(item.get("description"), 600, secrets)
    button = item.get("button")
    button_text = _text(button.get("content"), 100, secrets) if isinstance(button, dict) else ""
    completed, total = _integer(item.get("completed_num")), _integer(item.get("total_num"))
    progress = None
    progress_text = ""
    if completed is not None and total is not None and completed >= 0 and total >= 0:
        progress = {"completed_num": completed, "total_num": total}
        if total > 0:
            progress_text = f"进度 {completed} / {total}"
    status = " · ".join(part for part in (progress_text, button_text) if part)
    if not status:
        status = description or "官方接口未提供可读进度"
    reason = {
        "paid": "涉及灯牌、礼物或钻石消费，仅显示官方进度，程序不执行。",
        "playback": "需本人真实观看并由官方计时；这里只读取官方进度，不上报或伪造时长。",
        "checkin": "任务入口状态不等于签到结果；今日签到以圈子资料及日历复查为准。",
        "manual": "需本人在官方客户端核对并完成；程序仅读取任务状态，不自动发布、邀请或消费。",
    }[kind]
    return {
        "id": task_id, "title": title, "reference": description, "kind": kind,
        "status": status, "source": "official_http_snapshot",
        "observed_text": "\n".join(part for part in (title, description, progress_text, button_text) if part),
        "progress": progress, "manual_url": "", "can_open": False,
        # No server-provided task schema is trusted as a deep link. Known free
        # tasks can still open the configured circle entry for manual handling.
        "can_open_entry": task_id in {"heat_checkin", "live", "publish", "discussion", "invite"},
        "can_auto_checkin": False, "reason": reason,
        "api_task_type": task_type, "api_status": _integer(item.get("status")),
    }


def fetch_tasks(live_fans_club_info, *, headers=None, cookies=None, transport=None):
    """Read tasks for the exact public anchor described by circle metadata."""
    output = {"ok": False, "tasks": [], "attempts": [], "message": "尚未取得官方粉丝任务。"}
    if not isinstance(live_fans_club_info, dict):
        output["message"] = "圈子资料未提供有效的粉丝团入口。"
        return output
    anchor = str(live_fans_club_info.get("anchor_uid") or "")
    room = str(live_fans_club_info.get("room_id") or "")
    mode = _integer(live_fans_club_info.get("fans_type"))
    if not re.fullmatch(r"\d{1,32}", anchor) or (room and not re.fullmatch(r"\d{1,32}", room)) or mode is None or mode < 0:
        output["message"] = "粉丝团入口参数不能确认，未猜测主播或任务类型。"
        return output
    params = {"anchor_id": anchor, "anchor_open_id": anchor, "room_id": room,
              "source": 2, "fansclub_mode": mode, "aid": 1128, "device_platform": "android"}
    request_headers = {"User-Agent": "Mozilla/5.0"}
    if isinstance(headers, dict):
        request_headers.update({str(key): str(value) for key, value in headers.items()
                                if isinstance(key, str) and key.lower() not in _FORBIDDEN_HEADERS
                                and isinstance(value, (str, int))})
    secrets = _secrets(request_headers, cookies)
    attempt = {"method": "GET", "endpoint": BASE_URL + TASK_PATH,
               "http_status": None, "body_kind": "network_error",
               "business_code": None, "message": ""}
    output["attempts"].append(attempt)

    def failed(message):
        output["message"] = attempt["message"] = message
        return output

    try:
        with httpx.Client(base_url=BASE_URL, headers=request_headers,
                          cookies=_target_cookies(cookies), timeout=httpx.Timeout(18, connect=8),
                          follow_redirects=False, transport=transport) as client:
            response = client.get(TASK_PATH, params=params)
    except (httpx.HTTPError, ValueError, UnicodeError) as exc:
        return failed("官方任务请求没有完成：" + type(exc).__name__)
    attempt["http_status"] = response.status_code
    if response.is_redirect:
        attempt["body_kind"] = "redirect"
        return failed("任务接口返回跳转，未携带凭据跟随。")
    if not response.content.strip():
        attempt["body_kind"] = "empty"
        return failed("任务接口响应为空，不能确认任务状态。")
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        attempt["body_kind"] = "html" if response.text.lstrip().lower().startswith(("<!doctype html", "<html")) else "text"
        return failed("任务接口没有返回可验证的 JSON 数据。")
    attempt["body_kind"] = "json"
    if not isinstance(payload, dict):
        return failed("任务接口 JSON 结构不能确认业务状态。")
    code = _integer(payload.get("status_code"))
    attempt["business_code"] = code
    if response.is_error:
        return failed("任务接口 HTTP 请求未通过，未采用返回状态。")
    if code != 0:
        return failed("官方任务接口尚未确认登录。" if code == 8 else "任务接口返回业务错误，未采用任务状态。")
    data = payload.get("data")
    tasks = data.get("user_task") if isinstance(data, dict) else None
    items = tasks.get("items") if isinstance(tasks, dict) else None
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        return failed("任务接口未提供可确认的 user_task.items 列表。")
    if len(items) > 200:
        return failed("任务列表超出可核对范围，未采用返回状态。")
    normalized = [_task(item, index, secrets) for index, item in enumerate(items)]
    # Unknown or repeated types still need stable distinct row identities.
    seen = set()
    for task in normalized:
        original = task["id"]
        suffix = 2
        while task["id"] in seen:
            task["id"] = f"{original}_{suffix}"
            suffix += 1
        seen.add(task["id"])
    output.update(ok=True, tasks=normalized,
                  message=f"已读取 {len(normalized)} 项官方粉丝任务；状态和进度按本次接口记录显示。")
    attempt["message"] = output["message"]
    return output
