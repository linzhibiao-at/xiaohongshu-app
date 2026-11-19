import axios from 'axios'
import { ElMessage } from 'element-plus'
import { reactive } from 'vue'

export const session = reactive({
  token: localStorage.getItem('admin_token') || '',
  user: JSON.parse(localStorage.getItem('admin_user') || 'null'),
})

export function setSession(token, user) {
  session.token = token
  session.user = user
  if (token) {
    localStorage.setItem('admin_token', token)
    localStorage.setItem('admin_user', JSON.stringify(user))
  } else {
    localStorage.removeItem('admin_token')
    localStorage.removeItem('admin_user')
  }
}

const http = axios.create({ baseURL: import.meta.env.VITE_API_BASE || '' })

http.interceptors.request.use((config) => {
  if (session.token) config.headers.Authorization = `Bearer ${session.token}`
  return config
})

const FIELD_LABEL = { key: 'Key', name: '名称', description: '说明', system_prompt: '系统提示词', skills: 'Skills' }

// FastAPI 的 422 把校验失败放在 detail 数组里，转成能直接看懂的中文提示。
function detailMessage(detail) {
  if (typeof detail === 'string') return detail
  if (!Array.isArray(detail) || !detail.length) return ''
  return detail
    .map((d) => {
      const field = (d.loc || []).filter((x) => x !== 'body')
      const label = FIELD_LABEL[field[0]] || field.join('.') || '参数'
      const ctx = d.ctx || {}
      if (d.type === 'string_too_short') return `${label}至少需要 ${ctx.min_length} 个字符`
      if (d.type === 'string_too_long') return `${label}最多 ${ctx.max_length} 个字符`
      if (d.type === 'missing') return `${label}必填`
      if (d.type === 'string_pattern_mismatch') return `${label}格式不正确`
      return `${label}不合法`
    })
    .join('；')
}

http.interceptors.response.use(
  (res) => res.data,
  (err) => {
    const status = err.response?.status
    const detail = err.response?.data?.detail
    const msg = detailMessage(detail) || err.message
    if (status === 401 && session.token) {
      setSession('', null)
      location.hash = '#/login'
    }
    ElMessage.error(msg)
    return Promise.reject(err)
  },
)

export default http
