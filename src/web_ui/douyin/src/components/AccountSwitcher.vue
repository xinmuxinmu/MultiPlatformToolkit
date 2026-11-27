<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { BASE } from '../lib/base.js'

// 多账号 Cookie 管理：右上角账号切换器。
// 账号列表来自面板的 /panel/api/accounts（与查看器同一套 Bearer token 鉴权）。
const props = defineProps({
  authToken: { type: String, default: '' },
})

const accounts = ref([])
const activeId = ref(null)
const open = ref(false)
const switching = ref(false)
const loaded = ref(false)

function headers() {
  return props.authToken ? { Authorization: `Bearer ${props.authToken}` } : {}
}

async function load() {
  try {
    const res = await fetch(`${BASE}/panel/api/accounts`, { headers: headers() })
    if (!res.ok) return
    const data = await res.json()
    accounts.value = data.accounts || []
    activeId.value = data.active || null
  } catch {}
  loaded.value = true
}

function activeAccount() {
  return accounts.value.find(a => a.id === activeId.value) || null
}

function avatarHtml(a, size) {
  const cls = 'acct-ava-' + size
  if (a && a.avatar_url) {
    return `<img class="${cls}" src="${a.avatar_url}" alt="" referrerpolicy="no-referrer">`
  }
  const ch = ((a && a.name) || '?').trim().charAt(0) || '?'
  return `<span class="${cls} acct-ava-fb">${ch}</span>`
}

async function switchTo(id) {
  if (id === activeId.value || switching.value) return
  switching.value = true
  try {
    const res = await fetch(`${BASE}/panel/api/accounts/switch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...headers() },
      body: JSON.stringify({ id }),
    })
    if (!res.ok) {
      const d = await res.json().catch(() => ({}))
      alert(d.error || '切换失败')
      return
    }
    // 账号切换后重新加载页面，刷新会话列表与消息数据
    location.reload()
  } catch (e) {
    alert('切换失败: ' + (e.message || e))
  } finally {
    switching.value = false
  }
}

function toggle(e) {
  e.stopPropagation()
  open.value = !open.value
}
function onDocClick(e) {
  if (!e.target.closest('.acct-switch')) open.value = false
}

onMounted(() => {
  load()
  document.addEventListener('click', onDocClick)
})
onUnmounted(() => document.removeEventListener('click', onDocClick))
</script>

<template>
  <div v-if="loaded && accounts.length" class="acct-switch">
    <button class="acct-chip" type="button" :title="activeAccount()?.name || '账号'" @click="toggle">
      <span v-html="avatarHtml(activeAccount(), 26)"></span>
      <span class="acct-chip-name">{{ activeAccount()?.name || '账号' }}</span>
      <span class="acct-caret">▾</span>
    </button>
    <div v-if="open" class="acct-menu" role="menu">
      <button
        v-for="a in accounts"
        :key="a.id"
        class="acct-menu-item"
        type="button"
        role="menuitem"
        :class="{ active: a.id === activeId }"
        @click="switchTo(a.id)"
      >
        <span class="tick">{{ a.id === activeId ? '✓' : '' }}</span>
        <span v-html="avatarHtml(a, 22)"></span>
        <span class="mi-label">{{ a.name || '账号' }}</span>
      </button>
      <div class="acct-menu-sep"></div>
      <a class="acct-menu-item" :href="`${BASE}/panel`" role="menuitem">
        <span class="tick">＋</span><span class="mi-label">管理账号</span>
      </a>
    </div>
  </div>
</template>

<style scoped>
.acct-switch { position: relative; }
.acct-chip {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 4px 11px 4px 4px; border-radius: 999px;
  background: var(--bg-tertiary); border: 1px solid var(--border-color);
  cursor: pointer; font-size: 12px; font-weight: 600; color: var(--text-primary);
  transition: border-color 0.15s; max-width: 180px;
}
.acct-chip:hover { border-color: var(--accent); }
.acct-chip-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.acct-caret { color: var(--text-muted); font-size: 9px; }
.acct-menu {
  position: absolute; right: 0; top: calc(100% + 6px); min-width: 200px; max-width: 280px;
  background: var(--bg-secondary); border: 1px solid var(--border-color);
  border-radius: 12px; box-shadow: var(--shadow-pop); padding: 6px; z-index: 200;
}
.acct-menu-item {
  display: flex; align-items: center; gap: 9px; width: 100%;
  padding: 7px 9px; border-radius: 8px; cursor: pointer; border: none;
  background: none; color: var(--text-primary); font-size: 13px; text-align: left;
  text-decoration: none;
}
.acct-menu-item:hover { background: color-mix(in srgb, var(--accent) 12%, transparent); }
.acct-menu-item .tick { width: 13px; color: var(--accent); font-size: 11px; flex-shrink: 0; text-align: center; }
.acct-menu-item .mi-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.acct-menu-sep { height: 1px; background: var(--border-color); margin: 6px 4px; }
:deep(.acct-ava-22), :deep(.acct-ava-26) { border-radius: 50%; object-fit: cover; background: var(--bg-tertiary); flex-shrink: 0; display: block; }
:deep(.acct-ava-22) { width: 22px; height: 22px; }
:deep(.acct-ava-26) { width: 26px; height: 26px; }
:deep(.acct-ava-fb) { border-radius: 50%; background: color-mix(in srgb, var(--accent) 16%, transparent); color: var(--accent); display: inline-grid; place-items: center; font-weight: 700; }
:deep(.acct-ava-fb.acct-ava-22) { font-size: 11px; }
:deep(.acct-ava-fb.acct-ava-26) { font-size: 12px; }
</style>
