# 微博聊天记录导出

把微博网页版私信（`api.weibo.com/chat`）的聊天记录导出成本地文件，**离线可看**，图片、语音、视频一并保存，不怕过期。

导出结果是单个 HTML 页面，双击就能打开，样式与微博一致，支持全文搜索和按会话导出 txt。

## 特性

- **扫码登录**：弹出浏览器窗口用微博 App 扫码，登录态保存在本地，之后不用再扫
- **增量导出**：只拉各会话的新消息，历史记录自动合并，已下载的媒体不重复下
- **全量导出**：重新拉全部消息
- **媒体本地化**：图片、语音、视频、头像全部落到本地目录，不再依赖微博服务器
- **离线浏览页**：左侧会话列表 + 右侧气泡对话，顶部可全文搜索，可按会话导出 txt
- **原始数据**：同时生成 `raw.json`，字段齐全，方便二次处理
- 处理过长私信截断（自动调 `show_batch` 补全文）、iPhone HEIC 图片（自动转 JPEG）、已撤回消息标注

## 环境要求

| 项目 | 要求 |
| --- | --- |
| Python | 3.10 及以上，安装根目录 `requirements.txt` |
| 浏览器 | 本机 Chrome / Edge，或 `python -m playwright install chromium` |
| 可选 | ffmpeg（语音转 mp3 时需要，没有则只保留原始 `.amr`） |

## 快速开始

```bash
python -m pip install -r requirements.txt
python main.py
```

在统一应用中选择「微博聊天」：

1. 点「扫码登录」，用微博 App 扫码
2. 选增量或全量导出，点「开始导出」
3. 完成后点「打开结果文件夹」，双击 `微博聊天记录.html` 查看

## 命令行用法（可选）

```bat
python modules/weibo_chat/tool/export.py                    :: 增量导出
python modules/weibo_chat/tool/export.py --mode=full        :: 全量导出
python modules/weibo_chat/tool/export.py --max-group=20000
python modules/weibo_chat/tool/export.py --only-uid=123456  :: 只更新指定会话
python modules/weibo_chat/tool/export.py --login            :: 只登录
python modules/weibo_chat/tool/export.py --status           :: JSON 登录状态
python modules/weibo_chat/tool/export.py --logout           :: 清除登录态
```

## 输出结构

```
data/weibo_chat/accounts/<账号>/export/
├─ 微博聊天记录.html    离线浏览页
├─ raw.json             完整原始数据
├─ images/              聊天图片
├─ audio/               语音（.mp3 可播 / .amr 原始）
├─ videos/              视频
└─ avatars/             头像
```

## 隐私与安全

> **务必注意**

- `data/weibo_chat/accounts/<账号>/state.json` 保存登录态（Cookie），**等同于账号凭证，不要外发或提交到仓库**
- `data/` 是私人数据目录，**已在 `.gitignore` 中排除**。独立 CLI 默认使用 `modules/weibo_chat/data/`，也可通过 `WEIBO_CHAT_HOME/OUT/STATE/PROFILE` 环境变量指定；统一应用会自动设置账号目录
- 本工具只在你自己的电脑上运行，数据不上传任何第三方服务器
- 换电脑迁移登录态时，用界面上的「导出登录态 / 导入登录态」，传完后记得删掉那个文件

## 常见问题

- **缺少 Playwright**：在运行程序所用的 Python 环境执行 `python -m pip install -r requirements.txt`
- **找不到浏览器**：安装 Chrome / Edge，或执行 `python -m playwright install chromium`。非标准路径可设置 `CHAT_BROWSER_PATH`
- **接口报错 / 拉不到消息**：微博接口可能改版；实现位于 `modules/chat_export/weibo.py`。拉取失败会停止本次导出，保留原 `raw.json`
- **群聊消息不全**：粉丝群动辄上万条，默认只取最近 3000 条，可在界面调大上限（`--max-group`），耗时会相应变长
- **想强制全量重导**：使用 `--mode=full`，无需删除旧数据

导出器完全由 Python 驱动，不需要 Node.js/npm。HTML 只用于离线浏览产物。

## 免责声明

仅供个人备份自己的聊天记录使用。请遵守微博用户协议与相关法律法规，不要用于抓取他人数据或任何商业用途。使用本工具产生的后果由使用者自行承担。
