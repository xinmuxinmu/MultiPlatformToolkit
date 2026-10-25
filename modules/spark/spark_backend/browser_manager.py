"""浏览器驱动管理模块"""
import os
import re
import sys
import subprocess
from selenium import webdriver
from selenium.common.exceptions import SessionNotCreatedException, WebDriverException
from selenium.webdriver.common.by import By

from spark_backend.config import config, normalize_browser_name
from spark_backend import runtime


DRIVER_EXE = {'edge': 'msedgedriver.exe', 'chrome': 'chromedriver.exe'}

# 各浏览器默认安装目录（用于读取主版本号，判断 PATH 里的驱动是否过旧）
BROWSER_DIRS = {
    'edge': [
        r'C:\Program Files (x86)\Microsoft\Edge\Application',
        r'C:\Program Files\Microsoft\Edge\Application',
    ],
    'chrome': [
        r'C:\Program Files\Google\Chrome\Application',
        r'C:\Program Files (x86)\Google\Chrome\Application',
        os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\Application'),
    ],
    'chromium': [
        r'C:\Program Files\Chromium\Application',
        os.path.expandvars(r'%LOCALAPPDATA%\Chromium\Application'),
    ],
    'brave': [
        r'C:\Program Files\BraveSoftware\Brave-Browser\Application',
        r'C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application',
    ],
}


def ensure_localhost_bypass_proxy():
    """把本机地址排除在系统代理之外。

    坑点：开着 HTTP_PROXY（公司代理 / VPN 客户端 / 抓包工具）时，webdriver 与
    浏览器之间的 localhost 通信也会被代理劫持，表现为驱动能启动、但之后任何
    命令都报 "unhandled request"，页面上就是「浏览器初始化失败」。
    """
    hosts = ['localhost', '127.0.0.1', '::1']
    for key in ('NO_PROXY', 'no_proxy'):
        current = [p.strip() for p in (os.environ.get(key) or '').split(',') if p.strip()]
        for h in hosts:
            if h not in current:
                current.append(h)
        os.environ[key] = ','.join(current)


def _installed_major(browser_name):
    """从安装目录读出浏览器主版本号"""
    for base in BROWSER_DIRS.get(browser_name, []):
        try:
            if not base or not os.path.isdir(base):
                continue
            versions = [d for d in os.listdir(base) if re.match(r'^\d+\.', d)]
            if versions:
                versions.sort(key=lambda v: [int(x) for x in re.findall(r'\d+', v)])
                return int(versions[-1].split('.')[0])
        except Exception:
            continue
    return None


def _edge_installed_major():
    """兼容旧调用"""
    return _installed_major('edge')


