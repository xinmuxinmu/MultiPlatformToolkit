#!/usr/bin/env node
'use strict';
/*
 * 淘宝旺旺聊天记录导出（买家侧）
 *
 * 用 Playwright 复用登录态，打开聊天链接，自动加载历史消息，解析 DOM，
 * 生成 raw.json + 离线查看器 HTML（淘宝聊天记录.html）。
 *
 * 用法：
 *   node export.js --login                 打开浏览器登录淘宝（保存登录态）
 *   node export.js --status                输出 {"ok":true,"uid":"..."} 
 *   node export.js --input=input.json      读取 {"urls":[...]} 批量导出
 *   node export.js --url=... [--url=...]   直接指定聊天链接
 *   node export.js --mode=full             不合并旧数据，重新生成
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const ROOT = __dirname;
const HOME = process.env.TAOBAO_CHAT_HOME || path.join(__dirname, '..', 'data');
const OUT = process.env.TAOBAO_CHAT_OUT || path.join(HOME, 'export');
const PROFILE = process.env.TAOBAO_CHAT_PROFILE || path.join(HOME, 'profile');
const TEMPLATE = path.join(ROOT, 'template.html');
const RAW_FILE = path.join(OUT, 'raw.json');
const VIEWER_FILE = path.join(OUT, '淘宝聊天记录.html');
const CHAT_ENTRY = 'https://market.m.taobao.com/app/im/chat/index.html?type=web';
const LOGIN_URL = 'https://login.taobao.com/member/login.jhtml?redirectURL=' +
  encodeURIComponent('https://www.taobao.com/');
// playwright-core 不自带浏览器，找本机 Chrome / Edge
const CHROME = [
  process.env.TAOBAO_CHROME_PATH,
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
].filter(Boolean).find((p) => fs.existsSync(p));

const OPT = parseArgs(process.argv.slice(2));

function parseArgs(argv) {
  const opt = { login: false, status: false, headless: false, mode: 'incremental',
                input: '', urls: [] };
  for (const arg of argv) {
    if (arg === '--login') opt.login = true;
    else if (arg === '--status') opt.status = true;
    else if (arg === '--headless') opt.headless = true;
    else if (arg.startsWith('--mode=')) opt.mode = arg.slice(7);
    else if (arg.startsWith('--input=')) opt.input = arg.slice(8);
    else if (arg.startsWith('--url=')) opt.urls.push(arg.slice(6));
    else if (!arg.startsWith('--')) opt.urls.push(arg);
  }
  return opt;
}

function log(...args) {
  console.log(...args);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function loadPlaywright() {
  const candidates = [
    'playwright-core',
    path.join(ROOT, 'node_modules', 'playwright-core'),
    path.join(ROOT, '..', '..', 'weibo_chat', 'tool', 'node_modules', 'playwright-core'),
    path.join(ROOT, '..', '..', '..', 'node_modules', 'playwright-core'),
  ];
  for (const candidate of candidates) {
    try { return require(candidate); } catch (err) { /* try next */ }
  }
  throw new Error('未找到 playwright-core，请先在 modules/taobao_chat/tool 下执行 npm install');
}

async function launch(headless) {
  const { chromium } = loadPlaywright();
  if (!CHROME) {
    log('[*] 未找到本机 Chrome/Edge，尝试使用 Playwright 自带浏览器（若失败请安装 Chrome）');
  }
  fs.mkdirSync(PROFILE, { recursive: true });
  const context = await chromium.launchPersistentContext(PROFILE, {
    headless,
    ...(CHROME ? { executablePath: CHROME } : {}),
    viewport: { width: 1280, height: 880 },
    locale: 'zh-CN',
    args: ['--disable-blink-features=AutomationControlled'],
  });
  await context.addInitScript(
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});");
  return context;
}

async function loginCookies(context) {
  const cookies = await context.cookies().catch(() => []);
  const taobao = cookies.filter((c) => /(^|\.)taobao\.com$/.test(c.domain || ''));
  const byName = new Map(taobao.map((c) => [c.name, c]));
  // 只认 "unb"（用户数字 ID）。游客访问登录页也会拿到 cookie2/_tb_token_，
  // 用它们判断会把「打开登录页」误判成「已登录」，浏览器刚开就被关掉。
  const unb = byName.get('unb');
  const ok = !!(unb && unb.value);
  return { ok, uid: unb && unb.value ? String(unb.value) : '' };
}

