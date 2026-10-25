"""读取火花模块（续火花）的数据，供聊天导出共用同一套会话/好友配置。

火花模块把数据放在 SPARK_HOME/data（统一应用运行时由 app/paths.py 设置）或
<modules>/spark/data（独立运行）。这里只读不写：火花的任务/好友缓存由火花
模块自己维护，导出这边只拿「昵称 -> sec_uid」的映射做会话定位兜底。

火花的定位方式比按昵称找 DOM 稳（昵称会带火花天数、改名会失配），所以
导出在按名字找不到会话时，用火花存下的 sec_uid 走 React fiber 匹配。
"""
import json
import os


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
