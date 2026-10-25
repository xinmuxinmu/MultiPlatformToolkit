<script setup>
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import ConversationList from './components/ConversationList.vue'
import MessageList from './components/MessageList.vue'
import SearchBar from './components/SearchBar.vue'
import ScreenshotView from './components/ScreenshotView.vue'
import AccountSwitcher from './components/AccountSwitcher.vue'
import SparkPanel from './components/SparkPanel.vue'
import { BASE } from './lib/base.js'

// 统一抖音页面把查看器放进 iframe 时，账号切换与登录由外层页面负责。
const isEmbed = new URLSearchParams(location.search).get('embed') === '1'

// 截图模式：后端 headless 浏览器访问 /screenshot?...&token=<临时token>
const isScreenshotMode = location.pathname.endsWith('/screenshot')

const activeConversation = ref(null)
const searchHighlight = ref('')
const jumpToSeq = ref(null)
const mobileLayout = window.matchMedia('(max-width: 768px)')
const sidebarOpen = ref(!mobileLayout.matches)
const syncSidebarLayout = () => { sidebarOpen.value = !mobileLayout.matches }
mobileLayout.addEventListener('change', syncSidebarLayout)
onUnmounted(() => mobileLayout.removeEventListener('change', syncSidebarLayout))
const searchOpen = ref(false)
const sparkOpen = ref(window.matchMedia('(min-width: 1100px)').matches)
const sparkFire = ref({})
const sparkTasks = ref({})
const sparkSecUids = ref({})
const readerLayout = ref(null)

async function loadSparkMap() {
  try {
    const res = await fetch('/api/spark/map')
    if (!res.ok) return
    const data = await res.json()
    sparkFire.value = data.fire || {}
    sparkTasks.value = data.tasks || {}
    sparkSecUids.value = data.sec_uids || {}
  } catch {}
}

function toggleSpark() {
  if (!activeConversation.value) return
  sparkOpen.value = !sparkOpen.value
  if (sparkOpen.value) searchOpen.value = false
}

// 火花页面直接嵌在查看器里，不再切到外层标签
const workspaceView = ref('messages')
const workspaceNav = [
  { key: 'messages', label: '消息' },
  { key: 'friends', label: '好友' },
  { key: 'groups', label: '分组' },
  { key: 'tasks', label: '任务' },
  { key: 'calendar', label: '火花日历' },
  { key: 'settings', label: '设置' },
]
const SPARK_VIEWS = new Set(['friends', 'groups', 'tasks', 'calendar', 'settings'])
const sparkFrameSrc = ref('')

