import { createApp } from 'vue'
import App from './App.vue'
import './style.css'
import { BASE } from './lib/base'

// 统一入口把本应用挂在 /douyin-chat，源码里的 /api、/panel、/media 都在运行时补前缀。
if (BASE) {
  const origFetch = window.fetch.bind(window)
  window.fetch = (input, init) => {
    if (typeof input === 'string' && /^\/(api|panel|media)\//.test(input)) {
      input = BASE + input
    }
    return origFetch(input, init)
  }
}

createApp(App).mount('#app')
