"""发送进度状态

批量发送是长耗时操作（每个好友要定位会话、输入、等发送确认，2~10 秒），
接口如果同步返回，前端只能一直转圈，用户不知道进行到哪了。

这里用一个内存状态记录最近一次发送任务的进度，前端轮询 /Time/progress 显示：
「正在发送 3/20：奶昔」「成功 2，失败 0」。
"""
import threading

from spark_backend.utils import now_str


_lock = threading.RLock()

_state = {
    'job_id': '',
    'kind': '',            # batch / single
    'running': False,
    'cancel': False,
    'total': 0,
    'done': 0,
    'ok': 0,
    'fail': 0,
    'current': '',         # 当前正在发送的好友
    'stage': '',           # 当前阶段（打开会话 / 输入发送 / 等待确认）
    'started_at': '',
    'finished_at': '',
    'message': '',         # 结束后的一句话总结
    'detail': [],          # [{name, ok, msg}]
}


def begin(kind, total, message=''):
    """开始一个新任务，清空上一次的进度"""
    with _lock:
        _state.update({
            'job_id': f'{kind}-{now_str()}',
            'kind': kind,
            'running': True,
            'cancel': False,
            'total': int(total),
            'done': 0,
            'ok': 0,
            'fail': 0,
            'current': '',
            'stage': '准备中',
            'started_at': now_str(),
            'finished_at': '',
            'message': message,
            'detail': [],
        })
        return dict(_state)


def update(**kwargs):
    """更新进度字段（忽略未知字段）"""
    with _lock:
        for key, value in kwargs.items():
            if key in _state:
                _state[key] = value


def add_detail(name, ok, msg):
    with _lock:
        _state['detail'].append({'name': name, 'ok': bool(ok), 'msg': msg})
        # 只保留最近 200 条，避免长时间运行内存膨胀
        if len(_state['detail']) > 200:
            _state['detail'] = _state['detail'][-200:]


def finish(message=''):
    with _lock:
        _state.update({
            'running': False,
            'stage': '已结束',
            'finished_at': now_str(),
            'message': message or _state.get('message', ''),
            'current': '',
        })
        return dict(_state)


def request_cancel():
    with _lock:
        if not _state['running']:
            return False
        _state['cancel'] = True
        _state['stage'] = '正在取消（当前好友发完就停）'
        return True


def is_cancelled():
    with _lock:
        return bool(_state['cancel'])


def snapshot():
    with _lock:
        data = dict(_state)
        data['detail'] = list(_state['detail'])
        return data
