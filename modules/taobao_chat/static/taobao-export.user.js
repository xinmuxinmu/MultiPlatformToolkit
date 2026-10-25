// ==UserScript==
// @name         淘宝旺旺聊天记录导出（买家侧 H5）
// @namespace    local.tb-ww-export
// @version      1.0
// @description  在 market.m.taobao.com 的旺旺聊天页导出与卖家的完整聊天记录，自动向上加载历史，支持 JSON / Markdown / CSV
// @author       local
// @match        *://market.m.taobao.com/app/im/chat-core/*
// @match        *://market.m.taobao.com/app/im/chat/*
// @match        *://market.wapa.taobao.com/app/im/chat-core/*
// @match        *://market.wapa.taobao.com/app/im/chat/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

/*
 * 目标页面：淘宝旺旺网页版聊天（买家侧）
 *   https://market.m.taobao.com/app/im/chat/index.html?&uid=cntaobaoXXX&type=web
 * 外层页面会嵌入 iframe：
 *   https://market.m.taobao.com/app/im/chat-core/index.html?<同样的 query>
 * 聊天内容在 iframe 里，本脚本作用于该 iframe。
 *
 * 真实 DOM（来自 chat-core 0.0.59 源码）：
 *   .message-list
 *     .message-item-line                 一条消息
 *       .message-item[.self]             自己发的带 self
 *         .item[.self]
 *           img.headPic
 *           div
 *             .first-line
 *               .nick                     发送人昵称
 *               .time                     2026-10-19 20:31:22
 *             .message-text               文本气泡
 *             img.imui-msg-img            图片
 *             video.im-message-video      视频
 *             audio                       语音
 *       系统消息没有 .message-item，只有 .content
 */

