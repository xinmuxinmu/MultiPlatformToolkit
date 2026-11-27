"""Pinned official circle HTTP requests with conservative state verification.

The protocol and field names come from the cached official fandom frontend.
Authentication is supplied by the caller. No response, credential or query
string is included in diagnostics, and no unrelated task is ever submitted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any

import httpx


BASE_URL = "https://fandom.starplaylet.com"
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
IS_LOGIN = "/ent/fandom/user/is_login"
META = "/ent/fandom/meta/"
CALENDAR = "/ent/fandom/user/calendar/"
JOIN = "/ent/fandom/join/"
_SCM_VERSION = "1.0.0.757"
_COMMON_BLOCKED = {
    "fandom_id", "business_id", "business_type", "join_type", "action", "level",
    "biz", "query_date", "user_id", "is_preview", "SceneMap", "scene_map",
}


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,20}", value):
        return int(value)
    return None


def _true(value: Any) -> bool:
    return value is True or (type(value) is int and value == 1)


def _now(value=None) -> datetime:
    if value is None:
        return datetime.now(SHANGHAI)
    if isinstance(value, datetime):
        return (value.replace(tzinfo=SHANGHAI) if value.tzinfo is None else value).astimezone(SHANGHAI)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, SHANGHAI)
    raise ValueError("now 需要时间戳或 datetime")


def _calendar_today(payload: dict | None, today: datetime, fandom_id: str) -> bool | None:
    """Read only the requested circle's current Shanghai month/day.

    Official calendar code maps months[].days[] with type=1/2 to signed days.
    The live API omits `days` for a month with month_signed_days=0. That
    explicit zero is unchecked; a positive count never identifies today.
    Missing both the array and its explicit zero remains unknown.
    """
    if not isinstance(payload, dict) or _integer(payload.get("status_code")) != 0:
        return None
    supplied_id = payload.get("fandom_id")
    if supplied_id is not None and str(supplied_id) != fandom_id:
        return None
    months = payload.get("months")
    if not isinstance(months, list):
        return None
    default_year = today.year
    end = payload.get("query_end_date")
    if end is not None:
        try:
            default_year = datetime.fromtimestamp(float(end), SHANGHAI).year
        except (OverflowError, OSError, TypeError, ValueError):
            return None
    for month in months:
        if not isinstance(month, dict):
            continue
        year = _integer(month.get("year", default_year))
        number = _integer(month.get("month"))
        if (year, number) != (today.year, today.month):
            continue
        days = month.get("days")
        if not isinstance(days, list):
            return False if "days" not in month and _integer(month.get("month_signed_days")) == 0 else None
        malformed = False
        for day in days:
            if not isinstance(day, dict):
                malformed = True
                continue
            try:
                # The official endpoint stores seconds, never milliseconds.
                seconds = float(day.get("date"))
                if abs(seconds) >= 100_000_000_000:
                    malformed = True
                    continue
                date = datetime.fromtimestamp(seconds, SHANGHAI).date()
            except (OverflowError, OSError, TypeError, ValueError):
                malformed = True
                continue
            if date == today.date() and _integer(day.get("type")) in (1, 2):
                return True
        return None if malformed else False
    return None


def _valid_circle(payload: dict | None, fandom_id: str) -> dict | None:
    if not isinstance(payload, dict) or _integer(payload.get("status_code")) != 0:
        return None
    fandom = payload.get("fandom")
    if not isinstance(fandom, dict) or str(fandom.get("fandom_id", "")) != fandom_id:
        return None
    if _true(payload.get("is_invalid")) or _true(fandom.get("IsInvalid")):
        return None
    return fandom


def _public_circle(fandom: dict | None) -> dict:
    if not fandom:
        return {}
    # Do not pass through server objects: they can contain login-related data.
    result = {}
    for key in ("fandom_id", "title", "fandom_type_name", "fandom_entity_name"):
        value = fandom.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            result[key] = str(value)[:200]
    group_id = fandom.get("discuss_group_id")
    if type(group_id) in (str, int) and re.fullmatch(r"[0-9]{1,32}", str(group_id)):
        result["discuss_group_id"] = str(group_id)
    live = fandom.get("live_fans_club_info")
    if isinstance(live, dict) and not _true(live.get("is_ban")):
        public_live = {}
        for key in ("anchor_uid", "room_id", "fans_type"):
            value = live.get(key)
            if key == "room_id" and value == "":
                # An offline anchor has no live room; the official task
                # request explicitly accepts an empty room_id in this case.
                public_live[key] = ""
            elif type(value) is int and value >= 0 and len(str(value)) <= 32:
                public_live[key] = value
            elif isinstance(value, str) and re.fullmatch(r"\d{1,32}", value):
                public_live[key] = value
        if public_live:
            result["live_fans_club_info"] = public_live
    return result


def _cookies(value, current: datetime) -> httpx.Cookies:
    jar = httpx.Cookies()
    if not isinstance(value, (list, tuple)):
        return jar
    for cookie in value:
        if not isinstance(cookie, dict):
            continue
        name, content = cookie.get("name"), cookie.get("value")
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        # Only the pinned target can receive this jar. Caller-supplied native
        # headers are separate; a douyin.com cookie is not silently re-scoped.
        if domain != "fandom.starplaylet.com" and domain != "starplaylet.com":
            continue
        if not isinstance(name, str) or not isinstance(content, str) or not name:
            continue
        expires = cookie.get("expires", cookie.get("expirationDate", -1))
        if isinstance(expires, (int, float)) and expires > 0 and expires <= current.timestamp():
            continue
        path = cookie.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            path = "/"
        jar.set(name, content, domain=str(cookie.get("domain")), path=path)
    return jar


def fetch_circle(fandom_id, *, account_id="", cookies=None, headers=None,
                 common_params=None, checkin=False, now=None, transport=None):
    """Read authenticated official state; optionally sign in and re-read it.

    `common_params` supplies native host query parameters and is private.
    Request-specific business fields always take precedence over that input.
    The caller must bind credentials to the intended account before invoking
    this method; this function never chooses or silently switches accounts.
    """
    fandom_id = str(fandom_id or "")
    if not re.fullmatch(r"\d{1,32}", fandom_id):
        raise ValueError("圈子 ID 需要是数字")
    current = _now(now)
    attempts = []
    probe = {"checked_at": current.isoformat(timespec="seconds"),
             "account_id": str(account_id), "used_account_cookies": False,
             "attempts": attempts}
    output = {"authenticated": False, "circle": {}, "join_status": None,
              "signed_today": None, "probe": probe,
              "result": {"status": "unconfirmed", "verified": False, "message": "尚未确认圈子状态。"}}
    common = {"scm_version": _SCM_VERSION, "from_app": "fandom_detail_douyin"}
    if isinstance(common_params, dict):
        common.update({str(k): v for k, v in common_params.items()
                       if isinstance(k, str) and k not in _COMMON_BLOCKED
                       and isinstance(v, (str, int, float, bool)) and v is not None})
    request_headers = {"User-Agent": "Mozilla/5.0", "x-scm-version": _SCM_VERSION}
    if isinstance(headers, dict):
        forbidden = {"host", "proxy-authorization", "content-length", "connection", "transfer-encoding"}
        request_headers.update({str(k): str(v) for k, v in headers.items()
                                if isinstance(k, str) and k.lower() not in forbidden
                                and isinstance(v, (str, int))})

    def set_result(status, message, verified=False):
        output["result"] = {"status": status, "verified": verified, "message": message}
        return output

    with httpx.Client(base_url=BASE_URL, headers=request_headers,
                      cookies=_cookies(cookies, current), timeout=httpx.Timeout(18, connect=8),
                      follow_redirects=False, transport=transport) as client:
        def request(method, path, params):
            attempt = {"method": method, "endpoint": BASE_URL + path,
                       "http_status": None, "body_kind": "network_error",
                       "business_code": None, "message": ""}
            attempts.append(attempt)
            try:
                response = client.request(method, path, params={**common, **params})
                probe["used_account_cookies"] |= bool(response.request.headers.get("cookie"))
            except httpx.HTTPError as exc:
                # Exception strings contain URLs and potentially query secrets.
                try:
                    probe["used_account_cookies"] |= bool(exc.request.headers.get("cookie"))
                except RuntimeError:
                    pass
                attempt["message"] = "请求没有完成：" + type(exc).__name__
                return None
            attempt["http_status"] = response.status_code
            if not response.content.strip():
                attempt.update(body_kind="empty", message="响应体为空，不能确认接口状态。")
                return None
            try:
                payload = response.json()
            except (ValueError, UnicodeDecodeError):
                kind = "html" if response.text.lstrip().lower().startswith(("<!doctype html", "<html")) else "text"
                attempt.update(body_kind=kind, message="响应不是可验证的 JSON 数据。")
                return None
            attempt["body_kind"] = "json"
            if not isinstance(payload, dict):
                attempt["message"] = "JSON 数据结构不能确认业务状态。"
                return None
            code = _integer(payload.get("status_code"))
            attempt["business_code"] = code
            if response.is_redirect:
                attempt["message"] = "接口返回跳转，未携带凭据跟随。"
                return None
            if response.is_error:
                attempt["message"] = "HTTP 请求未通过，未采用响应中的状态。"
                return None
            if code == 8:
                attempt["message"] = "用户未登录；当前凭据尚未获得趣追认证。"
            elif code == 0:
                attempt["message"] = "接口返回业务成功；任务完成仍以状态复查为准。"
            elif code is None:
                attempt["message"] = "响应没有可验证的业务码。"
            else:
                attempt["message"] = "接口返回业务错误，未采用任务完成状态。"
            return payload

        def read_meta():
            return _valid_circle(request("GET", META, {"fandom_id": fandom_id, "level": 0}), fandom_id)

        def read_calendar():
            payload = request("GET", CALENDAR,
                              {"business_id": fandom_id, "business_type": 3, "level": 0})
            return _calendar_today(payload, current, fandom_id)

        login = request("GET", IS_LOGIN, {"biz": 1})
        output["authenticated"] = bool(isinstance(login, dict)
                                        and _integer(login.get("status_code")) == 0
                                        and _true(login.get("is_login")))
        fandom = read_meta()
        calendar = read_calendar()
        if fandom:
            output["circle"] = _public_circle(fandom)
            value = _integer(fandom.get("user_join_status"))
            output["join_status"] = value if value in (0, 1, 2) else None
        # Do not expose signed calendar results from an unauthenticated or
        # mismatched circle response as the current account's state.
        if output["authenticated"] and fandom:
            output["signed_today"] = calendar
        if not output["authenticated"]:
            return set_result("authentication_required", "趣追接口尚未确认登录，需要有效的客户端认证。")
        if fandom is None:
            return set_result("invalid_circle", "未取得与当前圈子 ID 一致的有效圈子资料。")
        if ((calendar is True and output["join_status"] != 2)
                or (calendar is False and output["join_status"] == 2)):
            return set_result("unconfirmed", "圈子资料与今日日历的签到状态不一致，未提交签到。")
        if output["signed_today"] is True and output["join_status"] == 2:
            return set_result("already", "官方圈子资料及今日日历均确认已签到。", True)
        if not checkin:
            message = "已读取官方圈子资料，今日日历显示尚未签到。" if calendar is False else "已读取官方圈子资料，今日签到状态尚不能完整确认。"
            return set_result("observed_http", message)
        if output["join_status"] == 0:
            return set_result("join_required", "当前账号尚未加入圈子，未自动入圈或签到。")
        if output["join_status"] != 1:
            return set_result("unconfirmed", "圈子加入或签到状态尚不能确认，未提交签到。")
        if calendar is not False:
            return set_result("unconfirmed", "未能明确读取今日日历，未提交签到。")
        signed = request("POST", JOIN, {"business_id": fandom_id, "fandom_id": fandom_id,
                         "business_type": 3, "join_type": 3, "action": 1, "level": 0})
        if not isinstance(signed, dict) or _integer(signed.get("status_code")) != 0:
            if isinstance(signed, dict) and _integer(signed.get("status_code")) == 8:
                output["authenticated"] = False
            return set_result("checkin_failed", "签到请求未通过，未记录为签到成功。")
        after = read_meta()
        after_calendar = read_calendar()
        output["circle"] = _public_circle(after) if after else {}
        after_join = _integer(after.get("user_join_status")) if after else None
        output["join_status"] = after_join if after_join in (0, 1, 2) else None
        output["signed_today"] = after_calendar if after else None
        if after and after_join == 2 and after_calendar is True:
            return set_result("success", "签到后重新读取官方圈子资料及今日日历，已确认今日签到。", True)
        return set_result("unconfirmed", "签到请求返回后未能复查确认今日已签到，不记为成功。")
