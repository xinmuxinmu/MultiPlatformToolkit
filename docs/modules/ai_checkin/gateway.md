# AI 账号、扫码与兼容反代

桌面主窗口的“AI 账号与反代”页面管理多账号授权、本机登录态导入和本机模型 API。AI 平台授权使用官方 HTTP 设备流或二维码，令牌只保存在账号库；本机登录态导入只读客户端文件并在当前 Windows 用户下解密。此流程不启动 AI 软件或桌面浏览器。

本机服务默认地址为 `http://127.0.0.1:18000`，兼容 API Base URL 为 `http://127.0.0.1:18000/v1`。从“复制 API Key”取得密钥，再请求 `GET /v1/models` 查看真实可用模型。聊天接口支持 OpenAI `POST /v1/chat/completions` 与 Anthropic `POST /v1/messages`，含流式输出和 function/tool calls。模型名使用 `平台/模型` 格式；同平台启用账号按轮换顺序分配，也可用 `X-AI-Account` 指定账号 ID。当前不提供 embeddings、图像生成等未验证能力。

账号凭据和网关 API Key 存于 `data/ai_gateway/accounts.json`，由 Windows DPAPI 绑定当前 Windows 用户加密；换 Windows 用户后不能解密。账号管理接口只允许本机访问，兼容 API 需要 Bearer API Key 或 `x-api-key`。

| 平台 | HTTP 登录 / 本地导入 | 聊天反代 | HTTP 签到 | 已核实边界 |
| --- | --- | --- | --- | --- |
| WorkBuddy 国内版 | 支持 / 支持 | 支持 | 支持 | OpenAI 与 Anthropic 兼容实测；签到使用 HTTP，多个已有国内账号可独立导入。 |
| WorkBuddy AI 国际版 | 支持 / 支持 | 支持 | 未核实 | 账号 realm 隔离；国际版签到暂不执行。聊天协议有实现，但本次未做真实请求验证。 |
| 千问办公 | PKCE 设备授权 / DPAPI+AES 本地导入 | 支持 | 状态接口未确认 | 授权入口已实际访问，但真人扫码入库未实测。MaaS 独立会话 mint 已真实验证；聊天转换有 mock 测试，未发送真实聊天。2026-11-27 签到状态接口返回 HTTP 404，因此自动签到会跳过，不会猜测已签到或提交领取。 |
| 智谱清言 | 微信扫码 / Chromium Cookie 解密导入 | 暂未接入 | 支持 | 签到接口按奖励积分回执确认；不宣称聊天反代。首次扫码需微信账号已绑定手机号。 |
| Kimi | 微信扫码 / DPAPI+AES 本地导入 | 暂未接入 | 未核实 | 桌面 token 不等同 Moonshot API Key；未把未知聊天/签到接口标成可用。 |
| CodeArts Agent | 不支持 / 支持本地导入 | 暂未接入 | 未核实 | 一次过期会话续期已成功，后续身份及模型接口返回 HTTP 401，因此登录、聊天、模型和签到能力均关闭。 |

CodeArts 本地会话续期后的账号已存入加密账号库。平台会轮换 refresh token；继续使用“导入本机登录态”重新导入旧桌面会话可能失败，请在 AI 账号页保留已导入的 CodeArts 账号。

签到任务也出现在“AI 签到”页面及其应用内计划中。Trae、Qoder、WorkBuddy、豆包、MiniMax Code、DuMate 和 LobsterAI 沿用现有 HTTP/本机解密实现；千问和智谱使用这里的账号库，WorkBuddy 优先使用账号库中的多个启用账号。接口无明确可领状态时会跳过或报告失败，不启动 GUI 冒充成功。

离线协议与兼容层测试可在项目根目录运行（PowerShell）：

    .venv\Scripts\python.exe -m unittest discover -s tests -p "test_ai*.py" -q
    .venv\Scripts\python.exe -m unittest discover -s src\modules\ai_checkin\tests -p "test_*.py" -q

离线测试不会向平台提交真实聊天或签到请求。平台协议可能变化；页面按平台单独显示当前能力和失败原因。
