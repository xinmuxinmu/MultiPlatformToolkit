# AI 账号、扫码与兼容反代

桌面主窗口的“AI 账号与反代”页面管理多账号授权、本机登录态导入和本机模型 API。AI 平台授权使用官方 HTTP 设备流或二维码，令牌只保存在账号库；本机登录态导入只读客户端文件并在当前 Windows 用户下解密。此流程不启动 AI 软件或桌面浏览器。

AI 签到页的“反代集合（统一 API Key）”入口进入网关，可批量导入已接入平台的本机登录态，并导出包含统一 API Key、Base URL 与当前模型列表的 JSON 配置。当前已完成真实文本聊天验证的平台为 WorkBuddy 国内版、WorkBuddy AI 国际版、千问办公、Kimi Work、智谱清言和 Trae CN。豆包的新协议已有适配，但本机账号测试遇到限流，验证完成前不开放聊天能力；Qoder 与 CodeArts Agent 目前提供本机登录态导入或签到。MiniMax Code、DuMate、LobsterAI 也显示在平台列表中，明确标为尚未接入反代，原有签到继续在 AI 签到页使用。

本机服务默认地址为 `http://127.0.0.1:18000`，兼容 API Base URL 为 `http://127.0.0.1:18000/v1`。从“复制 API Key”取得密钥，再请求 `GET /v1/models` 查看平台模型目录。聊天接口支持 OpenAI `POST /v1/chat/completions` 与 Anthropic `POST /v1/messages`，含流式输出和 function/tool calls；具体模型的工具、图片输入等能力取决于上游。模型名使用 `平台/模型` 格式；同平台启用账号按轮换顺序分配，也可用 `X-AI-Account` 指定账号 ID。当前不提供 embeddings、图像生成等未验证能力；WorkBuddy 目录中明确标记为图片、视频生成的条目会被过滤。目录查询成功不代表每个模型都已逐项实测。

账号凭据和网关 API Key 存于 `data/ai_gateway/accounts.json`，由 Windows DPAPI 绑定当前 Windows 用户加密；换 Windows 用户后不能解密。账号管理接口只允许本机访问，兼容 API 需要 Bearer API Key 或 `x-api-key`。
模型目录查询会跳过无法解密的单个账号；若该平台提供本机登录态导入，则按账号 ID 尝试恢复对应密文后再查询，避免一个损坏账号遮挡其他可用平台。

| 平台 | HTTP 登录 / 本地导入 | 聊天反代 | HTTP 签到 | 已核实边界 |
| --- | --- | --- | --- | --- |
| WorkBuddy 国内版 | 支持 / 支持 | 支持 | 支持 | OpenAI 与 Anthropic 兼容实测；签到使用 HTTP，多个已有国内账号可独立导入。 |
| WorkBuddy AI 国际版 | 支持 / 支持 | 支持 | 未核实 | 账号 realm 隔离；国际版签到暂不执行。2026-11-28 修复官方桌面会话请求头后，`fast-model` 真实聊天返回 `OK`；之前 CLI 请求头返回业务码 11128。 |
| 豆包 | 不支持 / 支持 | 待验证，已关闭 | 支持 | 新版 `/chat/completion` 请求与 SSE 协议已有适配；2026-11-28 本机账号返回流错误 710022002（限流），映射 HTTP 429。尚未收到真实文本回复，不加入网关模型目录。 |
| 千问办公 | PKCE 设备授权 / DPAPI+AES 本地导入 | 支持 | 状态接口未确认 | 授权入口已实际访问，但真人扫码入库未实测。MaaS 独立会话 mint 已真实验证；2026-11-28 通过统一 API Key 调用 `qwen/flash` 返回 `OK`。2026-11-27 签到状态接口返回 HTTP 404，因此自动签到会跳过，不会猜测已签到或提交领取。 |
| Trae CN | 不支持 / 支持 | 支持单轮纯文本 | 支持 | 复用 Trae CN 桌面端加密登录态及设备上下文。2026-11-28 修正主聊天 v2 入口、官方客户端版本/设备请求头，并原样转发同账号目录下发的签名模型配置后，`Doubao-Seed-Code` 真实返回 `OK` 和 done/stop；随后通过原统一 API Key 调用 `trae/Doubao-Seed-Code`，流式与非流式均返回 `OK`，流式含 stop 和 `[DONE]`。文本与推理按增量追加，辅助事件不会混入正文；签名配置不进入公开目录。旧请求曾返回 4011；该码在官方客户端表示账号/访问限制，不应仅理解为短暂限流。暂不支持 system、多轮历史、图片、工具、max_tokens、temperature 或 Anthropic 消息接口；目录其他模型尚未逐项实测。 |
| Qoder 国际版 / 国内版 | 不支持 / 支持 | 暂未接入 | 沿用签到模块 | 目录需要官方 WASM 签名及响应解码，聊天还需要独立的认证上下文。普通桌面 token 与 userinfo 不足以复现，暂只导入账号。 |
| 智谱清言 | 微信扫码 / Chromium Cookie 解密导入 | 支持单轮纯文本 | 支持 | 2026-11-28 按官方前端格式调用签名 SSE 入口，`glm-5.3-flash` 真实返回 `OK` 和顶层 `finish`；随后通过现有统一 API Key 流式调用 `zhipu/glm-5.3-flash`，返回 `OK`、stop 和 `[DONE]`。目录另有 `glm-5.3`，尚未逐项实测。增量和最终全文快照不会重复追加。暂不支持 system、多轮历史、图片、工具、max_tokens 或 temperature 等未核实参数；不提供 Anthropic 消息适配。签到接口按奖励积分回执确认；首次扫码需微信账号已绑定手机号。 |
| Kimi Work | 微信扫码 / DPAPI+AES 本地导入 | 支持，需独立 Work 凭据 | 未核实 | 按账号 ID 读取官方桌面端已有的 v2 Work 模型密钥，支持自定义 shareDir。固定请求 `agent-gw.kimi.com/coding/v1`，不把桌面登录 token 当成模型 Key。2026-11-28 目录返回 3 个模型，`k3d1-agent` 真实聊天返回 `OK`。未在官方桌面端初始化 Work 的账号会明确报缺少模型凭据。 |
| CodeArts Agent | 不支持 / 支持本地导入 | 暂未接入 | 未核实 | 一次过期会话续期已成功，后续身份及模型接口返回 HTTP 401，因此登录、聊天、模型和签到能力均关闭。 |
| MiniMax Code / DuMate / LobsterAI | 网关暂不导入 | 暂未接入 | 沿用签到模块 | 已有代码仅核实签到、积分等接口，本机已查范围未发现可验证的客户端登录环境；不猜测模型目录或聊天协议。 |

