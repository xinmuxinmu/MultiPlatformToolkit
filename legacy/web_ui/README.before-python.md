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
| 网易云乐迷团 | `/signin#netease` | 乐迷团每日任务、亲密值进度，任务自动识别并可在设置里勾选，支持快捷登录 |
| 酷狗音乐 | `/signin#kugou` | 每日领 VIP（听歌 / 广告），乐迷团信息；勋章任务自动识别，默认自动尝试全部非付费任务（购买/付费/金币兑换除外），勾选会保存 |
| QQ 音乐 / QQ 签到 | `/signin#qq` | QQ 音乐每日签到、乐迷勋章与勋章任务；可自选要签到的歌手/勋章、任务类型，勾选会保存 |
| B站大会员大积分 | `/signin#bili-vip` | 签到、等级加速包、福利 / 浏览任务，复用 B站聊天记录登录态 |
| AI 工具签到 | `/ai-checkin` | Trae CN / Qoder（国际版+国内版）/ WorkBuddy / 豆包 / MiniMax / 百度搭子 / LobsterAI，读客户端登录态，Token 临期自动续期 |

统一入口 `http://127.0.0.1:18000/`，左侧栏切换模块。

## 快速开始

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -r requirements.txt
python main.py
```

Windows 也可直接双击 `启动.bat`（首次会自动建虚拟环境装依赖）。启动后会自动打开应用窗口；关闭应用窗口或控制台窗口都会结束后端进程。控制台默认保持空白，日志写入 `data/logs/app.log`；调试时设置 `APP_VERBOSE=1` 可恢复控制台输出。

窗口默认用 **PySide6 + Qt WebEngine**（`app/qt_window.py`）承载本地页面，窗口属于本进程，登录态存在 `data/qt_profile`。没装 PySide6 时自动回退到系统 Chrome/Edge 的 `--app` 窗口，功能不变。

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
  logs/app.log                     # 后端运行日志（控制台安静模式）
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
  ai_checkin/     AI 工具签到（Trae/Qoder/WorkBuddy/豆包…，vendor/ 里是 MIT 上游实现）
  bilibili_vip/   B站大会员大积分（core + routes + ui）
  spark/          火花
  starsky/
main.py         启动入口（uvicorn，默认 18000 端口）
```

### B站大会员大积分

