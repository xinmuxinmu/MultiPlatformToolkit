# chat-export-toolkit · 多平台聊天记录导出及签到续火花

本地运行的多平台工具集：把各平台私信/群聊导出成可离线浏览的本地页面，并提供一键签到与火花续期。所有数据只落在本机，不上传任何服务器。

## 功能模块

| 模块 | 路由 | 说明 |
| --- | --- | --- |
| 抖音聊天 / 火花 | `/douyin`、`/spark` | 抖音私信导出与火花续期，账号隔离 |
| 微博聊天记录 | `/weibo-chat` | 私信/群聊导出，图片语音视频本地化 |
| B站聊天记录 | `/bili-chat` | 私信 + 应援团群聊导出，分享卡片/图片离线可读 |
| 淘宝旺旺记录 | `/taobao` | 买家侧聊天导出 |
| 一键签到 | `/signin` | 微博 / 贴吧 / 吾爱 / StarSky |
| 网易云乐迷团 | `/signin#netease` | 乐迷团每日任务、亲密值进度，支持快捷登录 |

统一入口 `http://127.0.0.1:18000/`，左侧栏切换模块。

## 快速开始

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -r requirements.txt
python main.py
```

Windows 也可直接双击 `启动.bat`（首次会自动建虚拟环境装依赖）。

### 各平台导出工具依赖

微博 / B站 / 淘宝模块用 Playwright 复用已登录的浏览器会话：

```bash
cd modules/weibo_chat/tool && npm install    # weibo_chat 与 bilibili_chat 共用
cd modules/taobao_chat/tool && npm install
```

需要本机安装 Chrome 或 Edge。首次使用在模块页点「登录 / 添加账号」扫码，登录态按账号加密保存在本机，之后导出走无头模式。

## 数据存放

所有导出数据、登录态、账号索引都在 `data/` 下，已通过 `.gitignore` 排除，不会进仓库：

```
data/
  bilibili_chat/accounts/<账号>/   # state.json 登录态 + export/ 导出结果
  weibo_chat/accounts/<账号>/
  taobao_chat/
  douyin_chat/
```

导出产物是自包含的离线页面（`raw.json` + `*.html` + `images/` + `avatars/`），可直接双击打开或分享给本人设备。

## 目录结构

```
app/            统一壳：FastAPI 入口、页面外壳、共享 UI 资源
modules/
  bilibili_chat/  B站：routes.py + static/ + tool/(export.js, template.html)
  weibo_chat/     微博：同上结构
  taobao_chat/    淘宝：同上结构
  douyin_chat/    抖音：后端 + Vue 前端 + 提取器
  signin/         一键签到
  spark/          火花
  starsky/
main.py         启动入口（uvicorn，默认 18000 端口）
```

## 说明

- 仅供导出**本人账号**的聊天记录用于备份，请遵守各平台用户协议，不要用于抓取他人数据或商业分发。
- 平台接口与页面结构可能变化，若某模块失效，通常是接口路径或字段变更，见各模块 `README.md`。
