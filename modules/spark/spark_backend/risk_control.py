"""风控与消息选择模块"""
import os
import json
import random
import threading
from datetime import datetime

from spark_backend.config import config, DATA_DIR, DEFAULT_MESSAGE_TEMPLATES, normalize_message_templates
from spark_backend.utils import now_str, AiqingGongyu_text


RISK_STATE_FILE = os.path.join(DATA_DIR, 'risk_state.json')

# 发送线程与接口线程都会写这个文件，串行化避免写坏
_save_lock = threading.Lock()


def load_risk_state():
    default = {
        'paused': False,
        'pause_reason': '',
        'paused_at': '',
        'consecutive_failures': 0,
        'last_error': '',
        'last_failure_at': '',
        'last_success_at': '',
        'last_sent_at': '',
        'today_send_count': 0,
        'today_send_date': '',
        'recent_messages': []
    }
    if os.path.exists(RISK_STATE_FILE):
        try:
            with open(RISK_STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)
                if isinstance(state, dict):
                    default.update(state)
        except Exception:
            pass
    if not isinstance(default.get('recent_messages'), list):
        default['recent_messages'] = []
    return default


def prune_risk_state():
    today = datetime.now().strftime('%Y-%m-%d')
    if risk_state.get('today_send_date') != today:
        risk_state['today_send_date'] = today
        risk_state['today_send_count'] = 0
    keep_days = max(config.get('dedupe_window_days', 7), 7) + 2
    cutoff = datetime.now().timestamp() - keep_days * 86400
    kept = []
    for item in risk_state.get('recent_messages', []):
        try:
            sent_at = item.get('sent_at', '')
            ts = datetime.strptime(sent_at, '%Y-%m-%d %H:%M:%S').timestamp()
            if ts >= cutoff:
                kept.append(item)
        except Exception:
            continue
    risk_state['recent_messages'] = kept[-200:]


def save_risk_state():
    prune_risk_state()
    try:
        with _save_lock:
            with open(RISK_STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(risk_state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'⚠️ 风控状态保存失败: {e}')


def pause_risk(reason):
    risk_state['paused'] = True
    risk_state['pause_reason'] = reason
    risk_state['paused_at'] = now_str()
    save_risk_state()


def resume_risk():
    risk_state['paused'] = False
    risk_state['pause_reason'] = ''
    risk_state['paused_at'] = ''
    risk_state['consecutive_failures'] = 0
    risk_state['last_error'] = ''
    save_risk_state()


def record_send_success(name: str, content: str, source: str):
    risk_state['consecutive_failures'] = 0
    risk_state['last_error'] = ''
    risk_state['last_success_at'] = now_str()
    risk_state['last_sent_at'] = risk_state['last_success_at']
    risk_state['today_send_count'] = int(risk_state.get('today_send_count', 0)) + 1
    risk_state.setdefault('recent_messages', []).append({
        'name': name,
        'content': content[:200],
        'source': source,
        'sent_at': risk_state['last_success_at']
    })
    save_risk_state()
    _record_history(name, True, content, source)


def record_send_failure(reason: str, name: str = ''):
    risk_state['consecutive_failures'] = int(risk_state.get('consecutive_failures', 0)) + 1
    risk_state['last_error'] = reason[:300]
    risk_state['last_failure_at'] = now_str()
    threshold = config.get('max_consecutive_failures', 3)
    if risk_state['consecutive_failures'] >= threshold:
        pause_risk(f'连续失败 {risk_state["consecutive_failures"]} 次，已自动暂停任务。最近错误：{reason[:120]}')
    else:
        save_risk_state()
    _record_history(name, False, reason, '')


def _record_history(name, ok, detail, source):
    """写火花日历用的长期打卡历史；失败不影响主流程。"""
    try:
        from spark_backend import history
        history.record(name, ok, detail, source)
    except Exception:
        pass


def split_message_candidates(raw_message):
    message = str(raw_message or '').strip()
    if not message:
        candidates = list(config.get('message_templates') or DEFAULT_MESSAGE_TEMPLATES)
        daily_quote = AiqingGongyu_text()
        if daily_quote and daily_quote not in candidates:
            candidates.append(daily_quote)
        return normalize_message_templates(candidates)
    return normalize_message_templates(message)


def pick_message_content(name: str, raw_message: str):
    candidates = split_message_candidates(raw_message)
    dedupe_days = config.get('dedupe_window_days', 7)
    cutoff = datetime.now().timestamp() - dedupe_days * 86400
    recent_texts = set()
    for item in risk_state.get('recent_messages', []):
        try:
            if item.get('name') != name:
                continue
            ts = datetime.strptime(item.get('sent_at', ''), '%Y-%m-%d %H:%M:%S').timestamp()
            if ts >= cutoff:
                recent_texts.add(item.get('content', ''))
        except Exception:
            continue
    available = [item for item in candidates if item not in recent_texts]
    pool = available if available else candidates
    return random.choice(pool) if pool else AiqingGongyu_text()


# 全局风控状态（字典不重新绑定，只改属性，可 from import）
risk_state = load_risk_state()
