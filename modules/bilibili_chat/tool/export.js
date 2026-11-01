/**
 * B站聊天记录导出工具
 * 用法：统一壳通过 /bili-chat/ 页面触发，或命令行 node export.js
 * 首次/登录失效时会弹出浏览器窗口，扫码登录一次即可，之后自动记住。
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

// ---------- 加载 playwright-core（自动在常见位置查找，含 weibo_chat 已装的副本） ----------
function loadChromium() {
  const tryRequire = (p) => { try { return require(p); } catch (_) { return null; } };
  const nodeDir = path.dirname(process.execPath);
  const candidates = [
    'playwright-core',                                              // 本地 node_modules / NODE_PATH
    'playwright',
    path.join(__dirname, 'node_modules', 'playwright-core'),        // 工具目录下
    path.join(__dirname, '..', '..', 'weibo_chat', 'tool', 'node_modules', 'playwright-core'), // 复用微博模块
    path.join(__dirname, '..', 'node_modules', 'playwright-core'),  // 项目根目录
    path.join(nodeDir, '..', 'workspace', 'node_modules', 'playwright-core'),
    path.join(nodeDir, '..', '..', 'workspace', 'node_modules', 'playwright-core'),
    path.join(nodeDir, 'node_modules', 'playwright-core'),
  ];
  for (const c of candidates) {
    const m = tryRequire(c);
    if (m && m.chromium) return m.chromium;
  }
  console.error('未找到 playwright-core，请先在工具目录执行：npm install');
  process.exit(1);
}
const chromium = loadChromium();

const ROOT = __dirname;
// 统一应用通过环境变量把配置与输出指向 data/bilibili_chat
const OUT = process.env.BILI_CHAT_OUT || path.join(ROOT, '..', '导出数据');
const STATE = process.env.BILI_CHAT_STATE || path.join(ROOT, 'state.json');
const PROFILE = process.env.BILI_CHAT_PROFILE || path.join(ROOT, 'profile');
const TEMPLATE = path.join(ROOT, 'template.html');
const CHROME = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
].find(p => fs.existsSync(p));

// 统一应用的无头开关通过环境变量下发；单独运行脚本时保持无头导出
const HEADLESS = process.env.BILI_HEADLESS !== '0';

const NAV_API = 'https://api.bilibili.com/x/web-interface/nav';
const SESSIONS_API = 'https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions';
// 消息接口：2023 年后从 /svr/svr/fetch_session_msgs 迁到 /svr_sync/v1/svr_sync/fetch_session_msgs
const MSGS_API = 'https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs';
const LOGIN_URL = 'https://passport.bilibili.com/login';

let MAX_DM = 100000;      // 私信单会话上限
let MAX_GROUP = 3000;     // 群聊单会话上限（预留，B 站群聊暂按私聊接口处理）
const GAP = 200;            // 请求间隔 ms，调小更快但更易触发风控
const PAGE_SIZE = 20;       // 会话列表分页
const MSG_SIZE = 40;        // 消息列表分页

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const findExisting = (rel, exts) => {
  for (const e of exts) { const p = rel + '.' + e; if (fs.existsSync(path.join(OUT, p))) return p; }
  return null;
};

// ---------- 命令行参数 ----------
const argv = process.argv.slice(2);
const OPT = {};
argv.forEach(a => {
  const m = a.match(/^--([\w-]+)(?:=(.*))?$/);
  if (m) OPT[m[1]] = m[2] !== undefined ? m[2] : true;
});
if (OPT['max-group']) MAX_GROUP = Math.max(20, parseInt(OPT['max-group'], 10) || 3000);
if (OPT['max-dm']) MAX_DM = Math.max(20, parseInt(OPT['max-dm'], 10) || 100000);
const ONLY_UID = String(OPT['only-uid'] || '').trim();

// ---------- 登录态检测 ----------
async function navInfo(ctx) {
  const r = await ctx.request.get(NAV_API, { timeout: 20000 });
  if (!r.ok()) return {};
  const j = await r.json().catch(() => ({}));
  if (j && j.code === 0 && j.data && j.data.isLogin) return j.data;
  return {};
}

async function doStatus() {
  const out = { ok: false, name: '', uid: '' };
  if (!fs.existsSync(STATE)) { console.log(JSON.stringify(out)); return; }
  let browser = null;
  try {
    browser = await chromium.launch({ executablePath: CHROME, headless: true });
    const ctx = await browser.newContext({ storageState: JSON.parse(fs.readFileSync(STATE, 'utf8')) });
    const me = await navInfo(ctx);
    if (me.mid) { out.ok = true; out.name = me.uname || ''; out.uid = String(me.mid); }
  } catch (_) {}
  console.log(JSON.stringify(out));
  if (browser) await browser.close().catch(() => {});
}

// ---------- 消息内容解析 ----------
// 实测消息类型：1 文本 / 2 图片 / 3 语音 / 5 系统 / 10 通知卡片 / 11 分享卡片 / 18 富文本数组
function jsonToText(c) {
  if (c == null) return '';
  if (typeof c === 'string') return c;
  if (Array.isArray(c)) return c.map(x => (x && (x.text || x.content)) || '').filter(Boolean).join('');
  if (typeof c !== 'object') return '';
  const bits = [];
  if (c.title) bits.push(String(c.title));
  if (c.text) bits.push(String(c.text));
  if (c.desc && c.desc !== '-') bits.push(String(c.desc));
  if (typeof c.content === 'string') {
    const inner = c.content.trim();
    if (inner.startsWith('[') || inner.startsWith('{')) {
      try { bits.push(jsonToText(JSON.parse(inner))); } catch (_) { bits.push(inner); }
    } else bits.push(inner);
  }
  if (Array.isArray(c.modules)) {
    for (const mod of c.modules) {
      if (mod && (mod.title || mod.detail)) bits.push([mod.title, mod.detail].filter(Boolean).join('：'));
    }
  }
  return bits.filter(Boolean).join('\n');
}

function parseContent(m) {
  // 返回 { text, pic, audio, recall }
  const res = { text: '', pic: '', audio: '', recall: false };
  if (m.msg_status === 2 || m.msg_status === 4) { res.recall = true; return res; }
  const raw = m.content !== undefined ? m.content : m.msg_content;
  let c = {};
  try { c = JSON.parse(raw || '{}'); } catch (_) { c = raw; }
  if (typeof c === 'string') c = { content: c };
  if (!c || typeof c !== 'object') c = { content: String(c || '') };

  const text = typeof c.content === 'string' ? c.content : '';
  const url = typeof c.url === 'string' ? c.url : '';

  // 图片 / 语音
  if (m.msg_type === 2 || (url && (c.width || c.height || c.imageType))) {
    res.pic = url || (text && /^https?:\/\//.test(text) ? text : '');
    return res;
  }
  if (m.msg_type === 3 || (text && /^https?:\/\//.test(text) && /\.(m4a|mp3|aac|amr)/i.test(text))) {
    res.text = '[语音消息]';
    res.audio = url || text;
    return res;
  }
  // 分享卡片（视频 / 专栏 / 动态等）
  if (m.msg_type === 11) {
    const link = c.bvid ? 'https://www.bilibili.com/video/' + c.bvid : '';
    res.text = ['【分享】' + (c.title || '卡片内容'), c.desc && c.desc !== '-' ? c.desc : '', link].filter(Boolean).join('\n');
    res.pic = typeof c.cover === 'string' ? c.cover : '';
    return res;
  }
  // 通知卡片（直播开始提醒等）
  if (m.msg_type === 10) {
    res.text = ['【通知】' + (c.title || '系统通知'), c.text || '', c.jump_uri || ''].filter(Boolean).join('\n');
    return res;
  }
  if (m.msg_type === 5) {                  // 系统消息（content 常常只是裸数字占位）
    const t = jsonToText(c).trim();
    res.text = /^\d{10,}$/.test(t) ? '[系统消息]' : (t || '[系统消息]');
    return res;
  }
  // 文本（含富文本数组）与未知类型兜底
  const fallback = jsonToText(c).trim();
  res.text = (!fallback || /^\d{10,}$/.test(fallback)) ? '[非文本消息]' : fallback;
  return res;
}

async function main() {
  if (!CHROME) { console.log('未找到 Chrome / Edge，请先安装浏览器。'); process.exit(1); }

  if (OPT.status) { await doStatus(); return; }
  if (OPT.logout) {
    [STATE, PROFILE].forEach(p => { try { fs.rmSync(p, { recursive: true, force: true }); } catch (_) {} });
    console.log('已清除本地登录态。');
    return;
  }

  fs.mkdirSync(OUT, { recursive: true });

  let browser, ctx;

  // 1) 尝试复用已保存的登录态（--login 时跳过，强制重新扫码）
  if (!OPT.login && fs.existsSync(STATE)) {
    try {
      browser = await chromium.launch({ executablePath: CHROME, headless: HEADLESS });
      ctx = await browser.newContext({ storageState: JSON.parse(fs.readFileSync(STATE, 'utf8')) });
      const me = await navInfo(ctx);
      if (!me.mid) { await browser.close(); browser = null; }
      else console.log('登录态有效，开始导出…');
    } catch (e) { if (browser) await browser.close().catch(() => {}); browser = null; }
  }

  // 2) 需要登录：弹窗扫码
  if (!browser) {
    console.log('需要登录：请在打开的浏览器窗口里登录B站（扫码或账号密码）…');
    ctx = await chromium.launchPersistentContext(PROFILE, {
      executablePath: CHROME, headless: false,
      viewport: { width: 1200, height: 820 },
      args: ['--disable-blink-features=AutomationControlled'],
    });
    const page = ctx.pages()[0] || await ctx.newPage();
    await page.goto(LOGIN_URL, { waitUntil: 'domcontentloaded', timeout: 90000 }).catch(() => {});
    let me = {};
    for (let i = 0; i < 180; i++) {
      me = await navInfo(ctx).catch(() => ({}));
      if (me.mid) break;
      await page.waitForTimeout(2000);
    }
    if (!me.mid) { console.log('登录超时，退出。'); await ctx.close(); process.exit(1); }
    fs.writeFileSync(STATE, JSON.stringify(await ctx.storageState(), null, 1), 'utf8');
    console.log('登录成功，已保存登录态。');
    await ctx.close();
    if (OPT.login) { console.log('LOGIN_DONE'); return; }
    browser = await chromium.launch({ executablePath: CHROME, headless: HEADLESS });
    ctx = await browser.newContext({ storageState: JSON.parse(fs.readFileSync(STATE, 'utf8')) });
  }

  const api = async (url) => {
    const r = await ctx.request.get(url, { timeout: 30000 });
    if (!r.ok()) throw new Error('HTTP ' + r.status());
    const j = await r.json();
    if (j && j.code !== 0) throw new Error('API code=' + j.code + ' ' + (j.message || ''));
    return j.data || {};
  };

  await sleep(1000);

  const me = await navInfo(ctx);
  const myUid = String(me.mid || '');
  console.log('账号：' + me.uname + ' (' + myUid + ')');

  // ---- 会话列表（session_type=1 私信；按 session_ts 向前翻页） ----
  const sessions = [];
  let endTs = 0;
  for (let page = 0; page < 200; page++) {
    const u = SESSIONS_API + `?session_type=1&begin_ts=0&end_ts=${endTs}&page_size=${PAGE_SIZE}&build=0&mobi_app=web`;
    let d;
    try { d = await api(u); } catch (e) { console.log('  ! 会话列表 ' + e.message); break; }
    const list = d.session_list || [];
    if (!list.length) break;
    let minTs = Infinity;
    for (const s of list) {
      const ts = s.session_ts || (s.last_msg && s.last_msg.timestamp) || 0;
      if (ts > 0 && ts < minTs) minTs = ts;
      sessions.push(s);
    }
    if (!d.has_more) break;
    if (!isFinite(minTs) || (endTs > 0 && minTs >= endTs)) break;  // 防御：游标不动就停
    endTs = minTs;
    await sleep(GAP);
  }

  let convs = [];
  let skippedSys = 0;
  for (const s of sessions) {
    const talkerId = String(s.talker_id || (s.account_info && s.account_info.mid) || '');
    if (!talkerId) continue;
    // 「回复我的 / @我的 / 系统通知」聚合会话用固定号段，不是真实私信，跳过
    if (/^8444249301319\d+$/.test(talkerId)) { skippedSys++; continue; }
    const info = s.account_info || {};
    const isGroup = s.session_type === 2 || s.group_type === 1 || Boolean(s.group_name);
    convs.push({
      uid: talkerId,
      name: isGroup ? (s.group_name || '') : (info.name || info.username || info.nickname || ''),
      isGroup,
      avatar: isGroup ? (s.group_cover || '') : (info.pic_url || info.face || ''),
      lastTs: s.session_ts || 0,
      session: s,
    });
  }
  if (skippedSys) console.log('已跳过系统通知聚合会话：' + skippedSys + ' 个');

  // 私信会话列表本身不返回用户资料，批量查名片接口补名字和头像
  const need = convs.filter(c => !c.isGroup && (!c.name || !c.avatar)).map(c => c.uid);
  if (need.length) {
    console.log('补齐用户名片：' + need.length + ' 个');
    const cardMap = new Map();
    for (let i = 0; i < need.length; i += 50) {
      const batch = need.slice(i, i + 50);
      try {
        const cards = await api('https://api.vc.bilibili.com/account/v1/user/cards?uids=' + batch.join(','));
        for (const x of (Array.isArray(cards) ? cards : [])) {
          if (x && x.mid) cardMap.set(String(x.mid), x);
        }
      } catch (e) { console.log('  ! 名片接口 ' + e.message); }
      await sleep(GAP);
    }
    for (const c of convs) {
      const card = cardMap.get(c.uid);
      if (!card) continue;
      if (!c.name) c.name = card.name || '';
      if (!c.avatar) c.avatar = card.face || '';
    }
    console.log('名片补齐完成：' + cardMap.size + ' 个');
  }
  // 仍缺名字的兜底展示，避免界面上一串裸数字
  for (const c of convs) {
    if (!c.name) c.name = c.isGroup ? ('群 ' + c.uid) : ('UID ' + c.uid);
  }
  if (ONLY_UID) {
    convs = convs.filter(c => c.uid === ONLY_UID);
    if (!convs.length) {
      console.log('未找到要更新的会话 UID：' + ONLY_UID);
      process.exit(2);
    }
  }
  console.log('会话数：' + convs.length + (ONLY_UID ? '（只更新指定会话）' : ''));

  // 增量导出基线：只认由本工具生成（含 fixed 标记）的 raw.json
  const baseFile = path.join(OUT, 'raw.json');
  const base = fs.existsSync(baseFile) ? JSON.parse(fs.readFileSync(baseFile, 'utf8')) : null;
  const baseMap = new Map();
  if (base && base.fixed === true) base.convs.forEach(c => baseMap.set(c.uid, new Set(c.messages.map(m => m.id))));
  const incMode = OPT.mode !== 'full' && baseMap.size > 0;
  console.log(incMode ? '增量模式：基于上次导出补齐新消息' : '全量模式：导出全部消息');

  const results = [];
  let idx = 0;
  for (const c of convs) {
    idx++;
    const cap = c.isGroup ? MAX_GROUP : MAX_DM;
    const msgs = [];
    let endSeq = 0;
    const known = incMode ? baseMap.get(c.uid) : null;   // 全量模式必须完全忽略旧基线
    let hitKnown = false;

    for (let round = 0; round < 3000 && msgs.length < cap; round++) {
      const u = MSGS_API + `?talker_id=${c.uid}&session_type=1&begin_seqno=0&end_seqno=${endSeq}&size=${MSG_SIZE}&build=0&mobi_app=web`;
      let d;
      try { d = await api(u); } catch (e) { console.log('  ! ' + c.name + ' ' + e.message); break; }
      const arr = d.messages || [];
      if (!arr.length) break;
      let minSeq = Infinity;
      for (const m of arr) {
        const seq = Number(m.msg_seqno) || 0;
        if (seq > 0 && seq < minSeq) minSeq = seq;
        if (known && known.has(String(m.msg_seqno))) { hitKnown = true; break; }
        msgs.push(m);
      }
      if (hitKnown) break;   // 已碰到上次导出的位置，说明更新的都拉到了
      if (!d.has_more) break;  // 服务端确认没有更多历史
      const nextEnd = Number(d.min_seqno) || minSeq;
      if (!isFinite(nextEnd) || nextEnd <= 0 || (endSeq > 0 && nextEnd >= endSeq)) break;  // 游标不动就停
      endSeq = nextEnd;
      await sleep(GAP);
    }

    const norm = msgs.map(m => {
      const p = parseContent(m);
      const self = String(m.sender_uid) === myUid;
      return {
        id: String(m.msg_seqno), text: p.text, ts: (m.timestamp || 0) * 1000,
        from: String(m.sender_uid || ''),
        fromName: self ? (me.uname || '') : c.name,
        avatar: '',   // 后面按 sender 统一填
        recall: p.recall ? 1 : 0,
        picUrl: p.pic || '',
        audioUrl: p.audio || '',
        self,
      };
    }).sort((a, b) => a.ts - b.ts || (a.id < b.id ? -1 : 1));

    // 增量：把历史消息合并回来
    let merged = norm;
    if (known) {
      const bc = base.convs.find(x => x.uid === c.uid);
      if (bc && bc.messages.length) {
        const have = new Set(norm.map(m => m.id));
        merged = norm.concat(bc.messages.filter(m => !have.has(m.id)));
      }
    }
    merged.sort((a, b) => a.ts - b.ts || (a.id < b.id ? -1 : 1));

    results.push({ uid: c.uid, name: c.name, isGroup: c.isGroup, avatar: c.avatar, lastTs: c.lastTs, messages: merged });
    const added = norm.length;
    if (added) console.log('[' + idx + '/' + convs.length + '] ' + c.name + (c.isGroup ? ' (群)' : '') + ' -> +' + added + ' 条（共 ' + merged.length + '）');
  }

  // 只更新单个会话时，把上次导出里的其它会话原样保留
  if (ONLY_UID && base && Array.isArray(base.convs)) {    const have = new Set(results.map(c => c.uid));
    for (const c of base.convs) {
      if (!have.has(c.uid)) results.push(c);
    }
  }

  // ---- 媒体：图片（按 URL 去重，分享卡片封面会大量重复） ----
  const jobs = [];
  results.forEach(c => c.messages.forEach(m => { if (m.picUrl) jobs.push({ m, kind: 'image' }); }));
  console.log('待处理媒体：' + jobs.length);
  let done = 0;
  const imgCache = new Map();   // 远程 URL -> 本地相对路径
  for (const job of jobs) {
    const m = job.m;
    try {
      const url = m.picUrl;
      if (imgCache.has(url)) { m.pics = [imgCache.get(url)]; done++; continue; }
      const key = 'i_' + crypto.createHash('md5').update(url.split('?')[0]).digest('hex').slice(0, 16);
      const cached = findExisting('images/' + key, ['jpg', 'png', 'gif', 'webp']);
      if (cached) { imgCache.set(url, cached); m.pics = [cached]; done++; continue; }
      const r = await ctx.request.get(url, { timeout: 20000 });
      if (r.status() === 200) {
        const ct = r.headers()['content-type'] || '';
        const ext = ct.includes('png') ? 'png' : (ct.includes('gif') ? 'gif' : (ct.includes('webp') ? 'webp' : 'jpg'));
        fs.mkdirSync(path.join(OUT, 'images'), { recursive: true });
        const rel = 'images/' + key + '.' + ext;
        fs.writeFileSync(path.join(OUT, rel), await r.body());
        imgCache.set(url, rel);
        m.pics = [rel];
      }
    } catch (_) {}
    delete m.picUrl;
    done++;
    if (done % 40 === 0) console.log('  媒体 ' + done + '/' + jobs.length);
    await sleep(50);
  }
  results.forEach(c => c.messages.forEach(m => { if (!m.pics) m.pics = []; delete m.picUrl; }));

  // ---- 头像 ----
  fs.mkdirSync(path.join(OUT, 'avatars'), { recursive: true });
  const avSet = new Set();
  const meAvatarSrc = me.face || '';
  if (meAvatarSrc) avSet.add(meAvatarSrc);
  results.forEach(c => { if (c.avatar && c.avatar.startsWith('http')) avSet.add(c.avatar); });
  const avMap = new Map();
  for (const a of [...avSet]) {
    const hb = 'avatars/' + crypto.createHash('md5').update(a.split('?')[0]).digest('hex').slice(0, 16);
    const cached = findExisting(hb, ['jpg', 'png', 'gif', 'webp']);
    if (cached) { avMap.set(a, cached); continue; }
    try {
      const r = await ctx.request.get(a, { timeout: 15000 });
      if (r.status() === 200) {
        const ct = r.headers()['content-type'] || '';
        const ext = ct.includes('png') ? 'png' : (ct.includes('gif') ? 'gif' : 'jpg');
        const nm = hb + '.' + ext;
        fs.writeFileSync(path.join(OUT, nm), await r.body());
        avMap.set(a, nm);
      }
    } catch (_) {}
    await sleep(45);
  }
  results.forEach(c => {
    if (!c.avatar || !c.avatar.startsWith('avatars/')) c.avatar = avMap.get(c.avatar) || '';
  });
  // 消息头像按 sender 填：自己用 me 头像，对方用会话头像
  const meAv = avMap.get(meAvatarSrc) || '';
  results.forEach(c => c.messages.forEach(m => {
    if (m.avatar && m.avatar.startsWith('avatars/')) return;   // 已是本地路径
    m.avatar = m.self ? meAv : (c.avatar || '');
  }));

  results.forEach(c => {
    c.count = c.messages.length;
    delete c.lastTs;
    c.messages.forEach(m => {
      delete m.self;
      delete m.audioUrl;   // 语音暂不落盘（B 站语音 URL 带签名会过期，文本提示已保留）
    });
  });

  results.sort((a, b) => {
    const ca = a.count > 0 ? 1 : 0, cb = b.count > 0 ? 1 : 0;
    if (ca !== cb) return cb - ca;
    const ta = a.messages.length ? a.messages[a.messages.length - 1].ts : 0;
    const tb = b.messages.length ? b.messages[b.messages.length - 1].ts : 0;
    return tb - ta;
  });

  const total = results.reduce((s, c) => s + c.count, 0);
  const payload = {
    exportedAt: new Date().toISOString(),
    me: { uid: myUid, name: me.uname || '', avatar: meAv },
    fixed: true, total, convs: results,
  };
  fs.writeFileSync(path.join(OUT, 'raw.json'), JSON.stringify(payload, null, 1), 'utf8');

  const tpl = fs.readFileSync(TEMPLATE, 'utf8');
  const json = JSON.stringify(payload).replace(/</g, '\\u003c');
  fs.writeFileSync(path.join(OUT, 'B站聊天记录.html'), tpl.replace('__DATA__', json), 'utf8');

  console.log('');
  console.log('导出完成：' + results.length + ' 个会话，' + total + ' 条消息');
  console.log('文件位置：' + path.resolve(OUT));
  await browser.close();
}

main().catch(e => { console.log('导出失败：' + e.message); process.exit(1); });