def _driver_major(exe_path):
    """读出驱动文件的主版本号"""
    try:
        out = subprocess.run([exe_path, '--version'], capture_output=True,
                             text=True, timeout=8).stdout or ''
        m = re.search(r'(\d+)\.\d+\.\d+', out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def drop_mismatched_drivers(browser_name):
    """PATH 里可能有旧版驱动，Selenium 会优先用它而不是自动匹配的版本。

    版本不一致时把该目录从 PATH 移除，交给 Selenium Manager 使用匹配版本。
    """
    exe_name = DRIVER_EXE.get(browser_name)
    if not exe_name:
        return
    browser_major = _installed_major(browser_name)
    if not browser_major:
        return
    kept = []
    for path in (os.environ.get('PATH') or '').split(os.pathsep):
        if not path:
            continue
        candidate = os.path.join(path, exe_name)
        if os.path.exists(candidate):
            major = _driver_major(candidate)
            if major and major != browser_major:
                print(f'⚠️ 已忽略版本不匹配的驱动: {candidate} (v{major})，浏览器为 v{browser_major}')
                continue
        kept.append(path)
    os.environ['PATH'] = os.pathsep.join(kept)


def _platform_token():
    """UA 里的平台段，按真实系统填，别拿 Linux 冒充 Windows"""
    if os.name == 'nt':
        return 'Windows NT 10.0; Win64; x64'
    if sys.platform == 'darwin':
        return 'Macintosh; Intel Mac OS X 10_15_7'
    return 'X11; Linux x86_64'


def _headless_user_agent(browser_name):
    """无头模式的 UA 里带 HeadlessChrome，抖音会直接判定为机器人。

    这里换成与「本机浏览器主版本号 + 真实系统」一致的 UA，
    版本号对不上同样可疑，所以不用写死的 136。
    """
    major = _installed_major(browser_name) or 136
    ua = (f'Mozilla/5.0 ({_platform_token()}) AppleWebKit/537.36 (KHTML, like Gecko) '
          f'Chrome/{major}.0.0.0 Safari/537.36')
    if browser_name == 'edge':
        ua += f' Edg/{major}.0.0.0'
    return ua


def build_browser_options(account_id=None, force_visible=False):
    browser_name = normalize_browser_name(config.get('browser_name'))
    browser_path = (config.get('browser_path') or '').strip()
    # force_visible：弹出浏览器登录时即使用户开了无头模式，也强制显示窗口
    show_browser = bool(config.get('show_browser', True)) or force_visible
    # 与聊天导出共用账号目录：<账号>/spark_profile（浏览器内核不同，profile 独立）
    from spark_backend import accounts as account_store
    profile_dir = account_store.spark_profile_dir(account_id or runtime.active_account_id)
    os.makedirs(profile_dir, exist_ok=True)

    if browser_name == 'edge':
        options = webdriver.EdgeOptions()
    else:
        options = webdriver.ChromeOptions()

    options.add_experimental_option('excludeSwitches', ['enable-logging', 'enable-automation'])
    options.add_argument('log-level=3')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument('--disable-notifications')
    options.add_argument('--disable-popup-blocking')
    options.add_argument('--ignore-certificate-errors')
    options.add_argument('--no-sandbox')
    options.add_argument(f'--user-data-dir={profile_dir}')

    if show_browser:
        options.add_argument('--start-maximized')
    else:
        # 无头模式要点：
        # 1) --start-maximized 在无头下不生效，必须显式给窗口尺寸，
        #    否则默认 800x600，聊天页会少渲染好几行会话；
        # 2) 无头 UA 含 HeadlessChrome，必须覆盖；
        # 3) /dev/shm 太小会崩，加上 --disable-dev-shm-usage 更稳。
        options.add_argument('--headless=new')
        options.add_argument('--window-size=1400,900')
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--mute-audio')
        options.add_argument(f'user-agent={_headless_user_agent(browser_name)}')

    if browser_path:
        options.binary_location = browser_path
    return options


def create_webdriver(account_id=None, force_visible=False):
    browser_name = normalize_browser_name(config.get('browser_name'))
    # 启动前先修掉两个常见的环境坑，否则会表现为「初始化失败」
    ensure_localhost_bypass_proxy()
    drop_mismatched_drivers(browser_name)
    options = build_browser_options(account_id, force_visible)
    if browser_name == 'edge':
        return webdriver.Edge(options=options)
    return webdriver.Chrome(options=options)


def check_driver():
    """检查浏览器会话是否有效，无效则重置状态并返回错误响应"""
    # 延迟导入，避免循环依赖
    from spark_backend.scheduler import scheduled_tasks
    import schedule

    if not runtime.init or runtime.driver is None:
        return {'code': 400, 'data': '浏览器未初始化，请先点击初始化'}
    try:
        # 用一个轻量操作探测会话是否存活
        runtime.driver.current_url
        return None
    except Exception:
        # 会话已失效，关闭旧 driver 并重置状态
        try:
            runtime.driver.quit()
        except Exception:
            pass
        # 清理旧定时任务（job 仍绑定旧的 douyin 引用）
        for task_id, task in list(scheduled_tasks.items()):
            try:
                schedule.cancel_job(task['job'])
            except Exception:
                pass
        scheduled_tasks.clear()
        runtime.init = False
        runtime.driver = None
        runtime.douyin = None
        runtime.Login_is_bool = False
        runtime.login_pending = None
        runtime._user_cache['nickname'] = ''
        runtime._user_cache['avatar'] = ''
        return {'code': 400, 'data': '浏览器会话已断开，请重新初始化'}


def cleanup_on_exit():
    """程序退出时清理浏览器进程"""
    if runtime.init and runtime.driver:
        try:
            runtime.driver.quit()
        except Exception:
            pass
