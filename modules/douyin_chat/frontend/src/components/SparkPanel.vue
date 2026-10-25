<script setup>
import { ref, watch, computed } from 'vue'

const props = defineProps({
  conversation: { type: Object, default: null },
})
const emit = defineEmits(['open-page'])

const info = ref(null)
const loading = ref(false)
const error = ref('')
let seq = 0

const name = computed(() => props.conversation?.name || '')

async function load() {
  const current = name.value
  if (!current) { info.value = null; return }
  const my = ++seq
  loading.value = true
  error.value = ''
  try {
    const res = await fetch(`/api/spark/info?name=${encodeURIComponent(current)}`)
    if (!res.ok) throw new Error('HTTP ' + res.status)
    const data = await res.json()
    if (my === seq) info.value = data
  } catch (e) {
    if (my === seq) { error.value = '读取火花数据失败'; info.value = null }
  } finally {
    if (my === seq) loading.value = false
  }
}

watch(() => props.conversation?.conv_id, load, { immediate: true })

const fireText = computed(() => {
  const fire = String(info.value?.fire || '').trim()
  if (!fire) return ''
  return /^\d+$/.test(fire) ? `${fire} 天` : fire
})

const typeLabel = computed(() => ({
  text: '文字', sticker: '表情', both: '文字 + 表情',
}[info.value?.task?.send_type] || '文字'))

const todayState = computed(() => {
  const t = info.value?.today
  if (!t) return { text: '—', cls: '' }
  if (t.ok) return { text: '今日已续', cls: 'ok' }
  if (t.fail) return { text: '今日失败', cls: 'fail' }
  if (info.value?.task) return { text: '今日待续', cls: 'wait' }
  return { text: '今日无记录', cls: '' }
})

const recent = computed(() => (info.value?.recent || []).slice(0, 3))

function openSpark(path) {
  emit('open-page', path)
}
</script>

<template>
  <div class="spark-panel">
    <div class="sp-head">
      <div class="sp-title">🔥 火花详情</div>
      <button class="sp-refresh" title="刷新" :disabled="loading" @click="load">↻</button>
    </div>

    <div v-if="!conversation" class="sp-empty">选择一个会话查看火花详情</div>
    <div v-else-if="loading" class="sp-empty">读取中…</div>
    <div v-else-if="error" class="sp-empty err">{{ error }}</div>
    <template v-else>
      <div class="sp-name" :title="name">{{ name }}</div>

      <div class="sp-fire">
        <template v-if="fireText">
          <span class="sp-fire-num">{{ fireText }}</span>
          <span class="sp-fire-label">连续火花</span>
        </template>
        <span v-else class="sp-fire-none">暂无火花记录</span>
      </div>

      <div class="sp-rows">
        <div class="sp-row">
          <span>续火花任务</span>
          <b v-if="info?.task">{{ info.task.time }} · {{ typeLabel }}</b>
          <b v-else class="muted">未设置</b>
        </div>
        <div class="sp-row">
          <span>今日状态</span>
          <b :class="todayState.cls">{{ todayState.text }}</b>
        </div>
      </div>

      <div class="sp-recent">
        <div class="sp-sub">最近发送</div>
        <div v-if="recent.length === 0" class="sp-none">还没有发送记录</div>
        <div v-for="(item, i) in recent" :key="i" class="sp-recent-item">
          <span class="tm">{{ item.date }} {{ item.time }}</span>
          <span class="ok" :class="{ fail: !item.ok }">{{ item.ok ? '成功' : '失败' }}</span>
          <span class="detail" :title="item.detail">{{ item.detail || '—' }}</span>
        </div>
      </div>

      <div class="sp-actions">
        <button @click="openSpark('/calendar')">火花日历</button>
        <button @click="openSpark('/tasks')">任务管理</button>
        <button @click="openSpark('/settings')">设置</button>
      </div>
      <div class="sp-tip">数据来自火花助手；续火花/改时间在任务管理里操作。</div>
    </template>
  </div>
</template>

<style scoped>
.spark-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-secondary);
  border-left: 1px solid var(--border-color);
  overflow-y: auto;
}
.sp-head { display: flex; align-items: center; justify-content: space-between; padding: 14px 16px 10px; }
.sp-title { font-size: 14px; font-weight: 650; color: var(--text-primary); }
.sp-refresh { border: 1px solid var(--border-color); background: var(--bg-primary); color: var(--text-secondary); border-radius: 6px; width: 26px; height: 26px; cursor: pointer; }
.sp-refresh:hover { color: var(--accent); border-color: var(--accent); }
.sp-refresh:disabled { opacity: .5; cursor: default; }
.sp-empty { padding: 40px 16px; text-align: center; color: var(--text-muted); font-size: 13px; }
.sp-empty.err { color: var(--danger, #f87171); }
.sp-name { padding: 0 16px 10px; font-size: 12.5px; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sp-fire { margin: 0 12px 12px; padding: 14px; border-radius: 10px; background: var(--bg-primary); border: 1px solid var(--border-color); display: flex; align-items: baseline; gap: 8px; }
.sp-fire-num { font-size: 24px; font-weight: 700; color: #f59e0b; }
.sp-fire-label { font-size: 12px; color: var(--text-muted); }
.sp-fire-none { font-size: 13px; color: var(--text-muted); }
.sp-rows { padding: 0 16px; display: flex; flex-direction: column; gap: 8px; }
.sp-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; font-size: 12.5px; color: var(--text-muted); }
.sp-row b { color: var(--text-primary); font-weight: 600; text-align: right; }
.sp-row b.ok { color: #22c55e; }
.sp-row b.fail { color: #ef4444; }
.sp-row b.wait { color: #f59e0b; }
.sp-row b.muted { color: var(--text-muted); font-weight: 400; }
.sp-recent { margin: 16px 16px 0; border-top: 1px solid var(--border-color); padding-top: 12px; }
.sp-sub { font-size: 12px; font-weight: 650; color: var(--text-secondary); margin-bottom: 8px; }
.sp-none { font-size: 12px; color: var(--text-muted); }
.sp-recent-item { display: flex; gap: 8px; align-items: baseline; font-size: 11.5px; padding: 3px 0; }
.sp-recent-item .tm { flex: none; color: var(--text-muted); }
.sp-recent-item .ok { flex: none; color: #22c55e; }
.sp-recent-item .ok.fail { color: #ef4444; }
.sp-recent-item .detail { flex: 1; min-width: 0; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sp-actions { display: flex; gap: 8px; padding: 16px; }
.sp-actions button { flex: 1; padding: 7px 10px; font-size: 12px; color: var(--text-primary); background: var(--bg-primary); border: 1px solid var(--border-color); border-radius: 7px; cursor: pointer; }
.sp-actions button:hover { color: var(--accent); border-color: var(--accent); }
.sp-tip { padding: 0 16px 16px; font-size: 11px; color: var(--text-muted); line-height: 1.6; }
</style>