function openSparkPage(path) {
  const key = String(path || '').replace(/^\//, '').split('?')[0]
  if (!SPARK_VIEWS.has(key)) return
  workspaceView.value = key
  sparkFrameSrc.value = `/spark/${key}?embed=1&chrome=0&v=${Date.now()}`
}

function selectWorkspace(key) {
  if (key === 'messages') { workspaceView.value = 'messages'; return }
  openSparkPage('/' + key)
}

// 查看器内直接发消息：自动确保火花浏览器可用，发送后写入本地备份
const sendText = ref('')
const sendBusy = ref(false)
const sendStatus = ref('')
const msgReload = ref(0)
const localSelfUid = ref(localStorage.getItem('selfUid') || '')

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function notifySparkReady() {
  try { window.dispatchEvent(new Event('spark-browser-ready')) } catch (e) {}
}

async function ensureSparkReady() {
  let status = ''
  try {
    const res = await fetch('/spark/api/Api/GetInit')
    const data = await res.json()
    status = data.data || ''
  } catch (e) { /* 火花服务未启动时走初始化报错 */ }
  if (status === 'Yes') { notifySparkReady(); return }
  const res = await fetch('/spark/api/Api/Init')
  const data = await res.json()
  if (data.code !== 200) throw new Error(data.data || '火花浏览器初始化失败')
  for (let i = 0; i < 60; i++) {
    await sleep(1000)
    try {
      const s = await fetch('/spark/api/Api/GetInit')
      const sd = await s.json()
      if (sd.data === 'Yes') { notifySparkReady(); return }
    } catch (e) { /* 继续等待 */ }
  }
  throw new Error('火花浏览器初始化超时，请稍后重试')
}

async function sendMessage() {
  const conv = activeConversation.value
  const text = sendText.value.trim()
  if (!conv || !text || sendBusy.value) return
  sendBusy.value = true
  sendStatus.value = '正在检查火花浏览器…'
  try {
    await ensureSparkReady()
    sendStatus.value = '正在发送到抖音…'
    const params = new URLSearchParams({
      name: conv.name || '',
      text,
      sec_uid: sparkSecUids.value[conv.name] || '',
    })
    const res = await fetch('/spark/api/Api/Send?' + params.toString())
    const data = await res.json()
    if (data.code !== 200) throw new Error(data.data || '发送失败')
    // 已经发出去了：先清空输入，备份失败也不能让人误以为没发送
    sendText.value = ''
    sendStatus.value = '已发送，正在备份到本地记录…'
    try {
      const saved = await fetch(`/api/conversations/${encodeURIComponent(conv.conv_id)}/messages/local`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      })
      const sd = await saved.json()
      if (!saved.ok || sd.error) throw new Error(sd.error || '本地备份失败')
      if (sd.sender_uid) {
        localSelfUid.value = sd.sender_uid
        try { localStorage.setItem('selfUid', sd.sender_uid) } catch (e) {}
      }
      sendStatus.value = '已发送并备份到本地'
      msgReload.value++
    } catch (backupError) {
      sendStatus.value = '已发送，但本地备份失败：' + (backupError.message || backupError)
    }
    if (sendStatus.value === '已发送并备份到本地') {
      setTimeout(() => { if (!sendBusy.value) sendStatus.value = '' }, 2500)
    }
  } catch (e) {
    sendStatus.value = e.message || '发送失败'
  } finally {
    sendBusy.value = false
  }
}
async function setSearchOpen(open) {
  if (open) sparkOpen.value = false
  const list = readerLayout.value?.querySelector('.msg-list')
  const bottom = list && list.scrollHeight - list.scrollTop - list.clientHeight < 2
  const top = list?.getBoundingClientRect().top || 0
  const anchor = list && [...list.querySelectorAll(':scope > .msg-item')].find(row => row.getBoundingClientRect().bottom > top)
  const offset = anchor?.getBoundingClientRect().top
  searchOpen.value = open
  await nextTick()
  if (!list?.isConnected) return
  if (bottom) list.scrollTop = list.scrollHeight - list.clientHeight
  else if (anchor?.isConnected) list.scrollTop += anchor.getBoundingClientRect().top - offset
}

// Auth
const authChecking = ref(true)
const authenticated = ref(false)
const loginPassword = ref('')
const loginError = ref('')
const authToken = ref(localStorage.getItem('authToken') || '')
if (isScreenshotMode) {
  const qsToken = new URLSearchParams(location.search).get('token')
  if (qsToken) authToken.value = qsToken
}

async function checkAuth() {
  try {
    const headers = authToken.value ? { Authorization: `Bearer ${authToken.value}` } : {}
    const res = await fetch('/api/auth/check', { headers })
    const data = await res.json()
    if (!data.need_password || data.authenticated) {
      authenticated.value = true
    }
  } catch {}
  authChecking.value = false
}

async function doLogin() {
  loginError.value = ''
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: loginPassword.value }),
    })
    if (!res.ok) {
      loginError.value = '密码错误'
      return
    }
    const data = await res.json()
    authToken.value = data.token
    localStorage.setItem('authToken', data.token)
    authenticated.value = true
  } catch {
    loginError.value = '登录失败'
  }
}

// Inject auth token into all fetch requests, and keep root-relative API paths
// working when the app is mounted under /douyin-chat.
const _origFetch = window.fetch
const API_PREFIXES = ['/api/', '/panel/', '/media/']
function withBase(url) {
  if (typeof url === 'string' && API_PREFIXES.some(p => url.startsWith(p))) {
    return `${BASE}${url}`
  }
  return url
}
window.fetch = function(url, opts = {}) {
  url = withBase(url)
  if (authToken.value && typeof url === 'string' &&
      (url.startsWith(`${BASE}/api/`) || url.startsWith(`${BASE}/panel/`))) {
    opts.headers = opts.headers || {}
    if (opts.headers instanceof Headers) {
      opts.headers.set('Authorization', `Bearer ${authToken.value}`)
    } else {
      opts.headers['Authorization'] = `Bearer ${authToken.value}`
    }
  }
  return _origFetch.call(this, url, opts)
}

