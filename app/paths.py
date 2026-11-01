import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHELL_DIR = ROOT / "app" / "shell"

DATA_ROOT = Path(os.environ.get("APP_DATA_DIR") or (ROOT / "data")).resolve()
SIGNIN_HOME = DATA_ROOT / "signin"
SIGNIN_HOME.mkdir(parents=True, exist_ok=True)

DOUYIN_CHAT_HOME = DATA_ROOT / "douyin_chat"
DOUYIN_CHAT_HOME.mkdir(parents=True, exist_ok=True)
# dy_common.paths 在导入时读取此变量，必须早于 dy_backend 模块导入。
os.environ.setdefault("DOUYIN_CHAT_DATA_DIR", str(DOUYIN_CHAT_HOME))

SPARK_HOME = DATA_ROOT / "spark"
SPARK_HOME.mkdir(parents=True, exist_ok=True)
# spark_backend.config 在导入时读取此变量（数据、日志、浏览器 profile 均落在其下）。
os.environ.setdefault("SPARK_HOME", str(SPARK_HOME))

WEIBO_CHAT_HOME = DATA_ROOT / "weibo_chat"
WEIBO_CHAT_HOME.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("WEIBO_CHAT_HOME", str(WEIBO_CHAT_HOME))
os.environ.setdefault("WEIBO_CHAT_OUT", str(WEIBO_CHAT_HOME / "export"))
os.environ.setdefault("WEIBO_CHAT_STATE", str(WEIBO_CHAT_HOME / "state.json"))
os.environ.setdefault("WEIBO_CHAT_PROFILE", str(WEIBO_CHAT_HOME / "profile"))

TAOBAO_CHAT_HOME = DATA_ROOT / "taobao_chat"
TAOBAO_CHAT_HOME.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TAOBAO_CHAT_HOME", str(TAOBAO_CHAT_HOME))
os.environ.setdefault("TAOBAO_CHAT_OUT", str(TAOBAO_CHAT_HOME / "export"))
os.environ.setdefault("TAOBAO_CHAT_PROFILE", str(TAOBAO_CHAT_HOME / "profile"))

BILI_CHAT_HOME = DATA_ROOT / "bilibili_chat"
BILI_CHAT_HOME.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("BILI_CHAT_HOME", str(BILI_CHAT_HOME))
os.environ.setdefault("BILI_CHAT_OUT", str(BILI_CHAT_HOME / "export"))
os.environ.setdefault("BILI_CHAT_STATE", str(BILI_CHAT_HOME / "state.json"))
os.environ.setdefault("BILI_CHAT_PROFILE", str(BILI_CHAT_HOME / "profile"))

STARSKY_HOME = DATA_ROOT / "starsky"
STARSKY_HOME.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("STARSKY_HOME", str(STARSKY_HOME))

HOST = os.environ.get("APP_HOST") or "127.0.0.1"
PORT = int(os.environ.get("APP_PORT") or 18000)
os.environ.setdefault("APP_PORT", str(PORT))
