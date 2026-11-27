"""B站聊天导出 CLI：python modules/bilibili_chat/tool/export.py --help。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.chat_export.bilibili import LOGIN_URL, export, identity
from modules.chat_export.common import run_cli

if __name__ == "__main__":
    raise SystemExit(run_cli("B站", "BILI", Path(__file__).resolve().parent, export, identity, LOGIN_URL))
