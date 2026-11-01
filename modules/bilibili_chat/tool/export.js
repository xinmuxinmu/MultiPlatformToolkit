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
const MSGS_API = 'https://api.vc.bilibili.com/svr/svr/fetch_session_msgs';
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
function parseContent(m) {
  // 返回 { text, pic, recall }
  const res = { text: '', pic: '', recall: false };
  if (m.msg_status === 2 || m.msg_status === 4) { res.recall = true; return res; }
  let c = {};
  try { c = JSON.parse(m.msg_content || '{}'); } catch (_) { c = { content: String(m.msg_content || '') }; }
  const body = (c && typeof c.content === 'string') ? c.content : '';
  if (m.msg_type === 2 || (body && /^https?:\/\//.test(body) && (c.width || c.height || c.imageType))) {
    res.pic = body;                       // 图片消息：content 即图片 URL
    return res;
  }
  if (m.msg_type === 5) {                 // 系统消息
    res.text = body || '[系统消息]';
    return res;
  }
  if (m.msg_type === 3 && body && /^https?:\/\//.test(body)) { // 语音消息
    res.text = '[语音消息]';
    res.pic = ''; // 语音 URL 放 audio 字段由调用方处理
    res.audio = body;
    return res;
  }
  res.text = body || '[非文本消息]';
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
  for (const s of sessions) {
    const talkerId = String(s.talker_id || (s.account_info && s.account_info.mid) || '');
    if (!talkerId) continue;
    const info = s.account_info || {};
    const isGroup = s.session_type === 2 || s.group_type === 1;
    convs.push({
      uid: talkerId,
      name: info.username || info.nickname || talkerId,
      isGroup,
      avatar: info.face || '',
      lastTs: s.session_ts || 0,
      session: s,
    });
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
    const known = baseMap.get(c.uid);   // 增量模式：该会话已导出的消息 id（msg_seq）
    let hitKnown = false;

    for (let round = 0; round < 100000 && msgs.length < cap; round++) {
      const u = MSGS_API + `?talker_id=${c.uid}&session_type=1&begin_seq=0&end_seq=${endSeq}&size=${MSG_SIZE}&build=0&mobi_app=web`;
      let d;
      try { d = await api(u); } catch (e) { console.log('  ! ' + c.name + ' ' + e.message); break; }
      const arr = d.messages || [];
      if (!arr.length) break;
      let minSeq = Infinity;
      for (const m of arr) {
        const seq = Number(m.msg_seq) || 0;
        if (seq > 0 && seq < minSeq) minSeq = seq;
        if (known && known.has(String(m.msg_seq))) { hitKnown = true; break; }
        msgs.push(m);
      }
      if (hitKnown) break;   // 已碰到上次导出的位置，说明更新的都拉到了
      if (!isFinite(minSeq) || (endSeq > 0 && minSeq >= endSeq)) break;  // 游标不动就停
      endSeq = minSeq;
      await sleep(GAP);
    }

    const norm = msgs.map(m => {
      const p = parseContent(m);
      const self = String(m.sender_uid) === myUid;
      return {
        id: String(m.msg_seq), text: p.text, ts: (m.timestamp || 0) * 1000,
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
  if (ONLY_UID && base && Array.isArray(base.convs)) {
    const have = new Set(results.map(c => c.uid));
    for (const c of base.convs) {
      if (!have.has(c.uid)) results.push(c);
    }
  }

  // ---- 媒体：图片 ----
  const jobs = [];
  results.forEach(c => c.messages.forEach(m => { if (m.picUrl) jobs.push({ m, kind: 'image' }); }));
  console.log('待处理媒体：' + jobs.length);
  let done = 0;
  for (const job of jobs) {
    const m = job.m;
    try {
      const key = 'i_' + m.id;
      const cached = findExisting('images/' + key, ['jpg', 'png', 'gif', 'webp']);
      if (cached) { m.pics = [cached]; done++; continue; }
      const r = await ctx.request.get(m.picUrl, { timeout: 20000 });
      if (r.status() === 200) {
        const ct = r.headers()['content-type'] || '';
        const ext = ct.includes('png') ? 'png' : (ct.includes('gif') ? 'gif' : (ct.includes('webp') ? 'webp' : 'jpg'));
        fs.mkdirSync(path.join(OUT, 'images'), { recursive: true });
        const rel = 'images/' + key + '.' + ext;
        fs.writeFileSync(path.join(OUT, rel), await r.body());
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
