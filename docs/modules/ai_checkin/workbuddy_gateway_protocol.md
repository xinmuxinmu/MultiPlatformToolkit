# WorkBuddy 多账号授权与网关协议

核对日期：2026-11-27。实现：`src/modules/ai_gateway/providers/workbuddy.py`。

## 协议依据

用户提供的 `D:/PythonApps/wb` 是 wb2go 项目，其 MIT 授权已保存在 provider 目录的 `LICENSE-wb2go`。协议对应源文件：

- `internal/panel/oauth.go`：设备授权状态创建、轮询与账户资料解析。
- `internal/upstream/api.go`：真实路径、授权头、令牌刷新与模型目录。
- `internal/upstream/realm.go`：国内和国际服务的区域隔离。
- `internal/upstream/payload.go`、`sse.go`：强制上游流式、系统消息要求、OpenAI chunk 格式。

本项目只迁移必要的授权、模型与流传输行为，不引入参考项目对用户提示词进行改写的逻辑。

## 已实现路径

| 功能 | 方法与路径 | 行为 |
| --- | --- | --- |
| 创建授权 | POST `/v2/plugin/auth/state?platform=CLI` | 解析服务器签发的 `state` 和 `authUrl`，链接由页面生成二维码 |
| 轮询授权 | GET `/v2/plugin/auth/token?state=…` | 等待码 11217/11218/1001 不作为成功；支持 `data.auth` 信封 |
| 账号资料 | GET `/v2/plugin/login/account` | 缺少身份时使用 `/v2/plugin/account` 兜底，无法确认 uid 不入库 |
| 令牌续期 | POST `/v2/plugin/auth/token/refresh` | 只在续期请求发送 `X-Refresh-Token`，轮换值由调用方持久化 |
| 模型目录 | GET `/v3/config` | 失败时查询 `/v2/enterprises/personal/models`，不编造模型列表 |
| 对话流 | POST `/v2/chat/completions` | 上游始终 `stream=true`；原样保留工具调用、思考和 usage chunk |
| 签到状态 | POST `/v2/billing/meter/checkin-activity-status` | 明确已签到时跳过；活动关闭或状态未知时停止领取 |
| 签到领取 | POST `/v2/billing/meter/daily-checkin` | `code=0` 成功，`code=10001` 已签到，其余不误报成功 |

国内 OAuth 和对话使用 `copilot.tencent.com`，国内签到使用 `www.workbuddy.cn`。国际 OAuth 和对话使用 `www.workbuddy.ai`。`WorkBuddyAIProvider` 独立标记国际区域，不提供未确认的每日签到功能。

## 本地导入

国内和国际桌面凭据分别读取：

- `%LOCALAPPDATA%/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info`
- `%LOCALAPPDATA%/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop-ai.info`

旧版明文直接读取，新版 `$wbEncrypted` 字段复用已存在的 `ai_checkin/vendor/platforms/wb_helper.js`，在对应运行时以 `ELECTRON_RUN_AS_NODE=1` 和隐藏窗口执行本地解密。该助手没有网络与界面代码，访问令牌和刷新令牌均在内存中传递。

导入也读取参考项目 `D:/PythonApps/wb/accounts/*.json` 中区域匹配的账号；可通过 `WB_REFERENCE_ACCOUNTS_DIR` 修改目录。按 uid 去重，保留到期时间较新的凭据。源文件不回写，轮换后的凭据仅由本项目保存。复制既有会话后继续使用同一刷新令牌，可能触发服务端的会话轮换；需要各程序独立会话时，在本项目扫码新增授权。

## 验证记录

2026-11-27 的实际 HTTP 只读验证结果：

- 国内和国际创建授权均成功，返回官方 HTTPS 授权链接，授权轮询均返回等待状态。
- 国内导入 2 个账号，国际导入 1 个账号，桌面加密字段本地解密成功。
- `/v3/config` 国内返回 34 项，国际返回 28 项，均来自实际登录态。
- 国内 2 个账号分别返回 `today_checked_in=true` 和 `false`。本次验证没有调用领取，也没有执行令牌续期或收费聊天。
- `tests/test_ai_gateway_workbuddy.py` 验证 OAuth 信封和业务码、区域隔离、源文件只读、隐藏 Node 解密、令牌轮换、模型查询、工具/思考/usage 流保留、空流和业务错误处理、已签/未知状态不领取等行为。

完整聊天与扫码后的最终授权仍须真实用户完成授权及实际聊天验证；当前 HTTP 只读结果不等同于付费模型对话已实测。