onMounted(() => {
  if (!isScreenshotMode) checkAuth()
  loadSparkMap()
})

const themes = [
  { id: 'dark',   label: '深蓝', color: '#1a1a2e' },
  { id: 'wechat', label: '微信', color: '#95ec69' },
  { id: 'light',  label: '浅色', color: '#ffffff' },
  { id: 'warm',   label: '暖棕', color: '#8b5e3c' },
  { id: 'purple', label: '紫夜', color: '#6a2fad' },
]
const currentTheme = ref(localStorage.getItem('theme') || 'dark')
applyTheme(currentTheme.value)

function applyTheme(id) {
  currentTheme.value = id
  document.documentElement.setAttribute('data-theme', id === 'dark' ? '' : id)
  localStorage.setItem('theme', id)
}

function selectConversation(conv) {
  workspaceView.value = 'messages'
  activeConversation.value = conv
  searchHighlight.value = ''
  if (mobileLayout.matches) sidebarOpen.value = false
}

function onConversationDeleted(convId) {
  if (activeConversation.value?.conv_id === convId) {
    activeConversation.value = null
  }
}

function navigateToMessage(item) {
  workspaceView.value = 'messages'
  activeConversation.value = {
    conv_id: item.conv_id,
    name: item.conv_name || '未知',
  }
  searchHighlight.value = item.search_query ?? ''
  jumpToSeq.value = item.seq || null
}
</script>

<template>
  <!-- Screenshot mode: chrome-less static render for headless capture -->
  <ScreenshotView v-if="isScreenshotMode" />
  <!-- Loading -->
  <div v-else-if="authChecking" class="login-screen">
    <div class="login-box">
      <div class="login-loading">Loading...</div>
    </div>
  </div>
  <!-- Login -->
  <div v-else-if="!authenticated" class="login-screen">
    <div class="login-box">
      <div class="login-title">抖音聊天记录</div>
      <div class="login-subtitle">请输入密码</div>
      <form @submit.prevent="doLogin" class="login-form">
        <input
          v-model="loginPassword"
          type="password"
          placeholder="密码"
          class="login-input"
          autofocus
        />
        <button type="submit" class="login-btn">登录</button>
      </form>
      <div v-if="loginError" class="login-error">{{ loginError }}</div>
    </div>
  </div>
  <!-- Main app -->
  <div v-else class="app-layout">
    <div class="sidebar-overlay" :class="{ visible: sidebarOpen }" @click="sidebarOpen = false"></div>
    <div id="conversation-sidebar" class="app-sidebar" :class="{ open: sidebarOpen }" :inert="!sidebarOpen">
      <ConversationList
        :activeId="activeConversation?.conv_id"
        :fireByName="sparkFire"
        :taskByName="sparkTasks"
        @select="selectConversation"
        @deleted="onConversationDeleted"
      />
    </div>
    <div class="app-main">
      <div class="app-toolbar">
        <button class="sidebar-toggle" :aria-label="sidebarOpen ? '收起会话列表' : '展开会话列表'" :title="sidebarOpen ? '收起会话列表' : '展开会话列表'" :aria-expanded="sidebarOpen" aria-controls="conversation-sidebar" @click="sidebarOpen = !sidebarOpen">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/></svg>
        </button>
        <div class="app-title">抖音聊天记录</div>
        <button v-if="workspaceView === 'messages'" class="search-toggle" :disabled="!activeConversation" :aria-expanded="searchOpen" @click="setSearchOpen(!searchOpen)">⌕ 查找聊天记录</button>
        <button v-if="workspaceView === 'messages'" class="search-toggle" :disabled="!activeConversation" :aria-expanded="sparkOpen" @click="toggleSpark">🔥 {{ sparkOpen ? '收起火花' : '火花详情' }}</button>
        <div class="theme-switcher">
          <button
            v-for="t in themes"
            :key="t.id"
            class="theme-btn"
            :class="{ active: currentTheme === t.id }"
            :title="t.label"
            :style="{ background: t.color }"
            @click="applyTheme(t.id)"
          />
        </div>
        <AccountSwitcher v-if="!isEmbed" :auth-token="authToken" />
      </div>
      <div class="workspace-nav">
        <button
          v-for="item in workspaceNav"
          :key="item.key"
          :class="{ active: workspaceView === item.key }"
          @click="selectWorkspace(item.key)"
        >{{ item.label }}</button>
      </div>
      <div v-if="workspaceView === 'messages'" ref="readerLayout" class="reader-layout">
        <div class="chat-column">
          <MessageList
            :conversation="activeConversation"
            :searchHighlight="searchHighlight"
            :jumpToSeq="jumpToSeq"
            :reloadToken="msgReload"
            :selfUidOverride="localSelfUid"
            @jumped="jumpToSeq = null"
          />
          <div v-if="activeConversation" class="composer">
            <div class="composer-main">
              <input
                v-model="sendText"
                :placeholder="`发消息给 ${activeConversation.name || ''}（发送后自动备份到本地）`"
                :disabled="sendBusy"
                @keydown.enter.prevent="sendMessage"
              />
              <button :disabled="sendBusy || !sendText.trim()" @click="sendMessage">{{ sendBusy ? '发送中…' : '发送' }}</button>
            </div>
            <div v-if="sendStatus" class="composer-status">{{ sendStatus }}</div>
          </div>
        </div>
        <SparkPanel
          v-if="sparkOpen && activeConversation && !searchOpen"
          class="spark-slot"
          :conversation="activeConversation"
          @open-page="openSparkPage"
        />
        <div v-if="searchOpen && activeConversation" class="search-slot">
          <SearchBar :convId="activeConversation.conv_id" :convName="activeConversation.name" @navigate="navigateToMessage" @close="setSearchOpen(false)" />
        </div>
      </div>
      <div v-else class="spark-view">
        <iframe class="spark-frame" :src="sparkFrameSrc" title="火花"></iframe>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* Allocate the panel once; animate only its contents, without reflowing chat. */
