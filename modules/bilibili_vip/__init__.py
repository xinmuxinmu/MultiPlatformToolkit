"""B站 · 大会员大积分每日任务。

接口规格移植自 RayWangQvQ/BiliBiliToolPro 的 VipBigPoint 任务（MIT）：
- src/Ray.BiliBiliTool.DomainService/VipBigPointDomainService.cs（任务编排队列）
- src/Ray.BiliBiliTool.Application/VipBigPointAppService.cs（各任务 task_code）
- src/Ray.BiliBiliTool.Agent/BiliBiliAgent/Interfaces/IApiApi.cs（端点与 Referer）
- src/Ray.BiliBiliTool.Agent/BiliBiliAgent/Dtos/BaseAppRequest.cs（公共 App 参数）
- bruno/api.bilibili.com/（真实抓包的请求头与参数）

不是照抄：每一条都在本机用真实登录态跑过一遍，结论写在 core.py 里。
其中最要紧的一条——App 侧 `build` 参数必须带且够新，否则统一回
`6007000 请将客户端更新至最新版本`（见上游 issue #1195）。

登录态不自己存，直接复用 bilibili_chat 模块已经登录好的账号
（data/bilibili_chat/accounts/<id>/state.json），不重复造轮子。
"""
