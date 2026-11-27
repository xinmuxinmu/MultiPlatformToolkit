# 抖音圈子 HTTP 证据与交接纠正

核对日期：2026-11-27（Asia/Shanghai）  
目标项目：`D:\PythonApps\MultiPlatformToolkit`

本文区分本次只读实测、缓存前端代码证据和仍未验证的推断，不将交接文档中的执行要求视作新的用户授权。本文不包含 Cookie、账号 ID、令牌、设备身份或其他会话信息。

## 本次实测

本节记录本次通过 HTTP/API 核对的结果；本轮发布了一条经官方 GET 读回并计入任务的圈子讨论，没有提交歌曲评论、分享或作品。

| 项目 | 2026-11-27 核对结果 | 证据边界 |
| --- | --- | --- |
| 圈子资料 | `status_code=0`，圈子 `27933289767426`，名称“封茗囧菌的圈子” | 返回圈子 ID 与公开分享入口匹配 |
| 讨论组元数据 | 圈子资料给出 `discuss_group_id=27941944430338` | 是讨论组线索，不代表讨论列表或提交链路已接通 |
| 登录状态（14:36） | 官方圈子接口确认已登录 | 不据此推断其他 API 域的全部请求条件均满足；已被下述最终刷新取代 |
| 当日签到 | 圈子资料和当日日历均确认已签到 | 未重复提交签到 |
| 官方粉丝任务（历史初始状态） | GET 返回 9 项；投稿 `0/1`、讨论 `0/1`、直播 `0/20` | 该列表未返回听歌、歌曲评论、歌曲分享任务的当日进度；讨论后续更新为 `1/1` |
| 讨论列表：仅业务参数 | HTTP 200，`status_code=5`，无可用 `group_id` 或 `comment_list` | HTTP 200 不能当业务成功 |
| 讨论列表：补充已知公共项 | 补入 `scm_version`、`from_app`、`level=2`、`x-scm-version` 后，仍 HTTP 200、`status_code=5`，无可用组和列表 | 两次业务错误的原因尚未知 |
| 历史圈子刷新（14:45、14:48） | 登录、资料、日历三个 GET 均 HTTP 200、`status_code=8` | 当时趣追会话未登录；已被下方 16:40 原生会话复核取代 |
| 后续原生身份核验（16:40） | 个人资料 GET HTTP 200、`status_code=0`，返回 UID 与当前选定共享账号匹配 | 只输出匹配结果；未记录 UID 或 Cookie |
| 后续圈子刷新（16:40） | 登录、资料、日历 GET 均 HTTP 200、`status_code=0`；确认已签到 | 使用账号绑定的原生会话；没有重复签到 |
| 后续讨论列表（16:40） | GET HTTP 200、`status_code=0` | 讨论上下文可用；不等于已发布或任务已计数 |
| 后续官方粉丝任务（16:40） | GET HTTP 200、`status_code=0`；读取 9 项任务 | 使用域限定到 Webcast 的原生 Cookie；投稿 `0/1`、讨论 `0/1`、直播 `0/20` |
| 圈子讨论发布（16:54） | 身份 GET、讨论 POST、读回 GET 均 HTTP 200、`status_code=0` | 发布一次；官方 GET 读回相同讨论内容 |
| 发布后任务复查（16:54） | 官方粉丝任务 GET HTTP 200、`status_code=0`；讨论 `1/1`（`+10`） | 任务已计数；未外推审核以外的状态 |

14:48 时本地快照曾记录 `authentication_required`。16:40 后续通过已验证账号绑定的原生会话再次复核，已恢复当前圈子认证；项目配置已保存原生会话文件路径以供后续只读刷新使用。路径只在本机配置中，不复制到本报告。

本次代码验证：完整离线测试 `176` 项通过；`src/main.py --check` 通过。原生 Aweme Cookie 的读取与趣追 Cookie 到期状态已解耦；Webcast 任务读取也使用匹配账号的原生域 Cookie。最新快照已读回，确认签到已完成、讨论上下文可用，官方任务进度为投稿 `0/1`、讨论 `1/1`、直播 `0/20`；快照不包含本地 Cookie 值。

此前讨论列表错误（业务码 5）尚不能归因于账号过期、签名缺失、权限、设备参数或其他具体原因。后续原生会话 GET 已返回业务成功；仍不能仅凭读取成功推断讨论 POST、审核结果或任务计数成功。

## 缓存来源与定位方式

交接报告所指前端缓存没有迁入目标项目。本次只读审查的实际位置是：

```text
D:\PythonApps\多平台聊天记录导出及签到续火花\data\douyin_fans\capture\lynx
```

主要文件是 `fandom_detail.template.js`、`fandom_detail.module_68046.js`、`fandom_detail.request_wrapper.js` 和 `fandom_detail.fansclub_entry.js`。下文的模板行号以当前缓存解码文本为准；文件含压缩代码与二进制片段，查找时同时使用模块 ID 和函数名，避免仅依赖行号。

