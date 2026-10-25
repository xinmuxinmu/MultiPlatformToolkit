"""抖音表情包操作 Mixin 模块

将 Douyin 类中表情包相关方法抽离为独立 Mixin，
Douyin 类通过继承 StickerMixin 获得这些方法。
方法体内的 driver 统一用 self.driver（与 __init__ 一致，行为不变）。
"""
import time
import random
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from spark_backend.utils import TrueString, _dbg_report


class StickerMixin:
    """表情包相关操作混入类"""

    _sticker_categories = []
    _sticker_src_map = {}

    def _open_emoji_panel(self):
        """点击表情按钮打开表情面板。
        两步法：JS 定位按钮并标记 → ActionChains 真实点击（React 合成事件需要真实点击）。
        """
        driver = self.driver
        # Step 1: JS 找到表情按钮，标记 data-emoji-btn 属性
        result = driver.execute_script(r'''
            function isUnsafe(el) {
                if (!el) return true;
                if (el.tagName === 'A') return true;
                if (el.getAttribute && el.getAttribute('href')) return true;
                var cls = (el.getAttribute && el.getAttribute('class')) ? el.getAttribute('class') : ((el.className||'')+'').toString();
                if (/MessageItem|MessageBubble/i.test(cls)) return true;
                if (/avatar|Avatar|user|User|nick|Nick|link|Link|profile|Profile/i.test(cls)) return true;
                if (el.getAttribute && el.getAttribute('contenteditable') === 'true') return true;
                var clsLower = cls.toLowerCase();
                if (clsLower.indexOf('send') !== -1) return true;
                var txt = (el.textContent||'').trim();
                if (txt.indexOf('发送') !== -1 || txt.indexOf('Send') !== -1) return true;
                return false;
            }

            // 清除旧标记
            var old = document.querySelectorAll('[data-emoji-btn]');
            for (var i = 0; i < old.length; i++) old[i].removeAttribute('data-emoji-btn');

            var bottomLimit = window.innerHeight * 0.5;

            // 策略1：精准匹配已确认的表情按钮 class
            var preciseSelectors = [
                '[class*="messageMsgInputiconAction"]',
                '[class*="componentsemojiemojiPanel"]',
                '[class*="emojiBtn"]', '[class*="emoji-btn"]', '[class*="EmojiBtn"]',
                '[class*="emojiPicker"]', '[class*="emoji-picker"]',
                '[data-e2e*="emoji"]', '[class*="EmojiIcon"]',
                '[class*="chatEmoji"]', '[class*="editorEmoji"]',
                'button[aria-label*="表情"]', '[title*="表情"]',
                '[class*="emoji-toggle"]', '[class*="emojiToggle"]'
            ];
            for (var s = 0; s < preciseSelectors.length; s++) {
                var els = document.querySelectorAll(preciseSelectors[s]);
                for (var i = 0; i < els.length; i++) {
                    if (isUnsafe(els[i])) continue;
                    var rect = els[i].getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0 && rect.top < window.innerHeight && rect.bottom > 0) {
                        els[i].setAttribute('data-emoji-btn', '1');
                        return {ok: true, method: 'selector:' + preciseSelectors[s], tag: els[i].tagName};
                    }
                }
            }
            // 策略2：底部区域 class 含 emoji/sticker
            var all = document.querySelectorAll('[class*="emoji"], [class*="Emoji"], [class*="sticker"], [class*="Sticker"]');
            for (var i = 0; i < all.length; i++) {
                if (isUnsafe(all[i])) continue;
                var rect = all[i].getBoundingClientRect();
                if (rect.width >= 16 && rect.height >= 16 && rect.top > bottomLimit && rect.top < window.innerHeight) {
                    all[i].setAttribute('data-emoji-btn', '1');
                    return {ok: true, method: 'bottom-scan:' + ((all[i].getAttribute('class')||'')).slice(0,60), tag: all[i].tagName};
                }
            }
            return {ok: false, method: 'none'};
        ''')
        print(f'[emoji-panel] locate result: {result}', flush=True)

        if not result or not result.get('ok'):
            return False

        # Step 2: 用 ActionChains 真实点击标记的元素
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            el = driver.find_element(By.CSS_SELECTOR, '[data-emoji-btn="1"]')
            ActionChains(driver).move_to_element(el).click().perform()
            print(f'[emoji-panel] ActionChains click done', flush=True)
            driver.execute_script('var e=document.querySelector("[data-emoji-btn]"); if(e) e.removeAttribute("data-emoji-btn");')
            return True
        except Exception as e:
            print(f'[emoji-panel] ActionChains click failed: {e}', flush=True)
            try:
                driver.execute_script('var e=document.querySelector("[data-emoji-btn]"); if(e) e.click();')
                return True
            except:
                return False

    def _find_emoji_panel(self):
        """查找表情面板容器元素"""
        driver = self.driver
        return driver.execute_script('''
            var selectors = [
                '[class*="emojiEmojisModal"]', '[class*="EmojiModal"]',
                '[class*="emojiPanel"]', '[class*="emoji-panel"]',
                '[class*="stickerPanel"]', '[class*="sticker-panel"]',
                '[class*="emojiModalContent"]', '[class*="emojiContainer"]',
                '[class*="emoji-content"]', '[class*="EmojiContent"]',
                '[role="dialog"][class*="emoji"]', '[class*="emoji-popover"]',
                '[class*="EmojiPopover"]', '[class*="expression"]'
            ];
            for (var s = 0; s < selectors.length; s++) {
                var els = document.querySelectorAll(selectors[s]);
                for (var i = 0; i < els.length; i++) {
                    var rect = els[i].getBoundingClientRect();
                    if (rect.width > 100 && rect.height > 100 && rect.top < window.innerHeight && rect.bottom > 0) {
                        return els[i].className || 'found';
                    }
                }
            }
            // 找浮层：页面中间偏下、大尺寸、有很多img的div
            var divs = document.querySelectorAll('div');
            for (var i = 0; i < divs.length; i++) {
                var rect = divs[i].getBoundingClientRect();
                if (rect.width > 200 && rect.height > 150 && rect.top > window.innerHeight * 0.3 && rect.bottom < window.innerHeight + 50) {
                    var imgCount = divs[i].querySelectorAll('img').length;
                    if (imgCount >= 6) return divs[i].className || 'popup';
                }
            }
            return null;
        ''')

    def _emoji_tabs(self):
        """当前表情面板的可用 tab（跳过 disabled 占位）"""
        try:
            els = self.driver.find_elements(By.CSS_SELECTOR, '.emojiEmojisModalTabsubTab')
        except Exception:
            return []
        tabs = []
        for el in els:
            try:
                if 'disabled' in (el.get_attribute('class') or ''):
                    continue
                if not el.is_displayed():
                    continue
                tabs.append(el)
            except Exception:
                continue
        return tabs

    def _panel_visible(self):
        """表情内容面板是否已展开"""
        try:
            return bool(self.driver.execute_script(
                "var p=document.querySelector('[class*=\"componentsemojitabPanel\"]');"
                "return p ? p.getBoundingClientRect().height > 50 : false;"))
        except Exception:
            return False

    def _switch_emoji_tab(self, tab_index):
        """切到第 N 个可用 tab，并等图片加载稳定"""
        from selenium.webdriver.common.action_chains import ActionChains
        tabs = self._emoji_tabs()
        if tab_index < 0 or tab_index >= len(tabs):
            return False
        try:
            ActionChains(self.driver).move_to_element(tabs[tab_index]).click().perform()
        except Exception:
            return False
        time.sleep(0.9)
        try:
            self.driver.set_script_timeout(12)
            self.driver.execute_async_script(r'''
                var cb = arguments[arguments.length - 1];
                var panel = document.querySelector('[class*="componentsemojitabPanel"]');
                if (!panel) { cb({ok: false}); return; }
                var last = -1, stable = 0, waited = 0;
                function tick() {
                    var n = panel.querySelectorAll('img').length;
                    if (n === last && n > 0) {
                        stable++;
                        if (stable >= 2) { cb({ok: true, count: n}); return; }
                    } else { stable = 0; }
                    last = n;
                    waited += 250;
                    if (waited > 4000) { cb({ok: n > 0, count: n}); return; }
                    setTimeout(tick, 250);
                }
                tick();
            ''')
        except Exception:
            pass
        return True

    # 收集当前 tab 里所有表情图片（顺带处理面板内部滚动）
    _JS_COLLECT_PANEL = r'''
        var panel = document.querySelector('[class*="componentsemojitabPanel"]');
        if (!panel) return null;
        var seen = {}, srcs = [];
        function collect() {
            var imgs = panel.querySelectorAll('img');
            for (var i = 0; i < imgs.length; i++) {
                var s = imgs[i].src || '';
                if (s && s.length > 20 && !seen[s]) { seen[s] = 1; srcs.push(s); }
            }
        }
        collect();
        var small = panel.querySelectorAll('[class*="smallEmojiItem"]').length;
        var big = panel.querySelectorAll('[class*="emojiEmojiItem"]').length;
        var scroller = panel, best = panel.scrollHeight - panel.clientHeight;
        var kids = panel.querySelectorAll('div');
        for (var i = 0; i < kids.length; i++) {
            var over = kids[i].scrollHeight - kids[i].clientHeight;
            if (over > best) { best = over; scroller = kids[i]; }
        }
        if (best > 20) {
            var step = Math.max(80, Math.floor((scroller.clientHeight || 200) * 0.8));
            for (var pos = step; pos <= best + step; pos += step) {
                scroller.scrollTop = Math.min(pos, best);
                collect();
            }
            scroller.scrollTop = 0;
            collect();
        }
        return {srcs: srcs, small: small, big: big,
                kind: small > big ? 'small' : 'big',
                text: ((panel.innerText || '') + '').replace(/\s+/g, ' ').trim().slice(0, 60)};
    '''

    def _first_item_tooltip(self):
        """悬浮第一个表情，读它的提示文字。

        实测：收藏 tab 提示「移除表情」，表情商店 tab 提示「添加表情」。
        商店里的表情点一下只是「加入收藏」，不能直接发出去，收集时要跳过。
        """
        from selenium.webdriver.common.action_chains import ActionChains
        try:
            items = self.driver.find_elements(
                By.CSS_SELECTOR,
                '[class*="componentsemojitabPanel"] [class*="emojiEmojiItem"], '
                '[class*="componentsemojitabPanel"] [class*="smallEmojiItem"]')
            if not items:
                return ''
            ActionChains(self.driver).move_to_element(items[0]).perform()
            time.sleep(0.6)
            return self.driver.execute_script(r'''
                var pops = document.querySelectorAll('[class*="popup"], [class*="Popup"], [class*="tooltip"], [class*="Tooltip"]');
                for (var i = 0; i < pops.length; i++) {
                    var r = pops[i].getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        var t = ((pops[i].innerText || '') + '').trim();
                        if (t) return t.slice(0, 20);
                    }
                }
                return '';
            ''') or ''
        except Exception:
            return ''
        finally:
            try:
                ActionChains(self.driver).move_by_offset(6, 6).perform()
            except Exception:
                pass

    def _collect_stickers(self):
        """收集所有分类的表情

        实测抖音网页端表情面板有 4 个可用 tab（图标无文字，按内容识别）：
          0 自带大表情（续火花/比心…，点击即发送）
          1 自带 emoji（214 个小表情，点击插入输入框，需回车）
          2 收藏（悬浮提示「移除表情」，点击即发送）
          3 表情包（悬浮提示「添加表情」）
        """
        driver = self.driver
        categories, flat, seen = [], [], set()
        tabs = self._emoji_tabs()
        for idx in range(len(tabs)):
            if not self._switch_emoji_tab(idx):
                continue
            data = driver.execute_script(self._JS_COLLECT_PANEL)
            if not data or not data.get('srcs'):
                continue
            kind = data.get('kind') or 'big'
            # 商店 tab（悬浮提示「添加表情」）点了只是加入收藏，不能直接发送，跳过
            tip = self._first_item_tooltip() if idx >= 2 else ''
            if '添加' in tip and '移除' not in tip:
                print(f'[sticker] tab{idx} 是表情商店（{tip}），跳过', flush=True)
                continue
            label = self._sticker_tab_label(idx, kind, data.get('text') or '')
            icon_html = ''
            try:
                icon_html = (tabs[idx].get_attribute('innerHTML') or '')[:500]
            except Exception:
                pass
            srcs = list(data['srcs'])
            categories.append({
                'tab_index': idx,
                'label': label,
                'icon_html': icon_html,
                'kind': kind,
                'stickers': srcs,
            })
            for src in srcs:
                self._sticker_src_map[src] = idx
                if src not in seen:
                    seen.add(src)
                    flat.append(src)
        return {
            'categories': categories,
            'flat_list': flat,
            'debug': {'tab_count': len(tabs), 'category_count': len(categories)},
        }

    @staticmethod
    def _sticker_tab_label(idx, kind, text):
        """给没有文字的 tab 起个能看懂的名字"""
        if kind == 'small':
            return 'emoji'
        if text and len(text) > 6:
            return '自带表情'
        if idx == 2:
            return '收藏'
        if idx == 3:
            return '表情包'
        return f'表情{idx + 1}'

    def _click_sticker_by_src(self, sticker_src):
        """按 src 精准点击表情

        抖音 CDN 会给同一张图分配不同节点，src 里的签名参数每次刷新都会变，
        所以只比较文件名（path 末段）。返回 'small'（插入输入框）/ 'big'（点击即发送）。
        """
        from selenium.webdriver.common.action_chains import ActionChains
        driver = self.driver
        if not sticker_src:
            return None
        try:
            driver.set_script_timeout(15)
            info = driver.execute_script(r'''
                var target = arguments[0] || '';
                function norm(s) {
                    var u = (s || '').split('#')[0].split('?')[0];
                    var p = u.replace(/^https?:\/\/[^/]+\//, '');
                    var a = p.split('/');
                    return a[a.length - 1] || p;
                }
                var nt = norm(target);
                var panel = document.querySelector('[class*="componentsemojitabPanel"]');
                if (!panel) return {ok: false, reason: 'no-panel'};
                var imgs = panel.querySelectorAll('img');
                var found = null;
                for (var i = 0; i < imgs.length; i++) {
                    if (norm(imgs[i].src || '') === nt) { found = imgs[i]; break; }
                }
                if (!found) return {ok: false, reason: 'not-found', count: imgs.length};
                var item = found.closest('[class*="emojiEmojiItem"], [class*="smallEmojiItem"]') || found;
                var old = document.querySelectorAll('[data-dy-sticker]');
                for (var i = 0; i < old.length; i++) old[i].removeAttribute('data-dy-sticker');
                item.setAttribute('data-dy-sticker', '1');
                var cls = (item.className || '').toString();
                return {ok: true, kind: /smallEmoji/i.test(cls) ? 'small' : 'big'};
            ''', sticker_src)
            if not info or not info.get('ok'):
                print(f'[sticker] 未匹配到表情: {info}', flush=True)
                return None
            el = driver.find_element(By.CSS_SELECTOR, '[data-dy-sticker="1"]')
            ActionChains(driver).move_to_element(el).click().perform()
            return info.get('kind') or 'big'
        except Exception as e:
            print(f'[sticker] 点击失败: {e}', flush=True)
            return None
        finally:
            try:
                driver.execute_script(
                    'var e=document.querySelector("[data-dy-sticker]"); if(e) e.removeAttribute("data-dy-sticker");')
            except Exception:
                pass

    def _mark_emoji_messages(self):
        """给当前已有的表情消息打标记，之后新出现的节点就是刚发出去的那条。

        聊天列表是虚拟滚动，直接比数量会被「旧的被回收、新的加进来」抵消，
        实测同样发送成功，数量有时 +2 有时不变。
        """
        try:
            self.driver.execute_script(r'''
                var nodes = document.querySelectorAll('[class*="MessageItemEmoji"]');
                for (var i = 0; i < nodes.length; i++) nodes[i].setAttribute('data-dy-seen', '1');
            ''')
        except Exception:
            pass

    def _new_emoji_message_count(self):
        try:
            return int(self.driver.execute_script(
                "return document.querySelectorAll('[class*=\"MessageItemEmoji\"]:not([data-dy-seen])').length;") or 0)
        except Exception:
            return 0

    def _last_message_is_mine(self):
        try:
            return bool(self.driver.execute_script(r'''
                var msgs = document.querySelectorAll('[class*="MessageItem"]');
                for (var i = msgs.length - 1; i >= 0; i--) {
                    var el = msgs[i], p = el.parentElement;
                    if (p && /MessageItem/i.test((p.className || '').toString())) continue;
                    return /isFromMe/i.test((el.className || '').toString());
                }
                return false;
            '''))
        except Exception:
            return False

    @staticmethod
    def _editor_has_content(editor):
        try:
            if (editor.text or '').replace('\u200b', '').strip():
                return True
            return bool(editor.find_elements(By.TAG_NAME, 'img'))
        except Exception:
            return False

    def Get_Sticker_List(self, name=None, sec_uid=''):
        """打开表情面板，收集全部分类的表情（自带表情 / emoji / 收藏 / 表情包）"""
        lock = getattr(self, '_io_lock', None)
        if lock is None:
            return self._get_sticker_list_impl(name, sec_uid)
        with lock:
            return self._get_sticker_list_impl(name, sec_uid)

    def _get_sticker_list_impl(self, name=None, sec_uid=''):
        from selenium.webdriver.common.action_chains import ActionChains
        driver = self.driver
        try:
            # 30 分钟内复用上次收集结果：切 tab 收集一次要十几秒
            if self._sticker_categories and (time.time() - getattr(self, '_sticker_cache_at', 0)) < 1800:
                return {'code': 200, 'data': self._sticker_categories}
            if not self.ensure_chat_page():
                return {'code': 400, 'data': '抖音聊天页未就绪（未登录或页面未加载）'}
            # 表情按钮只在已打开会话时出现：有传好友就先按隐藏ID打开
            if name:
                if not self.Open_Chat(name, sec_uid):
                    return {'code': 400, 'data': f'未能打开与 {name} 的会话'}
            if not self._current_conversation_id():
                return {'code': 400, 'data': '请先在左侧选择一个好友，再打开表情面板'}
            try:
                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                time.sleep(0.2)
            except Exception:
                pass
            if not self._open_emoji_panel():
                return {'code': 400, 'data': '未找到表情按钮，请确认已进入对话'}
            time.sleep(0.8)
            if not self._panel_visible():
                self._open_emoji_panel()
                time.sleep(0.8)
            if not self._panel_visible():
                return {'code': 400, 'data': '表情面板没有打开，请再试一次'}

            data = self._collect_stickers()

            # 收完关掉抖音自己的面板，避免挡住后续点击
            try:
                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                time.sleep(0.2)
            except Exception:
                pass

            if not data.get('categories'):
                return {'code': 400, 'data': '未获取到表情，抖音页面结构可能已更新'}
            self._sticker_categories = data['categories']
            self._sticker_cache_at = time.time()
            print(f'[sticker] 收集完成: {[(c["label"], len(c["stickers"])) for c in data["categories"]]}', flush=True)
            return {'code': 200, 'data': data['categories']}
        except Exception as e:
            return {'code': 500, 'data': f'获取表情包失败: {str(e)[:60]}'}

    def Send_Sticker(self, name: str, sticker_index: int, sticker_src: str = None, sec_uid: str = ''):
        """发送表情包：打开表情面板，点击指定表情，发送（串行执行）"""
        lock = getattr(self, '_io_lock', None)
        if lock is None:
            return self._send_sticker_impl(name, sticker_index, sticker_src, sec_uid)
        with lock:
            return self._send_sticker_impl(name, sticker_index, sticker_src, sec_uid)

    def _send_sticker_impl(self, name, sticker_index, sticker_src=None, sec_uid=''):
        """发送表情

        实测两种行为：
        - 收藏 / 自带大表情：点一下就直接发出去了（面板随之关闭，输入框是空的）
        - 自带 emoji：点一下只是插入输入框，还要按回车
        旧实现按「输入框有没有内容」判断成功，导致前者明明发出去了却报失败，后者则根本没发出去。
        """
        from selenium.webdriver.common.action_chains import ActionChains
        driver = self.driver
        try:
            try:
                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                time.sleep(0.2)
            except Exception:
                pass
            if not self.Open_Chat(name, sec_uid):
                return TrueString(False, '未找到该好友')
            if not self._open_emoji_panel():
                return TrueString(False, '未找到表情按钮，请确认已进入聊天页面')
            time.sleep(random.uniform(0.6, 0.9))
            if not self._panel_visible():
                self._open_emoji_panel()
                time.sleep(0.8)
            if not self._panel_visible():
                return TrueString(False, '表情面板未打开，请重试')

            self._mark_emoji_messages()

            # 优先用缓存里的 src -> tab 映射；失效时逐个 tab 找
            kind = None
            tab = self._sticker_src_map.get(sticker_src, -1) if sticker_src else -1
            if tab >= 0:
                self._switch_emoji_tab(tab)
                kind = self._click_sticker_by_src(sticker_src)
            if kind is None and sticker_src:
                for idx in range(len(self._emoji_tabs())):
                    if not self._switch_emoji_tab(idx):
                        continue
                    kind = self._click_sticker_by_src(sticker_src)
                    if kind:
                        break
            if kind is None:
                try:
                    ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                except Exception:
                    pass
                return TrueString(False, '没找到这个表情（可能已被删除，或页面已刷新，请重新打开表情面板）')

            time.sleep(random.uniform(0.5, 0.9))

            # 自带 emoji 会插入输入框，需要回车发送
            editor = None
            finder = getattr(self, '_find_editor', None)
            if callable(finder):
                editor = finder(timeout=3)
            if editor is not None and self._editor_has_content(editor):
                try:
                    editor.click()
                    editor.send_keys(Keys.ENTER)
                except Exception:
                    pass
                for _ in range(12):
                    time.sleep(0.25)
                    if not self._editor_has_content(editor):
                        return TrueString(True, None)
                return TrueString(False, '表情已插入输入框但没发出去')

            # 点击即发送的表情：确认聊天里多了一条「新」表情消息
            for _ in range(14):
                if self._new_emoji_message_count() > 0:
                    return TrueString(True, None)
                time.sleep(0.25)
            # 列表虚拟化可能把新节点挤掉，退一步看：面板已关闭 + 最后一条是自己发的
            if not self._panel_visible() and self._last_message_is_mine():
                return TrueString(True, None)
            return TrueString(False, '表情似乎没发出去，请到聊天里确认一下')
        except Exception as e:
            return TrueString(False, f'发送表情异常: {str(e)[:80]}')
