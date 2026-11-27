"""抖音自动化核心类模块

Douyin 类继承 StickerMixin 获得表情包相关方法，
自身保留好友/聊天/登录相关操作。
方法体内的 driver 统一用 self.driver。

几个关键约定：
1. 所有对外操作开始前都会调用 ensure_chat_page()，确保当前标签页停在
   https://www.douyin.com/chat?isPopup=1，避免「用户点到别的页面后点不动」。
2. 好友定位以「隐藏 ID」为准（私聊取 React fiber 里的 sec_uid，群聊取会话 id），
   昵称只作为兜底与展示。同名好友、改名好友都不会再点错人。
3. 抖音前端用 React，行元素没有可用的 id 属性，隐藏 ID 只能从
   __reactFiber$ 的 memoizedProps 里读，因此行数据统一由 JS 一次性取回。
"""
import time
import random
import threading
from urllib.parse import urlparse

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from spark_backend.utils import TrueString, UserFriendsInfo, _dbg_report
from spark_backend.douyin_sticker import StickerMixin

# 聊天页地址（所有操作都要求回到这里）
CHAT_URL = 'https://www.douyin.com/chat?isPopup=1'
CHAT_PATH = '/chat'
# 会话行：抖音自己打的 data-e2e 标记，比 class 稳定
ROW_SELECTOR = '[data-e2e="conversation-item"]'
LIST_WRAPPER_SELECTOR = '[class*="conversationConversationListwrapper"]'

