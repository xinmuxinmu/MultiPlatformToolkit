"""Read and cross-check official discussion metadata without publishing.

The pinned GET and business fields follow the cached official fandom frontend.
Only allowlisted identifiers and a derived comment count leave this module;
response text, user objects, credentials and redirect targets never do.
"""
from __future__ import annotations

import json
import re
import time

import httpx


BASE_URL = "https://aweme.snssdk.com"
DISCUSSION_PATH = "/aweme/v1/medium/loadmore/comments/"
SCM_VERSION = "1.0.0.757"
_FORBIDDEN_HEADERS = {
    "host", "proxy-authorization", "content-length", "connection",
    "transfer-encoding", "x-scm-version",
}


def _identifier(value):
    if type(value) is int:
        value = str(value)
    return value if isinstance(value, str) and re.fullmatch(r"[0-9]{1,32}", value) else None


def _business_code(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,20}", value):
        return int(value)
    return None


def _native_cookies(cookies):
    jar = httpx.Cookies()
    for row in cookies if isinstance(cookies, (list, tuple)) else ():
        if not isinstance(row, dict):
            continue
        domain = str(row.get("domain") or "").lower().lstrip(".")
        if domain not in {"snssdk.com", "aweme.snssdk.com"}:
            continue
        if not isinstance(row.get("name"), str) or not isinstance(row.get("value"), str):
            continue
        expiry = row.get("expires", -1)
        if type(expiry) not in (int, float) or (expiry >= 0 and expiry <= time.time()):
            continue
        jar.set(row["name"], row["value"], domain=row["domain"], path=row.get("path") or "/")
    return jar


def _common_params(value):
    return {key: item for key, item in value.items()
            if key in {"aid", "device_platform", "scm_version", "from_app"}
            and type(item) in (str, int)} if isinstance(value, dict) else {}


def fetch_discussion_context(circle, *, headers=None, cookies=None, common_params=None, transport=None):
    """GET one discussion page, binding its group to known circle metadata.

    This function neither publishes nor implies completion of a discussion
    task. Authentication is supplied explicitly by the caller; no credentials
    or identifiers are selected from local account storage here.
    """
    output = {
        "ok": False, "context": {}, "comment_count": None, "attempts": [],
        "message": "尚未确认圈子讨论上下文。",
    }

    def failed(message, attempt=None):
        output["message"] = message
        if attempt is not None:
            attempt["message"] = message
        return output

    if not isinstance(circle, dict):
        return failed("圈子讨论元数据缺失，未发送请求。")
    business_id = _identifier(circle.get("fandom_id"))
    expected_group = _identifier(circle.get("discuss_group_id"))
    if business_id is None or expected_group is None:
        return failed("圈子或讨论组 ID 格式不能确认，未发送请求。")

    # level/addCommonParams belong to the native bridge, not the HTTP query.
    params = {
        "cid": "0", "sort_by": "recommend", "offset": 0, "count": 10,
        "business_id": business_id, "discuss_scene": "fandom",
        **_common_params(common_params),
        "scm_version": SCM_VERSION, "from_app": "fandom_detail_douyin",
    }
    request_headers = {"User-Agent": "Mozilla/5.0"}
    if isinstance(headers, dict):
        request_headers.update({str(key): str(value) for key, value in headers.items()
                                if isinstance(key, str) and key.lower() not in _FORBIDDEN_HEADERS
                                and isinstance(value, (str, int))})
    request_headers["x-scm-version"] = SCM_VERSION
    attempt = {
        "method": "GET", "endpoint": BASE_URL + DISCUSSION_PATH,
        "http_status": None, "body_kind": "network_error",
        "business_code": None, "message": "",
    }
    output["attempts"].append(attempt)
    try:
        with httpx.Client(base_url=BASE_URL, headers=request_headers, cookies=_native_cookies(cookies),
                          timeout=httpx.Timeout(18, connect=8), follow_redirects=False,
                          transport=transport) as client:
            response = client.get(DISCUSSION_PATH, params=params)
    except (httpx.HTTPError, ValueError, UnicodeError) as exc:
        return failed("讨论读取请求没有完成：" + type(exc).__name__, attempt)

    attempt["http_status"] = response.status_code
    if response.is_redirect:
        attempt["body_kind"] = "redirect"
        return failed("讨论接口返回跳转，未携带凭据跟随。", attempt)
    if not 200 <= response.status_code < 300:
        attempt["body_kind"] = "http_error"
        return failed("讨论接口 HTTP 请求未通过，未采用返回上下文。", attempt)
    if not response.content.strip():
        attempt["body_kind"] = "empty"
        return failed("讨论接口响应为空，不能确认上下文。", attempt)
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        attempt["body_kind"] = "non_json"
        return failed("讨论接口没有返回可验证的 JSON 数据。", attempt)
    attempt["body_kind"] = "json"
    if not isinstance(payload, dict):
        return failed("讨论接口 JSON 结构不能确认上下文。", attempt)
    code = payload.get("status_code")
    attempt["business_code"] = _business_code(code)
    if not (type(code) is int and code == 0 or isinstance(code, str) and code == "0"):
        return failed("讨论接口业务状态未通过，未采用返回上下文。", attempt)
    raw_group = payload.get("group_id")
    group_id = _identifier(raw_group)
    if raw_group not in (None, "") and (group_id is None or group_id != expected_group):
        return failed("讨论接口的讨论组 ID 与圈子元数据不一致。", attempt)
    # The official frontend uses comment_list || [] and retains the meta
    # group when an empty response does not return a group_id.
    group_id = group_id or expected_group
    # The official list component reads ``comment_list || []``. Missing or
    # null lists therefore mean an empty page; malformed non-empty values do
    # not. The group ID may similarly fall back to circle metadata.
    comments = payload.get("comment_list") or []
    if not isinstance(comments, list) or any(not isinstance(item, dict) for item in comments):
        return failed("讨论接口未提供可确认的评论列表，未采用上下文。", attempt)
    output.update(
        ok=True,
        context={
            "business_id": business_id, "discuss_scene": "fandom", "group_id": group_id,
            "channel_obj_id": json.dumps({"business_id": business_id, "discuss_scene": "fandom"},
                                         ensure_ascii=True, separators=(",", ":")),
            "business_scene": "fandom", "publish_scene": "video_bottom_input",
        },
        comment_count=len(comments),
        group_source="discussion_list" if raw_group not in (None, "") else "circle_meta",
        message=("已读取圈子讨论列表；讨论组 ID 已与圈子资料交叉核对；未提交讨论或确认任务完成。"
                 if raw_group not in (None, "") else
                 "已读取圈子讨论列表；讨论组 ID 沿用圈子资料，未由列表响应交叉核对；未提交讨论或确认任务完成。"),
    )
    attempt["message"] = output["message"]
    return output
