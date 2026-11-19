// H5 dev uses the vite proxy (relative /api). For the App build set VITE_API_BASE
// to the server's address, e.g. VITE_API_BASE=https://api.example.com
const BASE = import.meta.env.VITE_API_BASE || ''

export function getToken() {
  return uni.getStorageSync('token') || ''
}

export function setAuth(token, user) {
  uni.setStorageSync('token', token)
  uni.setStorageSync('user', user || null)
}

export function logout() {
  setAuth('', null)
  uni.reLaunch({ url: '/pages/login/login' })
}

export function request(method, url, data, { silent = false, timeout = 180000 } = {}) {
  return new Promise((resolve, reject) => {
    uni.request({
      url: BASE + url,
      method,
      data,
      timeout,
      header: { Authorization: getToken() ? `Bearer ${getToken()}` : '' },
      success(res) {
        if (res.statusCode >= 200 && res.statusCode < 300) return resolve(res.data)
        const detail = res.data && res.data.detail
        const msg = typeof detail === 'string' ? detail : `请求失败 (${res.statusCode})`
        if (res.statusCode === 401 && getToken()) logout()
        else if (!silent) uni.showToast({ title: msg, icon: 'none', duration: 2500 })
        reject(new Error(msg))
      },
      fail(err) {
        if (!silent) uni.showToast({ title: '网络错误', icon: 'none' })
        reject(new Error(err.errMsg || '网络错误'))
      },
    })
  })
}

export const api = {
  get: (url, opts) => request('GET', url, undefined, opts),
  post: (url, data, opts) => request('POST', url, data || {}, opts),
  put: (url, data, opts) => request('PUT', url, data, opts),
  del: (url, opts) => request('DELETE', url, undefined, opts),
}

export function isLoggedIn() {
  return !!getToken()
}

// 登录方式：wechat.enabled 为假时登录页整块隐藏微信入口。
export function getAuthMethods() {
  return api.get('/api/auth/methods', { silent: true })
}

// 授权必须在浏览器层整页跳转：uni.request 不会跟随 302。
// App 端没有 location 全局，用条件编译隔离，避免构建进 App 后运行时报错。
export function startWechatLogin() {
  // #ifdef H5
  location.href = `${BASE}/api/auth/wechat/authorize`
  // #endif
  // #ifndef H5
  uni.showToast({ title: '微信登录暂仅支持 H5', icon: 'none' })
  // #endif
}

// 回跳地址上的 ticket 是 60 秒一次性凭证，换成与账密登录同一套 JWT。
export function exchangeWechatTicket(ticket) {
  return api.post('/api/auth/wechat/exchange', { ticket })
}

// 「数据」页对访客开放，未登录时各页面都回到这里，由页面自己提供登录入口。
const PUBLIC_TAB = '/pages/analytics/analytics'

export function goLogin() {
  uni.navigateTo({ url: '/pages/login/login' })
}

export function requireLogin() {
  if (isLoggedIn()) return true
  uni.showToast({ title: '登录后可用', icon: 'none' })
  uni.switchTab({ url: PUBLIC_TAB })
  return false
}

export function fmtTime(s) {
  return s ? s.replace('T', ' ').slice(0, 16) : ''
}

export function pct(x) {
  return `${(x * 100).toFixed(1)}%`
}

// Server-sent events for the plan chat. H5 streams via fetch; App/mini-program
// buffers the whole SSE body with uni.request and replays the frames at once.
export async function streamChat(body, { onSession, onDelta, onDone, onError, signal } = {}) {
  const url = `${BASE}/api/plan-sessions/chat`
  const headers = { 'Content-Type': 'application/json', Authorization: getToken() ? `Bearer ${getToken()}` : '' }
  const dispatch = (event, data) => {
    if (event === 'session') onSession && onSession(data)
    else if (event === 'delta') onDelta && onDelta(data.text || '')
    else if (event === 'done') onDone && onDone(data)
    else if (event === 'error') onError && onError(new Error(data.detail || 'AI 调用失败'))
  }
  const parse = (block) => {
    let event = 'message'
    const lines = []
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) lines.push(line.slice(5).trim())
    }
    if (!lines.length) return
    try { dispatch(event, JSON.parse(lines.join('\n'))) } catch (e) { /* ignore malformed frame */ }
  }
  const fail = async (res) => {
    let msg = `请求失败 (${res.status})`
    try { const j = await res.json(); if (j.detail) msg = j.detail } catch (e) {}
    throw new Error(msg)
  }
  if (typeof fetch === 'function' && typeof TextDecoder === 'function') {
    const res = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body), signal })
    if (!res.ok) await fail(res)
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let idx
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        parse(buf.slice(0, idx))
        buf = buf.slice(idx + 2)
      }
    }
    if (buf.trim()) parse(buf)
    return
  }
  const text = await new Promise((resolve, reject) => {
    uni.request({
      url, method: 'POST', data: body, header: headers, timeout: 300000,
      success: (r) => (r.statusCode >= 200 && r.statusCode < 300
        ? resolve(typeof r.data === 'string' ? r.data : JSON.stringify(r.data))
        : reject(new Error((r.data && r.data.detail) || `请求失败 (${r.statusCode})`))),
      fail: (e) => reject(new Error(e.errMsg || '网络错误')),
    })
  })
  for (const block of text.split('\n\n')) parse(block)
}

// Minimal, safe markdown -> html for <rich-text>: escape first, then format.
export function mdToHtml(md) {
  const esc = (md || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  const inline = (t) => t.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>')
  const out = []
  let list = false
  let table = false
  const close = () => {
    if (list) out.push('</ul>')
    if (table) out.push('</table>')
    list = table = false
  }
  for (const raw of esc.split('\n')) {
    const line = raw.trim()
    if (/^\|.*\|$/.test(line)) {
      if (/^\|[\s:|-]+\|$/.test(line)) continue
      if (list) close()
      if (!table) out.push('<table style="border-collapse:collapse;margin:8px 0;font-size:13px">')
      table = true
      const cells = line.slice(1, -1).split('|').map((c) => `<td style="border:1px solid #ddd;padding:4px 6px">${inline(c.trim())}</td>`)
      out.push(`<tr>${cells.join('')}</tr>`)
    } else if (/^[-*] /.test(line) || /^\d+\. /.test(line)) {
      if (table) close()
      if (!list) out.push('<ul style="padding-left:18px;margin:6px 0">')
      list = true
      out.push(`<li>${inline(line.replace(/^([-*]|\d+\.) /, ''))}</li>`)
    } else {
      close()
      const h = line.match(/^(#{1,4}) (.*)$/)
      if (h) out.push(`<p style="font-weight:600;font-size:${20 - h[1].length * 2}px;margin:14px 0 6px">${inline(h[2])}</p>`)
      else if (line.startsWith('&gt; ')) out.push(`<p style="color:#888;margin:6px 0">${inline(line.slice(5))}</p>`)
      else if (line) out.push(`<p style="margin:6px 0">${inline(line)}</p>`)
    }
  }
  close()
  return out.join('')
}
