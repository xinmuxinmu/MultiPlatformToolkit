/**
 * 微博聊天记录导出工具
 * 用法：双击「导出聊天记录.bat」，或命令行 node export.js
 * 首次/登录失效时会弹出浏览器窗口，扫码登录一次即可，之后自动记住。
 */
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

// ---------- 加载 playwright-core（自动在常见位置查找） ----------
function loadChromium() {
  const tryRequire = (p) => { try { return require(p); } catch (_) { return null; } };
  const nodeDir = path.dirname(process.execPath);
  const candidates = [
    'playwright-core',                                              // 本地 node_modules / NODE_PATH
    'playwright',
    path.join(__dirname, 'node_modules', 'playwright-core'),        // 工具目录下
    path.join(__dirname, '..', 'node_modules', 'playwright-core'),  // 项目根目录
    // 便携版 Node：node.exe 往上若干层的 workspace/node_modules
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
// 统一应用通过环境变量把配置与输出指向 data/weibo_chat
const OUT = process.env.WEIBO_CHAT_OUT || path.join(ROOT, '..', '导出数据');
const STATE = process.env.WEIBO_CHAT_STATE || path.join(ROOT, 'state.json');
const PROFILE = process.env.WEIBO_CHAT_PROFILE || path.join(ROOT, 'profile');
const TEMPLATE = path.join(ROOT, 'template.html');
const CHROME = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
].find(p => fs.existsSync(p));

// 统一应用的无头开关通过环境变量下发；单独运行脚本时保持原先的无头导出
const HEADLESS = process.env.WEIBO_HEADLESS !== '0';

const FFMPEG = [
  'D:\\Program\\ffmpeg\\bin\\ffmpeg.exe',
  'C:\\ffmpeg\\bin\\ffmpeg.exe',
].find(p => fs.existsSync(p)) || 'ffmpeg';

const SRC = '&source=209678993&t=';
let MAX_DM = 100000;      // 私信单会话上限
let MAX_GROUP = 3000;     // 群聊单会话上限（群消息量大，默认取最近 3000 条）
const GAP = 180;            // 请求间隔 ms，调小更快但更易触发风控
const H = { Referer: 'https://api.weibo.com/chat' };

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

async function doStatus() {
  const out = { ok: false, name: '', uid: '' };
  if (!fs.existsSync(STATE)) { console.log(JSON.stringify(out)); return; }
  let browser = null;
  try {
    browser = await chromium.launch({ executablePath: CHROME, headless: true });
    const ctx = await browser.newContext({ storageState: JSON.parse(fs.readFileSync(STATE, 'utf8')) });
    const page = await ctx.newPage();
    await page.goto('https://api.weibo.com/chat', { waitUntil: 'domcontentloaded', timeout: 45000 });
    const j = await page.evaluate(async () => {
      try {
        const r = await fetch('/webim/query_primary_info.json?t=' + Date.now(), { credentials: 'include' });
        return await r.json();
      } catch (e) { return {}; }
    });
    if (j.profile && j.profile.id) { out.ok = true; out.name = j.profile.screen_name || ''; out.uid = String(j.profile.id); }
  } catch (_) {}
  console.log(JSON.stringify(out));
  if (browser) await browser.close().catch(() => {});
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

  let browser, ctx, page;

  // 1) 尝试复用已保存的登录态（--login 时跳过，强制重新扫码）
  if (!OPT.login && fs.existsSync(STATE)) {
    try {
      browser = await chromium.launch({ executablePath: CHROME, headless: HEADLESS });
      ctx = await browser.newContext({ storageState: JSON.parse(fs.readFileSync(STATE, 'utf8')) });
      page = await ctx.newPage();
      await page.goto('https://api.weibo.com/chat', { waitUntil: 'domcontentloaded', timeout: 60000 });
      const ok = await page.evaluate(async () => {
        try {
          const r = await fetch('/webim/query_primary_info.json?t=' + Date.now(), { credentials: 'include' });
          const j = await r.json();
          return !!(j.profile && j.profile.id);
        } catch (e) { return false; }
      });
      if (!ok) { await browser.close(); browser = null; }
      else console.log('登录态有效，开始导出…');
    } catch (e) { if (browser) await browser.close().catch(() => {}); browser = null; }
  }

  // 2) 需要登录：弹窗扫码
  if (!browser) {
    console.log('需要登录：请在打开的浏览器窗口里扫码登录微博…');
    ctx = await chromium.launchPersistentContext(PROFILE, {
      executablePath: CHROME, headless: false,
      viewport: { width: 1200, height: 820 },
      args: ['--disable-blink-features=AutomationControlled'],
    });
    page = ctx.pages()[0] || await ctx.newPage();
    await page.goto('https://api.weibo.com/chat', { waitUntil: 'domcontentloaded', timeout: 90000 }).catch(() => {});
    let logged = false;
    for (let i = 0; i < 180; i++) {
      const ok = await page.evaluate(async () => {
        try {
          const r = await fetch('/webim/query_primary_info.json?t=' + Date.now(), { credentials: 'include' });
          const j = await r.json();
          return !!(j.profile && j.profile.id);
        } catch (e) { return false; }
      }).catch(() => false);
      if (ok) { logged = true; break; }
      await page.waitForTimeout(2000);
    }
    if (!logged) { console.log('登录超时，退出。'); await ctx.close(); process.exit(1); }
    fs.writeFileSync(STATE, JSON.stringify(await ctx.storageState(), null, 1), 'utf8');
    console.log('登录成功，已保存登录态。');
    await ctx.close();
    if (OPT.login) { console.log('LOGIN_DONE'); return; }
    browser = await chromium.launch({ executablePath: CHROME, headless: HEADLESS });
    ctx = await browser.newContext({ storageState: JSON.parse(fs.readFileSync(STATE, 'utf8')) });
    page = await ctx.newPage();
    await page.goto('https://api.weibo.com/chat', { waitUntil: 'domcontentloaded', timeout: 60000 });
  }

  const api = (p) => page.evaluate(async (u) => {
    const r = await fetch(u, { credentials: 'include' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return await r.json();
  }, p);

  await page.waitForTimeout(3000);

  const meRes = await api('/webim/query_primary_info.json?t=' + Date.now());
  const prof = meRes.profile || {};
  const myUid = String(prof.id || '');
  console.log('账号：' + prof.screen_name + ' (' + myUid + ')');

  const cl = await api('/webim/2/direct_messages/contacts.json?special_source=3&add_virtual_user=3,4&is_include_group=1&need_back=0,0&is_include_folder=1&count=200' + SRC + Date.now());
  const allContacts = (cl.contacts || []).filter(c => c.user && c.user.id);
  let contacts = allContacts;
  if (ONLY_UID) {
    contacts = allContacts.filter(c => String(c.user.id) === ONLY_UID);
    if (!contacts.length) {
      console.log('未找到要更新的会话 UID：' + ONLY_UID);
      process.exit(2);
    }
  }
  console.log('会话数：' + contacts.length + (ONLY_UID ? '（只更新当前会话）' : ''));

  const convs = [];
  // 增量导出基线：只认由本工具生成（含 fixed 标记）的 raw.json
  const baseFile = path.join(OUT, 'raw.json');
  const base = fs.existsSync(baseFile) ? JSON.parse(fs.readFileSync(baseFile, 'utf8')) : null;
  const baseMap = new Map();
  if (base && base.fixed === true) base.convs.forEach(c => baseMap.set(c.uid, new Set(c.messages.map(m => m.id))));
  const incMode = OPT.mode !== 'full' && baseMap.size > 0;
  console.log(incMode ? '增量模式：基于上次导出补齐新消息' : '全量模式：导出全部消息');

  let idx = 0;
  for (const c of contacts) {
    idx++;
    const u = c.user;
    const name = u.remark || u.name || String(u.id);
    const isGroup = u.type === 2 || u.group_type === 1;
    const cap = isGroup ? MAX_GROUP : MAX_DM;
    const msgs = [];
    let cursor = 0, prev = null;
    const known = baseMap.get(String(u.id));   // 增量模式：该会话已导出的消息 id
    let hitKnown = false;

    while (msgs.length < cap) {
      let j;
      try {
        if (isGroup) {
          j = await api('/webim/groupchat/query_messages.json?convert_emoji=1&query_sender=1&count=20&id=' + u.id + '&max_mid=' + cursor + SRC + Date.now());
        } else {
          j = await api('/webim/2/direct_messages/conversation.json?convert_emoji=1&count=100&max_id=' + cursor + '&uid=' + u.id + '&is_include_group=0&from_contacts=1' + SRC + Date.now());
        }
      } catch (e) { console.log('  ! ' + name + ' ' + e.message); break; }
      const arr = (isGroup ? j.messages : j.direct_messages) || [];
      if (!arr.length) break;
      for (const m of arr) {
        if (known && known.has(String(m.id))) { hitKnown = true; break; }
        msgs.push(m);
      }
      if (hitKnown) break;   // 已碰到上次导出的位置，说明更新的都拉到了
      const next = String(arr[arr.length - 1].id);
      if (next === String(prev) || next === String(cursor)) break;
      prev = next; cursor = next;
      await sleep(GAP);
    }

    const norm = msgs.map(m => {
      if (isGroup) {
        return {
          id: String(m.id), text: m.content || '', ts: (m.time || 0) * 1000,
          from: String(m.from_uid || ''),
          fromName: (m.from_user && m.from_user.screen_name) || '',
          avatar: (m.from_user && m.from_user.profile_image_url) || '',
          media: m.media_type || 0, fids: m.att_ids || [], recall: m.recall_status || 0, large: false,
        };
      }
      return {
        id: String(m.id), text: m.text || '', ts: Date.parse(m.created_at) || 0,
        from: String(m.sender_id || ''),
        fromName: m.sender_screen_name || (m.sender && m.sender.screen_name) || '',
        avatar: (m.sender && (m.sender.profile_image_url || m.sender.avatar_large)) || '',
        media: m.media_type || 0, fids: m.att_ids || [], recall: m.recall_status || 0,
        large: m.isLargeDm === true,
      };
    }).sort((a, b) => a.ts - b.ts || (a.id < b.id ? -1 : 1));

    // 增量：把历史消息合并回来
    let merged = norm;
    if (known) {
      const bc = base.convs.find(c => c.uid === String(u.id));
      if (bc && bc.messages.length) {
        const have = new Set(norm.map(m => m.id));
        merged = norm.concat(bc.messages.filter(m => !have.has(m.id)));
      }
    }
    merged.sort((a, b) => a.ts - b.ts || (a.id < b.id ? -1 : 1));

    convs.push({
      uid: String(u.id), name, isGroup,
      avatar: u.profile_image_url || u.avatar_large || '',
      count: merged.length, messages: merged,
    });
    const added = norm.length;
    if (added) console.log('[' + idx + '/' + contacts.length + '] ' + name + (isGroup ? ' (群)' : '') + ' -> +' + added + ' 条（共 ' + merged.length + '）');
  }

  // 只更新单个会话时，把上次导出里的其它会话原样保留，避免 raw.json 只剩一个人
  if (ONLY_UID && base && Array.isArray(base.convs)) {
    const have = new Set(convs.map(c => String(c.uid)));
    for (const c of base.convs) {
      if (!have.has(String(c.uid))) convs.push(c);
    }
  }

  // ---- 长消息补全：列表接口对 isLargeDm 只返回截断文本，需单独取全文 ----
  const fillList = [];
  convs.forEach(c => c.messages.forEach(m => { if (m.large) fillList.push({ id: m.id, msg: m }); }));
  console.log('待补全的长消息：' + fillList.length);
  for (let i = 0; i < fillList.length; i += 20) {
    const batch = fillList.slice(i, i + 20);
    try {
      const j = await api('/webim/2/direct_messages/show_batch.json?is_complete=1&is_encoded=0&dmids=' + batch.map(b => b.id).join(',') + SRC + Date.now());
      const full = new Map();
      (j || []).forEach(m => full.set(String(m.id), m.text));
      batch.forEach(b => {
        const t = full.get(b.id);
        if (typeof t === 'string' && t.length > (b.msg.text || '').length) { b.msg.text = t; delete b.msg.large; }
      });
    } catch (_) {}
    await sleep(200);
  }

  // ---- 媒体：图片 / 语音 / 视频 ----
  const jobs = [];
  convs.forEach(c => c.messages.forEach(m => {
    if (!m.fids || !m.fids.length) return;
    const kind = m.media === 4 ? 'audio' : (m.media === 10 ? 'video' : 'image');
    jobs.push({ fid: String(m.fids[0]), kind });
  }));
  const seen = new Set();
  const uniq = jobs.filter(j => { const k = j.kind + ':' + j.fid; if (seen.has(k)) return false; seen.add(k); return true; });
  console.log('待处理媒体：' + uniq.length);

  const mediaMap = new Map();
  let done = 0;
  for (const job of uniq) {
    try {
      if (job.kind === 'image') {
        const cached = findExisting('images/' + job.fid, ['jpg', 'png', 'gif']);
        if (cached) { mediaMap.set('image:' + job.fid, cached); done++; continue; }
        const u = 'https://upload.api.weibo.com/2/mss/msget_thumbnail?fid=' + job.fid + '&high=600&width=600&size=600,600&source=209678993&imageType=origin';
        const r = await ctx.request.get(u, { timeout: 20000, headers: H });
        if (r.status() === 200) {
          const ct = r.headers()['content-type'] || '';
          const ext = ct.includes('png') ? 'png' : (ct.includes('gif') ? 'gif' : 'jpg');
          const rel = 'images/' + job.fid + '.' + ext;
          const buf = await r.body();
          fs.writeFileSync(path.join(OUT, rel), buf);
          // HEIC（iPhone 实况照片等）浏览器不识别，转成 JPEG
          if (buf.slice(4, 14).toString('latin1').indexOf('ftypheic') >= 0) {
            try {
              const tmp = 'images/_cvt_' + job.fid + '.jpg';
              execSync('"' + FFMPEG + '" -y -loglevel error -i "' + path.join(OUT, rel) + '" -update 1 "' + path.join(OUT, tmp) + '"', { stdio: 'pipe' });
              fs.renameSync(path.join(OUT, tmp), path.join(OUT, 'images/' + job.fid + '.jpg'));
              fs.unlinkSync(path.join(OUT, rel));
              mediaMap.set('image:' + job.fid, 'images/' + job.fid + '.jpg');
              done++; continue;
            } catch (_) {}
          }
          mediaMap.set('image:' + job.fid, rel);
        }
      } else {
        if (job.kind === 'video') {
          const cv = findExisting('videos/' + job.fid, ['mp4']);
          if (cv) { mediaMap.set('video:' + job.fid, cv); done++; continue; }
        }
        const r = await ctx.request.get('https://upload.api.weibo.com/2/mss/msget?source=209678993&fid=' + job.fid, { timeout: 90000, headers: H });
        if (r.status() === 200) {
          const buf = await r.body();
          if (job.kind === 'video') {
            fs.mkdirSync(path.join(OUT, 'videos'), { recursive: true });
            const rel = 'videos/' + job.fid + '.mp4';
            fs.writeFileSync(path.join(OUT, rel), buf);
            mediaMap.set('video:' + job.fid, rel);
          } else {
            fs.mkdirSync(path.join(OUT, 'audio'), { recursive: true });
            const amrRel = 'audio/' + job.fid + '.amr';
            const amrAbs = path.join(OUT, amrRel);
            fs.writeFileSync(amrAbs, buf);
            mediaMap.set('raw:' + job.fid, amrRel);
            const mp3rel = 'audio/' + job.fid + '.mp3';
            try {
              execSync('"' + FFMPEG + '" -y -i "' + amrAbs + '" -codec:a libmp3lame -q:a 5 "' + path.join(OUT, mp3rel) + '"', { stdio: 'pipe' });
              mediaMap.set('audio:' + job.fid, mp3rel);
            } catch (_) {
              mediaMap.set('audio:' + job.fid, amrRel);
            }
          }
        }
      }
    } catch (_) {}
    done++;
    if (done % 40 === 0) console.log('  媒体 ' + done + '/' + uniq.length);
    await sleep(50);
  }

  convs.forEach(c => c.messages.forEach(m => {
    if (m.pics || m.audio || m.video) return;   // 增量基线：已本地化过，不要重置
    const f = m.fids && m.fids[0] ? String(m.fids[0]) : null;
    m.pics = []; m.audio = ''; m.audioRaw = ''; m.video = '';
    if (f) {
      if (m.media === 4) { m.audio = mediaMap.get('audio:' + f) || ''; m.audioRaw = mediaMap.get('raw:' + f) || ''; }
      else if (m.media === 10) m.video = mediaMap.get('video:' + f) || '';
      else m.pics = (m.fids || []).map(x => mediaMap.get('image:' + String(x))).filter(Boolean);
    }
    delete m.fids;
  }));

  // ---- 头像 ----
  fs.mkdirSync(path.join(OUT, 'avatars'), { recursive: true });
  const avSet = new Set();
  const meAvatarSrc = prof.profile_image_url || prof.avatar_large || '';
  if (meAvatarSrc) avSet.add(meAvatarSrc);
  convs.forEach(c => { if (c.avatar && c.avatar.startsWith('http')) avSet.add(c.avatar); c.messages.forEach(m => { if (m.avatar && m.avatar.startsWith('http')) avSet.add(m.avatar); }); });
  const avs = [...avSet];
  const avMap = new Map();
  for (const a of avs) {
    const hb = 'avatars/' + require('crypto').createHash('md5').update(a.split('?')[0]).digest('hex').slice(0, 16);
    const cached = findExisting(hb, ['jpg', 'png', 'gif']);
    if (cached) { avMap.set(a, cached); continue; }
    try {
      const r = await ctx.request.get(a, { timeout: 15000, headers: H });
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
  convs.forEach(c => {
    if (!c.avatar || !c.avatar.startsWith('avatars/')) c.avatar = avMap.get(c.avatar) || '';
    c.messages.forEach(m => {
      if (m.avatar && m.avatar.startsWith('avatars/')) return;   // 已是本地路径
      m.avatar = avMap.get(m.avatar) || '';
    });
  });

  convs.sort((a, b) => {
    const ca = a.count > 0 ? 1 : 0, cb = b.count > 0 ? 1 : 0;
    if (ca !== cb) return cb - ca;
    const ta = a.messages.length ? a.messages[a.messages.length - 1].ts : 0;
    const tb = b.messages.length ? b.messages[b.messages.length - 1].ts : 0;
    return tb - ta;
  });

  const total = convs.reduce((s, c) => s + c.count, 0);
  const payload = {
    exportedAt: new Date().toISOString(),
    me: { uid: myUid, name: prof.screen_name || '', avatar: avMap.get(meAvatarSrc) || '' },
    fixed: true, total, convs,
  };
  fs.writeFileSync(path.join(OUT, 'raw.json'), JSON.stringify(payload, null, 1), 'utf8');

  const tpl = fs.readFileSync(TEMPLATE, 'utf8');
  const json = JSON.stringify(payload).replace(/</g, '\\u003c');
  fs.writeFileSync(path.join(OUT, '微博聊天记录.html'), tpl.replace('__DATA__', json), 'utf8');

  console.log('');
  console.log('导出完成：' + convs.length + ' 个会话，' + total + ' 条消息');
  console.log('文件位置：' + path.resolve(OUT));
  await browser.close();
}

main().catch(e => { console.log('导出失败：' + e.message); process.exit(1); });