async function waitForLogin(context, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const info = await loginCookies(context);
    if (info.ok) return info;
    await sleep(1500);
  }
  return { ok: false, uid: '' };
}

async function doStatus() {
  const context = await launch(true);
  try {
    const info = await loginCookies(context);
    log(JSON.stringify({ ok: info.ok, uid: info.uid, name: '' }));
  } finally {
    await context.close().catch(() => {});
  }
}

async function doLogin() {
  const context = await launch(false);
  let page = context.pages()[0] || await context.newPage();
  try {
    const existing = await loginCookies(context);
    if (existing.ok) {
      log(`✅ 已经是登录状态（uid=${existing.uid || '未知'}），无需重复登录`);
      return;
    }
    log('[*] 已打开浏览器，请扫码 / 输入账号密码登录淘宝…');
    await page.goto(LOGIN_URL, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
    const info = await waitForLogin(context, 5 * 60 * 1000);
    if (!info.ok) {
      log('[-] 登录超时（5 分钟），请重试');
      process.exitCode = 2;
      return;
    }
    log(`✅ 登录成功（uid=${info.uid || '未知'}），正在保存登录态…`);
    // 多等一会儿让 Chrome 把 Cookie 落盘，否则下次导出会误判成未登录
    await sleep(3000);
  } finally {
    await context.close().catch(() => {});
  }
}

/* ---------------- 页面内的解析逻辑（移植自油猴脚本，已按其真实 DOM 验证） ---------------- */
const FRAME_JS = `(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clean = (s) => String(s || '').replace(/\\u00a0/g, ' ').replace(/[ \\t]+\\n/g, '\\n').trim();

  function findScroller(root) {
    const nodes = (root || document).querySelectorAll('div');
    let best = null, bestH = 0;
    for (const el of nodes) {
      const st = getComputedStyle(el);
      if (!/(auto|scroll)/.test(st.overflowY)) continue;
      const over = el.scrollHeight - el.clientHeight;
      if (over > 40 && over > bestH) { bestH = over; best = el; }
    }
    return best;
  }
  function findLoadMore() {
    const list = document.querySelector('.message-list');
    if (!list) return null;
    for (const span of list.querySelectorAll('span')) {
      const t = (span.innerText || '').trim();
      if (t.indexOf('加载更多') >= 0 || t.indexOf('下拉') >= 0) return span;
    }
    return null;
  }
  async function loadAllHistory(onProgress) {
    const list = document.querySelector('.message-list');
    if (!list) throw new Error('没找到聊天列表，请确认已打开具体会话');
    let sc = findScroller(list);
    if (!sc && document.scrollingElement &&
        document.scrollingElement.scrollHeight > innerHeight) {
      sc = document.scrollingElement;
    }
    const origin = sc ? sc.scrollTop : 0;
    let last = -1, stable = 0, round = 0;
    while (round < 120 && stable < 3) {
      const before = document.querySelectorAll('.message-item-line').length;
      if (sc) sc.scrollTop = 0;
      await sleep(220);
      const more = findLoadMore();
      if (more) { try { more.click(); } catch (e) {} await sleep(750); }
      else await sleep(320);
      const now = document.querySelectorAll('.message-item-line').length;
      if (now === last && now === before) stable++; else stable = 0;
      last = now;
      round++;
      if (onProgress) onProgress(round, now);
    }
    if (sc) sc.scrollTop = origin;
    await sleep(120);
    return document.querySelectorAll('.message-item-line').length;
  }
  function splitTime(raw) {
    const m = /(\\d{4}-\\d{2}-\\d{2})[\\sT]+(\\d{1,2}:\\d{2}(?::\\d{2})?)/.exec(raw || '');
    if (m) return { date: m[1], time: m[2] };
    const t2 = /(\\d{1,2}:\\d{2}(?::\\d{2})?)/.exec(raw || '');
    return { date: '', time: t2 ? t2[1] : '' };
  }
  function extractMessages() {
    const lines = document.querySelectorAll('.message-item-line');
    const out = [];
    let selfNick = '';
    for (const line of lines) {
      const box = line.querySelector('.message-item');
      if (!box) {
        const cEl = line.querySelector('.content');
        const ct = clean(cEl ? cEl.innerText : line.innerText);
        if (ct) out.push({ date: '', time: '', sender: '系统', text: ct, imgUrl: '', self: false });
        continue;
      }
      const isSelf = box.classList.contains('self');
      const nickEl = box.querySelector('.nick');
      const nick = clean(nickEl ? nickEl.innerText : '');
      const timeEl = box.querySelector('.time');
      const t = splitTime(clean(timeEl ? timeEl.innerText : ''));
      if (isSelf && nick) selfNick = nick;
      const texts = [], imgs = [], extras = [];
      const textEl = box.querySelector('.message-text');
      if (textEl) {
        const tx = clean(textEl.innerText);
        if (tx) texts.push(tx);
      }
      for (const img of box.querySelectorAll('img.imui-msg-img, img.J_imImage')) {
        if (img.src) imgs.push(img.src);
      }
      const v = box.querySelector('video.im-message-video');
      if (v && v.src) extras.push('[视频] ' + v.src);
      const a = box.querySelector('audio');
      if (a && a.src) extras.push('[语音] ' + a.src);
      const f = box.querySelector('.file-msg');
      if (f) extras.push('[文件] ' + clean(f.innerText));
      const g = box.querySelector('.goods-item');
      if (g) extras.push('[商品卡片] ' + clean(g.innerText).replace(/\\n+/g, ' '));
      if (!texts.length && !imgs.length && !extras.length) {
        let whole = clean(box.innerText);
        if (nick) whole = whole.replace(nick, '');
        if (t.time) whole = whole.replace(t.time, '');
        whole = clean(whole);
        if (whole) texts.push(whole);
      }
      const body = texts.concat(extras).join('\\n');
      if (!body && !imgs.length) continue;
      const sender = nick || (isSelf ? (selfNick || '我') : '对方');
      out.push({ date: t.date, time: t.time, sender, text: body,
                 imgUrl: imgs[0] || '', self: isSelf });
      for (let i = 1; i < imgs.length; i++) {
        out.push({ date: t.date, time: t.time, sender, text: '', imgUrl: imgs[i], self: isSelf });
      }
    }
    const seen = Object.create(null), dedup = [];
    for (const r of out) {
      const k = [r.date, r.time, r.sender, r.text, r.imgUrl].join('\\u0001');
      if (seen[k]) continue;
      seen[k] = 1;
      dedup.push(r);
    }
    return dedup;
  }
  function partnerName() {
    let uid = '';
    try { uid = new URLSearchParams(location.search).get('uid') || ''; } catch (e) {}
    uid = uid.replace(/^cntaobao/, '');
    const counts = {};
    for (const n of document.querySelectorAll('.message-item-line .nick')) {
      const t = clean(n.innerText);
      if (t) counts[t] = (counts[t] || 0) + 1;
    }
    const cands = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
    return uid || cands[0] || '淘宝会话';
  }
  function partnerAvatar() {
    for (const line of document.querySelectorAll('.message-item-line')) {
      const box = line.querySelector('.message-item');
      if (!box || box.classList.contains('self')) continue;
      const img = box.querySelector('img.headPic');
      if (img && img.src) return img.src;
    }
    return '';
  }

  const before = document.querySelectorAll('.message-item-line').length;
  const after = await loadAllHistory();
  const messages = extractMessages();
  return {
    printed: window.__taobaoExportProgress || 0,
    before, after,
    partner: partnerName(),
    avatar: partnerAvatar(),
    messages,
  };
})()`;

async function waitForChatFrame(page, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const frames = page.frames();
    for (const frame of frames) {
      try {
        const has = await frame.evaluate(() => !!document.querySelector('.message-list'));
        if (has) return frame;
      } catch (err) { /* frame might be navigating */ }
    }
    await sleep(1000);
  }
  return null;
}

