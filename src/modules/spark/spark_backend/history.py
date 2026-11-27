"""续火花打卡历史：按天记录每次发送结果，供「火花日历」查看。

risk_state.recent_messages 只保留几天、用于去重；这里单独落一份长期历史
（send_history.json，按 account_id 区分账号），日历视图按月份读取。
"""
import json
import os
import threading
from datetime import datetime

from spark_backend.config import DATA_DIR
from spark_backend import runtime

HISTORY_FILE = os.path.join(DATA_DIR, 'send_history.json')
MAX_ENTRIES = 20000

# 发送线程/手动接口/批处理都会写，串行化避免写坏
_lock = threading.RLock()


def _write(entries):
    try:
        tmp = HISTORY_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(entries, f, ensure_ascii=False)
        os.replace(tmp, HISTORY_FILE)
    except OSError as e:
        print(f'⚠️ 打卡历史保存失败: {e}')


def _seed_from_recent(account_id):
    """首次启用时，把 risk_state 里最近几天的发送记录迁进历史，日历不至于全空。

    risk_state 是全局文件、不区分账号，这里统一记到当前账号名下。
    """
    try:
        from spark_backend.risk_control import risk_state
        recent = risk_state.get('recent_messages') or []
    except Exception:
        return []
    entries = []
    for item in recent:
        if not isinstance(item, dict):
            continue
        try:
            dt = datetime.strptime(str(item.get('sent_at') or ''), '%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
        name = str(item.get('name') or '').strip()
        if not name:
            continue
        entries.append({
            'date': dt.strftime('%Y-%m-%d'),
            'time': dt.strftime('%H:%M:%S'),
            'name': name,
            'ok': True,
            'detail': str(item.get('content') or '')[:200],
            'source': str(item.get('source') or ''),
            'account_id': account_id or 'default',
        })
    return entries[-200:]


def _ensure_seeded():
    if os.path.exists(HISTORY_FILE):
        return
    with _lock:
        if os.path.exists(HISTORY_FILE):
            return
        _write(_seed_from_recent(runtime.active_account_id or 'default'))


def _load():
    try:
        with open(HISTORY_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    return data if isinstance(data, list) else []


def record(name, ok, detail='', source='', account_id=None, at=None):
    """记一条打卡结果。name 为空（例如风控暂停）时不记。"""
    name = str(name or '').strip()
    if not name:
        return
    _ensure_seeded()
    dt = at or datetime.now()
    entry = {
        'date': dt.strftime('%Y-%m-%d'),
        'time': dt.strftime('%H:%M:%S'),
        'name': name,
        'ok': bool(ok),
        'detail': str(detail or '')[:200],
        'source': str(source or ''),
        'account_id': account_id or runtime.active_account_id or 'default',
    }
    with _lock:
        entries = _load()
        entries.append(entry)
        if len(entries) > MAX_ENTRIES:
            entries = entries[-MAX_ENTRIES:]
        _write(entries)


def calendar(month, account_id=None):
    """某个月（YYYY-MM）的打卡汇总：{date: {total, ok, fail, items:[...]}}"""
    _ensure_seeded()
    month = str(month or '').strip()
    days = {}
    for e in _load():
        if not isinstance(e, dict):
            continue
        if month and not str(e.get('date', '')).startswith(month):
            continue
        if account_id and str(e.get('account_id') or 'default') != account_id:
            continue
        date = str(e.get('date') or '')
        if not date:
            continue
        day = days.setdefault(date, {'total': 0, 'ok': 0, 'fail': 0, 'items': []})
        day['total'] += 1
        day['ok' if e.get('ok') else 'fail'] += 1
        day['items'].append({
            'name': str(e.get('name') or ''),
            'time': str(e.get('time') or ''),
            'ok': bool(e.get('ok')),
            'detail': str(e.get('detail') or ''),
            'source': str(e.get('source') or ''),
        })
    for day in days.values():
        day['items'].sort(key=lambda x: x['time'])
    return days


def months(account_id=None):
    """有记录的月份列表（新→旧），给前端切月份用。"""
    out = set()
    for e in _load():
        if not isinstance(e, dict):
            continue
        if account_id and str(e.get('account_id') or 'default') != account_id:
            continue
        date = str(e.get('date') or '')
        if len(date) >= 7:
            out.add(date[:7])
    return sorted(out, reverse=True)
