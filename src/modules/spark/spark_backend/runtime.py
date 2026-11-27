"""运行时全局状态模块

存放会被重新绑定的全局变量（driver/douyin/init 等）。
其他模块访问时必须用 `import runtime; runtime.xxx` 形式，
不能用 `from runtime import xxx`，否则重新绑定后引用失效。
"""
import threading


# 浏览器驱动与会话状态（会被重新绑定，必须用 runtime.xxx 访问）
driver = None
douyin = None
init = False
Login_is_bool = False

# 当前活跃账号（多账号）：数据隔离与浏览器 profile 选择都以此为准
active_account_id = 'default'

# 「弹出浏览器登录」等待态：{'account_id':..., 'started_at': ts}，None 表示无等待
login_pending = None

# 账号切换 / 浏览器登录的互斥锁（切号时要关旧浏览器、开新浏览器）
account_lock = threading.Lock()

# 用户信息缓存（字典本身不重新绑定，只改属性，可 from import）
_user_cache = {'nickname': '', 'avatar': ''}

# 初始化锁（不重新绑定，可 from import）
init_lock = threading.Lock()

# 服务启动时间（在主入口设置）
start_time = None