class Douyin(StickerMixin):
    # 滚动容器探测：抖音好友列表是虚拟滚动，只有视口内的行在 DOM 里，
    # 必须逐屏滚动才能把全部好友都收集到。
    _SCROLLER_DEF = r'''
        function __findFriendScroller() {
            var wrapper = document.querySelector('[class*="conversationConversationListwrapper"]');
            if (!wrapper) return null;
            var best = null, bestOver = 0;
            var nodes = [wrapper].concat(Array.prototype.slice.call(wrapper.querySelectorAll('*')));
            for (var i = 0; i < nodes.length; i++) {
                var el = nodes[i];
                var over = el.scrollHeight - el.clientHeight;
                if (over > 40 && el.clientHeight > 120 && over > bestOver) {
                    bestOver = over;
                    best = el;
                }
            }
            return best;
        }
    '''

    # 一次性把当前可见会话行的「昵称 / 头像 / 火花 / 隐藏ID / 是否当前会话」取回来。
    # 隐藏ID：私聊 = conversation._toParticipantSecUserId（sec_uid），群聊 = virtualItem.id。
    _JS_ROWS = r'''
            function secUidOf(row) {
                var names = Object.getOwnPropertyNames(row);
                for (var i = 0; i < names.length; i++) {
                    if (names[i].indexOf('__reactFiber$') !== 0) continue;
                    var f = row[names[i]];
                    for (var dep = 0; dep < 12 && f; dep++) {
                        var mp = f.memoizedProps;
                        if (mp) {
                            var c = mp.conversation;
                            if (c && typeof c === 'object') {
                                var v = c._toParticipantSecUserId || c.toParticipantSecUserId || '';
                                if (v) return String(v);
                            }
                            var v2 = mp.toParticipantSecUserId || mp.secUserId || '';
                            if (typeof v2 === 'string' && v2) return v2;
                        }
                        f = f.return;
                    }
                }
                return '';
            }
            function convIdOf(row) {
                var names = Object.getOwnPropertyNames(row);
                for (var i = 0; i < names.length; i++) {
                    if (names[i].indexOf('__reactFiber$') !== 0) continue;
                    var f = row[names[i]];
                    for (var dep = 0; dep < 12 && f; dep++) {
                        var mp = f.memoizedProps;
                        if (mp && mp.virtualItem && mp.virtualItem.id) return String(mp.virtualItem.id);
                        f = f.return;
                    }
                }
                return '';
            }
            function nameOf(row) {
                var els = row.querySelectorAll('[class*="ConversationItemtitle"]');
                for (var i = 0; i < els.length; i++) {
                    var cls = ((els[i].className || '') + '').toString();
                    if (/Wrapper/.test(cls)) continue;
                    var t = (els[i].innerText || els[i].textContent || '').trim();
                    if (t) return t.split('\n')[0].trim();
                }
                // 兜底：标题还没渲染出来时，从 title / aria-label / 头像 alt 里取，
                // 否则会存成「空昵称」，界面上就是一串 ID，也没法按名字找
                var cands = [];
                var tw = row.querySelector('[class*="ConversationItemtitleWrapper"]');
                if (tw) cands.push(tw.getAttribute('title'), tw.getAttribute('aria-label'));
                cands.push(row.getAttribute('title'), row.getAttribute('aria-label'));
                var img = row.querySelector('img[alt]');
                if (img) cands.push(img.getAttribute('alt'));
                for (var j = 0; j < cands.length; j++) {
                    var v = (cands[j] || '').replace(/\s+/g, ' ').trim();
                    if (v && v !== '头像' && v !== 'avatar' && v.length <= 40) return v;
                }
                return '';
            }
            function fireOf(row) {
                var els = row.querySelectorAll('[class*="commonStreaknormalText"]');
                for (var i = 0; i < els.length; i++) {
                    var t = (els[i].innerText || '').trim();
                    if (t) return t;
                }
                return '';
            }
            function avatarOf(row) {
                var img = row.querySelector('img');
                return img ? (img.src || '') : '';
            }
            var rows = document.querySelectorAll('[data-e2e="conversation-item"]');
            var out = [];
            for (var i = 0; i < rows.length; i++) {
                var el = rows[i];
                var sec = secUidOf(el);
                var conv = convIdOf(el);
                out.push({
                    index: i,
                    name: nameOf(el),
                    id: sec || conv || '',
                    sec_uid: sec,
                    conv_id: conv,
                    fire: fireOf(el),
                    avatar: avatarOf(el),
                    active: /curConversation/.test(((el.className || '') + '').toString())
                });
            }
            return out;
    '''

    def __init__(self, driver):
        self.driver = driver  # 将 driver 作为实例属性
        # 全量好友缓存：隐藏ID -> {id, name, avatar, fire, order}
        self.friends_cache = {}
        self.friends_cache_at = 0.0
        # 展示昵称 -> 隐藏ID；展示昵称可能带 "(2)" 后缀，原始昵称见 friend_raw_names
        self.friend_ids = {}
        self.friend_raw_names = {}
        # 兼容旧字段名：昵称 -> 隐藏ID（历史版本这里存的是 XPath，现已不再用 XPath 点击）
        self.friends_xpath_list = {}
        # 串行化所有浏览器操作：定时任务与手动批量可能同时触发，
        # 并行点会话/输入文字会互相踩踏（发错人）
        self._io_lock = threading.RLock()
        # 表情缓存：收集一次要切 3~4 个 tab、十几秒，页面刷新后没必要重来
        self._sticker_categories = []
        self._sticker_src_map = {}
        self._sticker_cache_at = 0.0

    # ==================== 页面状态 ====================

    @staticmethod
    def _is_chat_url(url):
        try:
            parsed = urlparse(url or '')
        except Exception:
            return False
        if 'douyin.com' not in (parsed.netloc or '').lower():
            return False
        path = (parsed.path or '/').rstrip('/')
        return path == CHAT_PATH

    def _has_chat_list(self):
        """聊天页的会话列表是否已经渲染出来"""
        try:
            self.driver.set_script_timeout(8)
            return bool(self.driver.execute_script(
                "return !!(document.querySelector('" + ROW_SELECTOR + "')"
                " || document.querySelector('" + LIST_WRAPPER_SELECTOR + "'));"
            ))
        except Exception:
            return False

    def _login_panel_visible(self):
        try:
            return bool(self.driver.find_elements(By.ID, 'douyin_login_comp_flat_panel'))
        except Exception:
            return False

    def _switch_to_chat_tab(self):
        """多标签时切回聊天页所在的标签"""
        try:
            handles = self.driver.window_handles
        except Exception:
            return
        if len(handles) <= 1:
            return
        for handle in handles:
            try:
                self.driver.switch_to.window(handle)
                if self._is_chat_url(self.driver.current_url):
                    return
            except Exception:
                continue
        try:
            self.driver.switch_to.window(handles[0])
        except Exception:
            pass

    def ensure_chat_page(self, reload=False):
        """确保当前停在抖音聊天页。

        - 当前就在聊天页且列表已渲染：直接返回 True（不会打断已打开的会话）
        - 不在聊天页（用户点了视频/主页/搜索页）：导航回聊天页并等待列表渲染
        - 在聊天页但列表没渲染出来：重新加载一次
        """
        driver = self.driver
        try:
            if not driver.window_handles:
                return False
        except Exception:
            return False

        self._switch_to_chat_tab()

        if not reload:
            try:
                if self._is_chat_url(driver.current_url) and self._has_chat_list():
                    return True
            except Exception:
                pass

        old_timeout = None
        try:
            old_timeout = driver.page_load_timeout
        except Exception:
            pass
        try:
            driver.set_page_load_timeout(20)
        except Exception:
            pass
        try:
            driver.get(CHAT_URL)
        except Exception as exc:
            # SPA 首屏资源多，get 超时很常见；DOM 往往已经可用，先停掉加载再判断
            print(f'⚠️ 打开聊天页超时/异常: {type(exc).__name__}')
            try:
                driver.execute_script('window.stop()')
            except Exception:
                pass
        finally:
            if old_timeout:
                try:
                    driver.set_page_load_timeout(old_timeout)
                except Exception:
                    pass

        try:
            WebDriverWait(driver, 15, 0.4).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ROW_SELECTOR))
            )
            return True
        except Exception:
            if self._has_chat_list():
                return True
            if self._login_panel_visible():
                print('⚠️ 抖音未登录，无法进入聊天页')
            else:
                print('⚠️ 聊天页列表未渲染（可能网络慢或页面结构变化）')
            return False

    # 兼容旧调用名
    _ensure_chat_page = ensure_chat_page

    def goto_login_page(self):
        """登录相关操作前回到聊天页。

        未登录时聊天页会弹出登录框，所以这里等「登录框或会话列表」任一出现即可，
        不像 ensure_chat_page 那样必须等到会话列表（未登录时永远等不到）。
        """
        try:
            if self._is_chat_url(self.driver.current_url) and self._login_panel_visible():
                return True
        except Exception:
            pass
        try:
            self.driver.get(CHAT_URL)
        except Exception:
            try:
                self.driver.execute_script('window.stop()')
            except Exception:
                pass
        end = time.time() + 12
        while time.time() < end:
            if self._login_panel_visible() or self._has_chat_list():
                return True
            time.sleep(0.4)
        return False

    # ==================== 会话行采集 ====================

    def _collect_rows(self):
        """取当前 DOM 里的会话行（虚拟列表，只有视口内的行）"""
        try:
            self.driver.set_script_timeout(10)
            rows = self.driver.execute_script(self._JS_ROWS)
            return rows if isinstance(rows, list) else []
        except Exception:
            return []

    def _merge_rows(self, rows, collected=None, seq=None):
        """把行数据并进缓存；返回本次新增条数"""
        target = self.friends_cache if collected is None else collected
        if seq is not None:
            counter = seq
        else:
            # 局部刷新（只看到一屏）时，新行排在已有顺序之后，别把顺序号冲掉
            counter = [max((v.get('order', 0) for v in target.values()), default=-1) + 1]
        added = 0
        for row in rows:
            rid = (row.get('id') or '').strip()
            name = (row.get('name') or '').strip()
            if not rid and not name:
                continue
            key = rid or name
            if key in target:
                # 已存在则只刷新可变字段（火花数、头像会变）
                info = target[key]
                if name:
                    info['name'] = name
                if row.get('fire'):
                    info['fire'] = row['fire']
                if row.get('avatar'):
                    info['avatar'] = row['avatar']
                continue
            target[key] = {
                'id': rid,
                'name': name,
                'avatar': row.get('avatar') or '',
                'fire': row.get('fire') or '',
                'order': counter[0],
            }
            counter[0] += 1
            added += 1
            if name and rid:
                self.friend_ids.setdefault(name, rid)
        return added

    def _infos_from_cache(self, cache=None):
        """缓存 -> UserFriendsInfo 列表；同名好友用 "(2)" 后缀区分"""
        source = cache if cache is not None else self.friends_cache
        items = sorted(source.values(), key=lambda v: v.get('order', 0))
        used = {}
        infos = []
        for info in items:
            raw = (info.get('name') or '').strip()
            if not raw:
                # 昵称没抓到：给个能认出来的占位名，靠隐藏ID仍然能定位和发消息
                tail = (info.get('id') or '')[-6:]
                raw = f'未命名好友（尾号{tail}）' if tail else '未命名好友'
                info['name_missing'] = True
            else:
                info['name_missing'] = False
            count = used.get(raw, 0) + 1
            used[raw] = count
            display = raw if count == 1 else f'{raw} ({count})'
            if info.get('id'):
                self.friend_ids[display] = info['id']
            if display != raw:
                self.friend_raw_names[display] = raw
            infos.append(UserFriendsInfo(display, info.get('avatar', ''), info.get('fire', ''), info.get('id')))
        self.friends_xpath_list = {v.username: v.uid for v in infos}
        return infos

    def missing_name_count(self):
        """缓存里没抓到昵称的好友数量"""
        return sum(1 for v in self.friends_cache.values() if not (v.get('name') or '').strip())

    def refill_missing_names(self, limit=80):
        """给没抓到昵称的好友补名字

        虚拟列表快速滚动时，偶尔会出现「行渲染出来了但标题还是空」的情况。
        这里按上次记录的位置滚回去重新读一次，读到了就回填缓存。
        """
        missing = [v for v in self.friends_cache.values() if not (v.get('name') or '').strip()]
        if not missing:
            return 0
        fixed = 0
        for info in missing[:limit]:
            order = info.get('order', 0)
            if self._scroll_to(max(0, order * 67 - 200)) is None:
                break
            time.sleep(0.35)
            for _ in range(4):
                rows = self._collect_rows()
                for row in rows:
                    if (row.get('id') or '') != info.get('id'):
                        continue
                    name = (row.get('name') or '').strip()
                    if name:
                        info['name'] = name
                        self.friend_ids[name] = info['id']
                        fixed += 1
                    break
                if info.get('name'):
                    break
                self._scroll_friend_list_to('step')
                time.sleep(0.3)
        if fixed:
            print(f'✅ 已补全 {fixed} 个好友昵称', flush=True)
        return fixed

    def load_cached_ids(self, payload):
        """从本地好友缓存里预置 昵称->隐藏ID，重启后未刷新列表也能按 ID 定位"""
        try:
            items = (payload or {}).get('list') or {}
            ids = (payload or {}).get('ids') or {}
        except Exception:
            return
        for name, value in items.items():
            uid = ''
            if isinstance(value, (list, tuple)) and len(value) >= 3:
                uid = str(value[2] or '')
            uid = uid or str(ids.get(name) or '')
            if uid:
                self.friend_ids.setdefault(str(name), uid)

    # ==================== 滚动 ====================

    def _scroll_friend_list_to(self, mode='step'):
        """滚动好友列表。mode: top / bottom / step（逐屏，用于虚拟列表遍历）

        step 只滚半屏：好友列表是虚拟滚动，视口外的行会被 DOM 回收，
        步长过大会整段跳过，导致抓取数量忽多忽少。
        """
        action = {
            'top': 's.scrollTop = 0;',
            'bottom': 's.scrollTop = s.scrollHeight;',
        }.get(mode, 's.scrollTop = s.scrollTop + Math.max(100, s.clientHeight * 0.6);')
        js = self._SCROLLER_DEF + '''
            var s = __findFriendScroller();
            if (!s) return {ok: false, reason: 'scroller-not-found'};
            var before = s.scrollTop;
            %s
            var after = s.scrollTop;
            return {ok: true, before: before, after: after,
                    scrollHeight: s.scrollHeight, clientHeight: s.clientHeight};
        ''' % action
        try:
            self.driver.set_script_timeout(10)
            return self.driver.execute_script(js) or {'ok': False}
        except Exception:
            return {'ok': False}

    def _scroll_to(self, pos):
        """跳到指定 scrollTop（用于按上次顺序快速定位，减少逐屏滚动）"""
        js = self._SCROLLER_DEF + '''
            var s = __findFriendScroller();
            if (!s) return null;
            var max = Math.max(0, s.scrollHeight - s.clientHeight);
            s.scrollTop = Math.max(0, Math.min(max, %d));
            return s.scrollTop;
        ''' % int(pos)
        try:
            self.driver.set_script_timeout(10)
            return self.driver.execute_script(js)
        except Exception:
            return None

    # ==================== 定位 ====================

    def _pick_row(self, rows, name, target_id=''):
        """在一屏行里挑出目标：优先隐藏ID，其次唯一昵称"""
        if target_id:
            for row in rows:
                if (row.get('id') or '') == target_id:
                    return row
            return None
        raw_name = self.friend_raw_names.get(name, name)
        for row in rows:
            if (row.get('name') or '').strip() == raw_name:
                return row
        return None

    def _scroll_find_row(self, name, target_id=''):
        """逐屏滚动查找目标行，返回行数据；找不到返回 None"""
        hint = None
        if target_id:
            info = self.friends_cache.get(target_id)
            if info and info.get('order', 0) >= 6:
                hint = info['order']

        # 先按上次抓取的顺序跳过去，命中就不用从头滚
        if hint:
            pos = max(0, hint * 67 - 200)
            if self._scroll_to(pos) is not None:
                time.sleep(0.45)
                for _ in range(5):
                    rows = self._collect_rows()
                    hit = self._pick_row(rows, name, target_id)
                    if hit:
                        return hit
                    info = self._scroll_friend_list_to('step')
                    if not info or not info.get('ok'):
                        break
                    if (info.get('after') or 0) - (info.get('before') or 0) < 2:
                        break
                    time.sleep(random.uniform(0.3, 0.4))

        self._scroll_friend_list_to('top')
        time.sleep(0.45)
        for _ in range(300):
            rows = self._collect_rows()
            hit = self._pick_row(rows, name, target_id)
            if hit:
                return hit
            info = self._scroll_friend_list_to('step')
            if not info or not info.get('ok'):
                return None
            moved = (info.get('after') or 0) - (info.get('before') or 0)
            if moved < 2:
                # 滚不动了，可能是懒加载还在请求，再等一轮确认
                time.sleep(0.7)
                info2 = self._scroll_friend_list_to('step')
                if not info2 or ((info2.get('after') or 0) - (info2.get('before') or 0)) < 2:
                    rows = self._collect_rows()
                    return self._pick_row(rows, name, target_id)
            time.sleep(random.uniform(0.3, 0.45))
        return None

    def _current_conversation_id(self):
        """当前打开会话的隐藏ID（没有打开会话时返回空串）"""
        rows = self._collect_rows()
        for row in rows:
            if row.get('active'):
                return (row.get('id') or '').strip()
        return ''

    def get_friend_id(self, name):
        """昵称 -> 隐藏ID（没有缓存则返回空串）"""
        if not name:
            return ''
        if name in self.friend_ids:
            return self.friend_ids[name]
        raw = self.friend_raw_names.get(name, name)
        return self.friend_ids.get(raw, '')

    def _locate_friend(self, name, sec_uid=''):
        """定位好友，返回行数据（含 index），失败返回 None"""
        target_id = (sec_uid or self.get_friend_id(name) or '').strip()
        rows = self._collect_rows()
        hit = self._pick_row(rows, name, target_id)
        if hit:
            self._merge_rows(rows)
            return hit
        hit = self._scroll_find_row(name, target_id)
        if hit:
            self._merge_rows([hit])
        return hit

    def _open_friend(self, name, sec_uid=''):
        """点开好友会话并校验打开的是不是同一个人，返回 (ok, message)"""
        if not name:
            return False, '好友昵称为空'
        if not self.ensure_chat_page():
            return False, '抖音聊天页未就绪（未登录或页面未加载）'

        target_id = (sec_uid or self.get_friend_id(name) or '').strip()
        hit = self._locate_friend(name, target_id)
        if not hit:
            if target_id:
                # 有隐藏ID却找不到，说明会话已被删除/拉黑
                return False, f'未找到好友: {name}（会话可能已删除，或对方已注销）'
            return False, f'未找到好友: {name}（对方可能已改昵称，请刷新好友列表后重新添加任务）'

        row_id = (hit.get('id') or '').strip()
        if not target_id and row_id:
            target_id = row_id
            self.friend_ids[name] = row_id

        # 已经打开的就是目标会话，直接复用（省一次点击）
        if hit.get('active') and (not target_id or row_id == target_id):
            return True, None

        clicked = False
        for attempt in range(2):
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, ROW_SELECTOR)
                if hit['index'] >= len(elements):
                    raise IndexError('行索引越界')
                elements[hit['index']].click()
                clicked = True
                break
            except Exception:
                # 行索引可能因重排失效，重新定位一次再点
                time.sleep(0.4)
                hit = self._locate_friend(name, target_id)
                if not hit:
                    break
        if not clicked:
            return False, f'点击好友失败: {name}'

        time.sleep(random.uniform(1.0, 1.8))

        current_id = self._current_conversation_id()
        if target_id and current_id and current_id != target_id:
            return False, f'打开的会话与目标不一致（{name}），已中止以免发错人'
        return True, None

    # ==================== 对外方法 ====================

    def Updara_FrinderList(self, full=False, use_cache=True):
        """抓取好友列表

        full=False：只抓当前 DOM 可见的行（快，用于点击定位前的刷新）
        full=True ：从顶部逐屏滚动到底，收集全部好友（好友列表是懒加载的，
                    不滚动只能拿到首屏那十几个）
        use_cache ：full 抓取结果缓存 10 分钟，连续刷新不必重复滚动整个列表
        """
        if not self.ensure_chat_page():
            return []

        if full and use_cache and self.friends_cache and (time.time() - self.friends_cache_at) < 600:
            return self._infos_from_cache()

        if not full:
            rows = self._collect_rows()
            self._merge_rows(rows)
            self.friends_xpath_list = {r.get('name'): r.get('id') for r in rows if r.get('name')}
            return [UserFriendsInfo(r.get('name') or '', r.get('avatar') or '',
                                    r.get('fire') or '', r.get('id') or '') for r in rows]

        collected = {}
        counter = [0]
        try:
            self._scroll_friend_list_to('top')
            time.sleep(0.5)
        except Exception:
            pass

        stable = 0
        # 步长 0.6 屏：再大容易跳行，再小轮次太多（好友多时明显变慢）
        for _ in range(600):
            self._merge_rows(self._collect_rows(), collected, counter)
            info = self._scroll_friend_list_to('step')
            if not info or not info.get('ok'):
                break
            time.sleep(random.uniform(0.3, 0.45))
            # 滚动后立刻再收一次：渲染有延迟，滚动前后各收一次能避免漏行
            self._merge_rows(self._collect_rows(), collected, counter)
            moved = (info.get('after') or 0) - (info.get('before') or 0)
            if moved < 2:
                # 到底了；连续两次都滚不动即认为加载完毕
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0

        self._merge_rows(self._collect_rows(), collected, counter)

        # 滚回顶部：让列表回到初始位置，后续点击更稳定
        try:
            self._scroll_friend_list_to('top')
            time.sleep(0.4)
        except Exception:
            pass

        self.friends_cache = collected
        self.friends_cache_at = time.time()

        # 有没抓到昵称的，立刻回滚补一次（大多数情况能补回来）
        if self.missing_name_count():
            self.refill_missing_names()

        return self._infos_from_cache()

    def Find_Friends(self, name: str):
        """好友是否存在：按昵称或隐藏ID判断"""
        if not name:
            return TrueString(False, '好友昵称为空')
        if name in self.friend_ids or name in self.friends_cache:
            return TrueString(True, None)
        # 优先用全量缓存：好友可能不在首屏，只看可见区会误判「不存在」。
        if self.friends_cache and (time.time() - self.friends_cache_at) < 120:
            return TrueString(False, f'未找到好友: {name}')
        self.Updara_FrinderList(full=True)
        if not self.friends_cache:
            return TrueString(False, '未初始化好友')
        return TrueString(name in self.friend_ids, None if name in self.friend_ids else f'未找到好友: {name}')

    def _find_editor(self, timeout=8):
        """定位消息输入框并打标记，返回 WebElement 或 None"""
        driver = self.driver
        end = time.time() + timeout
        while time.time() < end:
            try:
                driver.set_script_timeout(8)
                ok = driver.execute_script(r'''
                    // 清掉上一次的标记，避免拿到已经失效的旧元素
                    var olds = document.querySelectorAll('[data-dy-editor]');
                    for (var i = 0; i < olds.length; i++) olds[i].removeAttribute('data-dy-editor');
                    var eds = document.querySelectorAll('[contenteditable="true"]');
                    var best = null, bestTop = -1;
                    for (var i = 0; i < eds.length; i++) {
                        var r = eds[i].getBoundingClientRect();
                        if (r.width < 60 || r.height < 10) continue;
                        if (r.top < window.innerHeight * 0.35) continue;
                        if (r.top > bestTop) { bestTop = r.top; best = eds[i]; }
                    }
                    if (!best) return false;
                    best.setAttribute('data-dy-editor', '1');
                    return true;
                ''')
                if ok:
                    return driver.find_element(By.CSS_SELECTOR, '[data-dy-editor="1"]')
            except Exception:
                pass
            time.sleep(0.3)
        return None

    def _editor_text(self, editor):
        try:
            return (editor.text or '').replace('\u200b', '').strip()
        except Exception:
            return ''

    def Send_Frinder(self, name: str, text: str, sec_uid: str = '', on_stage=None):
        """给好友发文本：先按隐藏ID打开会话并校验，再输入发送

        on_stage: 可选回调，接收「正在打开会话 / 正在输入并发送 / 等待发送确认」，
                  用于把进度透出到前端进度条。
        """
        message = ' '.join(str(text or '').split())  # 换行会被当成多次回车，统一压成空格
        if not message:
            return TrueString(False, '消息内容为空')

        def _stage(text):
            if on_stage is None:
                return
            try:
                on_stage(text)
            except Exception:
                pass

        with self._io_lock:
            _stage('正在打开会话')
            ok, err = self._open_friend(name, sec_uid)
            if not ok:
                return TrueString(False, err)
            try:
                editor = self._find_editor()
                if editor is None:
                    return TrueString(False, '未找到消息输入框')
                editor.click()
                time.sleep(random.uniform(0.2, 0.4))
                # 清空残留内容，避免上一条失败的文字被一起发出去
                try:
                    editor.send_keys(Keys.CONTROL, 'a')
                    editor.send_keys(Keys.DELETE)
                except Exception:
                    pass
                _stage('正在输入并发送')
                editor.send_keys(message)
                time.sleep(random.uniform(0.3, 0.6))
                _stage('等待发送确认')
                editor.send_keys(Keys.ENTER)

                # 发送成功与否：输入框被清空即视为已提交
                for _ in range(12):
                    time.sleep(0.25)
                    if not self._editor_text(editor):
                        return TrueString(True, None)
                return TrueString(False, '消息未发出（输入框仍有内容，可能被风控或快捷键失效）')
            except Exception as exc:
                return TrueString(False, f'发送异常: {str(exc)[:80]}')

    def Open_Chat(self, name: str, sec_uid: str = ''):
        """点击好友进入对话窗口（按隐藏ID定位，支持滚动查找）"""
        with self._io_lock:
            ok, _err = self._open_friend(name, sec_uid)
            return ok

    def Get_Chat_History(self, name: str, sec_uid: str = ''):
        """获取当前对话的聊天记录"""
        driver = self.driver
        try:
            # #region debug-point A:get-chat-entry
            _dbg_report('A', 'Get_Chat_History:entry', '进入聊天记录同步流程', {'name': name})
            # #endregion
            with self._io_lock:
                ok, err = self._open_friend(name, sec_uid)
                if not ok:
                    # #region debug-point A:open-chat-failed
                    _dbg_report('A', 'Get_Chat_History:open_chat', '打开聊天失败', {'name': name, 'err': err})
                    # #endregion
                    return {'code': 400, 'data': err or '未找到该好友'}
                time.sleep(random.uniform(0.3, 0.6))
                messages = []
                try:
                    driver.set_script_timeout(8)
                    js_messages = driver.execute_script(r'''
                    function isVisible(el) {
                        if (!el) return false;
                        var r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) return false;
                        var s = window.getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                    }
                    function cleanText(t) { return (t||'').replace(/\s+/g,' ').trim(); }

                    var statusTexts = ['已读','送达','未读','已送达','点亮中','已撤回','该消息类型暂不能展示','系统消息'];
                    function isStatus(text) {
                        if (!text || text.length > 40) return false;
                        for (var i=0;i<statusTexts.length;i++) if (text.indexOf(statusTexts[i])!==-1) return true;
                        return false;
                    }

                    // 找输入框顶部边界（排除输入区域的消息）
                    var editorTop = window.innerHeight;
                    var eds = document.querySelectorAll('[contenteditable="true"]');
                    for (var i=0;i<eds.length;i++) {
                        if (!isVisible(eds[i])) continue;
                        var er = eds[i].getBoundingClientRect();
                        if (er.top > window.innerHeight*0.45 && er.top < editorTop) editorTop = er.top;
                    }

                    // 广义查询：找所有 MessageItem 顶层容器（parent 不含 MessageItem）
                    // 覆盖文本、图片、表情、不支持等所有消息类型
                    var allMsgs = document.querySelectorAll('[class*="MessageItem"]');
                    var seen = {};
                    var results = [];

                    for (var i=0;i<allMsgs.length;i++) {
                        var el = allMsgs[i];
                        if (!isVisible(el)) continue;
                        var r = el.getBoundingClientRect();
                        // 排除输入区域
                        if (r.top > editorTop + 8) continue;
                        // 排除完全滚出视图的
                        if (r.bottom < -200) continue;

                        // 只保留顶层容器：parent 不含 MessageItem
                        var p = el.parentElement;
                        if (p) {
                            var pcls = ((p.className||'')+'').toString();
                            if (/MessageItem/i.test(pcls)) continue;
                        }

                        // 判断方向：当前元素或祖先或后代含 isFromMe
                        var cls = ((el.className||'')+'').toString();
                        var isSelf = /isFromMe/i.test(cls);
                        if (!isSelf) {
                            // 向上查祖先
                            var pp = el.parentElement;
                            for (var d=0; d<5 && pp; d++) {
                                var pc = ((pp.className||'')+'').toString();
                                if (/isFromMe/i.test(pc)) { isSelf = true; break; }
                                pp = pp.parentElement;
                            }
                        }
                        if (!isSelf) {
                            // 向下查后代
                            var desc = el.querySelector('[class*="isFromMe"]');
                            if (desc) isSelf = true;
                        }

                        // 提取文本：优先 pureText，其次 bubbleText，最后用元素自身文本
                        var text = '';
                        var pureText = el.querySelector('[class*="pureText"]');
                        if (pureText) text = cleanText(pureText.innerText || pureText.textContent || '');
                        if (!text) {
                            var bubbleText = el.querySelector('[class*="bubbleTextContent"]');
                            if (bubbleText) text = cleanText(bubbleText.innerText || bubbleText.textContent || '');
                        }
                        if (!text) {
                            text = cleanText(el.innerText || el.textContent || '');
                        }

                        if (!text || text.length > 500) continue;
                        if (isStatus(text)) continue;

                        // 去重
                        var key = text + '|' + Math.round(r.top/6) + '|' + (isSelf ? '1' : '0');
                        if (seen[key]) continue;
                        seen[key] = 1;

                        results.push({text:text, is_self:isSelf, top:r.top});
                    }

                    // 如果 MessageItem 没找到，兜底用 MessageBoxContentrowBox
                    if (results.length === 0) {
                        var rows = document.querySelectorAll('[class*="MessageBoxContentrow"]');
                        for (var i=0;i<rows.length;i++) {
                            var el = rows[i];
                            if (!isVisible(el)) continue;
                            var r = el.getBoundingClientRect();
                            if (r.top > editorTop + 8) continue;
                            if (r.bottom < -200) continue;
                            var cls = ((el.className||'')+'').toString();
                            var isSelf = /isFromMe/i.test(cls);
                            if (!isSelf) {
                                var desc = el.querySelector('[class*="isFromMe"]');
                                if (desc) isSelf = true;
                            }
                            var text = cleanText(el.innerText || el.textContent || '');
                            if (!text || text.length > 500) continue;
                            if (isStatus(text)) continue;
                            var key = text + '|' + Math.round(r.top/6) + '|' + (isSelf ? '1' : '0');
                            if (seen[key]) continue;
                            seen[key] = 1;
                            results.push({text:text, is_self:isSelf, top:r.top});
                        }
                    }

                    results.sort(function(a,b){ return a.top - b.top; });
                    return results.map(function(it){ return {text:it.text, is_self:it.is_self}; });
                ''')
                    if isinstance(js_messages, list) and js_messages:
                        messages = js_messages
                except Exception:
                    pass
            # #region debug-point A:get-chat-result
            _dbg_report('A', 'Get_Chat_History:result', '聊天记录同步完成', {
                'message_count': len(messages),
                'preview': messages[:3]
            })
            # #endregion
            return {'code': 200, 'data': messages}
        except Exception as e:
            # #region debug-point E:get-chat-exception
            _dbg_report('E', 'Get_Chat_History:exception', '聊天记录同步异常', {'error': str(e)})
            # #endregion
            return {'code': 500, 'data': '获取聊天记录失败'}

    def LoginInit(self):
        driver = self.driver
        try:
            dle_user = driver.find_element(By.XPATH,
                                           value='//*[@id="douyin_login_comp_flat_panel"]/div/div[2]/div/div[4]/p')
            dle_user.click()
        except Exception:
            pass
