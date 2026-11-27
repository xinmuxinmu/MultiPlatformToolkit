"""已核对官方页面流程的免费动作；不导入 core，不伪造任务进度。"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse


COMPANION_TASK_TYPE = "set_medal_role_ai_player_singer"
_HOSTS = {"y.qq.com", "i.y.qq.com", "i2.y.qq.com"}
_PATHS = {"/n3/client/pages/ai_player_companion/index.html",
          "/n3/client/pages/ai_player_companion/"}
_BIZ_RE = re.compile(r"[A-Za-z0-9_-]{0,128}")
_HOME_MODULE = "music.aiPlayerCompanion.AiPlayerCompanionHomepageSrv"
_SCORE_MODULE = "music.aiPlayerCompanion.AiPlayerCompanionScoreSrv"
_ROLE_MODULE = "music.aiPlayerCompanion.AiPlayerCompanionRoleSrv"


class ActionError(Exception):
    """可展示的动作错误；changed 表示已尝试写入，调用方不应盲目重试。"""

    def __init__(self, message: str, *, code: str = "action_failed", changed: bool = False):
        super().__init__(message)
        self.code = code
        self.changed = changed


def _target_error() -> ActionError:
    return ActionError("小勋播放器任务入口或参数无效，请在官方页面手动确认", code="invalid_target")


def parse_companion_target(task: dict) -> tuple[int, str]:
    """只解析官方任务页面，不访问网址；兼容外层 qqmusic 的 url/weburl 参数。"""
    if not isinstance(task, dict) or task.get("TaskType") != COMPANION_TASK_TYPE:
        raise _target_error()
    url = task.get("BtnJumpURL")
    if not isinstance(url, str) or len(url) > 8192:
        raise _target_error()
    url = url.strip().replace("&amp;", "&")
    for _ in range(3):
        if not url or any(ord(char) < 32 or ord(char) == 127 for char in url):
            raise _target_error()
        try:
            parsed = urlparse(url)
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=64)
            if parsed.scheme in ("https", "http"):
                allowed_port = 443 if parsed.scheme == "https" else 80
                if (parsed.hostname not in _HOSTS or parsed.username or parsed.password
                        or parsed.port not in (None, allowed_port) or parsed.path not in _PATHS):
                    raise _target_error()
                role_values = query.get("roleId", [])
                biz_values = query.get("bizId", [""])
                if len(role_values) != 1 or len(biz_values) != 1:
                    raise _target_error()
                role, biz = role_values[0], biz_values[0]
                if not re.fullmatch(r"[0-9]{1,10}", role) or not _BIZ_RE.fullmatch(biz):
                    raise _target_error()
                role_id = int(role)
                if not 0 < role_id <= 2_147_483_647:
                    raise _target_error()
                return role_id, biz
            if parsed.scheme != "qqmusic":
                raise _target_error()
            # 多个目标参数存在歧义，不能选择其中之一后执行账号写入。
            nested = query.get("url", []) + query.get("weburl", [])
            if len(nested) != 1:
                raise _target_error()
            url = nested[0]
        except (ValueError, TypeError):
            raise _target_error() from None
    raise _target_error()


def _call(client, module: str, method: str, params: dict, *, data_required: bool = True):
    try:
        result = client._medal_call(module, method, params)
    except Exception:  # noqa: BLE001 - 不把第三方异常中的账号或响应正文传给界面。
        raise ActionError(f"小勋播放器 {method} 请求失败，请稍后手动重试", code="request_failed") from None
    if not isinstance(result, (tuple, list)) or len(result) != 2:
        raise ActionError(f"小勋播放器 {method} 响应结构异常", code="invalid_response")
    code, data = result
    if type(code) is not int or code != 0:
        raise ActionError(f"小勋播放器 {method} 未返回成功状态", code="service_error")
    if data_required and not isinstance(data, dict):
        raise ActionError(f"小勋播放器 {method} 缺少状态数据", code="invalid_response")
    return data


def _atlas_state(data: dict) -> bool:
    # 只有平台明确声明可免费解锁才执行；缺字段或字符串真假值均不能放行。
    pay = data.get("payConfig")
    if data.get("unlockable") is not True:
        raise ActionError("小勋播放器未明确允许解锁，请在官方页面手动确认", code="not_unlockable")
    if not isinstance(pay, dict) or pay.get("isNeedPay") is not False:
        raise ActionError("小勋播放器未明确标记免费，已跳过自动添加", code="not_free")
    if type(data.get("roleIsUse")) is not bool:
        raise ActionError("小勋播放器缺少使用状态，请在官方页面手动确认", code="missing_use_state")
    return data["roleIsUse"]


def _biz_id(value) -> str:
    if type(value) is int and value >= 0:
        value = str(value) if value else ""
    if not isinstance(value, str) or not _BIZ_RE.fullmatch(value):
        raise ActionError("小勋播放器艺人绑定状态不完整，请手动确认", code="missing_binding")
    return "" if value == "0" else value


def _home_state(data: dict, target_biz: str) -> dict:
    ext, wearing = data.get("ext"), data.get("wearing")
    if (not isinstance(ext, dict) or "curBizID" not in ext
            or not isinstance(wearing, dict) or "bizID" not in wearing
            or type(wearing.get("isWearing")) is not bool):
        raise ActionError("小勋播放器缺少艺人绑定状态，请在官方页面手动确认", code="missing_binding")
    current_biz, wearing_biz = _biz_id(ext["curBizID"]), _biz_id(wearing["bizID"])
    if any(biz and biz != target_biz for biz in (current_biz, wearing_biz)):
        raise ActionError("小勋播放器已绑定其他艺人，不自动换绑，请在官方页面手动确认", code="binding_conflict")
    role = wearing.get("roleId")
    if type(role) is not int or role < 0:
        raise ActionError("小勋播放器缺少佩戴角色状态，请在官方页面手动确认", code="missing_binding")
    return {"is_wearing": wearing["isWearing"], "role_id": role, "biz_id": wearing_biz}


def _matches(home: dict, role_id: int, biz_id: str) -> bool:
    return home["is_wearing"] and home["role_id"] == role_id and home["biz_id"] == biz_id


def select_free_companion(client, task: dict) -> dict:
    """添加免费小勋播放器，最多调用一次 SelectAiRole 并回读验证。

    成功返回动作状态；勋章任务是否完成仍须调用方独立回读任务列表确认。
    失败抛 ActionError，其 changed=True 时已经尝试写入，不能自动重试。
    """
    role_id, biz_id = parse_companion_target(task)
    home_params = {"roleId": role_id, "needRoleList": False, "companionType": 0, "bizID": biz_id}
    home = _home_state(_call(client, _HOME_MODULE, "GetHomePageInfo", home_params), biz_id)
    in_use = _atlas_state(_call(client, _SCORE_MODULE, "QueryAtlasV2", {"roleId": role_id}))
    result = {"ok": True, "changed": False, "verified": True, "already_selected": False,
              "role_id": role_id, "biz_id": biz_id}
    if in_use and _matches(home, role_id, biz_id):
        return {**result, "already_selected": True}
    select_params = {"roleId": role_id, "Inviter": ""}
    if biz_id:
        select_params["bizID"] = biz_id
    try:
        _call(client, _ROLE_MODULE, "SelectAiRole", select_params, data_required=False)
        in_use = _atlas_state(_call(client, _SCORE_MODULE, "QueryAtlasV2", {"roleId": role_id}))
        home = _home_state(_call(client, _HOME_MODULE, "GetHomePageInfo", home_params), biz_id)
        if not in_use or not _matches(home, role_id, biz_id):
            raise ActionError("小勋播放器添加后未核实使用和艺人绑定状态，请手动确认", code="verification_failed")
    except ActionError as exc:
        exc.changed = True
        raise
    return {**result, "changed": True}
