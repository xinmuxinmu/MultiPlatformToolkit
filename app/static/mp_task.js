/* 任务页主题同步
 *
 * 嵌在签到页 iframe 里时，父页面用 postMessage({type:'mp:settings'}) 下发主题；
 * 单独打开时读 localStorage（同源共享）或 /api/ui/settings 兜底。
 * 只做主题，不做翻译——这些页面的文案是写死的，过一遍词典反而容易改坏。
 */
(function () {
  'use strict';
  var THEMES = ['dark', 'light', 'ocean', 'purple'];

  function apply(theme) {
    var t = THEMES.indexOf(theme) >= 0 ? theme : 'light';
    if (document.documentElement.getAttribute('data-theme') !== t) {
      document.documentElement.setAttribute('data-theme', t);
    }
  }

  function stored() {
    try { return localStorage.getItem('mp-theme'); } catch (e) { return null; }
  }

  apply(stored() || 'light');

  window.addEventListener('message', function (e) {
    if (e.origin !== location.origin) return;
    var d = e.data || {};
    if (d.type === 'mp:settings' && d.theme) apply(d.theme);
  });

  window.addEventListener('storage', function (e) {
    if (e.key === 'mp-theme') apply(e.newValue);
  });

  // 首次打开、父页面还没下发过时，从后端设置里补一次
  if (!stored()) {
    fetch('/api/ui/settings', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.theme) apply(d.theme); })
      .catch(function () {});
  }
})();
