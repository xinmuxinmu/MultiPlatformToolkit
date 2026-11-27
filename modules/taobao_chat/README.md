# 淘宝旺旺聊天记录导出（买家侧）

导出你**自己**和卖家的聊天记录，包含历史消息、图片链接，生成离线可读的 HTML 浏览页。

已经集成到「多平台工具」：首页点「淘宝旺旺记录」进入控制台，**登录 → 粘贴聊天链接 → 导出 → 查看**，和微博聊天记录那套流程一致。

---

## 一、使用（推荐）

1. 打开工具首页 →「淘宝旺旺记录」`/taobao/`
2. 点 **登录淘宝**，在弹出的浏览器里扫码 / 账号密码登录（登录态会保存，之后不用重复登录）
3. 控制台默认勾选「自动获取账号内全部会话」，点 **开始导出** 即可自动读取淘宝聊天列表并逐个导出；
   也可以取消勾选，改为粘贴聊天窗口链接（每行一个）只导出指定卖家，链接形如：
   ```
   https://market.m.taobao.com/app/im/chat/index.html?&uid=cntaobao卖家旺旺名&type=web
   ```
5. 导出完成后点 **打开浏览页**：左侧会话列表，右侧聊天气泡，支持搜索、导出本会话/全部文本

说明：

- 默认是**增量合并**：新导出的消息按「时间 + 发送人 + 内容 + 图片」去重后并进已有记录，不会重复；勾选「全量重建」则替换旧数据。
- 聊天记录、登录态都只保存在本机 `data/taobao_chat/` 下，不上传任何地方。
- 自动获取会读取淘宝聊天页左侧的会话列表（最近联系人），列表之外的历史卖家可通过聊天链接单独导出。

命令行等价用法（在 `modules/taobao_chat/tool` 下）：

```bash
python export.py --login                       # 登录并保存登录态
python export.py --status                      # 检查登录态，输出 JSON
python export.py --url="https://market.m.taobao.com/app/im/chat/index.html?&uid=cntaobaoXXX&type=web"
python export.py --input=input.json            # {"urls": ["...", "..."]}
python export.py --url=... --mode=full         # 全量重建
```

依赖 Python 与 Playwright：在项目根目录运行 `python -m pip install -r requirements.txt`。优先使用本机 Chrome / Edge，也可运行 `python -m playwright install chromium` 安装浏览器内核。图片、头像、音频、视频会尽量下载到导出目录；失败时保留在线链接并写日志。

---

## 二、油猴脚本（旧方式，仍可用）

控制台右上角「下载油猴脚本」或在 `/taobao/userscript` 下载 `淘宝旺旺聊天记录导出.user.js`：

1. Chrome / Edge 安装 **Tampermonkey**
2. 把脚本拖进浏览器窗口 → 点「安装」
3. 打开卖家聊天页，页面右下角出现橙色面板「旺旺记录导出」
4. 点「导出全部（自动加载历史）」，得到 JSON，再拖进任意聊天查看器查看

脚本只导当前打开的那个会话，多个卖家需要分别打开、分别导出；集成版没有这个限制。

---

## 三、为什么之前的千牛脚本不行

你给的这个页面和「千牛客服工作台」完全是两套东西：

| | 千牛（卖家侧） | 你打开的这个（买家侧） |
|---|---|---|
| 域名 | `myseller.taobao.com` | `market.m.taobao.com/app/im/chat` |
| 用途 | 商家客服工作台 | 买家和卖家聊天 |
| DOM 结构 | `.message-container` / `.results-list` | `.message-list` / `.message-item-line` |

而且页面还套了一层 iframe：

```
外层  market.m.taobao.com/app/im/chat/index.html?...      ← 只是个壳
  └─ iframe  market.m.taobao.com/app/im/chat-core/index.html?...   ← 聊天内容在这里
```

集成版导出工具会自动找到 `chat-core` 这一层再解析。

---

## 四、导出内容

查看器里的数据来自 `data/taobao_chat/export/raw.json`，结构：

```json
{
  "exportedAt": "2026/10/25 01:20:00",
  "me": { "uid": "me", "name": "我" },
  "convs": [
    {
      "id": "tb:卖家旺旺名",
      "name": "卖家昵称",
      "count": 128,
      "messages": [
        { "from": "tb:卖家旺旺名", "fromName": "卖家昵称", "text": "亲，这款有现货", "ts": 1792850000000, "pics": [], "audio": "", "video": "" }
      ]
    }
  ],
  "total": 128
}
```

- 文本、图片、视频、语音（含原始链接）、文件/商品卡片都会保留
- 系统提示（如「以上是历史消息」）也会显示
- 图片是阿里 CDN 链接，会过期，要长期保存请及时另存

---

## 五、已知限制

1. 靠 DOM 抓取，淘宝前端改版可能失效；失效时看控制台日志里的 `[!]` 提示，改选择器即可。
2. 自动获取只覆盖淘宝会话列表里加载出来的最近联系人；更早的卖家可以取消自动获取后粘贴聊天链接。
3. 语音多为 `amr`，浏览器可能无法直接播放，可点「原始语音文件」下载。
4. 会话特别长时自动加载历史可能要几分钟，期间别关闭浏览器窗口。
5. 纯本地运行，登录态和聊天记录都在本机。
