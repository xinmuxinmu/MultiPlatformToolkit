# B站聊天记录导出（Python）

使用 Python Playwright 复用本机登录态，导出本人私信和应援团群聊。界面在统一应用的「B站聊天」中；不再依赖 Node.js/npm。

从项目根目录安装依赖后可独立运行：

```bat
python -m pip install -r requirements.txt
python modules/bilibili_chat/tool/export.py --login
python modules/bilibili_chat/tool/export.py
python modules/bilibili_chat/tool/export.py --mode=full
python modules/bilibili_chat/tool/export.py --only-uid=123456
python modules/bilibili_chat/tool/export.py --max-dm=100000 --max-group=3000
python modules/bilibili_chat/tool/export.py --status
python modules/bilibili_chat/tool/export.py --logout
```

浏览器优先使用本机 Chrome/Edge，也支持 `python -m playwright install chromium` 安装的 Chromium。非标准浏览器路径通过 `CHAT_BROWSER_PATH` 设置。

统一应用继续使用 `data/bilibili_chat/accounts/<账号>/` 下的 `state.json`、`profile/` 和 `export/`，原账号无需迁移。独立 CLI 默认使用 `modules/bilibili_chat/data/`；`BILI_CHAT_HOME/OUT/STATE/PROFILE` 可覆盖这些路径，`BILI_HEADLESS=0` 显示导出浏览器。

导出包含 `raw.json`、`B站聊天记录.html`、`images/`、`avatars/`、`audio/`。HTML 可离线打开；语音服务器允许下载时会保存原文件。文本、撤回标记、分享卡片、通知、富文本和群成员名片保留。

增量导出以原 `fixed: true` 基线合并，保留暂未出现在会话列表中的历史。`--mode=full` 忽略旧消息基线重新抓取；`--only-uid` 无论全量或增量都保留其他会话。拉取接口失败时停止并保留原 `raw.json`，不把部分抓取结果覆盖到历史记录。媒体下载失败的 URL 会保留以便下次重试。

登录态和聊天记录只应保存于本机，不要外发或提交 Git。平台接口变化、历史记录保存期限和服务器权限仍可能限制可导出的内容。