function uidFromUrl(url) {
  try {
    const u = new URL(url);
    return (u.searchParams.get('uid') || '').replace(/^cntaobao/, '');
  } catch (err) {
    return '';
  }
}

function todayStr() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function parseTs(date, time) {
  if (!date && !time) return 0;
  const stamp = `${date || todayStr()} ${time || '00:00:00'}`;
  const ts = new Date(stamp.replace(/-/g, '/')).getTime();
  return Number.isFinite(ts) ? ts : 0;
}

function toViewerMessage(raw, ctx) {
  const date = raw.date || ctx.lastDate || todayStr();
  if (raw.date) ctx.lastDate = raw.date;
  let ts = raw.time ? parseTs(date, raw.time) : 0;
  // 没带时间的（系统提示等）按顺序排在上一句后面，不要跳到当天零点
  if (!ts) ts = ctx.lastTs ? ctx.lastTs + 1 : Date.now();
  ctx.lastTs = ts;

  let text = raw.text || '';
  let audio = '';
  let video = '';
  const kept = [];
  for (const line of text.split('\n')) {
    const trimmed = line.trim();
    if (trimmed.startsWith('[视频] ')) { video = trimmed.slice(4).trim(); continue; }
    if (trimmed.startsWith('[语音] ')) { audio = trimmed.slice(4).trim(); continue; }
    kept.push(line);
  }
  text = kept.join('\n').trim();

  const self = !!raw.self;
  return {
    from: self ? 'me' : ctx.convId,
    fromName: raw.sender || (self ? '我' : ctx.partner),
    avatar: self ? '' : (ctx.avatar || ''),
    text,
    ts,
    pics: raw.imgUrl ? [raw.imgUrl] : [],
    audio,
    audioRaw: audio,
    video,
    recall: false,
  };
}