.search-slot > .search-panel { --panel-offset: 12px; animation: panel-enter .16s cubic-bezier(.2, 0, .2, 1); }
.app-sidebar.open > :deep(.conv-list) { --panel-offset: -12px; animation: panel-enter .16s cubic-bezier(.2, 0, .2, 1); }
@keyframes panel-enter {
  from { opacity: .75; transform: translateX(var(--panel-offset)); }
  to { opacity: 1; transform: translateX(0); }
}
@media (prefers-reduced-motion: reduce) {
  .search-slot > .search-panel, .app-sidebar.open > :deep(.conv-list) { animation: none; }
}

.workspace-nav {
  display: flex;
  gap: 2px;
  padding: 5px 10px;
  border-bottom: 1px solid var(--border-color);
  background: var(--bg-secondary);
  flex: none;
  overflow-x: auto;
}
.workspace-nav button {
  border: 0;
  background: transparent;
  color: var(--text-secondary);
  font: 600 12.5px/1 inherit;
  padding: 7px 11px;
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;
}
.workspace-nav button:hover { color: var(--accent); background: var(--bg-primary); }
.workspace-nav button.active { color: var(--accent); background: var(--bg-primary); box-shadow: inset 0 0 0 1px var(--border-color); }
.spark-view { flex: 1; min-height: 0; display: flex; background: #fff; }
.spark-frame { flex: 1; width: 100%; border: 0; background: #fff; }
.reader-layout { display: flex; flex: 1; min-height: 0; overflow: hidden; position: relative; }
.chat-column { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; }
.chat-column > .msg-panel { flex: 1; min-height: 0; }
.composer { flex: none; border-top: 1px solid var(--border-color); background: var(--bg-secondary); padding: 8px 10px; }
.composer-main { display: flex; gap: 8px; }
.composer input {
  flex: 1; min-width: 0; padding: 9px 12px; border: 1px solid var(--border-color);
  border-radius: 8px; background: var(--bg-primary); color: var(--text-primary);
  outline: none; font: inherit; font-size: 13px;
}
.composer input:focus { border-color: var(--accent); }
.composer button {
  flex: none; border: 0; background: var(--accent); color: #fff; border-radius: 8px;
  padding: 0 16px; font: 600 13px/1 inherit; cursor: pointer;
}
.composer button:disabled { opacity: .5; cursor: default; }
.composer-status { font-size: 11.5px; color: var(--text-muted); margin-top: 5px; }
.reader-layout > .msg-panel { min-width: 0; }
.search-slot { flex: 0 0 320px; width: 320px; min-height: 0; overflow: hidden; }
.spark-slot { flex: 0 0 300px; width: 300px; min-height: 0; overflow: hidden; }
@media (max-width: 800px) {
  .search-slot { position: absolute; inset: 0 0 0 auto; width: min(340px, 100%); z-index: 120; box-shadow: -8px 0 28px #0003; }
  .spark-slot { position: absolute; inset: 0 0 0 auto; width: min(320px, 100%); z-index: 119; box-shadow: -8px 0 28px #0003; }
}
.search-toggle { padding: 5px 10px; background: var(--bg-secondary); color: var(--text-secondary); border: 1px solid var(--border-color); border-radius: 6px; font-size: 12px; cursor: pointer; white-space: nowrap; }
.search-toggle:hover:not(:disabled), .search-toggle[aria-expanded="true"] { color: var(--accent); border-color: var(--accent); }
.search-toggle:disabled { opacity: .4; cursor: default; }

.login-screen {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  background: var(--bg-primary);
}
.login-box {
  text-align: center;
  padding: 40px;
  background: var(--bg-secondary);
  border-radius: 16px;
  border: 1px solid var(--border-color);
  min-width: 300px;
}
.login-title {
  font-size: 22px;
  font-weight: 700;
  color: var(--accent);
  margin-bottom: 6px;
}
.login-subtitle {
  font-size: 14px;
  color: var(--text-muted);
  margin-bottom: 24px;
}
.login-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.login-input {
  padding: 10px 14px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--bg-primary);
  color: var(--text-primary);
  font-size: 15px;
  outline: none;
  text-align: center;
}
.login-input:focus {
  border-color: var(--accent);
}
.login-btn {
  padding: 10px;
  border: none;
  border-radius: 8px;
  background: var(--accent);
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
  transition: filter 0.15s;
}
.login-btn:hover {
  filter: brightness(1.15);
}
.login-error {
  margin-top: 12px;
  color: #ff4d4f;
  font-size: 13px;
}
.login-loading {
  color: var(--text-muted);
  font-size: 14px;
}

.app-layout {
  display: flex;
  height: 100vh;
}

.app-sidebar {
  width: 300px;
  flex-shrink: 0;
}

.app-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
}

.app-toolbar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 11px 20px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-color);
}

