"""抖音自动续火花 - 后端主入口

模块拆分后的主入口文件，只包含：
- FastAPI 实例与中间件
- 所有 HTTP 路由
- 前端 SPA 静态文件托管
- 服务启动逻辑

业务逻辑分散在各子模块中：
  config.py          - 配置与常量
  runtime.py         - 运行时全局状态（driver/douyin/init 等）
  utils.py           - 通用工具与数据类
  browser_manager.py - 浏览器驱动管理
  risk_control.py    - 风控与消息选择
  auth.py            - 认证鉴权
  douyin_sticker.py  - 表情包 Mixin
  douyin_core.py     - Douyin 核心类
  scheduler.py       - 定时任务调度
"""
import os
import re
import gzip
import json
import time
import base64
import random
import platform
import sys
import threading
import webbrowser
import atexit
import uvicorn
from datetime import datetime

from selenium.webdriver import Keys
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException
from selenium.common.exceptions import SessionNotCreatedException
from selenium.common.exceptions import WebDriverException

from fastapi import FastAPI, Header, Request, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import get_route_path

# 日志同时输出到控制台与 logs/server.log：
# - 控制台便于实时观察（关闭 cmd 窗口即停止服务）
# - 文件便于事后排查（pythonw 无控制台时也靠它）
class _TeeStream:
    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
        return len(data)

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    @property
    def encoding(self):
        return 'utf-8'

    def writable(self):
        return True


# 本项目模块
from spark_backend.config import (
    config, save_config, BASE_DIR, DIST_DIR, APP_HOME,
    normalize_browser_name, get_browser_label,
    clamp_int, normalize_message_templates, DEFAULT_MESSAGE_TEMPLATES,
    normalize_clock,
    is_port_in_use, find_available_port,
)

# 日志：同时输出到控制台与 logs/server.log（打包后日志落在用户目录）
try:
    _log_dir = os.path.join(APP_HOME, 'logs')
    os.makedirs(_log_dir, exist_ok=True)
    _log_fp = open(os.path.join(_log_dir, 'server.log'), 'a', encoding='utf-8', buffering=1)
    if sys.stdout is None or sys.stderr is None:
        sys.stdout = sys.stderr = _log_fp
    else:
        sys.stdout = _TeeStream(sys.stdout, _log_fp)
        sys.stderr = _TeeStream(sys.stderr, _log_fp)
except Exception:
    pass

from spark_backend import runtime
from spark_backend import progress
from spark_backend import accounts as account_store
from spark_backend import groups as group_store
from spark_backend.friends_store import save_friends, load_friends
from spark_backend.runtime import _user_cache, init_lock
from spark_backend.utils import format_time, now_str
from spark_backend.browser_manager import create_webdriver, check_driver, cleanup_on_exit
from spark_backend.risk_control import (
    risk_state, prune_risk_state, save_risk_state,
    pause_risk, resume_risk,
    record_send_success, record_send_failure,
    pick_message_content,
)
from spark_backend.auth import (
    hash_pwd, verify_pwd, generate_token, verify_token, remove_token,
    require_auth,
)
from spark_backend.douyin_core import Douyin
from spark_backend.scheduler import (
    scheduled_tasks, save_tasks, schedule_task, restore_tasks, start_scheduler,
    unload_tasks,
    deliver_task,
)

# 运行时全局状态（会重新绑定的变量从 runtime 访问）
# driver / douyin / init / Login_is_bool / active_account_id → runtime.xxx
# _user_cache / init_lock → 已从 runtime 导入

# 启动即恢复配置里的活跃账号，并按账号重载分组 / 定时任务
account_store.apply_active_account()

app = FastAPI()

# CORS 配置（前后端同源，仅允许本地访问）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://127.0.0.1", "http://localhost:8080", "http://127.0.0.1:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# API 前缀重写中间件：将 /api/xxx 转发到 /xxx
class APIRewriteMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        scope = request.scope
        route_path = get_route_path(scope)
        if route_path.startswith('/api/'):
            rewritten = route_path[4:]
            root = scope.get('root_path', '')
            path = scope.get('path', '')
            # 新版 Starlette 的 scope["path"] 保留挂载前缀，老版已剥离
            scope['path'] = root + rewritten if root and path.startswith(root) else rewritten
        return await call_next(request)


app.add_middleware(APIRewriteMiddleware)

# 服务启动时间
runtime.start_time = datetime.now()


# ===== 抖音操作路由 =====