function messageKey(m) {
  return [m.ts, m.from, m.text, (m.pics && m.pics[0]) || '', m.audio || '', m.video || ''].join('|');
}

function mergeConv(convs, conv) {
  const existing = convs.find((c) => c.id === conv.id);
  if (!existing) {
    convs.push(conv);
    return conv;
  }
  const seen = new Set(existing.messages.map(messageKey));
  for (const m of conv.messages) {
    const key = messageKey(m);
    if (!seen.has(key)) {
      existing.messages.push(m);
      seen.add(key);
    }
  }
  existing.messages.sort((a, b) => (a.ts || 0) - (b.ts || 0));
  existing.count = existing.messages.length;
  if (!existing.avatar && conv.avatar) existing.avatar = conv.avatar;
  if (!existing.name || existing.name === '淘宝会话') existing.name = conv.name;
  return existing;
}

function loadExisting() {
  try {
    const data = JSON.parse(fs.readFileSync(RAW_FILE, 'utf-8'));
    if (data && Array.isArray(data.convs)) return data;
  } catch (err) { /* no previous data */ }
  return null;
}

function renderViewer(data) {
  const template = fs.readFileSync(TEMPLATE, 'utf-8');
  const safe = JSON.stringify(data).replace(/</g, '\\u003c');
  fs.mkdirSync(OUT, { recursive: true });
  fs.writeFileSync(VIEWER_FILE, template.replace('__DATA__', safe), 'utf-8');
}

