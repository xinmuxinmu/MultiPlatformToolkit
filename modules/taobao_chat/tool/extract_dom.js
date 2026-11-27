(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clean = (s) => String(s || '').replace(/\u00a0/g, ' ').replace(/[ \t]+\n/g, '\n').trim();

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
    const m = /(\d{4}-\d{2}-\d{2})[\sT]+(\d{1,2}:\d{2}(?::\d{2})?)/.exec(raw || '');
    if (m) return { date: m[1], time: m[2] };
    const t2 = /(\d{1,2}:\d{2}(?::\d{2})?)/.exec(raw || '');
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
      if (g) extras.push('[商品卡片] ' + clean(g.innerText).replace(/\n+/g, ' '));
      if (!texts.length && !imgs.length && !extras.length) {
        let whole = clean(box.innerText);
        if (nick) whole = whole.replace(nick, '');
        if (t.time) whole = whole.replace(t.time, '');
        whole = clean(whole);
        if (whole) texts.push(whole);
      }
      const body = texts.concat(extras).join('\n');
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
      const k = [r.date, r.time, r.sender, r.text, r.imgUrl].join('\u0001');
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
})()