.app-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  font-weight: 600;
  white-space: nowrap;
  color: var(--text-primary);
  letter-spacing: 0.01em;
}
.app-title::before {
  content: "";
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 20%, transparent);
}


.theme-switcher {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-left: auto;
}

.theme-btn {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: 1px solid rgba(255, 255, 255, 0.15);
  cursor: pointer;
  transition: transform 0.15s, box-shadow 0.15s;
  padding: 0;
}

.theme-btn:hover {
  transform: scale(1.15);
}

.theme-btn.active {
  transform: scale(1.1);
  box-shadow: 0 0 0 2px var(--bg-secondary), 0 0 0 4px var(--accent);
}

.sidebar-toggle {
  display: flex;
  background: none;
  border: none;
  color: var(--text-primary);
  font-size: 22px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 4px;
  flex-shrink: 0;
}
.sidebar-toggle:hover {
  background: var(--bg-tertiary);
}

.sidebar-overlay {
  display: none;
}

@media (min-width: 769px) {
  .app-sidebar:not(.open) { display: none; }
}

@media (max-width: 768px) {
  .sidebar-toggle {
    display: block;
  }

  .app-sidebar {
    position: fixed;
    left: 0;
    top: 0;
    height: 100vh;
    z-index: 1000;
    transform: translateX(-100%);
    width: 280px;
  }

  .app-sidebar.open {
    transform: translateX(0);
  }

  .sidebar-overlay {
    display: block;
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    z-index: 999;
    opacity: 0;
    pointer-events: none;
  }

  .sidebar-overlay.visible {
    opacity: 1;
    pointer-events: auto;
  }

  .app-toolbar {
    flex-wrap: wrap;
    padding: 8px 12px;
    gap: 8px;
  }

  .app-title {
    flex: 1;
    font-size: 14px;
  }

  .theme-switcher {
    order: 0;
    margin-left: 0;
    gap: 5px;
  }

}
</style>