async function exportChats() {
  let urls = [...OPT.urls];
  if (OPT.input) {
    try {
      const payload = JSON.parse(fs.readFileSync(OPT.input, 'utf-8'));
      if (Array.isArray(payload.urls)) urls = urls.concat(payload.urls);
    } catch (err) {
      throw new Error(`读取链接列表失败：${err.message || err}`);
    }
  }
  urls = [...new Set(urls.map((u) => String(u || '').trim()).filter(Boolean))];
  if (!urls.length) urls = [CHAT_ENTRY];

  log(`[*] 共 ${urls.length} 个聊天链接，开始导出${OPT.mode === 'full' ? '（全量重建）' : '（增量合并）'}`);

  const context = await launch(OPT.headless || process.env.TAOBAO_HEADLESS === '1');
  try {
    let login = await loginCookies(context);
    if (!login.ok) {
      const page0 = context.pages()[0] || await context.newPage();
      log('[*] 未检测到登录态，请在弹出的浏览器里登录淘宝…');
      await page0.goto(LOGIN_URL, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
      login = await waitForLogin(context, 5 * 60 * 1000);
      if (!login.ok) throw new Error('登录超时（5 分钟），请重新点击登录后再导出');
      log(`✅ 登录成功（uid=${login.uid || '未知'}）`);
    } else {
      log(`✅ 登录态有效（uid=${login.uid || '未知'}）`);
    }

    const previous = OPT.mode === 'full' ? null : loadExisting();
    const convs = previous ? previous.convs : [];
    const page = context.pages()[0] || await context.newPage();
    let index = 0;

    for (const url of urls) {
      index++;
      log(`\n[${index}/${urls.length}] ${url}`);
      try {
        await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
        await sleep(2500);

        let frame = await waitForChatFrame(page, 25000);
        if (!frame) {
          // 可能被重定向到登录页：等登录后重试一次
          const info = await loginCookies(context);
          if (!info.ok) {
            log('  [*] 页面要求登录，等待登录…');
            const after = await waitForLogin(context, 3 * 60 * 1000);
            if (after.ok) {
              await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
              await sleep(2500);
              frame = await waitForChatFrame(page, 20000);
            }
          }
        }
        if (!frame) {
          log('  [!] 没找到聊天窗口（.message-list），跳过。请确认链接是具体卖家的聊天页且已登录');
          continue;
        }

        log('  [*] 正在向上加载历史消息…');
        const result = await frame.evaluate(FRAME_JS);
        const rawMessages = result.messages || [];
        if (!rawMessages.length) {
          log('  [!] 这个会话没有解析到消息，跳过');
          continue;
        }
        log(`  [*] 已解析 ${rawMessages.length} 条消息（聊天窗口共 ${result.after} 行）`);

        const uid = uidFromUrl(url);
        const convId = uid ? `tb:${uid}` : `tb:${crypto.createHash('md5')
          .update(result.partner || url).digest('hex').slice(0, 12)}`;
        const ctx = { convId, partner: result.partner || '淘宝卖家',
                      avatar: result.avatar || '', lastDate: '', lastTs: 0 };
        const messages = rawMessages.map((m) => toViewerMessage(m, ctx));
        messages.sort((a, b) => (a.ts || 0) - (b.ts || 0));
        const conv = {
          id: convId,
          name: result.partner || '淘宝卖家',
          avatar: result.avatar || '',
          isGroup: false,
          count: messages.length,
          messages,
        };
        const merged = mergeConv(convs, conv);
        log(`  [+] 已合并进「${merged.name}」，当前该会话共 ${merged.count} 条消息`);
      } catch (err) {
        log(`  [!] 导出失败：${err.message || err}`);
      }
    }

    convs.sort((a, b) => {
      const at = a.messages.length ? a.messages[a.messages.length - 1].ts : 0;
      const bt = b.messages.length ? b.messages[b.messages.length - 1].ts : 0;
      return bt - at;
    });
    const total = convs.reduce((sum, c) => sum + (c.count || 0), 0);
    if (!convs.length) throw new Error('没有成功导出任何会话');

    const data = {
      exportedAt: new Date().toLocaleString('zh-CN'),
      me: { uid: 'me', name: previous && previous.me ? previous.me.name : '我',
            avatar: previous && previous.me ? previous.me.avatar : '' },
      convs,
      total,
    };
    fs.mkdirSync(OUT, { recursive: true });
    fs.writeFileSync(RAW_FILE, JSON.stringify(data, null, 2), 'utf-8');
    renderViewer(data);
    log(`\n✅ 导出完成：${convs.length} 个会话，共 ${total} 条消息`);
    log(`   查看页：${VIEWER_FILE}`);
  } finally {
    await context.close().catch(() => {});
  }
}

(async () => {
  try {
    if (OPT.status) { await doStatus(); return; }
    if (OPT.login) { await doLogin(); return; }
    await exportChats();
  } catch (err) {
    console.error(`[-] ${err.message || err}`);
    process.exitCode = 1;
  }
})();
