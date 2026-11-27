"""An explicitly requested, single discussion submission with read-back."""
from __future__ import annotations

import httpx

from .discussion import (BASE_URL, SCM_VERSION, _common_params, _identifier,
                         _native_cookies)


SELF_PATH = "/aweme/v1/user/profile/self/"
PUBLISH_PATH = "/aweme/v1/ent_discuss/publish/"
VERIFY_PATH = "/aweme/v1/ent_discuss/multi_query/"


def publish_discussion(context, text, *, expected_uid, cookies, headers=None,
                       common_params=None, transport=None):
    """Verify the selected native identity, POST once, then GET that comment.

    The caller must have direct authorization for the text and persist an
    intent before calling. Ambiguous requests are never retried here. A
    successful submission/read-back is not platform review or task credit.
    """
    out = {"status": "not_submitted", "comment_id": "", "submitted": False,
           "read_back": False, "task_completed": False, "attempts": [], "message": ""}
    business = _identifier(context.get("business_id")) if isinstance(context, dict) else None
    group = _identifier(context.get("group_id")) if isinstance(context, dict) else None
    uid = _identifier(expected_uid)
    if (business is None or group is None or uid is None or not isinstance(text, str)
            or not text.strip() or len(text) > 300 or "\x00" in text):
        out["message"] = "讨论内容、圈子或账号信息不完整，未提交。"
        return out
    # Values tied to the fandom scene are fixed, rather than caller-controlled.
    import json

    data = {"business_id": business, "business_scene": "fandom", "group_id": group,
            "channel_obj_id": json.dumps({"business_id": business, "discuss_scene": "fandom"},
                                         separators=(",", ":")),
            "text": text.strip(), "publish_scene": "video_bottom_input"}
    request_headers = {"User-Agent": "Mozilla/5.0", "x-scm-version": SCM_VERSION}
    for key, value in headers.items() if isinstance(headers, dict) else ():
        if isinstance(key, str) and key.lower() in {"user-agent", "accept"} and isinstance(value, str):
            request_headers[key] = value
    params = {**_common_params(common_params), "scm_version": SCM_VERSION,
              "from_app": "fandom_detail_douyin"}

    def request(client, method, path, *, query=None, form=None):
        attempt = {"method": method, "endpoint": BASE_URL + path,
                   "http_status": None, "business_code": None}
        out["attempts"].append(attempt)
        try:
            response = client.request(method, path, params={**params, **(query or {})}, data=form)
        except (httpx.HTTPError, ValueError, UnicodeError):
            return None
        attempt["http_status"] = response.status_code
        if not 200 <= response.status_code < 300:
            return None
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            return None
        if not isinstance(payload, dict):
            return None
        code = payload.get("status_code")
        attempt["business_code"] = code if type(code) is int else None
        return payload if type(code) is int and code == 0 else None

    with httpx.Client(base_url=BASE_URL, headers=request_headers,
                      cookies=_native_cookies(cookies), follow_redirects=False,
                      timeout=httpx.Timeout(20, connect=8), transport=transport) as client:
        identity = request(client, "GET", SELF_PATH)
        user = identity.get("user") if identity else None
        if not isinstance(user, dict) or _identifier(user.get("uid")) != uid:
            out["message"] = "原生接口未确认所选账号身份，未提交讨论。"
            return out
        out["status"] = "unconfirmed"
        result = request(client, "POST", PUBLISH_PATH, form=data)
        comment = result.get("comment") if result else None
        cid = _identifier(comment.get("cid")) if isinstance(comment, dict) else None
        if cid is None:
            out["message"] = "已尝试一次提交，未获得可核验的讨论 ID；不要自动重试。"
            return out
        owner = comment.get("user")
        if isinstance(owner, dict) and owner.get("uid") is not None and _identifier(owner["uid"]) != uid:
            out["message"] = "提交返回的作者与所选账号不一致，需人工核对；不要重试。"
            return out
        out.update(status="submitted", submitted=True, comment_id=cid,
                   message="讨论已提交，审核与任务计数仍待官方确认。")
        verified = request(client, "GET", VERIFY_PATH, query={"discuss_id_list": cid})
        comments = verified.get("comment_list") if verified else None
        if isinstance(comments, list):
            out["read_back"] = any(isinstance(item, dict) and _identifier(item.get("cid")) == cid
                                   and item.get("text") == text.strip() for item in comments)
        if out["read_back"]:
            out["message"] = "讨论已提交并通过官方 GET 读回；审核及任务计数仍需复查。"
    return out