接口移植自 [RayWangQvQ/BiliBiliToolPro](https://github.com/RayWangQvQ/BiliBiliToolPro)（MIT）的 `VipBigPoint` 任务，登录态直接复用「B站聊天记录」的账号，不用重复扫码。

实测结果：

| 任务 | 结果 |
| --- | --- |
| 每日签到 / 大会员福利 / 权益页 | 正常 |
| 大会员等级加速包 | 正常，但服务端要求先看过视频才给换 |
| 浏览追番频道 / 影视频道 / 会员购 | 正常，各 +10，积分实测到账 |
| 浏览装扮商城主页 | 走不通（`complete/v2` 稳定 `-400`），需手动打开装扮商城 |
| 观看剧集内容 | 走不通，领取接口要 App 端 `access_key`，且要真看满 10 分钟 |
| 购买类（影片 / 会员购 / 装扮 / 线下） | 程序一律不碰 |

关键坑：App 侧参数 `build` 必须带且够新，否则接口统一回 `6007000 请将客户端更新至最新版本`（见上游 issue #1195）；字段名是 `task_code` 下划线形式，会员购埋点是 `eventId` 驼峰。

### AI 工具签到

`/ai-checkin`（侧栏「AI 工具」）这一页签到的是 AI 办公客户端：Trae CN（字节）、Qoder 国际版 / 国内版（阿里）、WorkBuddy（腾讯）、豆包、MiniMax Code、百度搭子 DuMate、LobsterAI。

实现来自三个 MIT 项目，代码在 `modules/ai_checkin/vendor/`：

| 来源 | 用途 |
| --- | --- |
| [mcvea/AIAutomator](https://github.com/mcvea/AIAutomator) | TraeWork / WorkBuddy / MiniMax / DuMate / Lobster 的签到与凭据续期 |
| [tututashu/doubao-auto-checkin](https://github.com/tututashu/doubao-auto-checkin) | 豆包：读客户端凭据 + 无头浏览器签，不重启客户端 |
| [spirodelazz/ide-daily-checkin](https://github.com/spirodelazz/ide-daily-checkin) | Qoder 桌面端凭据提取（DPAPI + AES-GCM）与续期思路 |

使用要点：

- 先在对应客户端登录一次，脚本读本机登录态，不用填 Cookie；Qoder 也支持直接填 PAT（`pt-` 开头）或 Cookie。
- Qoder 桌面端 token 只对「自己那个站点」有效：国际版走 `openapi.qoder.sh`，国内版走 `openapi.qoder.com.cn`。站点不匹配会 401，程序会用同一个 token 自动换另一个站点重试，并在结果里写明切到了哪个站点。
- 「设备今日已签」不会算失败：上游把这种情况当失败返回，这里统一显示为「已签到」。
- 豆包：能读到客户端 Cookie（实测 29 条），但活动页在浏览器里会返回「网络异常，请重试」，拿不到签到入口，目前属于上游/平台限制，只能在豆包客户端里手动签。
- 「预览（不领取）」只查状态不发领取请求；「续期登录态」只对 `data/ai_checkin/auths` 下临期账号换新 token。
- 依赖：`playwright`（豆包要用浏览器内核）、`pycryptodome`（Trae / DuMate / Qoder 解密）；缺了页面顶部会提示。
- Qoder 桌面端凭据用 Windows DPAPI 加密，必须由登录 Qoder 的同一个 Windows 用户运行。
- 多账号凭据放在 `data/ai_checkin/auths/`（等同账号密码，别外发），没有该目录时走客户端单账号登录态。

### 每天自动签到（AI 工具）

两条通道，互不冲突：

1. **应用内定时**：页面顶部「每天 HH:MM 自动签到」开关（默认 10:20 开）。应用在跑就到点执行；如果启动时已经过点且今天没跑过，会补跑一次。状态写在 `data/ai_checkin/state.json`，日志在 `data/ai_checkin/checkin.log`。
2. **Windows 计划任务**（应用不开也能跑）：

```powershell
powershell -ExecutionPolicy Bypass -File install_ai_task.ps1                                  # 每天 10:20
powershell -ExecutionPolicy Bypass -File install_ai_task.ps1 -Time "09:30"
powershell -ExecutionPolicy Bypass -File install_ai_task.ps1 -Remove
```

命令行等价写法：

```bash
.venv/Scripts/python.exe -m modules.ai_checkin.runner          # 跑启用的平台
.venv/Scripts/python.exe -m modules.ai_checkin.runner --dry    # 只查状态
.venv/Scripts/python.exe -m modules.ai_checkin.runner --site trae --site workbuddy
```

默认启用：Trae CN、Qoder、WorkBuddy（这三个已实测可用）。豆包当前只能客户端内签到，默认关闭；MiniMax / 百度搭子 / LobsterAI 需要先装对应客户端再在页面打开开关。

### 界面风格

整体视觉参照 `D:\PythonApps\wb`（wb2go 面板）：绿色单强调色、近黑侧栏 + 2px 绿色右边框、选中项左侧 3px 绿条、卡片 1px 描边 + 顶部 3px 绿条、8px 圆角、数字用等宽字体。

- 主题只保留**深色 / 浅色**两套，旧的“蓝色 / 紫色”设置会自动映射到深色。
- 配色集中在 `app/static/mp_theme.css`（模块页）和 `app/static/mp_ui.css`（聊天类页面）的 `--mp-*` 变量里；外壳在 `app/shell/index.html`。
- 模块页的品牌色（酷狗蓝、B站粉、网易云紫）已统一改成 `var(--mp-accent)`，需要恢复某个品牌色时，在该页 `<style>` 里覆盖 `--task-accent` 即可。

## 说明

- 仅供导出**本人账号**的聊天记录用于备份，请遵守各平台用户协议，不要用于抓取他人数据或商业分发。
- 平台接口与页面结构可能变化，若某模块失效，通常是接口路径或字段变更，见各模块 `README.md`。