(function () {
    'use strict';

    if (window.__tbwwInjected) return;
    window.__tbwwInjected = true;

    var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
    var log = function () {
        var a = Array.prototype.slice.call(arguments);
        a.unshift('[旺旺导出]');
        console.log.apply(console, a);
    };

    /* ============ 定位滚动容器 ============ */
    function findScroller(root) {
        var nodes = (root || document).querySelectorAll('div');
        var best = null, bestH = 0;
        for (var i = 0; i < nodes.length; i++) {
            var el = nodes[i];
            var st = getComputedStyle(el);
            var oy = st.overflowY;
            if (!/(auto|scroll)/.test(oy)) continue;
            var over = el.scrollHeight - el.clientHeight;
            if (over > 40 && over > bestH) { bestH = over; best = el; }
        }
        return best;
    }

    /* ============ 「加载更多」按钮 ============ */
    function findLoadMore() {
        var list = document.querySelector('.message-list');
        if (!list) return null;
        var spans = list.querySelectorAll('span');
        for (var i = 0; i < spans.length; i++) {
            var t = (spans[i].innerText || '').trim();
            if (t.indexOf('加载更多') >= 0 || t.indexOf('下拉') >= 0) return spans[i];
        }
        return null;
    }

    /* ============ 自动向上加载全部历史 ============ */
    async function loadAllHistory(onProgress) {
        var list = document.querySelector('.message-list');
        if (!list) throw new Error('没找到聊天列表，请确认已打开具体会话');

        var sc = findScroller(list);
        if (!sc) {
            // 兜底：整个文档可滚动
            if (document.scrollingElement && document.scrollingElement.scrollHeight > innerHeight) {
                sc = document.scrollingElement;
            }
        }
        var origin = sc ? sc.scrollTop : 0;

        var last = -1, stable = 0, round = 0;
        var MAX = 120;

        while (round < MAX && stable < 3) {
            var before = document.querySelectorAll('.message-item-line').length;

            if (sc) sc.scrollTop = 0;
            await sleep(220);

            var more = findLoadMore();
            if (more) {
                try { more.click(); } catch (e) {}
                await sleep(750);
            } else {
                await sleep(320);
            }

            var now = document.querySelectorAll('.message-item-line').length;
            if (now === last && now === before) stable++; else stable = 0;
            last = now;
            round++;

            if (onProgress) onProgress(round, now);
            log('第 ' + round + ' 轮，已加载 ' + now + ' 条');
        }

        if (sc) sc.scrollTop = origin;
        await sleep(120);
        return document.querySelectorAll('.message-item-line').length;
    }

    /* ============ 解析时间 ============ */
    function splitTime(raw) {
        var m = /(\d{4}-\d{2}-\d{2})[\sT]+(\d{1,2}:\d{2}(?::\d{2})?)/.exec(raw || '');
        if (m) return { date: m[1], time: m[2] };
        var t2 = /(\d{1,2}:\d{2}(?::\d{2})?)/.exec(raw || '');
        return { date: '', time: t2 ? t2[1] : '' };
    }

    function clean(s) {
        return String(s || '').replace(/\u00a0/g, ' ').replace(/[ \t]+\n/g, '\n').trim();
    }

    /* ============ 提取单条消息 ============ */
    function extractMessages() {
        var lines = document.querySelectorAll('.message-item-line');
        var out = [];
        var selfNick = '';

        Array.prototype.forEach.call(lines, function (line) {
            var box = line.querySelector('.message-item');

            /* --- 系统提示（“以上是历史消息”之类） --- */
            if (!box) {
                var cEl = line.querySelector('.content');
                var ct = clean(cEl ? cEl.innerText : line.innerText);
                if (ct) {
                    out.push({ date: '', time: '', sender: '系统', text: ct, imgUrl: '', self: false });
                }
                return;
            }

            var isSelf = box.classList.contains('self');
            var nickEl = box.querySelector('.nick');
            var nick = clean(nickEl ? nickEl.innerText : '');
            var timeEl = box.querySelector('.time');
            var t = splitTime(clean(timeEl ? timeEl.innerText : ''));

            if (isSelf && nick) selfNick = nick;

            var texts = [];
            var imgs = [];
            var extras = [];

            /* 文本气泡 */
            var textEl = box.querySelector('.message-text');
            if (textEl) {
                var tx = clean(textEl.innerText);
                if (tx) texts.push(tx);
            }

            /* 图片 */
            Array.prototype.forEach.call(box.querySelectorAll('img.imui-msg-img, img.J_imImage'), function (img) {
                if (img.src) imgs.push(img.src);
            });

            /* 视频 / 语音 / 文件 / 商品卡片 */
            var v = box.querySelector('video.im-message-video');
            if (v && v.src) extras.push('[视频] ' + v.src);

            var a = box.querySelector('audio');
            if (a && a.src) extras.push('[语音] ' + a.src);

            var f = box.querySelector('.file-msg');
            if (f) extras.push('[文件] ' + clean(f.innerText));

            var g = box.querySelector('.goods-item');
            if (g) extras.push('[商品卡片] ' + clean(g.innerText).replace(/\n+/g, ' '));

            /* 兜底：没有已知结构就用整块文本，去掉昵称和时间 */
            if (!texts.length && !imgs.length && !extras.length) {
                var whole = clean(box.innerText);
                if (nick) whole = whole.replace(nick, '');
                if (t.time) whole = whole.replace(t.time, '');
                whole = clean(whole);
                if (whole) texts.push(whole);
            }

            var body = texts.concat(extras).join('\n');
            if (!body && !imgs.length) return;

            var sender = nick || (isSelf ? (selfNick || '我') : '对方');

            out.push({
                date: t.date,
                time: t.time,
                sender: sender,
                text: body,
                imgUrl: imgs[0] || '',
                self: isSelf
            });

            /* 多图：拆成多条，避免丢图 */
            for (var i = 1; i < imgs.length; i++) {
                out.push({
                    date: t.date, time: t.time, sender: sender,
                    text: '', imgUrl: imgs[i], self: isSelf
                });
            }
        });

        /* 去重 */
        var seen = Object.create(null);
        var dedup = [];
        out.forEach(function (r) {
            var k = [r.date, r.time, r.sender, r.text, r.imgUrl].join('\u0001');
            if (seen[k]) return;
            seen[k] = 1;
            dedup.push(r);
        });

        return dedup;
    }

    /* ============ 对方昵称 ============ */
    function partnerName() {
        var uid = '';
        try {
            var sp = new URLSearchParams(location.search);
            uid = sp.get('uid') || '';
        } catch (e) {}
        uid = uid.replace(/^cntaobao/, '');

        // 从消息里挑一个非“我”的发送人
        var counts = {};
        document.querySelectorAll('.message-item-line .nick').forEach(function (n) {
            var t = clean(n.innerText);
            if (t) counts[t] = (counts[t] || 0) + 1;
        });
        var cands = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; });
        return uid || cands[0] || '淘宝会话';
    }

    /* ============ 导出格式 ============ */
    function buildMarkdown(msgs, partner) {
        var lines = ['# 与 ' + partner + ' 的聊天记录', ''];
        lines.push('- 消息数：' + msgs.length);
        var dates = msgs.map(function (m) { return m.date; }).filter(Boolean).sort();
        if (dates.length) lines.push('- 时间范围：' + dates[0] + ' ~ ' + dates[dates.length - 1]);
        lines.push('- 导出时间：' + new Date().toLocaleString('zh-CN'), '', '---', '');
        var lastDate = '';
        msgs.forEach(function (m) {
            if (m.date && m.date !== lastDate) { lastDate = m.date; lines.push('## ' + m.date, ''); }
            lines.push('**' + [m.time, m.sender].filter(Boolean).join(' ') + '**', '');
            if (m.text) lines.push(m.text, '');
            if (m.imgUrl) lines.push('![](' + m.imgUrl + ')', '');
        });
        return lines.join('\n');
    }

    function buildCsv(msgs) {
        var rows = [['日期', '时间', '发送人', '内容', '图片链接']];
        msgs.forEach(function (m) { rows.push([m.date, m.time, m.sender, m.text, m.imgUrl]); });
        return '\ufeff' + rows.map(function (r) {
            return r.map(function (v) { return '"' + String(v || '').replace(/"/g, '""') + '"'; }).join(',');
        }).join('\r\n');
    }

    function buildJson(msgs, partner) {
        return JSON.stringify({
            user: partner,
            exportTime: new Date().toLocaleString('zh-CN'),
            count: msgs.length,
            chats: msgs.map(function (m) {
                return { date: m.date, time: m.time, sender: m.sender, text: m.text, imgUrl: m.imgUrl };
            })
        }, null, 2);
    }

    function download(name, content, mime) {
        var blob = new Blob([content], { type: (mime || 'text/plain') + ';charset=utf-8' });
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
    }

    /* ============ UI ============ */
    var panel, statusEl, btnAll, btnNow, btnMd, btnCsv, running = false;
    var lastMsgs = null, lastName = '';

    function buildUI() {
        if (document.getElementById('tbww-panel')) return;

        panel = document.createElement('div');
        panel.id = 'tbww-panel';
        panel.style.cssText = [
            'position:fixed', 'right:16px', 'bottom:16px', 'z-index:2147483647',
            'width:216px', 'background:#fff', 'border:1px solid #e6e8eb', 'border-radius:12px',
            'box-shadow:0 6px 24px rgba(0,0,0,.14)', 'padding:12px',
            'font:13px/1.5 -apple-system,BlinkMacSystemFont,"Microsoft YaHei",sans-serif',
            'color:#1f2329', 'user-select:none'
        ].join(';');

        panel.innerHTML = ''
            + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">'
            + '  <b style="font-size:13.5px;">旺旺记录导出</b>'
            + '  <span id="tbww-min" style="cursor:pointer;color:#8f959e;font-size:16px;line-height:1;">−</span>'
            + '</div>'
            + '<div id="tbww-status" style="font-size:12px;color:#646a73;margin-bottom:10px;">就绪</div>'
            + '<button id="tbww-all" style="width:100%;padding:8px;background:#ff5000;color:#fff;border:none;border-radius:8px;cursor:pointer;margin-bottom:6px;">导出全部（自动加载历史）</button>'
            + '<button id="tbww-now" style="width:100%;padding:7px;background:#fff;color:#1f2329;border:1px solid #e6e8eb;border-radius:8px;cursor:pointer;margin-bottom:8px;">只导出已加载的</button>'
            + '<div style="display:flex;gap:6px;">'
            + '  <button id="tbww-md" style="flex:1;padding:6px;background:#f5f6f8;border:1px solid #e6e8eb;border-radius:7px;cursor:pointer;font-size:12px;">Markdown</button>'
            + '  <button id="tbww-csv" style="flex:1;padding:6px;background:#f5f6f8;border:1px solid #e6e8eb;border-radius:7px;cursor:pointer;font-size:12px;">CSV</button>'
            + '</div>';

        document.body.appendChild(panel);

        statusEl = panel.querySelector('#tbww-status');
        btnAll = panel.querySelector('#tbww-all');
        btnNow = panel.querySelector('#tbww-now');
        btnMd = panel.querySelector('#tbww-md');
        btnCsv = panel.querySelector('#tbww-csv');

        var min = panel.querySelector('#tbww-min');
        var collapsed = false;
        min.onclick = function () {
            collapsed = !collapsed;
            Array.prototype.forEach.call(panel.children, function (c, i) {
                if (i > 0) c.style.display = collapsed ? 'none' : '';
            });
            min.innerText = collapsed ? '+' : '−';
        };

        btnAll.onclick = function () { runExport(true); };
        btnNow.onclick = function () { runExport(false); };

        btnMd.onclick = function () {
            if (!lastMsgs) return setStatus('请先导出一次');
            download('旺旺聊天_' + lastName + '.md', buildMarkdown(lastMsgs, lastName), 'text/markdown');
            setStatus('已下载 Markdown');
        };
        btnCsv.onclick = function () {
            if (!lastMsgs) return setStatus('请先导出一次');
            download('旺旺聊天_' + lastName + '.csv', buildCsv(lastMsgs), 'text/csv');
            setStatus('已下载 CSV');
        };
    }

    function setStatus(t) { if (statusEl) statusEl.textContent = t; }

    async function runExport(withHistory) {
        if (running) return;
        running = true;
        btnAll.disabled = btnNow.disabled = true;
        btnAll.style.opacity = btnNow.style.opacity = '.6';

        try {
            if (withHistory) {
                setStatus('正在向上加载历史…');
                var total = await loadAllHistory(function (round, n) {
                    setStatus('加载历史 第 ' + round + ' 轮，已 ' + n + ' 条');
                });
                log('加载完成，共 ' + total + ' 条 DOM 节点');
            }

            setStatus('正在解析…');
            var msgs = extractMessages();
            if (!msgs.length) {
                setStatus('没解析到消息，请确认会话已打开');
                alert('没解析到消息。\n\n可能原因：\n1. 当前没有打开具体会话\n2. 淘宝改版导致 DOM 变化\n\n可在 Console 里把 [旺旺导出] 相关日志发我。');
                return;
            }

            var partner = partnerName();
            lastName = partner;
            lastMsgs = msgs;

            var stamp = new Date();
            var tag = stamp.getFullYear() + ('0' + (stamp.getMonth() + 1)).slice(-2) + ('0' + stamp.getDate()).slice(-2)
                + '_' + ('0' + stamp.getHours()).slice(-2) + ('0' + stamp.getMinutes()).slice(-2);

            download('旺旺聊天_' + partner + '_' + tag + '.json', buildJson(msgs, partner), 'application/json');
            setStatus('完成：' + msgs.length + ' 条消息');
        } catch (e) {
            log('导出失败', e);
            setStatus('失败：' + e.message);
            alert('导出失败：' + e.message);
        } finally {
            running = false;
            btnAll.disabled = btnNow.disabled = false;
            btnAll.style.opacity = btnNow.style.opacity = '1';
        }
    }

    /* ============ 启动 ============ */
    function boot() {
        // 只在真正的聊天 iframe 里挂 UI
        if (!document.querySelector('.message-list')) {
            // 页面可能还没渲染完，重试几次
            if ((boot.tries = (boot.tries || 0) + 1) < 20) {
                setTimeout(boot, 800);
            }
            return;
        }
        buildUI();
        log('已就绪');
    }

    if (document.readyState === 'complete' || document.readyState === 'interactive') {
        setTimeout(boot, 600);
    } else {
        window.addEventListener('load', function () { setTimeout(boot, 600); });
    }

    /* 暴露给自动化测试 */
    window.__tbww = { extractMessages: extractMessages, loadAllHistory: loadAllHistory, findScroller: findScroller, splitTime: splitTime };
})();
