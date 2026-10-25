// 本应用由统一入口挂在子路径下，运行期基址从 Vite base 推导（末尾不留斜杠）。
export const BASE = ((import.meta.env && import.meta.env.BASE_URL) || '/').replace(/\/+$/, '')

export function mediaUrl(path) {
  return `${BASE}/media/${String(path || '').replace(/^\/+/, '')}`
}
