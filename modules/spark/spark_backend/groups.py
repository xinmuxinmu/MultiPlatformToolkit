"""好友分组：分组管理 + 按组批量生成/同步定时任务

设计要点：
- 一个好友只属于一个分组。加入新组时会自动从旧组移除，
  这样「组的时间 / 消息」对好友的归属唯一，不会互相覆盖。
- 分组配置是纯本地数据，不依赖浏览器；只有「应用到定时任务」
  时才校验浏览器与好友是否就绪，方便先规划、后执行。
"""
import os
import json
import threading
import uuid

from spark_backend.config import config, DATA_DIR, normalize_clock
from spark_backend.utils import now_str
from spark_backend import runtime


GROUPS_FILE = os.path.join(DATA_DIR, 'groups.json')

# 分组列表（原地修改，保证 from import 之后引用不失效）
# 内存里只放当前活跃账号的分组，账号切换时由 load_groups 重载
groups = []

# 保存时要把其他账号的分组写回磁盘，串行化避免写坏
_save_lock = threading.Lock()


def _account_of(item):
    """磁盘记录归属账号，兼容没有 account_id 的历史数据（归默认账号）"""
    return str((item or {}).get('account_id') or 'default')


def load_groups(account_id=None):
    """从 groups.json 读取分组，只加载指定（默认活跃）账号的，兼容旧数据"""
    aid = account_id or runtime.active_account_id or 'default'
    groups.clear()
    if not os.path.exists(GROUPS_FILE):
        return groups
    try:
        with open(GROUPS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, list):
            return groups
        for item in data:
            if not isinstance(item, dict):
                continue
            if _account_of(item) != aid:
                continue
            gid = str(item.get('id') or '').strip()
            if not gid:
                continue
            members = []
            for m in item.get('members') or []:
                m = str(m).strip()
                if m and m not in members:
                    members.append(m)
            groups.append({
                'id': gid,
                'name': str(item.get('name') or '未命名分组')[:40],
                'time': normalize_clock(item.get('time'), config.get('default_task_time', '22:00')),
                'text': str(item.get('text') or '')[:500],
                'send_type': item.get('send_type') if item.get('send_type') in ('text', 'sticker', 'both') else 'text',
                'sticker_src': str(item.get('sticker_src') or ''),
                'members': members,
                'account_id': aid,
                'created_at': item.get('created_at') or now_str(),
            })
    except Exception as e:
        print(f'⚠️ 分组数据读取失败: {e}')
    return groups


