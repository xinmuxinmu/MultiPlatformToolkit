/* 全局主题 / 中英文切换 —— 各模块页面共用
 * 由外层软件壳通过 postMessage({type:'mp:settings'}) 下发，
 * 单独打开页面时从 /api/ui/settings 读取并持久化到 localStorage。
 */
(function () {
  'use strict';
  if (window.__MP_UI_LOADED__) return;
  window.__MP_UI_LOADED__ = true;

  var THEMES = ['dark', 'light'];
  var SETTINGS_URL = '/api/ui/settings';
  var page = 'other';
  var p = location.pathname;
  if (p.indexOf('/weibo-chat') === 0) page = 'weibo';
  else if (p.indexOf('/taobao') === 0) page = 'taobao';
  else if (p.indexOf('/signin') === 0) page = 'signin';
  else if (p.indexOf('/starsky') === 0) page = 'starsky';
  else if (p.indexOf('/spark') === 0) page = 'spark';
  else if (p.indexOf('/douyin-chat') === 0) page = 'douyin-chat';
  document.documentElement.setAttribute('data-mp-page', page);

  var state = {
    theme: localStorage.getItem('mp-theme') || 'light',
    lang: localStorage.getItem('mp-lang') || 'zh'
  };
  var LANG_ONLY = !!window.__MP_UI_LANG_ONLY__;

  // ── 词典（按完整文本精确匹配，动态数字用下方规则） ──────────────────
  var DICT = {
    // 页面标题
    '多平台工具': 'Multi-tool', '微博聊天记录导出': 'Weibo Chat Export',
    '淘宝旺旺聊天记录导出': 'Taobao Chat Export', '一键签到': 'Daily Check-in',
    'StarSky 签到': 'StarSky Check-in', '抖音聊天记录': 'Douyin Chat History',
    '抖音火花助手': 'Douyin Spark', '微博聊天记录': 'Weibo Chat History',
    '淘宝聊天记录': 'Taobao Chat History', '抖音导出控制台': 'Douyin Export Console',
    '抖音聊天记录导出': 'Douyin Chat Export',

    // 通用
    '账号': 'Account', '新建账号': 'New account', '重命名': 'Rename', '删除': 'Delete',
    '默认账号': 'Default account',
    '登录状态': 'Login status', '检测登录状态': 'Check login', '检测中…': 'Checking…',
    '已登录': 'Logged in', '未登录': 'Not logged in', '未检测': 'Not checked',
    '检测失败': 'Check failed', '请扫码登录': 'Scan to log in',
    '空闲': 'Idle', '运行中': 'Running', '等待任务…': 'Waiting for tasks…',
    '清屏': 'Clear', '运行日志': 'Run log', '日志': 'Log', '停止': 'Stop',
    '已保存': 'Saved', '未安装': 'Not installed', '已就绪': 'Ready', '无': 'None',
    '可用': 'Available', '需全量重建': 'Full rebuild required',
    '退出码': 'Exit code', '启动时间': 'Started at', '导出时间': 'Exported at',
    '检测时间': 'Checked at', '媒体文件': 'Media files', '增量基线': 'Incremental baseline',
    '当前任务': 'Current task', '最近导出': 'Last export', '导出控制台': 'Export console',
    '聊天记录': 'Chat history', '返回首页': 'Back', '开始导出': 'Start export',
    '全量重建（不合并旧记录）': 'Full rebuild (do not merge)',
    '还没有导出记录': 'No export yet',
    '还没有导出记录，先在「导出控制台」导出一次。': 'No export yet. Export once in the console first.',
    '登录 / 导出 / 查看都只作用于当前账号': 'Login, export and viewing apply to the current account only.',
    '请扫码登录': 'Scan to log in', '微信': 'WeChat',
    '上次成功': 'Last run succeeded', '上次失败': 'Last run failed',
    '导出中': 'Exporting', '登录中': 'Logging in', '全量导出中': 'Full export',
    '等待': 'Waiting', '排队': 'Queued', '执行中…': 'Running…', '执行': 'Run',
    '完成': 'Done', '失败': 'Failed', '部分失败': 'Partially failed', '跳过': 'Skipped',
    '保存': 'Save', '保存 *': 'Save *', '保存设置': 'Save settings', '取消': 'Cancel',
    '关闭': 'Close', '知道了': 'Got it', '再试一次': 'Try again', '刷新': 'Refresh',
    '刷新页面，点列表里第一个请求': 'Refresh the page and click the first request in the list',
    '刷新页面，点列表里第一个请求（一般是 weibo.com 这个文档请求）': 'Refresh the page and click the first request in the list (usually the weibo.com document).',
    '在这里粘贴 Cookie…': 'Paste Cookie here…',
    '用': 'Use ', '登录': 'Log in', '退出程序': 'Quit app', '状态': 'Status',
    '配置': 'Configuration', '未知': 'Unknown', '闪退': 'Crashed',
    '成功': 'Success', '错误': 'Error', '提示': 'Notice', '警告': 'Warning',
    '是': 'Yes', '否': 'No',
    '全量重建（不合并旧记录）': 'Full rebuild (do not merge)',

    // 微博控制台
    '微博聊天记录导出': 'Weibo Chat Export',
    '私信文字、图片、语音、视频本地化，生成离线 HTML 浏览页': 'Localize DMs (text, images, voice, video) into an offline HTML viewer.',
    '登录态文件': 'Session file', '登录微博': 'Log in to Weibo',
    '微博聊天记录': 'Weibo chat history',
    '新账号备注名（留空自动命名）': 'Account name (leave blank to auto-name)',
    '修改账号备注名': 'Rename account',
    '账号已创建。点「登录微博」扫码登录后即可使用。': 'Account created. Click "Log in to Weibo" and scan the QR code to start.',
    '全量重建会重新拉取所有会话（可长达数分钟），并替换已有记录，确定继续？': 'Full rebuild will re-fetch all conversations (may take minutes) and replace existing records. Continue?',
    '将打开有头 Chrome 窗口显示微博二维码，请在窗口内扫码。继续？': 'A Chrome window will open with a Weibo QR code. Scan it to continue.',
    '首次使用先登录微博；默认增量导出只补新消息。导出会打开无头 Chrome 复用登录态，登录和导出期间请勿重复触发。': 'Log in to Weibo first. Incremental export only adds new messages. Export uses headless Chrome with your saved session; do not start it twice.',
    '切换失败': 'Switch failed', '新建失败': 'Create failed', '重命名失败': 'Rename failed',
    '删除失败': 'Delete failed', '启动导出失败': 'Failed to start export',
    '启动登录失败': 'Failed to start login', '检测登录状态失败': 'Failed to check login',
    '读取概览失败': 'Failed to load overview', '日志轮询中断': 'Log polling interrupted.',
    '确定删除账号「': 'Delete account "',
    '」？会同时删除它的登录态和导出数据，不可恢复。': '"? Its session and exported data will be deleted permanently.',

    // 淘宝控制台
    '淘宝旺旺聊天记录导出': 'Taobao Chat Export',
    '买家侧聊天记录 · 自动获取会话或按链接导出，生成离线 HTML 浏览页': 'Buyer-side chats: auto-discover conversations or export by link, with an offline HTML viewer.',
    '下载原始 JSON': 'Download raw JSON', '下载油猴脚本': 'Download userscript',
    '会话来源': 'Conversation source',
    '自动获取账号内全部会话（推荐）': 'Auto-discover all conversations (recommended)',
    '自动获取账号内全部会话': 'Auto-discover all conversations',
    '取消自动获取后，把淘宝聊天窗口的地址粘进来，每行一个，例如：': 'When auto-discovery is off, paste Taobao chat URLs here, one per line, e.g.:',
    '登录淘宝': 'Log in to Taobao', '淘宝聊天记录': 'Taobao chat history',
    '淘宝账号': 'Taobao account', '账号 uid': 'Account UID', '原始数据': 'Raw data',
    '请先登录淘宝': 'Please log in to Taobao first',
    '请勾选自动获取，或粘贴至少一个淘宝聊天链接': 'Enable auto-discovery or paste at least one Taobao chat link.',
    '账号已创建。点「登录淘宝」完成登录后即可使用。': 'Account created. Click "Log in to Taobao" to finish signing in.',
    '全量重建会用本次导出的内容替换已有记录，确定继续？': 'Full rebuild will replace existing records with this export. Continue?',
    '将打开浏览器窗口，请在窗口里完成淘宝登录。继续？': 'A browser window will open. Please complete the Taobao login there. Continue?',
    '取消自动获取后，把淘宝聊天窗口的地址粘进来，每行一个，例如：\nhttps://market.m.taobao.com/app/im/chat/index.html?&uid=cntaobao卖家旺旺名&type=web': 'When auto-discovery is off, paste Taobao chat URLs here, one per line, e.g.:\nhttps://market.m.taobao.com/app/im/chat/index.html?&uid=<seller>&type=web',
    '勾选自动获取时，会读取淘宝聊天列表里的全部会话逐个导出，不用再复制链接；取消勾选则只导出下面粘贴的聊天链接。': 'With auto-discovery on, every conversation in the Taobao chat list is exported one by one, so there is no need to copy links. Turn it off to export only the links pasted below.',
    '首次使用先点「登录淘宝」，扫码或账号密码登录一次即可；以后导出会自动复用登录态。': 'For first-time use, click "Log in to Taobao" and sign in once (QR code or password). Later exports reuse the saved session automatically.',
    '增量导出会把新消息合并进已有记录，不会重复；导出期间会自动打开浏览器加载历史，请勿关闭该窗口。': 'Incremental export merges new messages into existing records without duplicates. A browser window opens to load history during export; do not close it.',

    // 一键签到
    '一键签到': 'Daily Check-in',
    '微博 / 贴吧 / 吾爱': 'Weibo / Tieba / 52pojie',
    '微博超话 · 吾爱破解 · 百度贴吧 · StarSky 桌面签到　本地运行，Cookie 不出本机': 'Weibo Super Topic · 52pojie · Baidu Tieba · StarSky desktop check-in — runs locally, cookies never leave this machine.',
    '全部执行': 'Run all', '就绪': 'Ready', '点上面的按钮开始': 'Click a button above to start',
    'Cookie 存在': 'Cookies are stored in ', '，只在本机读写': ' and only read/written locally',
    'Cookie 一般能用很久，失效了重新复制一次就行 · ': 'Cookies usually last a long time; copy them again if they expire · ',
    '微博超话': 'Weibo Super Topic', '吾爱破解': '52pojie', '百度贴吧': 'Baidu Tieba',
    '超话签到 · 转发 · 评论': 'Super-topic check-in · repost · comment',
    '每日任务签到（走浏览器）': 'Daily task check-in (browser)',
    '批量签所有关注吧': 'Check in to all followed forums',
    '未运行': 'Not running', '如何获取 Cookie？': 'How to get Cookie?',
    '扫码登录': 'QR login', '签到': 'Check in',
    '微博扫码登录': 'Weibo QR login', '正在打开微博登录页…': 'Opening Weibo login…',
    '微博登录二维码': 'Weibo login QR code',
    '等浏览器窗口弹出微博登录页（第一次用会新开一个窗口）': 'Wait for the browser to open the Weibo login page (a new window the first time).',
    '用微博 App 扫页面上的二维码，或在右边弹窗里扫': 'Scan the QR code on the page with the Weibo app, or scan it in the dialog.',
    '手机上点确认后，这里会自动保存 Cookie，不用手动复制': 'Confirm on your phone; the Cookie will be saved automatically.',
    '正在打开浏览器…': 'Opening browser…', '浏览器已打开，在里面登录完、关掉窗口就行': 'Browser opened. Finish logging in there, then close the window.',
    '微博附加动作': 'Weibo extra actions', '自动转发': 'Auto repost',
    '转 TA 主页的最新原创微博，已经转过的自动跳过': 'Repost the latest original post from their profile; skip already reposted ones.',
    '转发条数': 'Repost count', '博主昵称': 'Blogger nickname',
    '例如 封茗囧菌': 'e.g. Fengming Jiongjun',
    '选填，留空会按昵称自动搜索': 'Optional; leave blank to search by nickname.',
    '话题筛选': 'Topic filter',
    '选填，只转正文里带这个词的': 'Optional; only repost posts containing this word.',
    '转发附言（每行一条，随机挑）': 'Repost comment (one per line, picked randomly)',
    '或者直接指定微博链接（每行一条，填了就不用去超话抓）': 'Or specify Weibo links directly (one per line; no need to scrape the super topic).',
    '自动评论': 'Auto comment',
    '在指定的一条微博下面连评几条': 'Post several comments under a chosen Weibo post.',
    '评论条数': 'Comment count', '水贴链接': 'Post URL',
    '评论内容（每行一条，按顺序循环用）': 'Comments (one per line, cycled in order)',
    '博主点赞': "Like blogger's posts",
    '在博主本人的原创微博里挑一条没点过的点赞': "Like an original post by the blogger that hasn't been liked.",
    '指定博主（留空＝转发目标博主，可填昵称或 UID）': 'Target blogger (blank = repost target; nickname or UID)',
    '只点博主本人发的微博；点过的记在': "Only likes the blogger's own posts; liked posts are recorded in ",
    '，不会重复点同一条。': ' to avoid duplicates.',
    '粉丝群消息': 'Fan group message',
    '在指定粉丝群里发一条消息，完成「消息互动」任务': 'Send a message in a fan group to complete the "message interaction" task.',
    '群名称': 'Group name', '发什么': 'Message',
    '每日私聊问候': 'Daily DM greeting',
    '按早/午/晚自动挑一条发给博主，每天只发一次': 'Automatically sends one message to the blogger (morning/noon/evening), once per day.',
    '发给谁': 'Recipient', '05-11点': '05:00–11:00', '11-14点': '11:00–14:00',
    '其他时间': 'Other times',
    '今天已经发过的话会记在': 'Sent messages are recorded in ',
    '，当天再跑不会重复发。': ' to avoid duplicates on the same day.',
    '铁粉打卡': 'Fan check-in',
    '打开手机版铁粉页，点「今日」打卡并领奖励（固定放最后，会弹浏览器）': 'Opens the mobile fan page, clicks "Today" and claims rewards (always last; opens a browser).',
    '博主 UID': 'Blogger UID', '你的 UID': 'Your UID',
    '打开铁粉页时地址栏形如：': 'The fan page URL looks like:',
    '公开留下内容': 'publicly visible content',
    '转发和评论会在你的微博上': 'Reposts and comments leave ',
    '，微博对这类自动操作的风控比签到严格得多。': " on your Weibo; Weibo's risk control is much stricter than for check-ins.",
    '，微博对这类自动操作的风控比签到严格得多。 频率太高可能触发验证码甚至限制账号。建议条数少一点、间隔长一点、别一天跑很多次。': " on your Weibo; Weibo's risk control is much stricter than for check-ins. Too many actions may trigger captchas or account limits. Keep counts low, intervals long, and avoid many runs per day.",
    '频率太高可能触发验证码甚至限制账号。建议条数少一点、间隔长一点、别一天跑很多次。': 'Too many actions may trigger captchas or account limits. Keep counts low, intervals long, and avoid many runs per day.',
    'Cookie 等于你的登录凭证，别发给别人。本程序只在你自己电脑上用它。': 'Cookies are your login credentials—never share them. This app only uses them on your own computer.',
    '怎么拿 Cookie': 'How to get Cookie',
    '用微博 App 扫码，自动拿 Cookie': 'Scan with the Weibo app to get Cookie automatically',
    '想手动弄也行：浏览器打开': 'Or manually: open',
    '并确认已登录': 'in your browser and make sure you are logged in',
    '打开开发者工具，切到': 'Open developer tools and switch to the',
    '里找到': 'and find the',
    '这一行': 'row',
    '整行内容（不含 "Cookie:" 这几个字）复制过来，粘到上面的输入框': 'Copy the whole line (without "Cookie:") and paste it above.',
    '整行内容（不含 "Cookie:" 这几个字）复制过来': 'Copy the whole line (without "Cookie:").',
    '里必须带': 'must contain the',
    '字段，否则转发/评论/点赞会被微博拒绝；': ' field, or reposts/comments/likes will be rejected by Weibo.',
    '没带上就再复制一次（F12 → 应用程序 → Cookie → weibo.com 里能看到它）': 'If missing, copy it again (F12 → Application → Cookies → weibo.com).',
    '用浏览器打开': 'Open',
    '整行内容复制过来。里面要包含': 'Copy the whole line. It must contain',
    '，有这个字段才会走客户端批量签到（经验更多、速度快）': '; this enables client-side batch check-in (more points, faster).',
    '整行内容复制过来': 'Copy the whole line.',
    'Cookie 已保存': 'Cookie saved', 'Cookie 已自动保存': 'Cookie saved automatically',
    '提醒：Cookie 里没有 XSRF-TOKEN，转发/评论/点赞会失败': 'Warning: Cookie has no XSRF-TOKEN; reposts/comments/likes will fail.',
    '保存失败': 'Save failed',
    '保存失败：程序可能已经退出了，重新双击启动签到.bat': 'Save failed: the app may have exited. Restart it with 启动签到.bat.',
    '启动失败': 'Start failed', '打不开浏览器': 'Cannot open browser',
    '打不开登录流程：程序可能已经退出了': 'Cannot open the login flow: the app may have exited.',
    '打开失败：程序可能已经退出了': 'Failed to open: the app may have exited.',
    '统一程序已退出，这个页面可以关掉了': 'The app has exited; you can close this page.',
    '确定要退出统一程序吗？退出后其他工具也会一起停止。': 'Quit the unified app? Other tools will stop too.',
    '转发 / 评论设置': 'Repost / comment settings',
    '转发 / 评论设置已保存': 'Repost / comment settings saved.',
    '请选择时间': 'Please select a time',

    // StarSky
    'StarSky 签到': 'StarSky Check-in',
    '截屏识别签到按钮并模拟点击，签到后可自动关闭客户端': 'Detects the check-in button on screen and clicks it; can close the client afterwards.',
    '运行环境': 'Environment', '任务检测中': 'Checking task',
    '计划任务': 'Scheduled task', '下次运行': 'Next run', '软件路径': 'App path',
    '最近签到': 'Last check-in', '文件更新': 'File updated', '最后一条': 'Last line',
    '启动方式': 'Launch method',
    '软件路径（留空自动识别）': 'App path (blank = auto-detect)',
    '自动启动': 'Auto start', '等待超时（秒）': 'Timeout (s)',
    '签到后关闭软件': 'Close app after check-in',
    '保存配置': 'Save config', '立即签到': 'Check in now',
    '签到期间会抢占鼠标和前台窗口': 'Check-in takes over the mouse and foreground window.',
    '每日自动签到': 'Daily auto check-in',
    '创建 / 更新任务': 'Create / update task', '删除任务': 'Delete task',
    '下次运行时间': 'Next run time',
    '任务以最高权限运行；创建/删除需管理员权限': 'Task runs with highest privileges; creating/deleting requires administrator rights.',
    '签到日志': 'Check-in log', '暂无日志': 'No logs yet',
    '普通权限': 'Standard user', '管理员': 'Administrator',
    '未创建': 'Not created', '计划任务已建': 'Task created', '无计划任务': 'No scheduled task',
    '已找到': 'Found', '未配置': 'Not configured', '直接运行': 'Run now',
    '确定删除计划任务「StarSky自动签到」？': 'Delete the scheduled task "StarSky Auto Check-in"?',
    '签到会抢占鼠标和前台窗口，并可能关闭 StarSky。继续？': 'Check-in will take over the mouse and foreground window and may close StarSky. Continue?',
    '操作失败': 'Operation failed', '无法连接后端': 'Cannot reach backend',
    '请求失败': 'Request failed', '等待超时': 'Timeout',

    // 查看器 / 通用动态
    '选择一个会话': 'Select a conversation',
    '从左侧选择一个会话查看聊天记录': 'Select a conversation on the left to view messages',
    '← 从左侧选择一个会话查看聊天记录': '← Select a conversation on the left to view messages',
    '搜索全部聊天内容…': 'Search all messages…',
    '更新本会话': 'Update this chat', '导出本会话': 'Export this chat',
    '导出全部文本': 'Export all text', '更新中…': 'Updating…',
    '这个会话没有可导出的消息': 'No messages to export in this conversation',
    '该消息已被撤回': 'This message was recalled',
    '非文本消息': '[non-text message]', '[非文本消息]': '[non-text message]',
    '原始 amr 文件': 'Original AMR file',
    '群': 'Group', '我': 'Me', '今天': 'Today', '昨天': 'Yesterday',
    '暂无消息': 'No messages', '没有找到匹配的消息': 'No matching messages',
    '搜索': 'Search', '个会话': 'conversations', '条消息': 'messages',
    '请先选择一个会话': 'Select a conversation first.',
    '这个会话没有可用的用户 ID，无法更新': 'This conversation has no user ID and cannot be updated.',
    '启动更新失败': 'Failed to start update.',
    '更新失败，请查看导出控制台日志': 'Update failed. Check the log in the export console.',
    '更新超时': 'Update timed out.',
    '更新失败': 'Update failed.',

    // 抖音火花助手
    '首页': 'Home', '好友': 'Friends', '好友分组': 'Friend groups', '定时': 'Schedules',
    '定时任务': 'Scheduled tasks', '火花日历': 'Spark calendar', '设置': 'Settings',
    '后台配置': 'Admin', '抖音火花助手': 'Douyin Spark', '火花筛选': 'Spark filter',
    '抖音': 'Douyin', '登录': 'Log in', '登 录': 'Log in', '退出登录': 'Log out',
    '初始化浏览器': 'Initialize browser', '初始化中...': 'Initializing...',
    '已初始化': 'Initialized', '未初始化': 'Not initialized',
    '刷新好友列表': 'Refresh friends', '刷新记录': 'Refresh messages',
    '刷新风控状态': 'Refresh risk state', '刷新验证码': 'Refresh code',
    '刷新': 'Refresh', '加载中...': 'Loading...', '准备中': 'Preparing',
    '正在加载好友…': 'Loading friends…', '加载消息中...': 'Loading messages...',
    '加载表情包中...': 'Loading stickers...', '暂无好友': 'No friends',
    '暂无好友数据': 'No friend data', '暂无定时任务': 'No scheduled tasks',
    '没有匹配的好友': 'No matching friends', '暂无消息记录，发送一条消息开始对话': 'No messages yet. Send one to start.',
    '选择好友开始聊天': 'Select a friend to start chatting',
    '输入消息，回车发送...': 'Type a message, press Enter to send...',
    '发送消息': 'Send message', '发送': 'Send', '发送成功': 'Sent', '发送失败': 'Send failed',
    '发送内容': 'Message', '发送进度': 'Send progress', '开始发送': 'Start sending',
    '停止发送': 'Stop sending', '正在发送…': 'Sending…', '正在准备发送…': 'Preparing to send…',
    '表情': 'Sticker', '表情包': 'Stickers', '文字': 'Text', '文字+表情': 'Text + sticker',
    '选择表情': 'Choose sticker', '选择要发送的表情': 'Choose a sticker to send',
    '未获取到表情包': 'No stickers available', '获取表情包失败': 'Failed to load stickers',
    '请先选择一个表情': 'Choose a sticker first.', '还没有选择表情': 'No sticker selected',
    '请先选择好友': 'Select a friend first.', '请选择好友': 'Select friends',
    '创建任务': 'Create task', '添加任务': 'Add task', '创建定时任务': 'New scheduled task',
    '定时任务管理': 'Scheduled task management', '批量创建': 'Batch create',
    '批量创建定时任务': 'Batch create scheduled tasks', '应用到任务': 'Apply to tasks',
    '分组': 'Group', '分组名称': 'Group name', '新建分组': 'New group',
    '编辑分组': 'Edit group', '删除分组': 'Delete group', '加入分组': 'Add to group',
    '目标分组': 'Target group', '选择分组': 'Select group', '分组筛选': 'Filter by group',
    '暂无成员，点「勾选成员」添加': 'No members. Click "Select members" to add.',
    '勾选成员': 'Select members', '保存成员': 'Save members',
    '还没有分组': 'No groups yet', '还没有分组，请先到「好友分组」页新建': 'No groups yet. Create one on the Friend groups page.',
    '全选当前结果': 'Select all results', '全选筛选': 'Select filtered', '清空': 'Clear',
    '多选': 'Multi-select', '批量删除': 'Batch delete', '批量立即执行': 'Run selected now',
    '立即执行': 'Run now', '改时间': 'Change time',
    '删除': 'Delete', '编辑': 'Edit', '创建': 'Create', '添加': 'Add', '保存': 'Save',
    '取消': 'Cancel', '关闭': 'Close', '确认': 'Confirm', '修改': 'Change', '切换': 'Switch',
    '重命名账号': 'Rename account', '删除账号': 'Delete account', '添加新账号': 'Add account',
    '切换抖音账号': 'Switch Douyin account', '点击切换抖音账号': 'Click to switch Douyin account',
    '当前账号': 'Current account', '账号备注': 'Account name', '账号备注名': 'Account name',
    '请填写账号备注名': 'Enter an account name.', '请输入新的备注名': 'Enter a new account name.',
    '重新登录当前账号': 'Log in again', '前往登录': 'Go to login', '手动登录': 'Manual login',
    '浏览器登录': 'Browser login', '验证码登录': 'Code login', '登录方式': 'Login method',
    '登录状态': 'Login status', '已登录': 'Logged in', '未登录': 'Not logged in',
    '已弹出浏览器，请完成登录': 'Browser opened. Please finish logging in.',
    '请在浏览器窗口中完成抖音登录': 'Please finish Douyin login in the browser window.',
    '请使用抖音App扫码登录': 'Scan the QR code with the Douyin app.',
    '抖音扫码登录': 'Douyin QR login', '登录二维码': 'Login QR code',
    '登录成功': 'Logged in', '登录失败': 'Login failed', '登录失败，请重试': 'Login failed. Try again.',
    '登录失败，Cookie无效': 'Login failed: invalid Cookie.', '登录失败，请检查Cookie': 'Login failed. Check the Cookie.',
    '登录已过期': 'Login expired', '登录已过期，请重新登录': 'Login expired. Please log in again.',
    '已登录': 'Logged in', '已强制退出登录': 'Forced logout complete',
    '确定要退出登录吗？': 'Log out?', '退出登录（管理后台）': 'Log out (admin)',
    '今日发送': 'Sent today', '今日计划': "Today's plan", '最近执行': 'Recent runs',
    '最近结果': 'Recent result', '下次执行': 'Next run', '执行时间': 'Run time',
    '发布时间': 'Send time', '内容 / 失败原因': 'Content / failure reason',
    '操作': 'Actions', '序号': 'No.', '类型': 'Type', '来源': 'Source',
    '头像': 'Avatar', '昵称': 'Nickname', '手机号': 'Phone', '结果': 'Result',
    '成功': 'Success', '失败': 'Failed', '取消登录': 'Cancel login', '登录中...': 'Logging in...',
    '等待登录': 'Waiting for login', '已取消登录': 'Login cancelled', '刷新成功': 'Refreshed',
    '刷新失败': 'Refresh failed', '刷新失败，请确保浏览器已初始化': 'Refresh failed. Make sure the browser is initialized.',
    '加载好友失败，请确认浏览器已初始化并登录': 'Failed to load friends. Make sure the browser is initialized and logged in.',
    '加载全部好友': 'Load all friends', '更新好友数据': 'Update friend data',
    '重新获取昵称': 'Refresh nicknames', '重新获取昵称失败': 'Failed to refresh nicknames',
    '系统设置': 'System settings', '系统信息': 'System info', '防封策略': 'Anti-ban policy',
    '风险状态': 'Risk status', '熔断阈值': 'Failure threshold', '去重天数': 'Dedupe days',
    '随机延迟分钟': 'Random delay (min)', '保存策略': 'Save policy',
    '恢复自动任务': 'Resume scheduled tasks', '自动任务': 'Scheduled sending',
    '自动任务正常': 'Scheduled sending is healthy', '自动任务已暂停': 'Scheduled sending is paused',
    '已暂停': 'Paused', '连续失败': 'Consecutive failures', '最近成功': 'Last success',
    '暂无': 'None yet', '任务默认值': 'Task defaults', '默认发布时间': 'Default send time',
    '默认消息': 'Default message', '消息模板池': 'Message template pool',
    '一行一个模板，或使用 || 分隔。留空会回退到默认模板。': 'One template per line, or separate with ||. Leave blank to use defaults.',
    '新建任务、新建分组时自动带上': 'Used automatically for new tasks and groups',
    '系统版本': 'System version', '运行状态': 'Run status', '运行时间': 'Uptime',
    '上次登录IP': 'Last login IP', '后端地址': 'Backend URL', '服务端口': 'Service port',
    '浏览器内核': 'Browser engine', '浏览器状态': 'Browser status', '浏览器显示': 'Browser display',
    '浏览器截图': 'Browser screenshot', '保存浏览器': 'Save browser settings',
    '显示窗口': 'Show window', '隐藏（无头）': 'Hidden (headless)', '隐藏': 'Hidden',
    '无头模式易崩溃，建议显示窗口': 'Headless mode is unstable; showing the window is recommended.',
    '调试功能': 'Debug tools', '安全确认': 'Security confirmation',
    '请输入密码以确认使用调试功能': 'Enter the password to use debug tools.',
    '强制登录状态': 'Force login state', '强制退出登录': 'Force logout',
    '快速操作': 'Quick actions', '管理任务': 'Manage tasks', '发起聊天': 'Start chat',
    '火花天数': 'Spark days', '有火花': 'Spark active', '无火花': 'No spark',
    '有记录天数': 'Days with records', '本月打卡成功': 'Check-ins this month',
    '覆盖好友': 'Friends covered', '失败次数': 'Failures', '打卡明细': 'Check-in details',
    '时间': 'Time', '手动': 'Manual', '自动': 'Auto', '来源': 'Source',
    '这一天没有记录': 'No records for this day',
    '点击有记录的日子查看明细；记录从升级后开始累积。': 'Click a day with records to see details; records accumulate after the upgrade.',
    '打卡明细': 'Check-in details', '当前': 'Current', '默认': 'Default', '全部': 'All',
    '未分组': 'Ungrouped', '未知用户': 'Unknown user', '名称未获取': 'Name unavailable',
    '人': 'people', '次': 'times', '天': 'days',
    '一': 'Mon', '二': 'Tue', '三': 'Wed', '四': 'Thu', '五': 'Fri', '六': 'Sat', '日': 'Sun',
    '提示': 'Notice', '稍后': 'Later', '网络错误': 'Network error',
    '服务器内部错误': 'Internal server error', '请求失败': 'Request failed',
    '请求的资源不存在': 'Requested resource not found',
    '无法连接到服务器，请检查后端服务是否启动': 'Cannot reach the server. Check that the backend is running.',
    '修改密码': 'Change password', '原密码': 'Current password', '新密码': 'New password',
    '确认密码': 'Confirm password', '请输入密码': 'Enter password', '请输入原密码': 'Enter current password',
    '请输入新密码': 'Enter new password', '密码不能为空': 'Password cannot be empty.',
    '新密码不能与原密码相同': 'The new password must differ from the current one.',
    '密码修改成功': 'Password changed.', '修改成功': 'Changed.', '修改失败': 'Change failed.',
    '保存成功': 'Saved.', '保存失败': 'Save failed.', '已保存': 'Saved',
    '创建成功': 'Created.', '创建失败': 'Create failed.', '删除成功': 'Deleted.',
    '删除失败': 'Delete failed.', '切换成功': 'Switched.', '切换失败': 'Switch failed.',
    '添加成功': 'Added.', '添加失败': 'Add failed.', '已删除': 'Deleted',
    '已应用': 'Applied', '应用完成': 'Applied', '成员已保存': 'Members saved.',
    '分组已创建': 'Group created.', '已重命名': 'Renamed.', '重命名失败': 'Rename failed.',
    '默认值已保存': 'Defaults saved.', '刷新记录': 'Refresh messages',
    '登录已过期，请重新登录': 'Login expired. Please log in again.',
    '加载任务列表失败': 'Failed to load tasks.', '加载分组失败': 'Failed to load groups.',
    '获取失败': 'Failed to get.', '获取失败，密码错误': 'Failed: wrong password.',
    '获取截图失败': 'Failed to get screenshot.', '复制失败': 'Copy failed.',
    '对方': 'Other party',

    // 抖音聊天查看器
    '抖音聊天记录': 'Douyin Chat History', '消息': 'Messages', '任务': 'Tasks',
    '会话': 'Conversations', '火花': 'Spark', '未命名': 'Unnamed',
    '密码': 'Password', '请输入密码': 'Enter password', '密码错误': 'Wrong password',
    '查找聊天记录': 'Search messages', '⌕ 查找聊天记录': '⌕ Search messages', '关闭查找': 'Close search',
    '搜索当前会话': 'Search this conversation', '搜索当前会话...': 'Search this conversation...',
    '清空搜索': 'Clear search', '输入关键词开始搜索': 'Type a keyword to search',
    '返回搜索': 'Back to search', '按日期查找': 'By date', '图片与视频': 'Images & videos',
    '图片': 'Images', '视频': 'Videos', '无匹配结果': 'No matches',
    '请选择图片或视频': 'Select images or videos',
    '当前会话暂无聊天记录': 'No messages in this conversation',
    '这一天没有聊天记录': 'No messages on this day',
    '搜索失败，请重试': 'Search failed. Try again.',
    '加载失败，请重试': 'Load failed. Try again.',
    '日历加载失败，请重试': 'Failed to load calendar. Try again.',
    '日期定位失败，请重试': 'Failed to locate date. Try again.',
    '搜索会话...': 'Search conversations...', '会话列表': 'Conversations',
    '初始化': 'Initialize', '初始化中…': 'Initializing…', '初始化失败': 'Initialization failed',
    '初始化超时，请稍后重试': 'Initialization timed out. Try again later.',
    '浏览器就绪': 'Browser ready', '检测中…': 'Checking…',
    '检测火花浏览器状态': 'Check spark browser status',
    '初始化火花浏览器（发消息 / 续火花共用）': 'Initialize spark browser (shared by messaging and spark)',
    '正在启动火花浏览器': 'Starting spark browser',
    '火花浏览器已就绪（发消息 / 续火花共用）': 'Spark browser ready (shared by messaging and spark)',
    '火花浏览器已就绪（发消息 / 续火花共用），点击可重新检测': 'Spark browser ready (shared by messaging and spark). Click to re-check.',
    '浏览器在线': 'Browser online',
    '删除会话': 'Delete conversation', '删除该会话数据': 'Delete conversation data', '删除该会话记录': 'Delete this conversation record',
    '删除中...': 'Deleting...', '暂无会话数据': 'No conversations',
    '确定删除会话「': 'Delete all data for conversation "', '」的所有数据？': '"?', '正在更新…': 'Updating…', '更新完成': 'Updated.',
    '只删除本机导出记录，不影响账号里的聊天。': 'Only the local export is deleted; chats in the account are untouched.',
    '这个会话缺少唯一 ID，无法删除': 'This conversation has no unique ID and cannot be deleted.',
    '读取导出数据失败': 'Failed to read export data', '写入导出数据失败': 'Failed to write export data',
    '会话不存在，可能已被删除': 'Conversation not found; it may have been deleted.',
    '任务进行中，请先停止再删除记录': 'A task is running. Stop it before deleting records.',
    '更新已停止': 'Update stopped.',
    '更新超时，请到导出控制台查看日志': 'Update timed out. Check the log in the export console.',
    '加载消息失败': 'Failed to load messages.', '消息响应格式错误': 'Invalid message response.',
    '↻ 更新聊天记录': '↻ Update messages',
    '只增量抓取当前会话': 'Incrementally fetch this conversation only',
    '选择哪个是你自己': 'Which one is you?',
    '选择后消息会区分左右显示': 'Messages will be aligned left/right after your choice.',
    '清除选择': 'Clear selection', '选择一个会话查看聊天记录': 'Select a conversation to view messages',
    '发送中…': 'Sending…', '正在发送到抖音…': 'Sending to Douyin…',
    '已发送并备份到本地': 'Sent and backed up locally.',
    '已发送，正在备份到本地记录…': 'Sent; backing up locally…',
    '已发送，但本地备份失败：': 'Sent, but local backup failed: ',
    '本地备份失败': 'Local backup failed', '正在检查火花浏览器…': 'Checking spark browser…',
    '火花浏览器初始化失败': 'Spark browser initialization failed',
    '火花浏览器初始化超时，请稍后重试': 'Spark browser initialization timed out. Try again later.',
    '深蓝': 'Deep blue', '微信': 'WeChat', '浅色': 'Light', '暖棕': 'Warm brown', '紫夜': 'Purple night',
    '收起会话列表': 'Collapse conversations', '展开会话列表': 'Expand conversations',
    '收起火花': 'Hide spark', '火花详情': 'Spark details',
    '管理账号': 'Manage accounts', '群成员': 'Group members',
    '用户名片 · 查看主页': 'Profile card · View profile', '用户头像': 'Avatar',
    '抖音号：': 'Douyin ID: ', '粉丝：': 'Followers: ',
    '引用的视频': 'Quoted video', '引用的消息不在当前记录中': 'The quoted message is not in the current records.',
    '引用的消息未归档或加载失败，无法定位': 'The quoted message is not archived or failed to load.',
    '引用的消息未归档，无法定位': 'The quoted message is not archived.',
    '已撤回': 'Recalled', '复制': 'Copy', '关闭聊天记录': 'Close chat history',
    '· 点击查看': '· Click to view', '重试': 'Retry', '关闭': 'Close',
    '续火花任务': 'Spark task', '今日状态': "Today's status",
    '今日失败': 'Failed today', '今日已续': 'Renewed today', '今日待续': 'Due today',
    '今日无记录': 'No record today', '最近发送': 'Recent sends',
    '还没有发送记录': 'No sends yet', '暂无火花记录': 'No spark records',
    '连续火花': 'Consecutive spark', '读取中…': 'Loading…',
    '读取火花数据失败': 'Failed to load spark data', '未设置': 'Not set',
    '选择一个会话查看火花详情': 'Select a conversation to view spark details',
    '火花详情': 'Spark details', '🔥 火花详情': '🔥 Spark details',
    '数据来自火花助手；续火花/改时间在任务管理里操作。': 'Data comes from Spark; renew or reschedule in Task management.',
    '任务管理': 'Task management', '今日无记录': 'No record today',

    // 抖音外层壳 / 账号中心
    '抖音 · 聊天记录与火花': 'Douyin · Chats & Spark',
    '聊天记录 · 火花': 'Chats · Spark', '抖音导出控制台': 'Douyin export console',
    '登录 / 添加账号': 'Log in / Add account', '账号管理': 'Account management',
    '切换': 'Switch', '当前': 'Current', '刷新列表': 'Refresh list',
    '账号数据保存在本机': 'Account data is stored locally',
    '登录态（Cookie）、聊天记录、火花任务都按账号隔离，切换后两个页面一起生效。': 'Session (Cookie), chats and spark tasks are isolated per account and apply to both pages after switching.',
    '还没有账号。点下方「登录 / 添加账号」扫码添加。': 'No accounts yet. Click "Log in / Add account" below to scan and add one.',
    '还没有账号，点击右上角「登录 / 添加账号」扫码登录。': 'No accounts yet. Click "Log in / Add account" at the top right.',
    '新的账号备注名（只改显示名，不影响登录态）': 'New account name (display name only; does not affect the session)',
    '会同时删除该账号的登录态、聊天记录和火花任务，不可恢复。': ' This will also delete the session, chats and spark tasks permanently.',
    '已切换账号，正在重新加载...': 'Account switched; reloading...',
    '正在切换账号...': 'Switching account...', '正在同步火花账号...': 'Syncing spark account...',
    '正在启动登录浏览器...': 'Opening the login browser...',
    '请在弹出的浏览器窗口中扫码登录': 'Scan the QR code in the opened browser window.',
    '登录成功，账号已保存': 'Logged in. Account saved.',
    '账号已删除': 'Account deleted.', '已重命名': 'Renamed.', '已取消登录': 'Login cancelled.',
    '账号读取失败': 'Failed to load accounts', '未读取到抖音号': 'Douyin ID unavailable',
    '返回多平台工具首页': 'Back to Multi-tool home', '抖音号': 'Douyin ID',
    '取消登录': 'Cancel login', '登录失败': 'Login failed', '请求失败': 'Request failed',
    '微博超话 · 获取 Cookie': 'Weibo Super Topic · Get Cookie',
    '吾爱破解 · 获取 Cookie': '52pojie · Get Cookie',
    '百度贴吧 · 获取 Cookie': 'Baidu Tieba · Get Cookie',
    '当前账号已登录。请输入新账号备注名（留空自动命名）': 'The current account is logged in. Enter a name for the new account (leave blank to auto-name).',
    '切换微博账号': 'Switch Weibo account', '切换淘宝账号': 'Switch Taobao account',
    '登录态、聊天记录和导出数据都按账号隔离，切换后立即生效。': 'Session, chats and export data are isolated per account and take effect immediately after switching.',
    '还没有账号，点击上方「登录 / 添加账号」。': 'No accounts yet. Click "Log in / Add account" above.',
    '还没有账号，点击下方「登录 / 添加账号」。': 'No accounts yet. Click "Log in / Add account" below.',
    '微博聊天记录': 'Weibo Chat History', '私信导出 · 离线浏览': 'DM export · Offline viewer',
    '淘宝旺旺记录': 'Taobao Chats', '买家侧聊天导出': 'Buyer chat export',
    '当前账号已登录。请输入新账号备注名（留空自动命名）': 'The current account is logged in. Enter a new account name (leave blank to auto-name).',
    '好友列表': 'Friends list', '启动自动化环境': 'Start automation environment',
    '添加或修改': 'Add or modify', '配置账户': 'Configure account',
    '待初始化': 'Not initialized', '好友数量': 'Friends',
    '👋 欢迎使用抖音火花助手': '👋 Welcome to Douyin Spark',
    '自动化管理你的抖音好友火花，保持联系不间断': 'Automate your Douyin friend sparks and stay in touch.',
    '系统会自动记录最近发送结果；达到连续失败阈值后，会自动暂停所有定时任务。': 'Recent send results are recorded; all scheduled tasks pause when the failure threshold is reached.',
    '定时任务在触发后随机延迟 0~N 分钟执行': 'Scheduled tasks run with a random delay of 0–N minutes.',
    '连续失败达到阈值后自动暂停任务': 'Pause tasks after consecutive failures reach the threshold.',
    '同一好友在该时间窗内尽量避免重复消息': 'Avoid duplicate messages to the same friend within this window.',
    '留空消息任务会优先从模板池中随机选取，并结合历史发送记录做去重': 'Blank messages are picked randomly from the template pool with history-based dedupe.',
    '默认消息内容': 'Default message', '保存默认值': 'Save defaults',
    '设置之后，手动建任务和新建分组都不用再重复填写时间和内容': 'After this, new tasks and groups reuse these defaults.',
    '修改后需重启后端生效': 'Restart the backend to apply.',
    '清空路径': 'Clear path', '显示': 'Show',
    'Linux 推荐选择 Chrome 或 Chromium；留空时自动查找系统已安装浏览器，修改后需重启后端生效': 'On Linux, Chrome or Chromium is recommended. Leave blank to auto-detect an installed browser; restart the backend after changes.',
    '暂未检测到异常，系统会自动记录发送结果并在连续失败时暂停任务。': 'No issues detected. Send results are recorded and scheduled sending pauses after repeated failures.',
    '个': 'items',
    '签到依赖屏幕像素识别与鼠标模拟：需要 StarSky 已登录、桌面未锁屏、窗口未被遮挡；': 'Check-in relies on screen-pixel recognition and mouse simulation: StarSky must be logged in, the desktop unlocked, and the window unobstructed. ',
    '计划任务存在时优先通过计划任务执行；否则普通权限启动本程序会请求 UAC 提权。首次使用建议先「创建每日任务」，再点「立即签到」验证。': 'If a scheduled task exists it is used first; otherwise the app requests UAC elevation. For first use, create the daily task and then run "Check in now" to verify.'
  };

  var RULES = [
    [/^(\d[\d,]*)\s*个会话$/, '$1 conversations'],
    [/^(\d[\d,]*)\s*条消息$/, '$1 messages'],
    [/^(\d[\d,]*)\s*个会话\s*·\s*([\d,]+)\s*条消息$/, '$1 conversations · $2 messages'],
    [/^(\d[\d,]*)\s*个会话，([\d,]+)\s*条消息$/, '$1 conversations, $2 messages'],
    [/^共\s*(\d[\d,]*)\s*条消息$/, '$1 messages'],
    [/^(\d[\d,]*)\s*条结果$/, '$1 results'],
    [/^(\d[\d,]*)\s*条$/, '$1 messages'],
    [/^(\d[\d,]*)\s*个$/, '$1 files'],
    [/^搜索\s*“(.+)”$/, 'Search “$1”'],
    [/^已登录（uid (.+)）$/, 'Logged in (uid $1)'],
    [/^已经登录淘宝（uid (.+)），无需重复登录$/, 'Already logged in to Taobao (uid $1); no need to log in again.'],
    [/^确定删除账号「(.+)」？会同时删除它的登录态和导出数据，不可恢复。$/, 'Delete account "$1"? Its session and exported data will be deleted permanently.'],
    [/^账号已创建。点「(.+)」扫码登录后即可使用。$/, 'Account created. Click "$1" and scan the QR code to start.'],
    [/^账号已创建。点「(.+)」完成登录后即可使用。$/, 'Account created. Click "$1" to finish signing in.'],
    [/^(\d+)\s*个文件$/, '$1 files'],
    [/^(.+)\s*·\s*获取 Cookie$/, '$1 · Get Cookie'],
    [/^今日发送：(\d+)$/, 'Sent today: $1'],
    [/^今日计划（(\d+) 个任务）$/, "Today's plan ($1 tasks)"],
    [/^已等待 (\d+) 秒（10 分钟未登录将自动取消）$/, 'Waiting $1s (auto-cancel after 10 minutes)'],
    [/^已选 \((\d+)\)：$/, 'Selected ($1):'],
    [/^保存成员（(\d+)）$/, 'Save members ($1)'],
    [/^创建任务 \((\d+)\)$/, 'Create task ($1)'],
    [/^加入分组 \((\d+)\)$/, 'Add to group ($1)'],
    [/^全选筛选 \((\d+)\)$/, 'Select filtered ($1)'],
    [/^删除 \((\d+)\)$/, 'Delete ($1)'],
    [/^立即执行 \((\d+)\)$/, 'Run now ($1)'],
    [/^批量创建 \((\d+)\)$/, 'Batch create ($1)'],
    [/^(\d[\d,]*)\s*次$/, '$1 times'],
    [/^(\d[\d,]*)\s*人$/, '$1 people'],
    [/^(\d[\d,]*)\s*天$/, '$1 days'],
    [/^(\d[\d,]*)\s*个任务$/, '$1 tasks'],
    [/^连续失败：(\d+) \/ (\d+)$/, 'Consecutive failures: $1 / $2'],
    [/^随机延迟：(\d+) 分钟$/, 'Random delay: $1 min'],
    [/^最后成功：(.*)$/, 'Last success: $1'],
    [/^上次登录IP：(.*)$/, 'Last login IP: $1'],
    [/^当前模式：(.*)$/, 'Current mode: $1'],
    [/^当前浏览器：(.*)$/, 'Current browser: $1'],
    [/^当前端口：(.*)$/, 'Current port: $1'],
    [/^当前账号：(.*)$/, 'Current account: $1'],
    [/^(\d+) 条结果$/, '$1 results'],
    [/^找到 (\d+) 条结果$/, 'Found $1 results'],
    [/^发消息给 (.*)（发送后自动备份到本地）$/, 'Message $1 (auto-backed up locally)'],
    [/^共 (\d+) 条消息，此操作不可恢复。$/, '$1 messages in total. This cannot be undone.'],
    [/^加载更多（(\d+)\/(\d+)）$/, 'Load more ($1/$2)'],
    [/^· (\d+) 条$/, '· $1'],
    [/^已取得 (\d+)\/(\d+) 条详情，其余仅保留摘要或尚未采集。$/, 'Loaded details for $1/$2 messages; the rest keep summaries only.'],
    [/^抖音号：(.*)$/, 'Douyin ID: $1'],
    [/^粉丝：(.*)$/, 'Followers: $1'],
    [/^已发送，但本地备份失败：(.*)$/, 'Sent, but local backup failed: $1'],
    [/^更新失败：(.*)$/, 'Update failed: $1'],
    [/^删除失败[:：](.*)$/, 'Delete failed: $1'],
    [/^切换失败[:：](.*)$/, 'Switch failed: $1'],
    [/^完成 · 成功(\d+) 已签(\d+) 失败(\d+)$/, 'Done · OK $1, already $2, failed $3'],
    [/^完成 · 成功(\d+) 已签(\d+)$/, 'Done · OK $1, already $2'],
    [/^(\d+)分\s*(\d+)秒$/, '$1m $2s'],
    [/^(\d+)小时\s*(\d+)分$/, '$1h $2m'],
    [/^(\d+)小时$/, '$1h'],
    [/^(\d+)天\s*(\d+)小时$/, '$1d $2h'],
    [/^(\d{4})年(\d{1,2})月(\d{1,2})日$/, '$1-$2-$3'],
    [/^(\d{4})年(\d{1,2})月(\d{1,2})日\s+(\d{2}:\d{2})$/, '$1-$2-$3 $4'],
    [/^(\d{1,2})月(\d{1,2})日\s+(\d{2}:\d{2})$/, '$1/$2 $3'],
    [/^(\d{1,2})月(\d{1,2})日$/, '$1/$2'],
    [/^重燃中\s*(\d+)\/(\d+)$/, 'Relighting $1/$2'],
    [/^重燃中\s*(\d+)\/(\d+)(🔥)?$/, 'Relighting $1/$2$3'],
    [/^好友数据时间：(.+)\s*（来自本地缓存，浏览器未初始化时也能查看；点「刷新」可更新）$/, 'Friend data: $1 (from local cache; viewable without the browser. Click Refresh to update.)'],
    [/^好友数据时间：(.+)$/, 'Friend data: $1'],
    [/^（来自本地缓存，浏览器未初始化时也能查看；点「刷新」可更新）$/, '(from local cache; viewable without initializing the browser. Click Refresh to update.)'],
    [/^抖音号\s+(.+)$/, 'Douyin ID $1'],
    [/^(.+) 打卡明细（(\d+) 条）$/, 'Check-in details · $1 ($2)']
  ];

  var SKIP = 'script,style,noscript,pre,code,svg,canvas,.log,.logs,#log,#logs,' +
    '.msgs,.messages,.message-list,.chat-messages,.chat-msg-row,.chat-bubble,.bub,.conv,.conv-name,.conv-last,.res,' +
    '.conv-avatar,.msg-list,.msg-item,.msg-body,.msg-bubble,.msg-system-block,.msg-share-card,.msg-media,.msg-ref-quote,.msg-sender,.msg-voice-transcript,' +
    '.acct-menu,.acct-switch,.acct-chip,.acct-menu-item,.sticker-panel,.member-picker,[data-mp-no-i18n],[contenteditable="true"]';

  var textRecords = new Map();
  var attrRecords = new Map();
  var translating = false;
  var observer = null;
  var titleOrig = '';

  function trimMatch(s) {
    var m = /^(\s*)([\s\S]*?)(\s*)$/.exec(s);
    return m ? { lead: m[1], body: m[2], tail: m[3] } : null;
  }

  function translateText(text) {
    if (!text) return null;
    var t = text.trim();
    if (!t) return null;
    if (Object.prototype.hasOwnProperty.call(DICT, t)) return DICT[t];
    var norm = t.replace(/\s+/g, ' ');
    if (norm !== t && Object.prototype.hasOwnProperty.call(DICT, norm)) return DICT[norm];
    for (var i = 0; i < RULES.length; i++) {
      if (RULES[i][0].test(t)) return t.replace(RULES[i][0], RULES[i][1]);
      if (norm !== t && RULES[i][0].test(norm)) return norm.replace(RULES[i][0], RULES[i][1]);
    }
    return null;
  }

  function translateValue(value) {
    if (!value) return null;
    var m = /^(\s*)([\s\S]*?)(\s*)$/.exec(value);
    if (!m) return null;
    var tr = translateText(m[2]);
    if (tr == null) return null;
    return m[1] + tr + m[3];
  }

  function isSkipped(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.closest && el.closest(SKIP)) return true;
    var tag = el.tagName;
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' || tag === 'TEXTAREA') return true;
    return false;
  }

  function translateTextNode(node) {
    if (!node || !node.nodeValue) return;
    var parent = node.parentElement;
    if (!parent) return;
    if (!parent.closest('.empty') && !parent.closest('.day') && isSkipped(parent)) return;
    var cur = node.nodeValue;
    var tr = translateValue(cur);
    if (tr == null || tr === cur) return;
    // 只在节点当前是中文时记录原文；已有英文译文不再覆盖
    var rec = textRecords.get(node);
    if (!rec || rec.last !== cur) textRecords.set(node, { orig: cur, last: tr });
    else rec.last = tr;
    translating = true;
    node.nodeValue = tr;
    translating = false;
  }

  function translateAttr(el, attr) {
    if (!el || !el.getAttribute) return;
    var tag = el.tagName;
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') return;
    if (el.closest && el.closest(SKIP)) return;
    var cur = el.getAttribute(attr);
    if (!cur) return;
    var tr = translateValue(cur);
    if (tr == null || tr === cur) return;
    var map = attrRecords.get(el);
    if (!map) { map = {}; attrRecords.set(el, map); }
    if (!map[attr] || map[attr].last !== cur) map[attr] = { orig: cur, last: tr };
    else map[attr].last = tr;
    el.setAttribute(attr, tr);
  }

  function translateRoot(root) {
    if (state.lang !== 'en' || !root) return;
    if (root.nodeType === 3) { translateTextNode(root); return; }
    if (root.nodeType !== 1 && root.nodeType !== 9) return;
    if (root.nodeType === 1) {
      ATTRS.forEach(function (a) { translateAttr(root, a); });
    }
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var node;
    while ((node = walker.nextNode())) translateTextNode(node);
    if (root.querySelectorAll) {
      root.querySelectorAll('[placeholder],[title],[aria-label]').forEach(function (el) {
        ATTRS.forEach(function (a) { translateAttr(el, a); });
      });
    }
  }

  function restoreChinese() {
    textRecords.forEach(function (rec, node) {
      if (node.isConnected) node.nodeValue = rec.orig;
    });
    textRecords.clear();
    attrRecords.forEach(function (map, el) {
      if (!el.isConnected) return;
      Object.keys(map).forEach(function (a) { el.setAttribute(a, map[a].orig); });
    });
    attrRecords.clear();
  }

  var ATTRS = ['placeholder', 'title', 'aria-label'];

  function applyLang(lang) {
    state.lang = lang === 'en' ? 'en' : 'zh';
    localStorage.setItem('mp-lang', state.lang);
    document.documentElement.lang = state.lang === 'zh' ? 'zh-CN' : 'en';
    if (!titleOrig) titleOrig = document.title;
    if (state.lang === 'en') {
      var titleTr = translateText(titleOrig);
      if (titleTr) document.title = titleTr;
    } else {
      document.title = titleOrig;
    }
    if (state.lang === 'zh') {
      if (observer) observer.disconnect();
      restoreChinese();
    } else {
      translateRoot(document.body);
      startObserver();
    }
    window.dispatchEvent(new CustomEvent('mp:lang', { detail: { lang: state.lang } }));
  }

  function startObserver() {
    if (observer || !document.body) return;
    observer = new MutationObserver(function (mutations) {
      if (translating || state.lang !== 'en') return;
      for (var i = 0; i < mutations.length; i++) {
        var m = mutations[i];
        if (m.type === 'characterData') translateTextNode(m.target);
        else if (m.type === 'attributes') translateAttr(m.target, m.attributeName);
        else {
          for (var j = 0; j < m.addedNodes.length; j++) translateRoot(m.addedNodes[j]);
        }
      }
    });
    observer.observe(document.body, {
      childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ATTRS
    });
  }

  function applyTheme(theme) {
    state.theme = THEMES.indexOf(theme) >= 0 ? theme : (theme === 'light' ? 'light' : 'dark');
    localStorage.setItem('mp-theme', state.theme);
    if (LANG_ONLY) return;
    document.documentElement.setAttribute('data-theme', state.theme);
    window.dispatchEvent(new CustomEvent('mp:theme', { detail: { theme: state.theme } }));
  }

  function forwardToFrames() {
    var payload = { type: 'mp:settings', theme: state.theme, lang: state.lang };
    document.querySelectorAll('iframe').forEach(function (frame) {
      try { frame.contentWindow.postMessage(payload, location.origin); } catch (e) { /* not ready */ }
    });
  }

  function ensureFrameAssets(frame) {
    var src = frame.src || '';
    if (!/\/weibo-chat\/viewer|\/taobao\/viewer/.test(src)) return;
    try {
      var doc = frame.contentDocument;
      if (!doc || !doc.head) return;
      if (doc.querySelector('script[src*="/shared/mp_ui.js"]')) return;
      var link = doc.createElement('link');
      link.rel = 'stylesheet';
      link.href = '/shared/mp_ui.css?v=2';
      doc.head.appendChild(link);
      var script = doc.createElement('script');
      script.src = '/shared/mp_ui.js?v=2';
      doc.head.appendChild(script);
    } catch (e) { /* cross-origin or not ready */ }
  }

  function applySettings(settings, forward) {
    if (!settings) return;
    if (settings.theme) applyTheme(settings.theme);
    if (settings.lang) applyLang(settings.lang);
    if (forward !== false) forwardToFrames();
  }

  window.addEventListener('message', function (event) {
    if (event.origin !== location.origin) return;
    var data = event.data;
    if (!data || data.type !== 'mp:settings') return;
    applySettings(data, true);
  });

  document.addEventListener('load', function (event) {
    if (!event.target || event.target.tagName !== 'IFRAME') return;
    ensureFrameAssets(event.target);
    forwardToFrames();
  }, true);

  // JS 里 alert/confirm/prompt 的中文提示也走同一套词典
  ['alert', 'confirm', 'prompt'].forEach(function (name) {
    var native = window[name];
    if (typeof native !== 'function') return;
    window[name] = function (message) {
      if (state.lang === 'en' && typeof message === 'string') {
        var tr = translateValue(message);
        if (tr != null) message = tr;
      }
      return native.apply(window, Array.prototype.slice.call(arguments).map(function (v, i) {
        return i === 0 ? message : v;
      }));
    };
  });

  window.MPUI = {
    get theme() { return state.theme; },
    get lang() { return state.lang; },
    applyTheme: applyTheme,
    applyLang: applyLang,
    applySettings: applySettings,
    t: translateText,
    translate: function (root) { translateRoot(root || document.body); },
    extend: function (dict) {
      if (!dict) return;
      Object.keys(dict).forEach(function (k) { DICT[k] = dict[k]; });
      if (state.lang === 'en') translateRoot(document.body);
    }
  };

  applyTheme(state.theme);
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { applyLang(state.lang); });
  } else {
    applyLang(state.lang);
  }

  fetch(SETTINGS_URL, { cache: 'no-store' })
    .then(function (r) { return r.json(); })
    .then(function (s) { applySettings(s, true); })
    .catch(function () { /* 离线导出页没有后端也保持本地设置 */ });
})();
