"""定时任务调度模块"""
import os
import json
import time
import random
import threading
import schedule

from spark_backend.config import config, DATA_DIR
from spark_backend.utils import now_str, format_time
from spark_backend.risk_control import risk_state, pick_message_content, record_send_success, record_send_failure
from spark_backend.browser_manager import check_driver
from spark_backend import runtime


# 定时任务存储（字典不重新绑定，只改属性，可 from import）
# 内存里只放当前活跃账号的任务，账号切换时由 restore_tasks 重载
scheduled_tasks = {}  # 格式: {任务ID: {job, time, name, account_id, ...}}

# 定时任务持久化文件（含所有账号的任务，按 account_id 区分）
TASKS_FILE = os.path.join(DATA_DIR, 'tasks.json')

# 批量执行、分组应用、定时任务可能在不同线程里同时落盘，
# 并发写同一个 json 会写出半截文件（任务全部丢失），这里串行化
_save_lock = threading.Lock()


def _task_account(task):
    return str((task or {}).get('account_id') or 'default')


def save_tasks():
    """将定时任务元数据持久化到 tasks.json（其他账号的任务原样保留）"""
    tasks_meta = []
    for task_id, task in scheduled_tasks.items():
        tasks_meta.append({
            'task_id': task_id,
            'time': task.get('time', ''),
            'name': task.get('name', ''),
            'sec_uid': task.get('sec_uid', ''),
            'msg': task.get('msg', ''),
            'send_type': task.get('send_type', 'text'),
            'sticker_src': task.get('sticker_src', ''),
            'group_id': task.get('group_id') or '',
            'account_id': _task_account(task),
        })
    try:
        with _save_lock:
            on_disk = []
            if os.path.exists(TASKS_FILE):
                try:
                    with open(TASKS_FILE, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        on_disk = data
                except Exception:
                    on_disk = []
            aid = runtime.active_account_id or 'default'
            others = [t for t in on_disk if str(t.get('account_id') or 'default') != aid]
            with open(TASKS_FILE, 'w', encoding='utf-8') as f:
                json.dump(others + tasks_meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'⚠️ 定时任务持久化失败: {e}')


def load_tasks_meta():
    """从 tasks.json 读取任务元数据"""
    if not os.path.exists(TASKS_FILE):
        return []
    try:
        with open(TASKS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def purge_account_tasks(account_id):
    """从磁盘删除某个账号的全部定时任务"""
    try:
        with _save_lock:
            if not os.path.exists(TASKS_FILE):
                return
            meta = load_tasks_meta()
            kept = [t for t in meta if str(t.get('account_id') or 'default') != account_id]
            with open(TASKS_FILE, 'w', encoding='utf-8') as f:
                json.dump(kept, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'⚠️ 清理账号任务失败: {e}')


def unload_tasks():
    """取消内存中全部定时任务（切换账号前调用，任务仍留在磁盘）"""
    for task_id, task in list(scheduled_tasks.items()):
        try:
            schedule.cancel_job(task['job'])
        except Exception:
            pass
    scheduled_tasks.clear()


def schedule_task(task_id: str, play_time: str, name: str, msg: str, group_id: str = '',
                  sec_uid: str = '', send_type: str = 'text', sticker_src: str = '',
                  account_id: str = ''):
    job = schedule.every().day.at(play_time).do(run_task_async, task_id)
    scheduled_tasks[task_id] = {
        'task_id': task_id,
        'time': play_time,
        'name': name,
        'sec_uid': sec_uid or '',
        'msg': msg or '',
        'send_type': send_type or 'text',
        'sticker_src': sticker_src or '',
        'group_id': group_id or '',
        'account_id': account_id or runtime.active_account_id or 'default',
        'job': job,
        'last_result': '',
        'last_generated_text': '',
        'last_run_at': '',
        'last_jitter_seconds': 0
    }
    return scheduled_tasks[task_id]


def run_task_async(task_id: str):
    threading.Thread(target=execute_task_send, args=(task_id,), daemon=True).start()


def deliver_task(task, on_stage=None):
    """按任务类型发送内容

    send_type:
      text   —— 只发文字（留空则从模板池随机）
      sticker—— 只发表情
      both   —— 先发文字，间隔 0.8~1.6 秒再发表情

    返回 (ok, error, desc)，desc 用于写 last_generated_text / 发送记录。
    """
    name = task.get('name', '')
    sec_uid = task.get('sec_uid', '')
    kind = task.get('send_type', 'text')
    if kind not in ('text', 'sticker', 'both'):
        kind = 'text'
    parts = []

    def stage(text):
        if on_stage is None:
            return
        try:
            on_stage(text)
        except Exception:
            pass

    if kind in ('text', 'both'):
        message = pick_message_content(name, task.get('msg', ''))
        task['last_generated_text'] = message
        stage('正在发送文字')
        result = runtime.douyin.Send_Frinder(name, message, sec_uid, on_stage=on_stage)
        if not result.is_bool:
            return False, result.string or '文字发送失败', message
        parts.append(message)
        if kind == 'both':
            time.sleep(random.uniform(0.8, 1.6))

    if kind in ('sticker', 'both'):
        src = task.get('sticker_src', '')
        if not src:
            return False, '任务缺少表情，请重新选择', ' + '.join(parts)
        stage('正在发送表情')
        result = runtime.douyin.Send_Sticker(name, 0, src, sec_uid)
        if not result.is_bool:
            err = result.string or '表情发送失败'
            if parts:
                err = f'文字已发送，表情失败：{err}'
            return False, err, ' + '.join(parts)
        parts.append('[表情包]')

    desc = ' + '.join(parts)
    task['last_generated_text'] = desc
    return True, None, desc


def execute_task_send(task_id: str):
    task = scheduled_tasks.get(task_id)
    if not task:
        return
    task['last_run_at'] = now_str()
    if risk_state.get('paused'):
        task['last_result'] = '已暂停'
        return
    drv_err = check_driver()
    if drv_err:
        task['last_result'] = drv_err.get('data', '浏览器异常')
        record_send_failure(task['last_result'], task['name'])
        return
    jitter_seconds = random.randint(0, config.get('task_jitter_minutes', 0) * 60) if config.get('task_jitter_minutes', 0) > 0 else 0
    task['last_jitter_seconds'] = jitter_seconds
    if jitter_seconds > 0:
        time.sleep(jitter_seconds)
    try:
        ok, err, desc = deliver_task(task)
        if ok:
            task['last_result'] = '发送成功'
            record_send_success(task['name'], desc, 'scheduled')
        else:
            task['last_result'] = err or '发送失败'
            record_send_failure(f'任务 {task_id} 失败：{task["last_result"]}', task['name'])
    except Exception as e:
        task['last_result'] = str(e) or '发送异常'
        record_send_failure(f'任务 {task_id} 异常：{task["last_result"]}', task['name'])


def restore_tasks():
    """浏览器初始化后，从持久化文件恢复【当前活跃账号】的定时任务"""
    aid = runtime.active_account_id or 'default'
    meta_list = load_tasks_meta()
    restored = 0
    for meta in meta_list:
        try:
            if str(meta.get('account_id') or 'default') != aid:
                continue
            task_id = meta.get('task_id', '')
            play_time = meta.get('time', '22:00')
            name = meta.get('name', '')
            sec_uid = meta.get('sec_uid', '')
            msg = meta.get('msg', '')
            group_id = meta.get('group_id', '')
            send_type = meta.get('send_type', 'text')
            sticker_src = meta.get('sticker_src', '')
            if not name or task_id in scheduled_tasks:
                continue
            schedule_task(task_id, play_time, name, msg, group_id, sec_uid, send_type, sticker_src,
                          account_id=aid)
            restored += 1
        except Exception as e:
            print(f'⚠️ 恢复任务失败: {e}')
            continue
    if restored > 0:
        print(f'✅ 已恢复 {restored} 个定时任务（账号 {aid}）')


# 定时线程
_scheduler_started = False  # 防止重复启动调度线程


def run_schedule():
    """后台线程运行定时任务"""
    while True:
        schedule.run_pending()
        time.sleep(1)


def start_scheduler():
    """启动定时任务调度线程（仅启动一次）"""
    global _scheduler_started
    if _scheduler_started:
        return
    scheduler_thread = threading.Thread(target=run_schedule, daemon=True)
    scheduler_thread.start()
    _scheduler_started = True