def save_groups():
    """保存当前账号的分组；其他账号的分组原样写回"""
    try:
        with _save_lock:
            on_disk = []
            if os.path.exists(GROUPS_FILE):
                try:
                    with open(GROUPS_FILE, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        on_disk = data
                except Exception:
                    on_disk = []
            aid = runtime.active_account_id or 'default'
            others = [g for g in on_disk if _account_of(g) != aid]
            payload = others + groups
            with open(GROUPS_FILE, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f'⚠️ 分组保存失败: {e}')
        return False


def purge_account_groups(account_id):
    """从磁盘删除某个账号的全部分组"""
    try:
        with _save_lock:
            if not os.path.exists(GROUPS_FILE):
                return
            with open(GROUPS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, list):
                return
            kept = [g for g in data if _account_of(g) != account_id]
            with open(GROUPS_FILE, 'w', encoding='utf-8') as f:
                json.dump(kept, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'⚠️ 清理账号分组失败: {e}')


def find_group(group_id):
    for g in groups:
        if g['id'] == group_id:
            return g
    return None


def find_group_of_member(name):
    for g in groups:
        if name in g['members']:
            return g
    return None


def add_group(name, time=None, text=None, send_type='text', sticker_src=''):
    name = str(name or '').strip()[:40]
    if not name:
        return None, '分组名称不能为空'
    if any(g['name'] == name for g in groups):
        return None, f'分组「{name}」已存在'
    if send_type not in ('text', 'sticker', 'both'):
        send_type = 'text'
    if send_type in ('sticker', 'both') and not str(sticker_src or '').strip():
        return None, '表情包分组需要先选择一个表情'
    group = {
        'id': 'g' + uuid.uuid4().hex[:10],
        'name': name,
        # 新建组默认继承全局默认时间与默认消息，省去重复填写
        'time': normalize_clock(time, config.get('default_task_time', '22:00')),
        'text': str(text if text is not None else config.get('default_message', ''))[:500],
        'send_type': send_type,
        'sticker_src': str(sticker_src or '').strip(),
        'members': [],
        'account_id': runtime.active_account_id or 'default',
        'created_at': now_str(),
    }
    groups.append(group)
    save_groups()
    return group, None


def edit_group(group_id, name=None, time=None, text=None, send_type=None, sticker_src=None):
    group = find_group(group_id)
    if not group:
        return None, '分组不存在'
    if name is not None:
        new_name = str(name).strip()[:40]
        if not new_name:
            return None, '分组名称不能为空'
        if any(g['name'] == new_name and g['id'] != group_id for g in groups):
            return None, f'分组「{new_name}」已存在'
        group['name'] = new_name
    if time is not None:
        group['time'] = normalize_clock(time, group['time'])
    if text is not None:
        group['text'] = str(text)[:500]
    if send_type is not None:
        group['send_type'] = send_type if send_type in ('text', 'sticker', 'both') else 'text'
    if sticker_src is not None:
        group['sticker_src'] = str(sticker_src or '').strip()
    if group.get('send_type') in ('sticker', 'both') and not group.get('sticker_src'):
        return None, '表情包分组需要先选择一个表情'
    save_groups()
    return group, None


def delete_group(group_id):
    """删除分组，任务本身保留，仅解除分组归属"""
    group = find_group(group_id)
    if not group:
        return False, '分组不存在'
    groups.remove(group)
    save_groups()
    try:
        from spark_backend.scheduler import scheduled_tasks, save_tasks
        changed = False
        for task in scheduled_tasks.values():
            if task.get('group_id') == group_id:
                task['group_id'] = ''
                changed = True
        if changed:
            save_tasks()
    except Exception as e:
        print(f'⚠️ 解除任务分组归属失败: {e}')
    return True, None


def set_members(group_id, names):
    """覆盖式设置组成员，同时把这些人从其他分组移除（好友唯一归属）"""
    group = find_group(group_id)
    if not group:
        return None, '分组不存在'
    cleaned = []
    for name in names or []:
        name = str(name).strip()
        if name and name not in cleaned:
            cleaned.append(name)
    for other in groups:
        if other['id'] == group_id:
            continue
        other['members'] = [m for m in other['members'] if m not in cleaned]
    group['members'] = cleaned
    save_groups()
    return group, None


def add_members(group_id, names):
    group = find_group(group_id)
    if not group:
        return None, '分组不存在'
    merged = list(group['members'])
    for name in names or []:
        name = str(name).strip()
        if name and name not in merged:
            merged.append(name)
    return set_members(group_id, merged)


def remove_member(group_id, name):
    group = find_group(group_id)
    if not group:
        return None, '分组不存在'
    group['members'] = [m for m in group['members'] if m != name]
    save_groups()
    return group, None


def task_of_friend(name):
    """按好友名查找其定时任务（任务ID含时间，不能直接反查）"""
    from spark_backend.scheduler import scheduled_tasks
    for task_id, task in scheduled_tasks.items():
        if task.get('name') == name:
            return task_id, task
    return None, None


def apply_group(group_id):
    """把分组的时间与消息同步到组内每个好友的定时任务

    已有任务 -> 取消旧调度后按组配置重建（时间/消息被组覆盖）
    没有任务 -> 新建
    返回 (code, message, detail)
    """
    import schedule
    from spark_backend.scheduler import scheduled_tasks, schedule_task, save_tasks
    from spark_backend.browser_manager import check_driver
    from spark_backend import runtime

    group = find_group(group_id)
    if not group:
        return 404, '分组不存在', {}
    if not group['members']:
        return 400, '分组内还没有好友，请先勾选成员', {}

    drv_err = check_driver()
    if drv_err:
        return 400, drv_err.get('data', '浏览器未就绪，请先在首页初始化浏览器'), {}

    play_time = group['time']
    message_text = group['text']

    created = updated = fail = 0
    details = []
    for name in list(group['members']):
        try:
            found = runtime.douyin.Find_Friends(name)
            if not found.is_bool:
                fail += 1
                details.append({'name': name, 'ok': False, 'msg': '好友不存在或列表未加载'})
                continue

            old_id, old_task = task_of_friend(name)
            if old_id:
                try:
                    schedule.cancel_job(old_task['job'])
                except Exception:
                    pass
                scheduled_tasks.pop(old_id, None)

            new_id = f'{play_time}_{name}'
            # 同时间同好友可能已存在同 ID 的残留任务
            stale = scheduled_tasks.pop(new_id, None)
            if stale:
                try:
                    schedule.cancel_job(stale['job'])
                except Exception:
                    pass

            # 组里配了表情就带上表情；文字组沿用组消息
            group_type = group.get('send_type', 'text')
            if group_type in ('sticker', 'both'):
                send_type = group_type
                sticker_src = group.get('sticker_src', '')
            else:
                send_type = 'text'
                sticker_src = ''
            schedule_task(new_id, play_time, name, message_text, group_id,
                          runtime.douyin.get_friend_id(name), send_type, sticker_src)
            if old_id:
                updated += 1
            else:
                created += 1
            details.append({'name': name, 'ok': True, 'msg': play_time})
        except Exception as e:
            fail += 1
            details.append({'name': name, 'ok': False, 'msg': str(e) or '应用失败'})

    save_tasks()
    summary = f'已应用：新建 {created} 个，更新 {updated} 个'
    if fail:
        summary += f'，失败 {fail} 个'
    return 200, summary, {
        'created': created,
        'updated': updated,
        'fail': fail,
        'details': details,
    }


def list_groups_with_status():
    """分组列表 + 每个成员的任务同步状态，供前端展示"""
    out = []
    for group in groups:
        members = []
        synced = 0
        for name in group['members']:
            _, task = task_of_friend(name)
            is_synced = bool(task) and task.get('time') == group['time'] and \
                task.get('send_type', 'text') == group.get('send_type', 'text') and \
                (task.get('sticker_src') or '') == (group.get('sticker_src') or '') and \
                (task.get('msg') or '') == (group.get('text') or '')
            if is_synced:
                synced += 1
            members.append({
                'name': name,
                'has_task': bool(task),
                'task_time': task.get('time', '') if task else '',
                'synced': is_synced,
                'last_result': task.get('last_result', '') if task else '',
            })
        out.append({
            'id': group['id'],
            'name': group['name'],
            'time': group['time'],
            'text': group['text'],
            'send_type': group.get('send_type', 'text'),
            'sticker_src': group.get('sticker_src', ''),
            'members': members,
            'member_count': len(members),
            'synced_count': synced,
            'created_at': group.get('created_at', ''),
        })
    return out


def member_group_map():
    """好友名 -> 分组名，前端好友列表直接标注归属"""
    return {m: g['name'] for g in groups for m in g['members']}


load_groups()