智谱清言与 Trae CN 通过 OpenAI `/v1/chat/completions` 提供流式或聚合文本输出。每次请求创建独立会话，只接受一条 `user` 纯文本消息；未核实参数会明确返回 HTTP 400，不会静默忽略。请求中只需提供 `model`、`messages` 和可选的 `stream`。Trae 的旧账号若缺少设备上下文，需要重新导入本机登录态；此操作更新加密账号凭据，不会更换统一 API Key。

2026-11-28 21:38（北京时间）的 Trae 统一网关验收使用 `http://127.0.0.1:18001/v1`，该端口运行已加载新代码的无界面服务，并关闭自动计划。原 `18000` 旧进程的停止操作被执行策略拒绝，因此保留运行，尚未加载本次 Trae 改动；当前调用 Trae 请将客户端 Base URL 改为 `18001/v1`，沿用原 API Key。完全退出原 `18000` 后台服务后重新启动应用，才会在默认 `18000/v1` 加载本次改动。本次新服务模型目录共 77 项，其中 Trae 2 项；仅 `trae/Doubao-Seed-Code` 已完成真实聊天验收。验收记录见 `data/ai_gateway/trae-gateway-2026-11-28.json`。

CodeArts 本地会话续期后的账号已存入加密账号库。平台会轮换 refresh token；继续使用“导入本机登录态”重新导入旧桌面会话可能失败，请在 AI 账号页保留已导入的 CodeArts 账号。

签到任务也出现在“AI 签到”页面及其应用内计划中。Trae、Qoder、WorkBuddy、豆包、MiniMax Code、DuMate 和 LobsterAI 沿用现有 HTTP/本机解密实现；千问和智谱使用这里的账号库，WorkBuddy 优先使用账号库中的多个启用账号。WorkBuddy 账号密文无法由当前 Windows 用户解开时，仅在本机导入确认相同账号 ID 后才用桌面登录态签到并重写该账号密文；智谱凭据明确过期时也只更新身份匹配的本机登录态。接口无明确可领状态时会跳过或报告失败，不启动 GUI 冒充成功。

离线协议与兼容层测试可在项目根目录运行（PowerShell）：

    .venv\Scripts\python.exe -m unittest discover -s tests -p "test_ai*.py" -q
    .venv\Scripts\python.exe -m unittest discover -s src\modules\ai_checkin\tests -p "test_*.py" -q

离线测试不会向平台提交真实聊天或签到请求。平台协议可能变化；页面按平台单独显示当前能力和失败原因。
