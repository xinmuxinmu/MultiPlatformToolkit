"""好友列表本地缓存

抖音好友列表要逐屏滚动才能抓全（158 位约 19 秒），每次重启都重抓体验很差。
这里把结果落盘：浏览器未就绪时先返回上次的数据，界面不会一片空白。

多账号：每个账号一份缓存文件，切换账号后看到的是该账号自己的好友。
默认账号沿用 friends_cache.json（历史数据无缝保留）。
"""
import os
import json
import time

from spark_backend.config import DATA_DIR
from spark_backend.utils import now_str
from spark_backend import runtime


def _cache_file(account_id=None):
    aid = account_id or runtime.active_account_id or 'default'
    if aid == 'default':
        return os.path.join(DATA_DIR, 'friends_cache.json')
    return os.path.join(DATA_DIR, f'friends_cache_{aid}.json')


def save_friends(friends, account_id=None):
    """friends: [{'name','avatar','fire','id'}, ...]"""
    if not friends:
        return False
    payload = {
        'saved_at': now_str(),
        'ts': int(time.time()),
        'list': {f['name']: [f.get('avatar', ''), f.get('fire', ''), f.get('id', '')]
                 for f in friends},
        # 单独存一份 昵称->隐藏ID：重启后不必先滚一遍好友列表也能按 ID 找人
        'ids': {f['name']: f.get('id', '') for f in friends if f.get('id')},
    }
    try:
        with open(_cache_file(account_id), 'w', encoding='utf-8') as fp:
            json.dump(payload, fp, ensure_ascii=False)
        return True
    except Exception as e:
        print(f'⚠️ 好友缓存保存失败: {e}')
        return False


def load_friends(account_id=None):
    """返回 {'saved_at','ts','list'}，没有可用缓存时返回 None"""
    path = _cache_file(account_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as fp:
            data = json.load(fp)
        if not isinstance(data, dict):
            return None
        items = data.get('list')
        if not isinstance(items, dict) or not items:
            return None
        return data
    except Exception:
        return None


def purge_account_cache(account_id):
    """删除某个账号的好友缓存文件"""
    path = _cache_file(account_id)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f'⚠️ 删除好友缓存失败: {e}')
