"""配置与常量模块"""
import os
import json
import socket
import platform
import sys


# 是否运行在 PyInstaller 打包后的可执行文件中
IS_FROZEN = getattr(sys, 'frozen', False)

if IS_FROZEN:
    # 打包后：代码与前端资源被解压到 _MEIPASS（临时目录，只读）
    RESOURCE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable)))
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    # 开发模式：项目根目录
    RESOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    BASE_DIR = RESOURCE_DIR

DIST_DIR = os.path.join(RESOURCE_DIR, 'dist')

# 可写目录：
# 安装到 Program Files 后没有写权限，数据必须放到用户目录
if IS_FROZEN:
    APP_HOME = os.path.join(
        os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'),
        'DouyinSpark'
    )
else:
    APP_HOME = BASE_DIR

# 统一入口通过 SPARK_HOME 把数据、日志与浏览器 profile 指向 data/spark
APP_HOME = os.environ.get('SPARK_HOME') or APP_HOME

DATA_DIR = os.path.join(APP_HOME, 'data')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')

# 确保运行时数据目录存在（config / tasks / risk_state 均写入此处）
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError as e:
    print(f'⚠️ 数据目录创建失败: {e}')

BROWSER_LABELS = {
    'edge': 'Microsoft Edge',
    'chrome': 'Google Chrome',
    'chromium': 'Chromium',
    'brave': 'Brave'
}

BROWSER_ALIASES = {
    'msedge': 'edge',
    'microsoft-edge': 'edge',
    'google-chrome': 'chrome',
    'google chrome': 'chrome',
    'chromium-browser': 'chromium',
    'brave-browser': 'brave',
    'brave browser': 'brave'
}

DEFAULT_MESSAGE_TEMPLATES = [
    '今天也要开心呀',
    '来给你续一下小火花',
    '忙完记得回我一下呀',
    '保持联系，火花不能断',
    '给你留个言，祝你今天顺顺利利'
]


def get_default_browser_name():
    """Windows 保持 Edge 兼容性，Linux/macOS 默认改为 Chrome。"""
    return 'edge' if os.name == 'nt' else 'chrome'


def normalize_browser_name(name):
    browser_name = (name or '').strip().lower()
    browser_name = BROWSER_ALIASES.get(browser_name, browser_name)
    return browser_name if browser_name in BROWSER_LABELS else get_default_browser_name()


def get_browser_label(browser_name):
    return BROWSER_LABELS.get(normalize_browser_name(browser_name), BROWSER_LABELS[get_default_browser_name()])


def normalize_message_templates(value):
    templates = []
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = str(value or '').replace('||', '\n').splitlines()
    for item in raw_items:
        text = str(item or '').strip()
        if text and text not in templates:
            templates.append(text[:200])
    return templates[:30] if templates else list(DEFAULT_MESSAGE_TEMPLATES)


def clamp_int(value, default, minimum, maximum):
    try:
        out = int(value)
    except Exception:
        out = default
    return max(minimum, min(maximum, out))


def normalize_clock(value, fallback='22:00'):
    """把时间字符串归一化为 HH:MM，非法则返回 fallback"""
    raw = str(value or '').replace('：', ':').strip()
    if not raw:
        return fallback
    try:
        parts = raw.split(':')
        if len(parts) != 2:
            return fallback
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return fallback
        return f'{hour:02d}:{minute:02d}'
    except Exception:
        return fallback


def load_config():
    """读取配置文件，不存在则返回默认配置"""
    default = {
        'port': 8080,
        'show_browser': True,
        'browser_name': get_default_browser_name(),
        'browser_path': '',
        'active_account_id': 'default',
        'task_jitter_minutes': 8,
        'max_consecutive_failures': 3,
        'dedupe_window_days': 7,
        'default_task_time': '22:00',
        'default_message': '',
        'message_templates': list(DEFAULT_MESSAGE_TEMPLATES)
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                default.update(cfg)
        except Exception:
            pass
    default['browser_name'] = normalize_browser_name(default.get('browser_name'))
    default['browser_path'] = (default.get('browser_path') or '').strip()
    default['active_account_id'] = str(default.get('active_account_id') or 'default').strip() or 'default'
    default['task_jitter_minutes'] = clamp_int(default.get('task_jitter_minutes', 8), 8, 0, 60)
    default['max_consecutive_failures'] = clamp_int(default.get('max_consecutive_failures', 3), 3, 1, 10)
    default['dedupe_window_days'] = clamp_int(default.get('dedupe_window_days', 7), 7, 1, 30)
    default['default_task_time'] = normalize_clock(default.get('default_task_time'), '22:00')
    default['default_message'] = str(default.get('default_message') or '').strip()[:500]
    default['message_templates'] = normalize_message_templates(default.get('message_templates'))
    return default


def save_config(cfg):
    """保存配置到文件"""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'⚠️ 配置保存失败: {e}')


def is_port_in_use(port):
    """检测端口是否被占用"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', port)) == 0


def find_available_port(preferred):
    """从首选端口开始找可用端口，最多尝试 20 个"""
    for offset in range(20):
        candidate = preferred + offset
        if not is_port_in_use(candidate):
            return candidate
    # 首选附近都满了，让系统分配随机端口
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('localhost', 0))
        return s.getsockname()[1]


def get_profile_dir(browser_name, account_id=None):
    """浏览器 profile 目录。

    默认账号沿用旧目录（edge_user_data 等），历史登录态无缝保留；
    多账号时每个账号独立目录，登录态互不干扰。
    """
    browser_name = normalize_browser_name(browser_name)
    if not account_id or account_id == 'default':
        if browser_name == 'edge':
            # 放在可写目录，打包安装到 Program Files 后也能持久化登录态
            return os.path.join(APP_HOME, 'edge_user_data')
        return os.path.join(APP_HOME, f'{browser_name}_user_data')
    return os.path.join(APP_HOME, f'{browser_name}_user_data_{account_id}')


# 全局配置对象（模块加载时读取一次，之后只修改属性不重新绑定）
config = load_config()