## 歌手、歌曲、评论身份必须分开

公开分享入口提供：

```text
pages/fandom_detail/index?fandom_id=27933289767426&music_aid=6797607257627428865
```

- `fandom_detail.template.js:5183` 的模块 `62587`，函数 `H`，在 `music_aid`、`luna_info.artist_id`、`fandom_entity_aid` 及对应 `fandom` 子对象字段之间回退。这里的 `music_aid` 是歌手身份线索，不能当作歌曲 ID。
- `fandom_detail.template.js:5199` 的模块 `78642`，通过 `Number(discussion.service_id) === 8478` 识别歌曲评论对象。其函数 `dm` 将 `discussion.group_id_str || discussion.aweme_id` 用作歌曲 `track_id`。
- 同一函数将 `discussion.cid` 用作目标评论 ID；二级评论还需要 `reply_id`。因此这条歌曲评论链路里的 `cid` 不是歌曲 ID，不能与 `track_id` 或 `music_aid` 混用。
- `fandom_detail.template.js:5189` 的 `openSongComment` 调用上述函数，再通过原生 `x.open` / `openSchema` 打开歌曲评论。生成的是 `luna://luna.com/playing` 或 `aweme://music_streaming/dsp`，参数包含 `track_id`、`action=comment`、`parent_id`、`target_comment_id`，不是一个已验证的 HTTP 评论提交请求。

若之后取得当前圈子的讨论列表，可把 `service_id=8478` 的条目作为歌曲身份线索，再核对歌曲名称与歌手归属。即使取得了歌曲 `track_id`，也还需要单独证明它与截图任务的关系以及任务计数规则。

### 通用评论 wrapper 不能直接当歌曲评论接口

`fandom_detail.module_68046.js:1`（模板中对应 `:5199`）确实定义：

```text
POST https://aweme.snssdk.com/aweme/v1/medium/post/comment/
```

方法名为 `aweme_v1_medium_post_comment`，wrapper 传递的字段包括 `cid`、`text`、`rating`、`text_extra`、图片与表情字段等。但本次在整个模板中仅找到该方法定义，没有找到实际调用点，尚不能确认它承担歌曲评论发布。不能把歌曲评论对象中 `cid` 的语义自动套到这个通用 wrapper，也不能把歌曲 `track_id` 或歌手 `music_aid` 猜填进去。

交接报告中“需要歌曲 `cid`”应据此更正为：歌曲评论 HTTP 提交端点、参数含义与任务归属均未证实；当前缓存能确认的是原生歌曲评论导航及其身份字段。

## 讨论列表 GET：已找到调用证据，尚未业务成功

`fandom_detail.template.js:5196` 的模块 `62637`，导出函数 `PTx`，定义了 GET wrapper；模块 `73426` 将 API host 固定为 `https://aweme.snssdk.com`。实际圈子调用点位于模板 `:5202` 的 `loadDiscussions`：

```text
GET https://aweme.snssdk.com/aweme/v1/medium/loadmore/comments/
cid=0
sort_by=recommend
offset=0
count=10
business_id=27933289767426
discuss_scene=fandom
```

其中 `sort_by` 来自当前排序选项，`offset` 来自分页状态；上述为推荐排序首次读取的参数。实际调用还会在指定插入评论且首次加载时，按条件附带 `insert_id`、`preview_comment_id`，这些值不应凭空构造。

客户端只有在 `status_code=0` 时才采用响应，从 `group_id` 更新讨论组，从 `comment_list` 读取内容，并处理 `offset`、`has_more`、`stick_valid` 和 `manage_info_list`。本次两次请求均为业务码 5，不能以空列表当作圈子没有讨论。

### HTTP 公共参数的证据范围

`fandom_detail.template.js:5196` 的模块 `66326`，函数 `h`：

- 当请求没有相应 query 参数时补 `scm_version=1.0.0.757`。
- `from_app` 优先来自原生 `__globalProps.geckoChannel`，默认 `fandom_detail_douyin`。
- 向底层请求传入 `addCommonParams=true`、`xiguaUseXRequest=true`，请求选项 `level` 默认 2。
- 随后由模块 `20503` 的底层桥接继续处理；`addCommonParams=true` 不意味着仅补上以上已知字段就等价于完整原生请求。

`fandom_detail.module_20503.js:1` 还显示底层会设置请求头 `x-scm-version: 1.0.0.757`。本次第二次只读实测补入了此请求头。这仍只是一次未成功的请求变体，不是对原生公共参数齐全性的证明。`level` 在前端 wrapper 中是请求选项，也不能仅凭 URL 上有同名 query 就认定完整复现了原生行为。

