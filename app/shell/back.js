/* 统一入口的“返回首页”按钮 + 浏览器后退兜底。
 *
 * 浏览器 app 模式窗口如果直接打开某个工具页，窗口自带的后退按钮没有历史
 * 记录可退。这里在页面加载时补一个哨兵历史项，用户点后退会落到首页；同时
 * 在右下角放一个明确的返回按钮。嵌入式 iframe（统一抖音页面）里不显示，
 * 截图页也不显示。
 */
(function () {
  'use strict';

  var path = location.pathname.replace(/\/+$/, '') || '/';
  if (window.top !== window.self) return;
  if (path === '/' || path === '/douyin') return;
  if (/\/screenshot$/.test(path) || /[?&]screenshot=1/.test(location.search)) return;

  // 直接打开工具页（没有历史）时，让浏览器后退按钮也能回到首页。
  try {
    if (history.length <= 1) {
      history.pushState({ backHome: true }, '', location.href);
      window.addEventListener('popstate', function () {
        location.replace('/');
      });
    }
  } catch (e) {
    /* file:// 或受限环境忽略 */
  }

  function mount() {
    if (document.getElementById('app-back-home')) return;
    var style = document.createElement('style');
    style.textContent =
      '#app-back-home{position:fixed;right:16px;bottom:16px;z-index:2147483647;' +
      'display:inline-flex;align-items:center;gap:7px;padding:9px 14px;border-radius:999px;' +
      'background:rgba(255,255,255,.94);color:#334155;border:1px solid rgba(148,163,184,.5);' +
      'box-shadow:0 4px 16px rgba(15,23,42,.16);font:600 13px/1 -apple-system,' +
      'BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;text-decoration:none;' +
      'cursor:pointer;backdrop-filter:blur(8px);opacity:.9;transition:opacity .15s,border-color .15s}' +
      '#app-back-home:hover{opacity:1;border-color:#93b4f8;color:#2563eb}' +
      '#app-back-home svg{width:15px;height:15px;flex:none}';
    document.head.appendChild(style);

    var a = document.createElement('a');
    a.id = 'app-back-home';
    a.href = '/';
    a.title = '返回多平台工具首页';
    a.setAttribute('aria-label', '返回多平台工具首页');
    a.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M19 12H5"/><path d="M12 19l-7-7 7-7"/></svg><span>返回首页</span>';
    document.body.appendChild(a);
  }

  if (document.body) mount();
  else document.addEventListener('DOMContentLoaded', mount);
})();
