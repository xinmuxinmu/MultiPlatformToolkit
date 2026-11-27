# 千问办公协议核查

核查日期：2026-11-27。依据本机官方安装包
`D:/Program/qw/QwenWorkCN/2.1.0-26111906/resources/app.asar` 的
`out/main/main.js` 与 `out/main/chunks/constants-CxL4lf4N.js`。未运行客户端或浏览器。

## 本地登录态

- 默认目录为 `%APPDATA%/QwenWorkCN`，可用 `QWENWORK_AUTH_DIR` 覆盖。
- `auth-v2.dat` 在本机为 Chromium `v10` AES-GCM 密文，密钥来自同目录
  `Local State` 的 `os_crypt.encrypted_key`，由当前 Windows 用户 DPAPI 解开。
- 支持 schemaVersion 2、3；导入前验证账号 id、生产 apiOrigin、schema3 active 阶段及有效期。
- 本地文件全程只读。现有桌面登录态有效期不足时不尝试轮换共享 refreshToken，
  避免新旧会话相互覆盖；过期时提示重新扫码。

## 设备授权与二维码

默认生产授权和 API origin 均为 `https://gateway.qwenwork.cn`。
`constants.Gi=N=On` 决定默认站点域名，不能把 web identity 的 qwenwork.cn 直接当作设备授权域名。

- 授权入口：`GET /device/selectAccounts`。查询参数为 challenge、challenge_method=S256、
  nonce、machine_id、client_id、redirect_uri。客户端公开 client_id 为
  `e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb`，redirect_uri 为 `qwenwork-cn://`。
- 原客户端 `startDeviceFlow()` 生成 PKCE verifier/challenge 与 nonce；授权 URL 可在账号页转二维码。
- 轮询：`GET /api/v1/deviceToken/poll?nonce=...&verifier=...&challenge_method=S256`。
  HTTP 404 表示尚未得到授权记录；授权有效期为 300 秒。
- 有 token 时必须核对响应 nonce、code_challenge、code_challenge_method，全部匹配后才保存账号。
- 2026-11-27 实际通过 HTTP 访问生成的授权入口，重定向至 qwenwork.cn `/biz/signin`，最终 HTTP 200。
  用户尚未完成扫码，因此扫码成功入库只做了协议模拟测试，不能声称已完成真人登录实测。

## 签到

客户端 `fetchCreditsGrowthCardRuntime()` / `claimCreditsGrowthCardDailyCheckIn()` 使用：

| 操作 | 方法 | 路径 |
| --- | --- | --- |
| 查询 | GET | `/sash/api/v1/me/daily-check-in/status` |
| 领取 | POST，无请求体 | `/sash/api/v1/me/daily-check-in/claim` |

认证为 Bearer token，带客户端 `X-QwenWork-*` headers。状态取响应 `data.status`
或根级 status；只有 `CLAIMABLE` 才发送领取请求，`CLAIMED_TODAY` 表示今日已签。
`DISABLED` / `NOT_STARTED` / `ENDED` 明确跳过。领取仅 `result=CLAIMED` 算成功，
`ALREADY_CLAIMED` 算已领取，不用 HTTP 200 或 code=0 猜测结果。

真实本地账号已解密成功，但状态接口在 2026-11-27 返回 HTTP 404，所以本次结果是
`skipped`，没有发送领取请求，也无法确认今天是否已签到。无凭据验证记录保存在
`data/ai_checkin/qwen_live_verification.json`。

## 反代边界

官方主进程证据确认，聊天使用 MaaS 独立会话，不需要保持千问桌面程序运行：

- `GET /api/desktop/chat-modes` 向 `https://gateway.qwenwork.cn` 查询模型目录；响应 schema 2 的
  catalog 项包含 key、format、harness 和 enable。网关只发布启用且格式已支持的条目，公开 ID 加一次
  平台前缀，例如 `qwen/flash`。2026-11-27 实际读取到 14 个启用条目。
- `POST /api/desktop-sessions` 使用千问主账号 Bearer token、客户端版本 headers 和空 JSON 对象，
  签发单独的 MaaS access token、session_id、expires_at_ms、ttl_ms 和 base_url。令牌不替换桌面
  `token`/`refreshToken`，只保存在网关加密凭据中。接近过期时按官方客户端协议调用
  `/v1/access-tokens/exchange`；已过期则向固定千问网关申请同 session_id 的新 MaaS 凭据。
- 官方模型代理地址以响应 base_url 为准。实现仅接受 HTTPS 的已验证精确主机
  `maas.xiaoluozi.cn` 与本机真实签发验证返回的 `api9.qwenwork.cn`，且只接收空路径或 `/v1`；
  未列出的区域节点不会收到 MaaS token。
- OpenAI 格式按 `/v1/chat/completions` 透传 SSE 和工具调用/usage；Anthropic Messages 格式按
  `/v1/messages` 上游请求，并将文本、推理、工具参数片段和 token 用量映射为 OpenAI-compatible
  SSE，供统一 `/v1/chat/completions` 和 `/v1/messages` 接口使用。仅模型目录明确列出的格式会开放。

真实只读目录验证记录：`data/ai_checkin/qwen_models_verification.json`。真实 session mint 验证记录
`data/ai_checkin/qwen_mint_verification.json` 只留脱敏的 scheme、精确 hostname、路径、期限和会话
存在性，不包含 access token、session_id 或千问主账号凭据；未发送真实聊天请求。

测试：`python -m unittest discover -s tests -p test_ai_qwen_protocol.py -v` 与
`python -m unittest discover -s tests -p test_ai_qwen_maas.py -v`。
