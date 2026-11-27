# 多平台工具箱 · MultiPlatformToolkit

GitHub：[xinmuxinmu/MultiPlatformToolkit](https://github.com/xinmuxinmu/MultiPlatformToolkit)

Python 原生桌面工具：聊天记录备份、每日签到、音乐乐迷任务、抖音火花与圈子任务入口。界面使用 PySide6 Widgets，本机服务和业务执行使用 Python，不需要 Node.js、npm、Vue 构建或 PowerShell 业务脚本。

## 启动

Windows 双击 **启动.bat**。首次启动会创建 `.venv` 并安装 Python 依赖，失败时会显示原因；环境已就绪时直接打开桌面界面。重复启动会唤起已有窗口。

本机项目主目录已迁移到 `D:\PythonApps\MultiPlatformToolkit`，请从新目录启动和继续开发。旧中文目录暂留迁移备份，其中的启动入口会转向新目录；账号和导出数据以新目录的 `data/` 为准。

需要 Python 3.10+，建议使用 Python 3.12，并安装 Chrome 或 Edge 供扫码登录与真实网页操作。没有本机浏览器时可用 `.venv\Scripts\python.exe -m playwright install chromium` 安装 Playwright Chromium。

```powershell
# 手动安装与启动
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py

# 显示启动与服务日志
启动.bat --verbose

# 只检查依赖和服务结构，不执行签到
.venv\Scripts\python.exe main.py --check

# 仅运行本机 Python 服务，本次不恢复自动任务
.venv\Scripts\python.exe main.py --headless --no-schedule
```

默认服务地址 `127.0.0.1:18000`。可用 `APP_PORT` 修改端口，`APP_DATA_DIR` 修改数据目录。主界面是原生窗口，不再用浏览器页面作为应用外壳。

## 使用方式

侧栏按「每日任务 / 聊天记录 / 自动维护」分组，可搜索工具。底部切换深浅色与打开全局设置；配色、控件和窗口布局参考本机 `yd` 界面规范。

签到类页面保留「概览 / 设置」流程：概览看账号、任务进度、勋章与日志；设置中登录、勾选任务、保存。底层字段放在高级设置中，日常使用不需要编辑 JSON。刷新不会覆盖未保存设置。

聊天类页面先添加或选择账号，再登录、导出；支持增量更新、会话/消息搜索、分页阅读、本机附件与目录打开。抖音另支持整库导入、导出、定时采集、图片/视频补齐和 Python 长图渲染。删除与覆盖导入需明确确认。

## 功能

| 模块 | 功能 |
| --- | --- |
| QQ 音乐 | 每日签到、乐迷勋章、歌词卡签到、服务端任务发现、歌手与任务类型勾选 |
| 网易云乐迷团 | 任务、亲密值进度与快捷登录 |
| 酷狗音乐 | App 二维码登录、网页登录、每日任务与乐迷勋章 |
| B站大会员 / 小黑盒 | 各平台已有每日任务 |
| 一键签到 | 微博、贴吧、吾爱破解 |
| AI 工具 | Trae、Qoder 国内/国际、WorkBuddy、豆包、MiniMax、DuMate、LobsterAI；保留登录态续期与每日定时 |
| 抖音 / 微博 / B站 / 淘宝 | Python 聊天导出、多账号、本机媒体、离线备份 |
| 火花续期 | 账号、好友、分组、定时消息和风控配置 |
| StarSky | Python/Win32 签到与 Windows 每日计划 |
| 抖音粉丝团 | 趣追「封茗囧菌的圈子」官方入口解析、截图任务参考与可用网页状态检测 |

## QQ 音乐修复范围

- 兼容新版/旧版任务列表与服务端新任务类型，按实际能力区分可执行、需客户端、消费和已下架。
- 免费网页任务会真实访问、按平台条件处理，然后重抓进度；返回成功但进度未变时不会伪造「已完成」。
- 修复字符串状态误判、歌手卡池串用、复查失败伪造完成等问题。
- 看广告、真实收听、分享及小游戏需要平台客户端能力；不伪造广告 Token、时长、游戏结果，不自动消耗抽卡次数或钻石。
- 旧登录 Cookie 已过期时需要重新登录。本次验证以官方页面/代码核对和模拟回归为主，未执行真实账号付费、发布或奖励领取，不将模拟测试等同实际积分到账。

## 封茗囧菌圈子

默认已接入本次提供的官方分享链接，能解析到：趣追、封茗囧菌的圈子、小程序 `tte9a89f1576a4b96b01`、圈子 `27933289767426`。该链接的官方网页只有「打开小程序」入口，任务面板在抖音极速版客户端内；浏览器端没有可直接执行的签到接口。

已加入截图中的听歌、评论、分享、投稿、讨论、邀请、团热度打卡、直播与消费类任务。截图数值明确标为参考，尚未同步时不会显示为实时进度。当前分享入口会明确提示在客户端完成；不会假报自动签到成功。评论、分享、投稿与邀请由本人操作，钻石/灯牌/礼物不自动执行。

## 定时任务

AI 应用内定时保留原配置。Windows 外部计划任务改用 Python 管理：

```powershell
.venv\Scripts\python.exe install_ai_task.py --time 10:20
.venv\Scripts\python.exe install_ai_task.py --time 09:30 --wake
.venv\Scripts\python.exe install_ai_task.py --remove
```

StarSky 页面重新保存时间会更新为 Python 计划任务。旧版指向 `.ps1` 的计划任务会显示迁移提示。不要并行运行多个版本，以免同时操作同一个浏览器 profile。

## 数据与目录

```text
MultiPlatformToolkit/
  app/desktop/          原生界面、主题、控件、后台请求
  app/server.py         本机 Python API
  modules/              各平台 Python 业务
  modules/chat_export/  微博与 B站共享导出服务
  legacy/web_ui/        迁移前网页界面存档，日常运行不需要构建
  scripts/              本地验证和预览辅助脚本
  tests/                不使用真实账号的回归测试
  data/                 原有账号、Cookie、导出记录与日志（不进 Git）
  bootstrap.py          Python 环境检查与桌面启动
  main.py               原生应用 / 本机服务入口
  启动.bat              Windows 薄启动入口
```

保留原有 `data/` 内容和账号目录。浏览器中的 DOM 解析脚本、淘宝用户脚本以及导出 HTML 中的 JavaScript 属于网页资源，不是 Node 执行依赖。迁移前主说明保存在 `legacy/web_ui/README.before-python.md`，便于核对原来的功能说明和上游来源。

## 验证

测试使用合成聊天、模拟平台响应、临时目录与无账号浏览器页面，不读取真实聊天或执行签到。

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe main.py --check
```

仅用于本人账号备份与个人任务管理。平台登录、活动与任务开放情况以官方客户端为准。
