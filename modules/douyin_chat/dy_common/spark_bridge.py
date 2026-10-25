"""读取火花模块（续火花）的数据，供聊天导出共用同一套会话/好友配置。

火花模块把数据放在 SPARK_HOME/data（统一应用运行时由 app/paths.py 设置）或
<modules>/spark/data（独立运行）。这里只读不写：火花的任务/好友缓存由火花
模块自己维护，导出这边只拿「昵称 -> sec_uid」的映射做会话定位兜底。

火花的定位方式比按昵称找 DOM 稳（昵称会带火花天数、改名会失配），所以
导出在按名字找不到会话时，用火花存下的 sec_uid 走 React fiber 匹配。
"""
import json
import os
from datetime import datetime


def spark_data_dir() -> str:
    module_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = []
    env_home = os.environ.get("SPARK_HOME")
    if env_home:
        candidates.append(os.path.join(env_home, "data"))
    # 独立运行（无 SPARK_HOME）时按目录布局兜底：modules/spark/data 或 data/spark/data
    candidates.append(os.path.join(os.path.dirname(module_root), "spark", "data"))
    candidates.append(os.path.join(os.path.dirname(os.path.dirname(module_root)), "data", "spark", "data"))
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[-1]


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def load_spark_task_friends() -> dict:
    """{昵称: sec_uid}，只取火花定时任务里的目标（用户实际配置要续火花的人）。"""
    out = {}
    tasks = _load_json(os.path.join(spark_data_dir(), "tasks.json"))
    if not isinstance(tasks, list):
        return out
    for t in tasks:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        sec_uid = str(t.get("sec_uid") or "").strip()
        if name and sec_uid:
            out.setdefault(name, sec_uid)
    return out


def load_spark_fire_days(account_id: str = "") -> dict:
    """{昵称: 火花天数}，来自火花好友缓存（friends_cache_<账号>.json）。"""
    data_dir = spark_data_dir()
    candidates = []
    if account_id:
        candidates.append(os.path.join(data_dir, f"friends_cache_{account_id}.json"))
    candidates.append(os.path.join(data_dir, "friends_cache.json"))
    try:
        candidates.extend(
            os.path.join(data_dir, fn) for fn in os.listdir(data_dir)
            if fn.startswith("friends_cache") and fn.endswith(".json")
        )
    except OSError:
        pass

    out = {}
    for path in candidates:
        data = _load_json(path)
        items = (data or {}).get("list") if isinstance(data, dict) else None
        if not isinstance(items, dict):
            continue
        for name, value in items.items():
            if name in out or not isinstance(value, list) or len(value) < 2:
                continue
            fire = str(value[1] or "").strip()
            if fire:
                out[name] = fire
        if out:
            break
    return out


def load_spark_tasks(account_id: str = "") -> dict:
    """{昵称: 任务信息}，同一好友有多个任务时取最早的一个。"""
    tasks = _load_json(os.path.join(spark_data_dir(), "tasks.json"))
    if not isinstance(tasks, list):
        return {}
    out = {}
    for t in tasks:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        if account_id and str(t.get("account_id") or "default") != account_id:
            continue
        info = {
            "time": str(t.get("time") or ""),
            "msg": str(t.get("msg") or ""),
            "send_type": str(t.get("send_type") or "text"),
        }
        current = out.get(name)
        if current is None or info["time"] < current["time"]:
            out[name] = info
    return out


def load_spark_history(account_id: str = "") -> list:
    """火花日历的发送历史（send_history.json）。"""
    data = _load_json(os.path.join(spark_data_dir(), "send_history.json"))
    if not isinstance(data, list):
        return []
    if not account_id:
        return [e for e in data if isinstance(e, dict)]
    return [e for e in data if isinstance(e, dict)
            and str(e.get("account_id") or "default") == account_id]


def spark_info_for(name: str, account_id: str = "") -> dict:
    """某个好友的火花概览：天数、定时任务、今日是否已续、最近发送。"""
    name = str(name or "").strip()
    fire = load_spark_fire_days(account_id).get(name, "")
    task = load_spark_tasks(account_id).get(name)
    history = [e for e in load_spark_history(account_id)
               if str(e.get("name") or "").strip() == name]
    today = datetime.now().strftime("%Y-%m-%d")
    today_items = [e for e in history if str(e.get("date") or "") == today]
    recent = list(reversed(history[-5:]))
    return {
        "name": name,
        "fire": fire,
        "task": task,
        "today": {
            "count": len(today_items),
            "ok": any(e.get("ok") for e in today_items),
            "fail": any(not e.get("ok") for e in today_items),
        },
        "recent": [
            {
                "date": str(e.get("date") or ""),
                "time": str(e.get("time") or ""),
                "ok": bool(e.get("ok")),
                "detail": str(e.get("detail") or ""),
                "source": str(e.get("source") or ""),
            }
            for e in recent
        ],
    }


def load_spark_friends() -> dict:
    """{昵称: sec_uid}。

    火花任务里的目标优先（那是用户明确配置要续火花的人），好友缓存补全
    其余会话。两边都只取「昵称 + sec_uid 都齐全」的条目。
    """
    out = load_spark_task_friends()
    data_dir = spark_data_dir()

    try:
        entries = os.listdir(data_dir)
    except OSError:
        entries = []
    for fn in entries:
        if not (fn.startswith("friends_cache") and fn.endswith(".json")):
            continue
        data = _load_json(os.path.join(data_dir, fn))
        items = (data or {}).get("list") if isinstance(data, dict) else None
        if not isinstance(items, dict):
            continue
        for name, value in items.items():
            if name in out or not isinstance(value, list) or len(value) < 3:
                continue
            sec_uid = str(value[2] or "").strip()
            if sec_uid:
                out[name] = sec_uid
    return out