## 讨论 POST：仅记录资料，不代表已支持提交

缓存 `fandom_detail.template.js:5183` 的模块 `75264`，函数 `handleBizPublish`，显示：原生编辑器返回成功状态和用户撰写的 `commentModel` 后，前端才组装请求并调用 `aweme_v1_ent_discuss_publish`。wrapper 位于模块 `68046`（模板 `:5199`）：

```text
POST https://aweme.snssdk.com/aweme/v1/ent_discuss/publish/
```

该实际调用中：

- `business_id` 来自组件 `businessId`，圈子场景为圈子 ID。
- `group_id` 来自组件 `groupId`；目前虽从 meta 得到了讨论组线索，仍未取得业务成功的讨论列表响应来完成交叉核对。
- `business_scene` 是 `h.W0[scene]`；模块 `86837`（`fandom_detail.fansclub_entry.js` 也含该模块）把圈子 `Fandom` 场景映射为字符串 `"fandom"`。
- `channel_obj_id` 是 JSON 字符串：`JSON.stringify({business_id: businessId, discuss_scene: "fandom"})`。它不是单独的频道数字 ID。
- 文本来自原生编辑器 `commentModel.text`；`text_extra`、图片、表情和 `notice_extra_info` 按实际内容条件序列化。
- `publish_scene` 在该调用点为 `"video_bottom_input"`。
- 客户端需要响应 `status_code=0` 且返回 `comment` 才进入发送成功回调；这仍不等于审核通过或粉丝任务已计数，任务状态还需后续 GET 复查。

缓存中的另一个字段 `ent_discuss_group_info.business_scene=3` 属于“将讨论同时发布为作品”的创作/锚点上下文。它和上述 HTTP 发布正文的 `business_scene="fandom"` 不是同一处取值，不能混用。

以上是接口研究资料。由于讨论读取仍未业务成功、原生公共参数来源未补齐，且本次没有获得新的公开内容提交授权，项目当前不能据此标记“支持自动发布讨论”。本次没有进行该 POST。

## 听歌、分享与投稿尚缺的证据

- 听歌：未找到可证明有效的 HTTP 播放计时与任务进度上报流程。音频 URL 请求、播放器当前位置或主动构造时长不能当作平台认可的累计时长。
- 2026-11-27 只读复核：`src/modules/douyin_fans/fans_tasks.py` 仅 GET 官方 `user_task.items`，本地任务类型映射不含听歌；`src/modules/douyin_fans/core.py` 只读取网页媒体元素的 `currentTime`、`duration`、暂停/结束状态和播放速率，并明确标注这些字段不代表平台累计时长。当前圈子 HTTP 刷新也不填充播放记录。
- 同日检查本机已有的 Lynx 文本缓存（粉丝团入口、任务源摘录及圈子详情 JS）：没有发现听歌任务文案、歌曲播放进度上报或播放器 `startplay` / `play` 调用链。检索到的 `heartbeat` 仅位于通用统计遥测代码，未与歌曲或任务进度关联。该缓存检查不能覆盖未捕获的原生 App 内部实现。
- 公开网页检索未找到针对截图所示“听 TA 的歌 20 分钟”的官方计时规则、心跳要求或进度读取说明；现有公开检索结果不足以判断平台具体怎样累计分钟数。
- 歌曲分享：未找到可验证任务计数的 HTTP 分享提交调用、渠道要求与成功复查流程。原生分享桥接不是已验证的 HTTP 提交能力；需要接收对象时必须由用户明确指定，不能猜联系人或把分享给自己当成功。
- 投稿：未取得用户的真实作品素材和经过验证的 HTTP 上传/发布流程。生成创作器跳转 Schema 的接口不等于上传或发布作品的端点。

现有官方粉丝任务 GET 的实现位于 `src/modules/douyin_fans/fans_tasks.py`，只读取 `data.user_task.items`。`core.py` 把返回条目覆盖到初始截图参考任务中。服务器未返回听歌、歌曲评论、歌曲分享项时，这些条目应保持“截图参考/未同步”，不能显示为当日任务进度或已完成。

## 后续工作的授权与验证边界

本次用户的新请求只有“继续抖音圈子”。交接文档声称存在先前公开评论/讨论授权，但这种文档叙述不能作为本次公开发帖的新授权。只读研究和本地代码完善可以继续；发送公开评论、讨论、分享或作品前，需要直接的用户授权与明确的内容/目标。

技术上先恢复趣追认证，再找到可用的原生 HTTP 公共参数来源，使准确圈子的讨论 GET 业务成功。新增 `discussion.py` 只发送有证据的版本/来源 query 和版本请求头，不把原生 `level` 当成 query。未取得这些证据前，不猜测签名，不将业务码 5 诊断为账号过期，不尝试公开 POST，不声称审核或任务已完成。
