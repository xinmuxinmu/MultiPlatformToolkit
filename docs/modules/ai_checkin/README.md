# AI 平台自动签到与账号反代

AI 签到模块覆盖 Trae CN、Qoder 国内版与国际版、WorkBuddy、WorkBuddy AI、千问办公、智谱清言、豆包、MiniMax Code、DuMate 和 LobsterAI。各平台通过 HTTP 接口查询签到状态并领取；身份凭据从本机文件、数据库或 Cookie 库读取，必要时在本机解密。签到流程不打开平台界面或浏览器。账号扫码、多账号管理和 OpenAI/Anthropic 兼容反代见 gateway.md。

首次升级时，旧配置会一次性启用清单内全部平台；之后页面开关由用户设置控制。登录态缺失、过期或无法解密时，该平台会在日志里说明原因。WorkBuddy 新版本的加密凭据由隐藏的 Electron Node 运行模式调用本机原生密钥绑定进行本地解密；该子进程只返回解密后的令牌，签到本身仍走 HTTP。解密失败时不会发送签到请求。

Qoder 登录令牌通过本机 DPAPI/AES-GCM 解密；若需要更新令牌，会通过 HTTP 续期。MiniMax、Trae、WorkBuddy 等适配器同样只用本地登录态和 HTTP 请求。豆包从本机客户端 Cookie 库解密登录态，再调用客户端使用的活动接口；只有服务端明确返回签到成功标记才报告成功。千问和智谱使用账号库中的多账号登录态；千问状态接口当前返回 HTTP 404 时会跳过，智谱按服务端积分回执判定。

页面和命令行共享同一配置。应用运行期间可用每日定时；应用关闭时可使用 Windows 计划任务：

```powershell
.venv\Scripts\python.exe src\install_ai_task.py --time 09:30
.venv\Scripts\python.exe src\install_ai_task.py --remove
```

离线测试位于 `tests/test_ai_checkin_workbuddy.py` 和 `src/modules/ai_checkin/tests/`；测试以模拟响应验证流程，不会提交真实签到请求。