@app.get('/Home')
def Home(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    return {'time': runtime.start_time}


# ===== 浏览器会话与账号切换的内部工具 =====

def _quit_browser():
    """关闭当前浏览器并清理会话状态（不动账号数据）"""
    try:
        if runtime.driver is not None:
            runtime.driver.quit()
    except Exception:
        pass
    runtime.driver = None
    runtime.douyin = None
    runtime.init = False
    runtime.Login_is_bool = False
    runtime.login_pending = None
    _user_cache['nickname'] = ''
    _user_cache['avatar'] = ''


def _douyin_logged_in():
    """综合判断抖音是否已登录：聊天页无登录面板 + 浏览器里存在会话 Cookie。

    只看登录面板有误判风险：页面还没加载完时面板也没出现，会被当成已登录。
    """
    try:
        if runtime.douyin._login_panel_visible():
            return False
        cookies = runtime.driver.get_cookies() or []
        names = {c.get('name') for c in cookies if isinstance(c, dict)}
        return bool(names & {'sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard'})
    except Exception:
        return False


def _create_account_driver(account_id=None, force_visible=False):
    """按账号创建浏览器并完成基础配置；失败返回 (None, 响应dict)"""
    browser_name = normalize_browser_name(config.get('browser_name'))
    browser_path = (config.get('browser_path') or '').strip()
    try:
        driver = create_webdriver(account_id=account_id, force_visible=force_visible)
        # 无头模式默认窗口只有 800x600，会少渲染好几行会话，统一放大到 1400x900
        try:
            driver.set_window_size(1400, 900)
        except Exception:
            pass
        # 设置页面加载策略为 eager（DOM 就绪即可，不等所有资源）
        try:
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        except Exception:
            pass
        driver.set_page_load_timeout(30)

        # 会话健康检查：驱动版本不匹配或本机代理劫持 localhost 时，
        # 驱动能启动但后续命令全部失效，必须在这里拦下来给出明确原因。
        try:
            driver.current_url
        except Exception as sess_err:
            try:
                driver.quit()
            except Exception:
                pass
            return None, {
                'code': 500,
                'data': f'浏览器会话异常：{str(sess_err)[:150]}。'
                        f'常见原因：驱动版本与浏览器不一致，或系统代理拦截了本机连接。'
            }
        return driver, None
    except SessionNotCreatedException as e:
        return None, {'code': 400, 'data': f'浏览器驱动启动失败：{str(e)[:180]}'}
    except WebDriverException as e:
        detail = str(e)[:180]
        if browser_path:
            return None, {'code': 500, 'data': f'{get_browser_label(browser_name)} 启动失败，请检查路径是否正确: {browser_path}（{detail}）'}
        return None, {'code': 500, 'data': f'{get_browser_label(browser_name)} 启动失败：{detail}'}
    except Exception as e:
        return None, {'code': 500, 'data': f'初始化失败: {str(e) or "未知错误"}'}


def _fill_missing_sec_uids():
    """老任务里没有隐藏ID：用缓存补上，之后好友改名也不会发错人"""
    try:
        filled = False
        for task in scheduled_tasks.values():
            if not task.get('sec_uid'):
                uid = runtime.douyin.get_friend_id(task.get('name', ''))
                if uid:
                    task['sec_uid'] = uid
                    filled = True
        if filled:
            save_tasks()
    except Exception:
        pass


def _try_auto_login_with_saved_cookies():
    """profile 丢了登录态但账号里存过 Cookie：自动注入恢复登录"""
    if not runtime.init or not runtime.douyin:
        return
    account = account_store.get_account(runtime.active_account_id)
    if not account or not account.get('cookies'):
        return
    try:
        # 登录面板没出现且有会话 Cookie = 其实已登录（只是列表渲染慢），直接标记
        runtime.douyin.goto_login_page()
        if _douyin_logged_in():
            runtime.Login_is_bool = True
            return
        added = 0
        for cookie in account['cookies']:
            if not isinstance(cookie, dict) or not cookie.get('name'):
                continue
            try:
                runtime.driver.add_cookie(cookie)
                added += 1
            except Exception:
                # 个别字段不合法（sameSite/domain 等）不影响其余 cookie
                continue
        if added:
            runtime.driver.refresh()
            time.sleep(1.5)
            if runtime.douyin.ensure_chat_page():
                runtime.Login_is_bool = True
                print(f'✅ 账号「{account["name"]}」已通过保存的 Cookie 恢复登录')
    except Exception:
        pass


def _start_browser_session(account_id=None):
    """按账号启动浏览器会话，返回响应 dict"""
    driver, err = _create_account_driver(account_id)
    if err:
        return err
    runtime.driver = driver
    runtime.douyin = Douyin(driver)
    # 预置「昵称 -> 隐藏ID」：重启后还没刷新好友列表时，也能按 ID 找人
    runtime.douyin.load_cached_ids(load_friends())
    chat_ok = runtime.douyin.ensure_chat_page()
    if not chat_ok:
        print('⚠️ 初始化后未能进入抖音聊天页，请确认已登录或稍后点「刷新好友」重试')
    runtime.init = True
    runtime.Login_is_bool = bool(chat_ok)  # 聊天页能进基本等于已登录
    _user_cache['nickname'] = ''
    _user_cache['avatar'] = ''
    start_scheduler()  # 启动调度线程
    restore_tasks()  # 恢复持久化的定时任务（仅当前账号）
    _fill_missing_sec_uids()
    if not chat_ok:
        _try_auto_login_with_saved_cookies()
    else:
        # 登录态存回与聊天导出共用的 Cookie 存档：导出那边 profile 掉登录时能直接恢复
        _save_current_cookies_to_account()
    browser_name = normalize_browser_name(config.get('browser_name'))
    return {'code': 200, 'data': f'{get_browser_label(browser_name)} 初始化成功'}


@app.get('/Api/Init')  # 初始化浏览器
def Init(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    if runtime.login_pending:
        return {'code': 400, 'data': '正在等待浏览器登录，请先在弹出的窗口里完成登录'}

    with init_lock:  # 加锁，防止重复点击触发多次初始化
        if runtime.init and runtime.driver is not None:
            try:
                # 会话还活着才算是「重复初始化」
                runtime.driver.current_url
                return {'code': 200, 'data': 'init Repeated!'}
            except Exception:
                # 浏览器被用户手动关掉，但状态还留着：清掉再重新初始化
                _quit_browser()
        return _start_browser_session()


@app.get('/Api/GetInit')  # 获取初始化状态
def GetInit(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    return {'code': 200, 'data': 'Yes' if runtime.init else 'No'}


# ===== 多账号管理 =====

def _switch_to_account(account_id):
    """切换活跃账号：关旧浏览器 -> 换 profile -> 重载该账号任务/分组 -> 启动浏览器"""
    if not account_store.get_account(account_id):
        return {'code': 404, 'data': '账号不存在'}
    with runtime.account_lock:
        _quit_browser()
        unload_tasks()  # 取消旧账号的定时调度（任务仍留在磁盘）
        account_store.set_active_account(account_id)
        group_store.load_groups()  # 重载新账号的分组
        restore_tasks()  # 恢复新账号的定时任务
        resp = _start_browser_session(account_id=account_id)
        if resp.get('code') != 200:
            return resp
        # profile 登录态失效时，尝试用账号存档的 Cookie 恢复
        if not runtime.Login_is_bool:
            _try_auto_login_with_saved_cookies()
        account = account_store.get_account(account_id)
        return {'code': 200, 'data': f'已切换到账号「{account["name"] if account else account_id}」'}


def _fetch_self_info():
    """用登录 Cookie 调自我信息接口，拿到 uid / 昵称 / 头像。"""
    if runtime.driver is None:
        return {}
    try:
        runtime.driver.set_script_timeout(15)
        return runtime.driver.execute_async_script("""
            const api = arguments[0];
            const done = arguments[arguments.length - 1];
            fetch(api, {credentials: 'include'})
              .then(r => r.json())
              .then(j => {
                if (!j || j.status_code !== 0 || !j.user) return done(null);
                const u = j.user;
                done({
                  uid: String(u.uid || ''),
                  sec_uid: u.sec_uid || '',
                  nickname: u.nickname || '',
                  avatar_url: (u.avatar_thumb && u.avatar_thumb.url_list && u.avatar_thumb.url_list[0]) || ''
                });
              })
              .catch(() => done(null));
        """, account_store.SELF_PROFILE_API) or {}
    except Exception:
        return {}


def _finish_browser_login():
    """浏览器登录完成：抓取 Cookie 存档、记录昵称、进入聊天页"""
    account = account_store.get_account(runtime.active_account_id)
    # 1. 抓取浏览器 Cookie 存档到账号
    try:
        if runtime.driver is not None:
            cookies = runtime.driver.get_cookies()
            if cookies:
                account_store.save_account_cookies(runtime.active_account_id, cookies)
    except Exception:
        pass
    runtime.Login_is_bool = True
    runtime.login_pending = None
    # 2. 进入聊天页并抓昵称 / 抖音号，回写到统一账号
    try:
        if runtime.douyin.ensure_chat_page():
            nickname = _extract_nickname()
            if nickname:
                _user_cache['nickname'] = nickname
            info = _fetch_self_info()
            if info or nickname:
                account_store.set_account_identity(
                    runtime.active_account_id,
                    uid=info.get('uid', ''),
                    sec_uid=info.get('sec_uid', ''),
                    avatar_url=info.get('avatar_url', ''),
                    nickname=info.get('nickname') or nickname or '',
                )
    except Exception:
        pass


def _account_public(account):
    """账号信息（去掉 Cookie，给前端展示）"""
    return {
        'id': account['id'],
        'name': account['name'],
        'nickname': account.get('nickname', ''),
        'is_default': account_store.is_default_account(account['id']),
        'is_active': account['id'] == runtime.active_account_id,
        # 只有活跃账号能探到实时登录态
        'active_login': bool(runtime.init and runtime.Login_is_bool) if account['id'] == runtime.active_account_id else False,
        'has_cookies': bool(account.get('cookies')),
        'cookie_saved_at': account.get('cookie_saved_at', ''),
        'created_at': account.get('created_at', ''),
        'last_used_at': account.get('last_used_at', ''),
    }


@app.get('/Api/Account/List')  # 账号列表
def account_list(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    return {'code': 200, 'data': {
        'accounts': [_account_public(a) for a in account_store.list_accounts()],
        'active_id': runtime.active_account_id,
        'login_pending': bool(runtime.login_pending),
    }}


@app.post('/Api/Account/Add')  # 新增账号（只建数据，不登录）
def account_add(payload: dict = Body(default=None), authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    name = str((payload or {}).get('name') or '').strip()[:40]
    if not name:
        return {'code': 400, 'data': '请填写账号备注名'}
    account, err = account_store.create_account(name)
    if err:
        return {'code': 400, 'data': err}
    return {'code': 200, 'data': account}


@app.get('/Api/Account/Rename')  # 重命名账号
def account_rename(account_id: str, name: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    account, err = account_store.rename_account(account_id, name)
    if err:
        return {'code': 400, 'data': err}
    return {'code': 200, 'data': account}


@app.get('/Api/Account/Remove')  # 删除账号
def account_remove(account_id: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    if account_id == runtime.active_account_id:
        return {'code': 400, 'data': '请先切换到其他账号，再删除当前账号'}
    ok, err = account_store.delete_account(account_id)
    if not ok:
        return {'code': 400, 'data': err}
    return {'code': 200, 'data': '账号已删除（Cookie、定时任务、分组、好友缓存与浏览器目录已一并清理）'}


@app.get('/Api/Account/Switch')  # 切换账号
def account_switch(account_id: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    if runtime.login_pending:
        return {'code': 400, 'data': '正在等待浏览器登录，请先完成或取消登录'}
    if account_id == runtime.active_account_id:
        return {'code': 200, 'data': '该账号已是当前账号'}
    return _switch_to_account(account_id)


@app.post('/Api/Account/BrowserLogin')  # 弹出浏览器登录，自动抓取 Cookie
def account_browser_login(payload: dict = Body(default=None), authorization: str = Header(None)):
    """弹出浏览器窗口，用户在窗口里完成抖音登录后，系统自动抓取 Cookie 存档。

    payload: {'account_id': 'xxx'} 重新登录指定账号
             {'name': '备注名'}      添加新账号并登录
             {}                     重新登录当前账号
    """
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    payload = payload or {}
    name = str(payload.get('name') or '').strip()[:40]
    account_id = str(payload.get('account_id') or '').strip()

    with runtime.account_lock:
        if runtime.login_pending:
            return {'code': 400, 'data': '已有登录窗口正在等待，请先完成或取消'}

        if account_id:
            account = account_store.get_account(account_id)
            if not account:
                return {'code': 404, 'data': '账号不存在'}
        elif name:
            account, err = account_store.create_account(name)
            if err:
                return {'code': 400, 'data': err}
        else:
            account = account_store.get_account(runtime.active_account_id)
            if not account:
                return {'code': 404, 'data': '当前账号不存在，请先添加账号'}

        # 目标账号不是活跃账号：先切换数据上下文（任务/分组/缓存按账号隔离）
        if account['id'] != runtime.active_account_id:
            _quit_browser()
            unload_tasks()
            account_store.set_active_account(account['id'])
            group_store.load_groups()
            restore_tasks()
        else:
            _quit_browser()
            unload_tasks()
            group_store.load_groups()
            restore_tasks()

        # 弹出浏览器（强制显示窗口，登录必须看得见）
        driver, err = _create_account_driver(account_id=account['id'], force_visible=True)
        if err:
            return err
        runtime.driver = driver
        runtime.douyin = Douyin(driver)
        runtime.douyin.load_cached_ids(load_friends())
        runtime.init = True
        runtime.Login_is_bool = False
        _user_cache['nickname'] = ''
        _user_cache['avatar'] = ''
        start_scheduler()

        # 打开抖音聊天页：未登录会自动弹出登录面板
        try:
            runtime.douyin.goto_login_page()
        except Exception as e:
            _quit_browser()
            return {'code': 500, 'data': f'打开抖音页面失败: {str(e)[:120]}'}

        # profile 里还有效登录态：直接完成
        if _douyin_logged_in():
            _finish_browser_login()
            return {'code': 200, 'data': f'账号「{account["name"]}」已是登录状态，Cookie 已更新存档'}

        runtime.login_pending = {'account_id': account['id'], 'started_at': time.time()}
        return {'code': 200, 'data': '已弹出浏览器，请在窗口中完成抖音登录（扫码/验证码/密码均可）'}


@app.get('/Api/Account/LoginState')  # 轮询浏览器登录状态
def account_login_state(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    if not runtime.login_pending:
        return {'code': 200, 'data': {
            'status': 'idle',
            'logged_in': bool(runtime.init and runtime.Login_is_bool),
            'account_id': runtime.active_account_id,
        }}
    pending = runtime.login_pending
    elapsed = int(time.time() - pending.get('started_at', time.time()))

    # 超过 10 分钟自动收尾，避免浏览器一直挂着
    if elapsed > 600:
        _quit_browser()
        return {'code': 200, 'data': {'status': 'expired', 'logged_in': False, 'message': '登录等待已超时，请重新点击登录'}}

    drv_err = check_driver()
    if drv_err:
        return {'code': 200, 'data': {'status': 'expired', 'logged_in': False,
                                      'message': drv_err.get('data', '浏览器已关闭，登录取消')}}

    try:
        runtime.douyin.goto_login_page()
        if not _douyin_logged_in():
            # 登录面板还在 / 没有会话 Cookie = 还没登上，继续等
            return {'code': 200, 'data': {'status': 'waiting', 'logged_in': False, 'elapsed': elapsed}}
    except Exception:
        return {'code': 200, 'data': {'status': 'waiting', 'logged_in': False, 'elapsed': elapsed}}

    # 登录面板消失 = 登录成功：抓 Cookie 存档
    try:
        _finish_browser_login()
        return {'code': 200, 'data': {
            'status': 'success', 'logged_in': True,
            'account_id': pending.get('account_id', ''),
            'nickname': _user_cache.get('nickname', ''),
        }}
    except Exception as e:
        return {'code': 500, 'data': f'登录成功但保存失败: {str(e)[:100]}'}


@app.post('/Api/Account/CancelLogin')  # 取消等待中的浏览器登录
def account_cancel_login(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    if not runtime.login_pending:
        return {'code': 400, 'data': '当前没有等待中的登录'}
    _quit_browser()
    return {'code': 200, 'data': '已取消登录'}


@app.post('/Api/login')  # 登录 传入cooke
def Login(cooke: str = Body(default=None), gzip_flag: bool = Body(default=False), authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    drv_err = check_driver()
    if drv_err:
        return drv_err
    if cooke:
        try:
            decoded_bytes = base64.b64decode(cooke)
            if gzip_flag:
                try:
                    decoded_bytes = gzip.decompress(decoded_bytes)
                except Exception:
                    return {'code': 404,
                            'data': 'login-error-gzip decompress failed, check cookie format and gzip flag'}
            cookie_list = decoded_bytes.decode('utf-8')
            # 前端做了 JSON.stringify(cookie)，需要先 json.loads 去掉外层引号
            cookie_b64 = json.loads(cookie_list)
            # 再 base64 解码得到 cookie JSON
            cookie_json = base64.b64decode(cookie_b64).decode('utf-8')
            cookies = json.loads(cookie_json)
            if not isinstance(cookies, list) or not cookies:
                return {'code': 404, 'data': 'Cookie格式错误，请检查'}
            # add_cookie 要求当前页面已经在目标域名下，否则整批都会失败
            runtime.douyin.goto_login_page()
            added = 0
            for cookie in cookies:
                if not isinstance(cookie, dict) or not cookie.get('name'):
                    continue
                try:
                    runtime.driver.add_cookie(cookie)
                    added += 1
                except Exception:
                    # 个别字段不合法（sameSite/domain 等）不影响其余 cookie
                    continue
            if added == 0:
                return {'code': 404, 'data': 'Cookie写入失败，请确认是在 douyin.com 下导出的 Cookie'}
        except Exception:
            return {'code': 404, 'data': 'Cookie格式错误，请检查'}
        runtime.driver.refresh()
        try:
            runtime.driver.find_element(By.XPATH, '//*[@id="douyin_login_comp_flat_panel"]/picture')
            runtime.Login_is_bool = False
            return {'code': 404, 'data': 'login-error-cooker cant login'}
        except NoSuchElementException:
            runtime.Login_is_bool = True
            runtime.douyin.ensure_chat_page()
            # 登录成功：把当前浏览器里的 Cookie 存档到活跃账号
            _save_current_cookies_to_account()
            return {'code': 200, 'data': 'ok'}
    else:
        return {'code': 404, 'data': 'login-error-not cooker'}


def _save_current_cookies_to_account():
    """把当前浏览器 Cookie 存档到活跃账号（手动登录/导出后调用）"""
    try:
        if runtime.driver is None:
            return
        cookies = runtime.driver.get_cookies()
        if cookies:
            account_store.save_account_cookies(runtime.active_account_id, cookies)
    except Exception:
        pass


@app.get('/Api/Pnglogin')  # 扫码登录后检查状态
def PngLogin(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    drv_err = check_driver()
    if drv_err:
        return drv_err
    # 先回到聊天页（登录弹窗只在这里），否则停在别的页面会误判为「已登录」
    runtime.douyin.goto_login_page()
    try:
        runtime.driver.find_element(By.XPATH, '//*[@id="douyin_login_comp_flat_panel"]/picture')
        # 找到登录弹窗 = 未登录
        runtime.Login_is_bool = False
        return {'code': 404, 'data': '系统繁忙,请稍后重新登录'}
    except NoSuchElementException:
        # 没有登录弹窗 = 已登录
        runtime.Login_is_bool = True
        return {'code': 200, 'data': 'ok'}


@app.get('/Api/GetLogin')  # 获取登录
def GetLogin(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    # 实际检查抖音页面登录状态，而非只看缓存标记
    if not runtime.init or runtime.driver is None:
        runtime.Login_is_bool = False
        return {'code': 200, 'data': 'No'}
    try:
        # 不在聊天页时页面上的登录弹窗状态不可信（例如停在视频页），直接返回上次结果
        if not runtime.douyin._is_chat_url(runtime.driver.current_url):
            return {'code': 200, 'data': 'Yes' if runtime.Login_is_bool else 'No'}
        # 短暂等待页面渲染（降低频率，避免频繁触发风控）
        time.sleep(0.3)
        runtime.driver.find_element(By.XPATH, '//*[@id="douyin_login_comp_flat_panel"]/picture')
        # 找到登录弹窗 = 未登录
        runtime.Login_is_bool = False
        return {'code': 200, 'data': 'No'}
    except NoSuchElementException:
        # 没有登录弹窗 = 已登录
        runtime.Login_is_bool = True
        return {'code': 200, 'data': 'Yes'}
    except Exception:
        # 页面异常时回退到缓存标记
        return {'code': 200, 'data': 'Yes' if runtime.Login_is_bool else 'No'}


@app.get('/Api/login/Init/GetLoginPng')  # 获取登录扫码
def GetLoginPng(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    try:
        drv_err = check_driver()
        if drv_err:
            return drv_err
        runtime.douyin.goto_login_page()
        Douyin.LoginInit(runtime.douyin)
        try:
            error = runtime.driver.find_element(By.XPATH, '//*[@id="animate_qrcode_container"]/div[2]/div/p[1]')
            img_element = runtime.driver.find_element(By.XPATH, '//*[@id="animate_qrcode_container"]/div[2]/img')
            img_element.click()
        except:
            pass
        img_element = runtime.driver.find_element(By.XPATH, '//*[@id="animate_qrcode_container"]/div[2]/img')
        login_src = img_element.get_attribute('src')
        try:
            is_rust = runtime.driver.find_element(By.XPATH, '//*[@id="animate_qrcode_container"]/div[2]/div')
            is_rust.click()
            time.sleep(5)
            img_element = runtime.driver.find_element(By.XPATH, '//*[@id="animate_qrcode_container"]/div[2]/img')
            login_src = img_element.get_attribute('src')
        except:
            pass
        if login_src:
            return {'code': 200, 'data': login_src}
        else:
            return {'code': 404, 'data': 'cant find LoginPng src attribute'}
    except NoSuchElementException:
        return {'code': 404, 'data': 'cant find img element'}
    except Exception as e:
        msg = str(e)
        if 'InvalidSessionId' in msg or 'session' in msg.lower():
            return {'code': 500, 'data': '浏览器会话已断开，请重新初始化'}
        return {'code': 500, 'data': '获取二维码失败，请确保浏览器已正常启动'}


@app.get('/Api/login/Init/GetCooker')  # 获取cooke
def GetCooke(password: str = Query(None), authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    # 验证密码
    if not password or not verify_pwd(password):
        return {'code': 400, 'data': '密码错误'}
    drv_err = check_driver()
    if drv_err:
        return drv_err
    if runtime.Login_is_bool:
        try:
            cooke = runtime.driver.get_cookies()
            cookie_json = json.dumps(cooke)
            cookie_base64 = base64.b64encode(cookie_json.encode('utf-8')).decode('utf-8')
            return {'code': 200, 'data': {'cooke': cookie_base64}}
        except Exception as e:
            return {'code': 500, 'data': '获取Cookie失败'}
    else:
        return {'code': 400, 'data': '未登录'}


@app.get('/Api/GetFriendsList')  # 获取好友列表
def GetFrindesList(force: int = 0, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err

    def _from_cache():
        cached = load_friends()
        if not cached:
            return None
        return {'code': 200, 'data': {
            'count': len(cached['list']),
            'list': cached['list'],
            'cached_at': cached.get('saved_at', ''),
            'stale': True,
        }}

    # 浏览器没就绪时先返回本地缓存，避免重启后好友列表变空白
    drv_err = check_driver()
    if drv_err:
        return _from_cache() or drv_err

    try:
        # full=True：自动滚动到底，把懒加载的好友全部抓下来
        # 10 分钟内重复请求走内存缓存，避免每次刷新都重新滚一遍
        friends_list = runtime.douyin.Updara_FrinderList(full=True, use_cache=(force == 0))
        if not friends_list:
            if not runtime.Login_is_bool:
                return {'code': 400, 'data': '抖音未登录，请先扫码登录'}
            return _from_cache() or {'code': 404, 'data': '暂无好友或页面未加载'}
        dicts = {}
        for v in friends_list:
            # 第三位是隐藏ID（sec_uid / 会话id），前端不用也不会破坏原有结构，
            # 后端靠它定位好友，昵称重复或改名都不会点错人
            dicts[v.username] = [v.avatar, v.fire, v.uid]
        save_friends([{'name': v.username, 'avatar': v.avatar, 'fire': v.fire, 'id': v.uid}
                      for v in friends_list])
        return {'code': 200, 'data': {
            'count': len(friends_list),
            'list': dicts,
            'cached_at': now_str(),
            'stale': False,
            # 没抓到昵称的人数（界面会提示「重新获取昵称」）
            'missing_names': runtime.douyin.missing_name_count(),
        }}
    except Exception as e:
        return _from_cache() or {'code': 404, 'data': f'获取好友列表失败: {e}'}


@app.get('/Api/RepairFriendNames')  # 重新获取好友昵称
def RepairFriendNames(authorization: str = Header(None)):
    """重新抓一遍好友列表，把没抓到昵称的好友补上名字

    昵称抓不到通常是这两种情况：列表滚动太快、标题还没渲染就被收走了；
    或者抖音改了行结构。这里强制重抓 + 逐个回滚补名，补到就写回本地缓存。
    """
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    drv_err = check_driver()
    if drv_err:
        return drv_err
    try:
        before = runtime.douyin.missing_name_count()
        friends_list = runtime.douyin.Updara_FrinderList(full=True, use_cache=False)
        if not friends_list:
            if not runtime.Login_is_bool:
                return {'code': 400, 'data': '抖音未登录，请先扫码登录'}
            return {'code': 404, 'data': '没能读取好友列表，请确认浏览器停在聊天页'}
        save_friends([{'name': v.username, 'avatar': v.avatar, 'fire': v.fire, 'id': v.uid}
                      for v in friends_list])
        after = runtime.douyin.missing_name_count()
        fixed = max(0, before - after)
        msg = f'已重新获取，共 {len(friends_list)} 位好友'
        if fixed:
            msg += f'，补全 {fixed} 个昵称'
        if after:
            msg += f'，仍有 {after} 个没取到（可稍后再试一次）'
        return {'code': 200, 'data': msg, 'fixed': fixed, 'missing': after}
    except Exception as e:
        return {'code': 500, 'data': f'重新获取昵称失败: {str(e)[:80]}'}


@app.get('/Api/Send')  # 发送信息
def Send(name: str, text: str, sec_uid: str = Query(None), authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    drv_err = check_driver()
    if drv_err:
        return drv_err
    if progress.snapshot().get('running'):
        return {'code': 400, 'data': '批量发送正在进行，请等它结束再发送'}
    publish = True
    progress.begin('chat', 1, f'发送给 {name}')
    try:
        # 用实例方法调用（原来写死 Douyin.Send_Frinder(runtime.douyin, ...)，
        # 一旦 douyin 被替换/包装就会绕过去，行为不一致）
        out = runtime.douyin.Send_Frinder(
            name, text, sec_uid or '',
            on_stage=(lambda stage: progress.update(current=name, stage=stage)) if publish else None)
        if out.is_bool:
            record_send_success(name, text, 'manual')
            if publish:
                progress.update(done=1, ok=1)
                progress.finish(f'已发送给 {name}')
            return {'code': 200, 'data': 'Send successfully'}
        else:
            record_send_failure(f'手动发送失败：{out.string or "发送失败"}', name)
            if publish:
                progress.update(done=1, fail=1)
                progress.finish(f'{name} 发送失败：{out.string or "发送失败"}')
            return {'code': 404, 'data': out.string or '发送失败'}
    except Exception as e:
        record_send_failure(f'手动发送异常：{str(e) or "发送失败"}', name)
        if publish:
            progress.finish(f'{name} 发送异常')
        return {'code': 500, 'data': '发送失败'}


@app.get('/Api/GetStickerList')  # 获取表情包列表
def GetStickerList(name: str = Query(None), sec_uid: str = Query(None),
                   authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    drv_err = check_driver()
    if drv_err:
        return drv_err
    try:
        result = runtime.douyin.Get_Sticker_List(name, sec_uid or '')
        return result
    except Exception as e:
        return {'code': 500, 'data': '获取表情包失败'}


@app.get('/Api/SendSticker')  # 发送表情包
def SendSticker(name: str, sticker_index: int, sticker_src: str = Query(None),
                sec_uid: str = Query(None), authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    drv_err = check_driver()
    if drv_err:
        return drv_err
    try:
        out = runtime.douyin.Send_Sticker(name, sticker_index, sticker_src, sec_uid or '')
        if out.is_bool:
            record_send_success(name, f'[sticker]{sticker_index}', 'sticker')
            return {'code': 200, 'data': '表情包发送成功'}
        else:
            record_send_failure(f'发送表情包失败：{str(out.string) if out.string else "发送失败"}', name)
            return {'code': 400, 'data': str(out.string) if out.string else '发送失败'}
    except Exception as e:
        record_send_failure(f'发送表情包异常：{str(e) or "发送失败"}', name)
        return {'code': 500, 'data': '发送表情包失败'}


@app.get('/Api/GetChatHistory')  # 获取聊天记录
def GetChatHistory(name: str, sec_uid: str = Query(None), authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    drv_err = check_driver()
    if drv_err:
        return drv_err
    try:
        result = runtime.douyin.Get_Chat_History(name, sec_uid or '')
        return result
    except Exception as e:
        return {'code': 500, 'data': '获取聊天记录失败'}


def _extract_nickname():
    """从当前页面提取抖音昵称（调用方需确保已在聊天页）"""
    nickname = ''
    # 方法1：从页面全局变量 / localStorage 同步读取昵称
    try:
        nickname = (runtime.driver.execute_script(r"""
            function clean(t) { return (t || '').replace(/\s+/g, ' ').trim(); }
            function valid(t) {
                t = clean(t);
                if (!t || t.length > 30) return '';
                if (t === '抖音' || t === '未知用户') return '';
                return t;
            }
            var out = '';
            try {
                var sigi = window.SIGI_STATE || window.__INIT_PROPS__;
                if (sigi) {
                    var m = JSON.stringify(sigi).match(/"nickname"\s*:\s*"([^"]+)"/);
                    if (m && m[1]) out = m[1];
                }
            } catch (e) {}
            if (!out) {
                try {
                    var stores = [localStorage, sessionStorage];
                    for (var s = 0; s < stores.length && !out; s++) {
                        for (var i = 0; i < stores[s].length; i++) {
                            var k = stores[s].key(i);
                            try {
                                var v = stores[s].getItem(k);
                                if (!v || v.length < 5) continue;
                                if (v.indexOf('nickname') === -1 && k.toLowerCase().indexOf('user') === -1) continue;
                                var m2 = v.match(/"nickname"\s*:\s*"([^"]+)"/);
                                if (m2 && m2[1]) { out = m2[1]; break; }
                            } catch (e) {}
                        }
                    }
                } catch (e) {}
            }
            return valid(out);
        """) or '').strip()
    except Exception:
        pass

    # 方法2：从页面源码正则提取（兜底）
    if not nickname:
        try:
            page = runtime.driver.page_source or ''
            m = re.search(r'"nickname"\s*:\s*"([^"]+)"', page)
            if m:
                cand = m.group(1).strip()
                if cand and len(cand) < 30 and cand not in ('抖音', '未知用户'):
                    nickname = cand
        except Exception:
            pass
    return nickname


@app.get('/Api/GetUsername')  # 获取用户名（头像不再抓取）
def GetUserInfo(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    drv_err = check_driver()
    if drv_err:
        return drv_err
    if not runtime.Login_is_bool:
        return {'code': 200, 'data': {'nickname': '', 'avatar': ''}}

    # 有缓存直接返回
    if _user_cache['nickname'] and _user_cache['nickname'] != '未知用户':
        return {'code': 200, 'data': {'nickname': _user_cache['nickname'], 'avatar': ''}}

    # 说明：不再为了头像去打开「我的主页」。
    # 个人主页需要额外导航（慢），页面数据里混着作品封面，取到的头像经常是错的，
    # 而且多一次跳转更容易触发风控。界面上头像位已有默认图标兜底，这里只取昵称。
    nickname = ''

    if not runtime.douyin.ensure_chat_page():
        return {'code': 200, 'data': {'nickname': '', 'avatar': ''}}

    nickname = _extract_nickname()

    if nickname:
        _user_cache['nickname'] = nickname
        # 把昵称回写到账号，多账号列表里能直接认出是谁
        account_store.set_account_nickname(runtime.active_account_id, nickname)
    _user_cache['avatar'] = ''
    return {'code': 200, 'data': {'nickname': nickname, 'avatar': ''}}


@app.get('/Api/GetScrlk')  # 获取截图
def GetScrlk(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    drv_err = check_driver()
    if drv_err:
        return drv_err
    try:
        # 直接取 PNG 字节，不再往当前工作目录写 temp.png：
        # 安装到 Program Files 后工作目录不可写，旧写法会直接报错
        img_data = base64.b64encode(runtime.driver.get_screenshot_as_png()).decode('utf-8')
        return {'code': 200, 'data': img_data}
    except Exception as e:
        return {'code': 400, 'data': f'截图错误:{e}'}


@app.get('/Api/DieLogin')  # 取消登录
def DieLogin(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    drv_err = check_driver()
    if drv_err:
        return drv_err
    try:
        runtime.driver.delete_all_cookies()
        runtime.driver.refresh()
        runtime.Login_is_bool = False
        # 同步清掉账号里存档的 Cookie，避免下次切换时又登回来
        account_store.clear_account_cookies(runtime.active_account_id)
        _user_cache['nickname'] = ''
        _user_cache['avatar'] = ''
        return {'code': 200, 'data': '已清除Cooke'}
    except Exception as e:
        return {'code': 500, 'data': '清除Cookie失败'}


@app.get('/Api/LoginPhone')  # 验证码登录
def LoginPhone(areacode: str, phone: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    drv_err = check_driver()
    if drv_err:
        return drv_err
    try:
        runtime.douyin.goto_login_page()
        Douyin.LoginInit(runtime.douyin)
        areacode_value = runtime.driver.find_element(By.XPATH, '//*[@id="douyin_login_comp_normal_input_id"]/div[1]/div/input')
        areacode_value.clear()
        areacode_value.send_keys(areacode.strip())
        inp = runtime.driver.find_element(By.XPATH, '//*[@id="normal-input"]')
        inp.send_keys(phone)
        span = runtime.driver.find_element(By.XPATH, '//*[@id="douyin_login_comp_button_input_id"]/span')
        span.click()
        time.sleep(2)
        if span.text.strip() == '获取验证码':
            return {'code': 400, 'data': '验证码发送失败'}
        else:
            return {'code': 200, 'data': '验证码发送成功'}
    except Exception as e:
        return {'code': 400, 'data': '验证码发送失败'}


@app.get('/Api/LoginPhoneInput')  # 验证码登录 2 输入验证码
def LoginPhoneInput(code: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    drv_err = check_driver()
    if drv_err:
        return drv_err
    try:
        inp = runtime.driver.find_element(By.XPATH, '//*[@id="button-input"]')
        inp.send_keys(code)
        button = runtime.driver.find_element(By.XPATH, '//*[@id="douyin_login_comp_btn_id"]')
        button.click()
        time.sleep(2)
        try:
            login_div = runtime.driver.find_element(By.XPATH, '//*[@id="douyin_login_comp_flat_panel"]/picture')
            return {'code': 400, 'data': '登录失败'}
        except:
            runtime.Login_is_bool = True
            return {'code': 200, 'data': '登录成功'}
    except Exception as e:
        return {'code': 400, 'data': '验证码登录失败'}


@app.get('/Api/LoginDebug')  # 强制登录状态（调试用，需密码确认）
def LoginDebug(password: str = Query(None), authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    # 调试接口需二次密码确认，防止误用
    if not password or not verify_pwd(password):
        return {'code': 403, 'data': '需要密码确认才能使用此功能'}
    if runtime.Login_is_bool == False:
        runtime.Login_is_bool = True
        return {'code': 200, 'data': 'OK'}
    else:
        return {'code': 400, 'data': '已是登录状态,无需设定'}


# ===== 定时任务操作 =====

@app.get('/Time/add')
def add_time(time: str, name: str, text: str = None, send_type: str = 'text',
             sticker_src: str = Query(None), authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    drv_err = check_driver()
    if drv_err:
        return drv_err
    send_type = (send_type or 'text').lower()
    if send_type not in ('text', 'sticker', 'both'):
        send_type = 'text'
    sticker_src = (sticker_src or '').strip()
    if send_type in ('sticker', 'both') and not sticker_src:
        return {'code': 400, 'data': '请先选择一个要发送的表情' }
    # 检查是否已存在该好友的定时任务
    for task_id, task in scheduled_tasks.items():
        if task_id.endswith(f"_{name}"):
            return {'code': 400, 'data': f'好友 {name} 已有定时任务，请先删除或修改'}

    temp = runtime.douyin.Find_Friends(name)
    if temp.is_bool:
        play_time = format_time(time)
        msg = '' if text is None else text
        # 生成唯一任务ID
        task_id = f"{play_time}_{name}"
        # 记住好友的隐藏ID：以后昵称被改，任务照样能找到人
        sec_uid = runtime.douyin.get_friend_id(name)
        # 若该好友已在某个分组里，任务自动带上分组归属
        group_obj = group_store.find_group_of_member(name)
        schedule_task(task_id, play_time, name, msg,
                      group_obj['id'] if group_obj else '', sec_uid,
                      send_type, sticker_src)
        save_tasks()  # 持久化
        kind = {'sticker': '表情包', 'both': '文字+表情包'}.get(send_type, '文字')
        return {'code': 200, 'data': f'已添加定时任务（{kind}）: {play_time}', 'task_id': task_id}
    else:
        return {'code': 404, 'data': temp.string}


@app.get('/Time/del')
def del_time(task_id: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    """根据任务ID删除定时任务"""
    import schedule
    if task_id in scheduled_tasks:
        task = scheduled_tasks[task_id]
        try:
            schedule.cancel_job(task['job'])
        except Exception:
            pass
        del scheduled_tasks[task_id]
        save_tasks()  # 持久化
        return {'code': 200, 'data': f'已删除任务: {task_id}'}
    else:
        return {'code': 404, 'data': '任务ID不存在'}


@app.get('/Time/run')
def run_time_once(task_id: str, authorization: str = Header(None)):
    """立即执行一次任务：不等定时时间，也不走随机延迟"""
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    task = scheduled_tasks.get(task_id)
    if not task:
        return {'code': 404, 'data': '任务不存在'}
    drv_err = check_driver()
    if drv_err:
        return drv_err
    if risk_state.get('paused'):
        return {'code': 400, 'data': '风控已暂停，请先在设置页恢复自动任务'}
    if progress.snapshot().get('running'):
        return {'code': 400, 'data': '批量发送正在进行，请等它结束再单独执行'}
    # 单发也把阶段写进进度状态，前端就能显示「正在打开会话 / 正在输入并发送」
    publish = not progress.snapshot().get('running')
    if publish:
        progress.begin('single', 1, f'发送给 {task["name"]}')
    try:
        task['last_run_at'] = now_str()
        stage_cb = (lambda stage: progress.update(current=task['name'], stage=stage)) if publish else None
        ok, err, desc = deliver_task(task, on_stage=stage_cb)
        if ok:
            task['last_result'] = '发送成功（手动）'
            record_send_success(task['name'], desc, 'manual')
            if publish:
                progress.update(done=1, ok=1)
                progress.finish(f'已发送给 {task["name"]}')
            return {'code': 200, 'data': f'已发送给 {task["name"]}：{desc}'}
        task['last_result'] = err or '发送失败'
        record_send_failure(f'手动执行 {task["name"]} 失败：{task["last_result"]}', task['name'])
        if publish:
            progress.update(done=1, fail=1)
            progress.finish(f'{task["name"]} 发送失败：{task["last_result"]}')
        return {'code': 400, 'data': task['last_result']}
    except Exception as e:
        task['last_result'] = str(e) or '发送异常'
        record_send_failure(f'手动执行 {task["name"]} 异常：{task["last_result"]}', task['name'])
        if publish:
            progress.finish(f'{task["name"]} 发送异常：{task["last_result"]}')
        return {'code': 500, 'data': task['last_result']}


@app.post('/Time/runBatch')
def run_batch_once(payload: dict = Body(...), authorization: str = Header(None)):
    """批量立即执行：按顺序逐个发送，每个之间留随机间隔（模拟真人，降低风控）

    这里只负责「开始」：真正的发送放到后台线程，接口立刻返回 job_id，
    前端轮询 /Time/progress 显示进度（20 个好友可能要跑好几分钟，
    同步返回的话前端只能干转圈，看不到进行到哪）。
    """
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    task_ids = payload.get('task_ids') or []
    if not isinstance(task_ids, list) or not task_ids:
        return {'code': 400, 'data': '请先选择要执行的任务'}
    drv_err = check_driver()
    if drv_err:
        return drv_err
    if risk_state.get('paused'):
        return {'code': 400, 'data': '风控已暂停，请先在设置页恢复自动任务'}
    if progress.snapshot().get('running'):
        return {'code': 400, 'data': '已有发送任务正在进行，请等它结束或先点「停止」'}

    job = progress.begin('batch', len(task_ids), f'共 {len(task_ids)} 个任务')
    threading.Thread(target=_batch_send_worker, args=(list(task_ids),), daemon=True).start()
    return {'code': 200, 'data': f'已开始发送，共 {len(task_ids)} 个任务', 'job_id': job['job_id']}


def _batch_send_worker(task_ids):
    """批量发送后台线程：逐个发送并实时更新进度"""
    total = len(task_ids)
    ok = fail = 0
    processed = 0
    try:
        drv_err = check_driver()
        if drv_err:
            progress.finish(drv_err.get('data', '浏览器未就绪'))
            return

        for idx, task_id in enumerate(task_ids):
            if progress.is_cancelled():
                break
            task = scheduled_tasks.get(task_id)
            if not task:
                fail += 1
                progress.add_detail(str(task_id), False, '任务不存在')
                progress.update(done=idx + 1, ok=ok, fail=fail, stage='任务不存在，已跳过')
                continue

            name = task['name']
            progress.update(current=name, stage='准备发送')
            try:
                task['last_run_at'] = now_str()
                sent_ok, sent_err, desc = deliver_task(
                    task, on_stage=lambda stage, _n=name: progress.update(current=_n, stage=stage))
                if sent_ok:
                    task['last_result'] = '发送成功（手动）'
                    record_send_success(name, desc, 'manual')
                    ok += 1
                    progress.add_detail(name, True, desc)
                else:
                    task['last_result'] = sent_err or '发送失败'
                    record_send_failure(f'批量执行 {name} 失败：{task["last_result"]}', name)
                    fail += 1
                    progress.add_detail(name, False, task['last_result'])
            except Exception as e:
                task['last_result'] = str(e) or '发送异常'
                record_send_failure(f'批量执行 {name} 异常：{task["last_result"]}', name)
                fail += 1
                progress.add_detail(name, False, task['last_result'])

            processed += 1
            progress.update(done=idx + 1, ok=ok, fail=fail, stage='已完成')
            # 间隔发送，避免一次性连续操作触发风控
            if idx < total - 1 and not progress.is_cancelled():
                progress.update(stage='间隔中，准备下一位', current='')
                time.sleep(random.uniform(1.5, 3.0))
    finally:
        cancelled = progress.is_cancelled() and processed < total
        summary = f'批量执行完成：成功 {ok} 个，失败 {fail} 个'
        if cancelled:
            summary += f'（已手动停止，还剩 {total - processed} 个未发送）'
        progress.finish(summary)
        try:
            save_tasks()
        except Exception:
            pass


@app.get('/Time/progress')
def get_send_progress(authorization: str = Header(None)):
    """查询发送进度（前端轮询）"""
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    data = progress.snapshot()
    percent = 0
    if data['total']:
        percent = int(min(100, round(data['done'] * 100.0 / data['total'])))
    data['percent'] = percent
    return {'code': 200, 'data': data}


@app.post('/Time/batchCancel')
def cancel_send_batch(authorization: str = Header(None)):
    """停止正在进行的批量发送（当前好友发完就停，不会中断正在输入的消息）"""
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    if progress.request_cancel():
        return {'code': 200, 'data': '已请求停止，当前好友发送完成后结束'}
    return {'code': 400, 'data': '当前没有正在进行的发送任务'}


@app.get('/Time/edit')
def edit_time(name: str, new_time: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    drv_err = check_driver()
    if drv_err:
        return drv_err
    """修改指定好友的定时任务时间"""
    import schedule
    # 查找该好友的现有任务
    old_task_id = None
    for task_id, task in scheduled_tasks.items():
        if task_id.endswith(f"_{name}"):
            old_task_id = task_id
            break

    if not old_task_id:
        return {'code': 404, 'data': f'好友 {name} 没有定时任务'}

    # 取消旧任务
    old_task = scheduled_tasks[old_task_id]
    try:
        schedule.cancel_job(old_task['job'])
    except Exception:
        pass

    # 解析旧任务信息
    parts = old_task_id.split('_', 1)
    old_time = parts[0] if len(parts) == 2 else ""

    # 创建新任务（保留原消息，不覆盖）
    new_play_time = format_time(new_time)
    msg = old_task.get('msg', '')
    sec_uid = old_task.get('sec_uid', '') or runtime.douyin.get_friend_id(name)

    # 先生成新任务再删旧任务时，如果新旧 ID 相同（时间没变），
    # 旧任务会把刚建好的新任务一起删掉 → 任务凭空消失。
    # 所以先移除旧记录，再建新任务。
    new_task_id = f"{new_play_time}_{name}"
    scheduled_tasks.pop(old_task_id, None)
    schedule_task(new_task_id, new_play_time, name, msg, old_task.get('group_id', ''), sec_uid)
    save_tasks()  # 持久化

    return {
        'code': 200,
        'data': f'已将 {name} 的定时任务从 {old_time} 修改为 {new_play_time}',
        'old_time': old_time,
        'new_time': new_play_time,
        'task_id': new_task_id
    }


@app.get('/Time/getlist')
def get_time_list(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    """获取当前所有定时任务列表"""
    tasks = []
    for task_id, task in scheduled_tasks.items():
        # 解析任务ID获取信息
        parts = task_id.split('_', 1)
        if len(parts) == 2:
            time_str, name = parts
            group_id = task.get('group_id') or ''
            group_obj = group_store.find_group(group_id) if group_id else None
            tasks.append({
                'task_id': task_id,
                'time': time_str,
                'name': name,
                'group_id': group_id,
                'group_name': group_obj['name'] if group_obj else '',
                'msg': task.get('msg', ''),
                'send_type': task.get('send_type', 'text'),
                'sticker_src': task.get('sticker_src', ''),
                'next_run': str(task['job'].next_run) if task['job'].next_run else None,
                'last_result': task.get('last_result', ''),
                'last_generated_text': task.get('last_generated_text', ''),
                'last_run_at': task.get('last_run_at', ''),
                'last_jitter_seconds': task.get('last_jitter_seconds', 0),
                'jitter_minutes': config.get('task_jitter_minutes', 0)
            })
    return {'code': 200, 'data': {'count': len(tasks), 'tasks': tasks}}


# ===== 好友分组 =====

@app.get('/Group/getlist')
def group_get_list(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    return {
        'code': 200,
        'data': {
            'groups': group_store.list_groups_with_status(),
            'member_map': group_store.member_group_map(),
            'default_time': config.get('default_task_time', '22:00'),
            'default_message': config.get('default_message', ''),
        }
    }


@app.get('/Group/add')
def group_add(name: str, time: str = None, text: str = None, send_type: str = 'text',
              sticker_src: str = Query(None), authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    group, err = group_store.add_group(name, time, text, send_type, sticker_src or '')
    if err:
        return {'code': 400, 'data': err}
    return {'code': 200, 'data': group}


@app.get('/Group/edit')
def group_edit(group_id: str, name: str = None, time: str = None, text: str = None,
               send_type: str = Query(None), sticker_src: str = Query(None),
               authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    group, err = group_store.edit_group(group_id, name, time, text, send_type, sticker_src)
    if err:
        return {'code': 400, 'data': err}
    return {'code': 200, 'data': group}


@app.get('/Group/del')
def group_del(group_id: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    ok, err = group_store.delete_group(group_id)
    if not ok:
        return {'code': 404, 'data': err}
    return {'code': 200, 'data': '分组已删除（定时任务保留）'}


@app.post('/Group/setmembers')
def group_set_members(payload: dict = Body(...), authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    group, err = group_store.set_members(payload.get('group_id', ''), payload.get('names') or [])
    if err:
        return {'code': 400, 'data': err}
    return {'code': 200, 'data': group}


@app.post('/Group/addmembers')
def group_add_members(payload: dict = Body(...), authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    group, err = group_store.add_members(payload.get('group_id', ''), payload.get('names') or [])
    if err:
        return {'code': 400, 'data': err}
    return {'code': 200, 'data': group}


@app.get('/Group/removeMember')
def group_remove_member(group_id: str, name: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    group, err = group_store.remove_member(group_id, name)
    if err:
        return {'code': 400, 'data': err}
    return {'code': 200, 'data': group}


@app.get('/Group/apply')
def group_apply(group_id: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    code, message, detail = group_store.apply_group(group_id)
    return {'code': code, 'data': message, 'detail': detail}


# ===== 后台登录 =====

@app.get('/Api/Login/Admin')
def admin_login(username: str, password: str, request: Request = None):
    from spark_backend import auth
    if username == 'admin' and verify_pwd(password):
        client_host = '127.0.0.1'
        if request and request.client:
            client_host = '127.0.0.1' if request.client.host in ('::1', '127.0.0.1') else request.client.host
        auth._last_login_ip = client_host
        token = generate_token()
        return {'code': 200, 'data': token}
    else:
        return {'code': 400, 'data': '登录失败'}


@app.get('/Api/GetLastLoginIP')
def get_last_login_ip(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    from spark_backend import auth
    return {'code': 200, 'data': auth._last_login_ip}


# 退出登录
@app.get('/Api/logout')
def logout(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    token = authorization[7:]
    remove_token(token)
    return {'code': 200, 'data': '已退出登录'}


# 密码修改
@app.get('/Api/ChangePassword')
def change_password(old_password: str, new_password: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    from spark_backend import auth
    if not verify_pwd(old_password):
        return {'code': 400, 'data': '原密码错误'}
    auth._password_hash = hash_pwd(new_password)
    config['password_hash'] = auth._password_hash
    config.pop('password', None)  # 确保明文已清除
    save_config(config)
    return {'code': 200, 'data': '密码修改成功'}


# ===== 任务默认值 =====

@app.get('/Api/GetDefaults')
def get_task_defaults(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    return {'code': 200, 'data': {
        'time': config.get('default_task_time', '22:00'),
        'message': config.get('default_message', ''),
    }}


@app.get('/Api/SetDefaults')
def set_task_defaults(time: str = None, message: str = None, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    if time is not None:
        config['default_task_time'] = normalize_clock(time, '22:00')
    if message is not None:
        config['default_message'] = str(message).strip()[:500]
    save_config(config)
    return {'code': 200, 'data': '默认值已保存，新建任务和新建分组都会自动带上'}


# ===== 端口配置 =====

@app.get('/Api/GetPort')
def get_port(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    # 统一入口下端口由主程序固定，配置里的 port 不再生效
    return {'code': 200, 'data': int(os.environ.get('APP_PORT') or config.get('port', 8080))}


@app.get('/Api/SetPort')
def set_port(port: int, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    if os.environ.get('APP_PORT'):
        return {'code': 400, 'data': '端口由统一程序管理，此处修改不会生效'}
    try:
        if not (1 <= port <= 65535):
            return {'code': 400, 'data': '端口范围 1-65535'}
        config['port'] = port
        save_config(config)
        return {'code': 200, 'data': f'端口已保存为 {port}，重启后端后生效'}
    except Exception as e:
        return {'code': 500, 'data': '保存失败'}


@app.get('/Api/GetBrowserMode')
def get_browser_mode(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    return {'code': 200, 'data': config.get('show_browser', True)}


@app.get('/Api/SetBrowserMode')
def set_browser_mode(show: bool, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    try:
        config['show_browser'] = show
        save_config(config)
        return {'code': 200, 'data': f'浏览器显示模式已保存为 {"显示" if show else "隐藏"}，重启后端后生效'}
    except Exception as e:
        return {'code': 500, 'data': '保存失败'}


@app.get('/Api/GetBrowserConfig')
def get_browser_config(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    browser_name = normalize_browser_name(config.get('browser_name'))
    return {
        'code': 200,
        'data': {
            'browser_name': browser_name,
            'browser_label': get_browser_label(browser_name),
            'browser_path': (config.get('browser_path') or '').strip(),
            'profile_dir': account_store.account_profile_dir(),
            'platform': platform.system().lower()
        }
    }


@app.get('/Api/SetBrowserConfig')
def set_browser_config(browser_name: str, browser_path: str = Query(''), authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    browser_name = normalize_browser_name(browser_name)
    browser_path = (browser_path or '').strip()
    try:
        config['browser_name'] = browser_name
        config['browser_path'] = browser_path
        save_config(config)
        path_hint = f'，自定义路径：{browser_path}' if browser_path else '，使用系统默认安装路径'
        return {'code': 200, 'data': f'浏览器已切换为 {get_browser_label(browser_name)}{path_hint}，重启后端后生效'}
    except Exception:
        return {'code': 500, 'data': '保存失败'}


@app.get('/Api/GetRiskConfig')
def get_risk_config(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    return {
        'code': 200,
        'data': {
            'task_jitter_minutes': config.get('task_jitter_minutes', 8),
            'max_consecutive_failures': config.get('max_consecutive_failures', 3),
            'dedupe_window_days': config.get('dedupe_window_days', 7),
            'message_templates': list(config.get('message_templates') or DEFAULT_MESSAGE_TEMPLATES)
        }
    }


@app.post('/Api/SetRiskConfig')
def set_risk_config(payload: dict = Body(default=None), authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    payload = payload or {}
    try:
        config['task_jitter_minutes'] = clamp_int(payload.get('task_jitter_minutes', config.get('task_jitter_minutes', 8)), 8, 0, 60)
        config['max_consecutive_failures'] = clamp_int(payload.get('max_consecutive_failures', config.get('max_consecutive_failures', 3)), 3, 1, 10)
        config['dedupe_window_days'] = clamp_int(payload.get('dedupe_window_days', config.get('dedupe_window_days', 7)), 7, 1, 30)
        config['message_templates'] = normalize_message_templates(payload.get('message_templates', config.get('message_templates')))
        save_config(config)
        save_risk_state()
        return {'code': 200, 'data': '防封配置已保存'}
    except Exception:
        return {'code': 500, 'data': '保存失败'}


@app.get('/Api/GetRiskStatus')
def get_risk_status(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    prune_risk_state()
    recent = list(risk_state.get('recent_messages', []))[-10:]
    recent.reverse()
    return {
        'code': 200,
        'data': {
            'paused': bool(risk_state.get('paused')),
            'pause_reason': risk_state.get('pause_reason', ''),
            'paused_at': risk_state.get('paused_at', ''),
            'consecutive_failures': int(risk_state.get('consecutive_failures', 0)),
            'max_consecutive_failures': config.get('max_consecutive_failures', 3),
            'today_send_count': int(risk_state.get('today_send_count', 0)),
            'last_error': risk_state.get('last_error', ''),
            'last_failure_at': risk_state.get('last_failure_at', ''),
            'last_success_at': risk_state.get('last_success_at', ''),
            'last_sent_at': risk_state.get('last_sent_at', ''),
            'task_jitter_minutes': config.get('task_jitter_minutes', 8),
            'dedupe_window_days': config.get('dedupe_window_days', 7),
            'recent_messages': recent
        }
    }


@app.post('/Api/ResumeRisk')
def resume_risk_api(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    resume_risk()
    return {'code': 200, 'data': '风控状态已恢复，自动任务可继续运行'}


@app.get('/Api/SparkCalendar')
def spark_calendar(month: str = Query(''), authorization: str = Header(None)):
    """火花日历：某个月的每日续火花打卡明细。"""
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    from spark_backend import history as history_store

    month = (month or datetime.now().strftime('%Y-%m')).strip()
    if not re.match(r'^\d{4}-\d{2}$', month):
        return {'code': 400, 'data': '月份格式应为 YYYY-MM'}
    account_id = account_store.active_account_id()
    days = history_store.calendar(month, account_id)
    all_months = history_store.months(account_id)
    if month not in all_months:
        all_months.append(month)
        all_months.sort(reverse=True)
    planned = [
        {'name': t.get('name', ''), 'time': t.get('time', ''),
         'send_type': t.get('send_type', 'text')}
        for t in scheduled_tasks.values()
        if str(t.get('account_id') or 'default') == str(account_id)
    ]
    planned.sort(key=lambda x: (x['time'], x['name']))
    return {
        'code': 200,
        'data': {
            'month': month,
            'months': all_months,
            'today': datetime.now().strftime('%Y-%m-%d'),
            'days': days,
            'planned': planned,
            'account_id': account_id,
        },
    }


# ===== 前端静态文件托管 =====
if os.path.exists(DIST_DIR):
    # 挂载静态资源目录（如果存在）
    assets_dir = os.path.join(DIST_DIR, 'assets')
    if os.path.isdir(assets_dir):
        app.mount('/assets', StaticFiles(directory=assets_dir), name='assets')

    # 前端是「无构建」的静态文件，改完代码后浏览器很容易继续吃旧缓存，
    # 表现为界面没有新按钮、操作看似无效。统一禁用缓存，刷新即生效。
    _NO_CACHE = {
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'Pragma': 'no-cache',
        'Expires': '0',
    }

    # SPA 路由回退：所有非 API 请求都返回 index.html 或对应静态文件
    @app.get('/{path:path}')
    async def serve_spa(path: str):
        root = os.path.realpath(DIST_DIR)
        file_path = os.path.realpath(os.path.join(root, path))
        # 防止路径穿越：用 commonpath 判断，避免 /dist2 这类同前缀目录绕过
        try:
            inside = os.path.commonpath([root, file_path]) == root
        except ValueError:
            inside = False
        if not inside:
            return FileResponse(os.path.join(DIST_DIR, 'index.html'), media_type='text/html', headers=_NO_CACHE)
        if os.path.isfile(file_path):
            # 根据扩展名设置正确的 Content-Type
            import mimetypes
            mimetypes.add_type('application/javascript', '.js')
            mimetypes.add_type('text/css', '.css')
            content_type, _ = mimetypes.guess_type(file_path)
            return FileResponse(file_path, media_type=content_type, headers=_NO_CACHE)
        return FileResponse(os.path.join(DIST_DIR, 'index.html'), media_type='text/html', headers=_NO_CACHE)


atexit.register(cleanup_on_exit)


if __name__ == "__main__":
    saved_port = config.get('port', 8080)

    if is_port_in_use(saved_port):
        actual = find_available_port(saved_port)
        print(f'⚠️ 端口 {saved_port} 已被占用，自动切换到 {actual}')
        config['port'] = actual
        save_config(config)
    else:
        actual = saved_port
    # 启动即恢复已保存的定时任务，避免重启后任务列表显示为空。
    # 浏览器初始化时 Init 会再调用一次 restore_tasks，已存在的任务会自动跳过。
    start_scheduler()
    restore_tasks()

    print(f'✅ 项目已运行在 {actual} 端口')
    print(f'🚀 服务已启动：http://localhost:{actual}')

    # 启动后自动打开管理后台；无桌面环境时静默跳过
    def _open_console():
        try:
            webbrowser.open(f'http://localhost:{actual}')
            print(f'🌐 已自动打开管理后台')
        except Exception:
            print('⚠️ 无法自动打开浏览器，请手动访问上面的地址')

    threading.Timer(1.5, _open_console).start()

    uvicorn.run(
        app,
        host="localhost",
        port=actual,
        reload=False
    )
