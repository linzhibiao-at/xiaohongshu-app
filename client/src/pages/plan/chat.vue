<script setup>
import { onLoad, onUnload } from '@dcloudio/uni-app'
import { computed, nextTick, reactive, ref } from 'vue'

import { api, mdToHtml, requireLogin, streamChat } from '../../api'

const OPENING =
  '你好，我是你的小红书起号专家。先聊聊：你想做哪个赛道、账号现在什么情况、多久想达到什么目标？信息越具体，方案越落地。'

const session = ref(null)
const newSession = ref(true)
const messages = ref([])
const input = ref('')
const sending = ref(false)
const generating = ref(false)
const llmOn = ref(true)
const accounts = ref([])
const pickedIdx = ref(0)
const anchor = ref('')

const templateForm = reactive({ niche: '', goal: '' })
const templateBusy = ref(false)

const accountOptions = computed(() => ['全新账号（无数据）', ...accounts.value.map((a) => a.nickname)])
const PLAN_LABEL = { pending: '生成中', running: '生成中', done: '已完成', failed: '生成失败' }
const PLAN_TAG = { pending: 'tag-warn', running: 'tag-warn', done: 'tag-ok', failed: 'tag-bad' }

// A trailing run of "A. xxx" / "1. xxx" lines in the latest reply becomes tappable in place,
// so the options are shown once (inside the reply) instead of duplicated below it.
const OPTION_RE = /^[-*\u2022\u00b7]?\s*\*{0,2}([A-Da-d]|\d{1,2})[.、)）．]\*{0,2}\s*(.+)$/

function splitOptions(text) {
  const lines = (text || '').split('\n')
  const flat = lines.map((l) => l.trim())
  // allowSkip: tolerate a short closing sentence after the list (lettered options only).
  const findBlock = (allowSkip, lettersOnly) => {
    let end = flat.length - 1
    while (end >= 0 && !flat[end]) end -= 1
    if (end < 0) return null
    const block = []
    let blockEnd = -1
    let skipped = 0
    let i = end
    for (; i >= 0; i -= 1) {
      const line = flat[i]
      if (!line) continue
      const m = line.match(OPTION_RE)
      const lettered = m && /[A-Da-d]/.test(m[1])
      if (m && (!lettersOnly || lettered)) {
        if (blockEnd < 0) blockEnd = i
        block.unshift({
          key: m[1].toUpperCase(),
          text: m[2].replace(/\*\*/g, '').trim(),
          raw: line.replace(/^[-*\u2022\u00b7]\s*/, '').replace(/\*\*/g, '').trim(),
        })
        skipped = 0
        continue
      }
      if (!block.length) {
        if (!allowSkip || (skipped += 1) > 3) return null
        continue
      }
      break
    }
    return block.length >= 2 ? { start: i + 1, end: blockEnd, block } : null
  }
  const found = findBlock(false, false) || findBlock(true, true)
  if (!found) return { body: text || '', options: [], tail: '' }
  return {
    body: lines.slice(0, found.start).join('\n').trim(),
    options: found.block,
    tail: lines.slice(found.end + 1).join('\n').trim(),
  }
}

const lastSplit = computed(() => {
  const last = messages.value[messages.value.length - 1]
  if (!last || last.role !== 'assistant' || last.plan_id || last.streaming) return null
  const parts = splitOptions(last.content)
  return parts.options.length ? parts : null
})

// Index of the bubble whose options are interactive (-1 when none).
const hotIndex = computed(() => (lastSplit.value ? messages.value.length - 1 : -1))

let controller = null
let flushTimer = null
let timers = []
let seq = 0

function key(prefix) {
  seq += 1
  return `${prefix}${Date.now()}_${seq}`
}

function scrollDown() {
  nextTick(() => {
    anchor.value = ''
    nextTick(() => {
      anchor.value = `m-${messages.value.length - 1}`
    })
  })
}

function push(item) {
  // reactive() so later mutations (streaming text, plan state) trigger re-render.
  const row = reactive(item)
  messages.value.push(row)
  scrollDown()
  return row
}

function pickedAccountId() {
  const a = accounts.value[pickedIdx.value - 1]
  return a ? a.id : null
}

function decorate(row) {
  return reactive({
    key: key('h'),
    id: row.id,
    role: row.role,
    content: row.content || '',
    plan_id: row.plan_id || null,
    planState: row.plan_id ? 'pending' : null,
    planError: '',
    streaming: false,
    opening: false,
  })
}

async function refreshPlan(m) {
  try {
    const p = await api.get(`/api/plans/${m.plan_id}`, { silent: true })
    m.planState = p.status
    m.planError = p.error || ''
  } catch (e) {
    m.planState = 'failed'
    m.planError = e.message || '方案读取失败'
  }
}

onLoad(async (query) => {
  if (!requireLogin()) return
  const [st, accs] = await Promise.all([
    api.get('/api/agent/status', { silent: true }).catch(() => ({ llm_configured: true })),
    api.get('/api/accounts', { silent: true }).catch(() => []),
  ])
  llmOn.value = st.llm_configured !== false
  accounts.value = accs

  if (query && query.id) {
    const detail = await api.get(`/api/plan-sessions/${query.id}`)
    session.value = detail.session
    newSession.value = false
    messages.value = detail.messages.map(decorate)
    scrollDown()
    await Promise.all(messages.value.filter((m) => m.plan_id).map(refreshPlan))
    // Reasoning-model plans can take many minutes; keep watching the ones still running.
    messages.value
      .filter((m) => m.plan_id && ['pending', 'running'].includes(m.planState))
      .forEach((m) => pollPlan(m.plan_id, m))
    return
  }
  push({
    key: key('open'),
    role: 'assistant',
    content: OPENING,
    plan_id: null,
    planState: null,
    planError: '',
    streaming: false,
    opening: true,
  })
})

onUnload(() => {
  if (controller) controller.abort()
  if (flushTimer) clearTimeout(flushTimer)
  timers.forEach((t) => clearTimeout(t))
  timers = []
})

async function send() {
  const text = input.value.trim()
  if (!text || sending.value || !llmOn.value) return
  input.value = ''
  const userMsg = reactive({
    key: key('u'), id: null, role: 'user', content: text,
    plan_id: null, planState: null, planError: '', streaming: false,
  })
  const bubble = reactive({
    key: key('a'), id: null, role: 'assistant', content: '',
    plan_id: null, planState: null, planError: '', streaming: true,
  })
  messages.value.push(userMsg, bubble)
  scrollDown()
  sending.value = true
  controller = typeof AbortController === 'function' ? new AbortController() : null

  let buf = ''
  const body = { message: text }
  if (session.value) body.session_id = session.value.id
  else body.account_id = pickedAccountId()

  try {
    await streamChat(body, {
      signal: controller ? controller.signal : undefined,
      onSession: (d) => {
        session.value = { id: d.session_id, title: d.title }
        newSession.value = false
        if (d.user_message) userMsg.id = d.user_message.id
      },
      onDelta: (t) => {
        buf += t
        if (!flushTimer) {
          flushTimer = setTimeout(() => {
            bubble.content = buf
            flushTimer = null
            scrollDown()
          }, 50)
        }
      },
      onDone: (d) => {
        if (flushTimer) {
          clearTimeout(flushTimer)
          flushTimer = null
        }
        bubble.content = d.message.content
        bubble.id = d.message.id
        bubble.streaming = false
        scrollDown()
      },
      onError: (e) => {
        if (flushTimer) {
          clearTimeout(flushTimer)
          flushTimer = null
        }
        bubble.streaming = false
        if (!bubble.content) messages.value = messages.value.filter((m) => m !== bubble)
        uni.showToast({ title: e.message || 'AI 调用失败', icon: 'none' })
      },
    })
  } catch (e) {
    if (flushTimer) {
      clearTimeout(flushTimer)
      flushTimer = null
    }
    bubble.streaming = false
    if (!bubble.content) messages.value = messages.value.filter((m) => m !== bubble)
    if (e && e.name !== 'AbortError') uni.showToast({ title: e.message || '网络错误', icon: 'none' })
  } finally {
    sending.value = false
    controller = null
    // A dropped connection can end the stream without a done/error frame.
    if (bubble.streaming && !bubble.content) {
      messages.value = messages.value.filter((m) => m !== bubble)
    }
    bubble.streaming = false
    scrollDown()
  }
}

async function pollPlan(planId, m, tries = 0) {
  await refreshPlan(m)
  if (['pending', 'running'].includes(m.planState) && tries < 400) {
    timers.push(setTimeout(() => pollPlan(planId, m, tries + 1), 3000))
  }
}

async function generate() {
  if (!session.value || generating.value || sending.value) return
  generating.value = true
  try {
    const res = await api.post(`/api/plan-sessions/${session.value.id}/plan`, {})
    const card = push({
      key: key('p'),
      id: null,
      role: 'assistant',
      content: '',
      plan_id: res.plan_id,
      planState: 'pending',
      planError: '',
      streaming: false,
    })
    pollPlan(res.plan_id, card)
  } finally {
    generating.value = false
  }
}

function openPlan(m) {
  uni.navigateTo({ url: `/pages/plan/detail?id=${m.plan_id}` })
}

// Tapping a chip answers with that option, so the model keeps the flow going.
function pick(o) {
  if (sending.value || !llmOn.value) return
  input.value = o.raw
  send()
}

async function submitTemplate() {
  if (!templateForm.niche.trim() || !templateForm.goal.trim()) {
    return uni.showToast({ title: '请填写赛道和目标', icon: 'none' })
  }
  templateBusy.value = true
  try {
    const plan = await api.post('/api/plans', {
      niche: templateForm.niche.trim(),
      goal: templateForm.goal.trim(),
      account_id: pickedAccountId(),
      persona_key: 'growth_coach',
    })
    uni.navigateTo({ url: `/pages/plan/detail?id=${plan.id}` })
  } finally {
    templateBusy.value = false
  }
}
</script>

<template>
  <view class="wrap">
    <scroll-view class="chat" scroll-y :scroll-into-view="anchor" scroll-with-animation>
      <view
        v-for="(m, i) in messages"
        :id="`m-${i}`"
        :key="m.key"
        class="msg"
        :class="m.role"
      >
        <view v-if="m.plan_id" class="card plan-card" @click="openPlan(m)">
          <view class="row">
            <text class="title" style="margin: 0">起号方案</text>
            <text class="tag" :class="PLAN_TAG[m.planState] || ''">{{ PLAN_LABEL[m.planState] || '生成中' }}</text>
          </view>
          <view class="muted" style="margin-top: 8px">{{ m.planError || '点击查看方案详情' }}</view>
        </view>
        <view v-else class="bubble" :class="{ typing: m.streaming }">
          <template v-if="m.role === 'assistant' && i === hotIndex">
            <rich-text v-if="lastSplit.body" :nodes="mdToHtml(lastSplit.body)" />
            <view class="opts">
              <view
                v-for="o in lastSplit.options"
                :key="o.key"
                class="opt"
                :class="{ 'opt-off': sending }"
                @click="pick(o)"
              >
                <text class="opt-key">{{ o.key }}</text>
                <text class="opt-text">{{ o.text }}</text>
              </view>
            </view>
            <rich-text v-if="lastSplit.tail" :nodes="mdToHtml(lastSplit.tail)" />
          </template>
          <rich-text v-else-if="m.role === 'assistant'" :nodes="mdToHtml(m.content || (m.streaming ? '思考中…' : ''))" />
          <text v-else>{{ m.content }}</text>
        </view>
      </view>
      <view id="m-end" style="height: 8px"></view>
    </scroll-view>

    <view class="footer">
      <view v-if="!llmOn" class="card" style="margin-bottom: 8px">
        <view class="muted" style="margin-bottom: 8px">AI 未启用（未配置 LLM_API_KEY），可先用模板快速生成方案。</view>
        <input v-model="templateForm.niche" class="input" placeholder="赛道，如 健身 / 母婴 / 考研" />
        <input v-model="templateForm.goal" class="input" placeholder="目标，如 30 天涨粉 1000" />
        <button class="btn" :disabled="templateBusy" @click="submitTemplate">模板生成方案</button>
      </view>

      <template v-else>
        <view class="row toolbar">
          <picker
            v-if="newSession"
            :range="accountOptions"
            :value="pickedIdx"
            @change="(e) => (pickedIdx = Number(e.detail.value))"
          >
            <view class="picker-mini">结合账号：{{ accountOptions[pickedIdx] }}</view>
          </picker>
          <text v-else class="muted">{{ session && session.title ? session.title : '对话中' }}</text>
          <button class="btn btn-ghost btn-sm" :disabled="!session || generating || sending" @click="generate">
            {{ generating ? '生成中…' : '生成起号方案' }}
          </button>
        </view>
        <view class="inputbar">
          <textarea
            v-model="input"
            class="textarea chat-input"
            auto-height
            :disabled="sending"
            placeholder="说说你的赛道、现状和目标…"
            @confirm="send"
          />
          <button class="btn send" :disabled="sending || !input.trim()" @click="send">{{ sending ? '…' : '发送' }}</button>
        </view>
      </template>
    </view>
  </view>
</template>

<style scoped>
.wrap {
  display: flex;
  flex-direction: column;
  height: calc(100vh - var(--window-top) - var(--window-bottom));
}
.chat {
  flex: 1;
  padding: 12px;
  box-sizing: border-box;
}
.msg {
  display: flex;
  margin: 10px 0;
}
.msg.user {
  justify-content: flex-end;
}
.bubble {
  max-width: 80%;
  border-radius: 12px;
  padding: 10px 12px;
  line-height: 1.6;
  word-break: break-word;
  font-size: 14px;
}
.msg.user .bubble {
  background: #ff2442;
  color: #fff;
  border-bottom-right-radius: 2px;
}
.msg.assistant .bubble {
  background: #fff;
  border-bottom-left-radius: 2px;
}
.bubble.typing::after {
  content: '▍';
  color: #ff2442;
  animation: blink 1s infinite;
}
@keyframes blink {
  50% {
    opacity: 0;
  }
}
.opts {
  margin-top: 6px;
}
.opt {
  display: flex;
  align-items: flex-start;
  padding: 4px 2px;
  border-radius: 6px;
}
.opt:active {
  background: #fff0f2;
}
.opt-off {
  opacity: 0.5;
}
.opt-key {
  color: #ff2442;
  font-weight: 600;
  width: 18px;
  flex-shrink: 0;
}
.opt-text {
  flex: 1;
  line-height: 1.55;
}
.plan-card {
  width: 100%;
  margin-bottom: 0;
}
.footer {
  background: #fff;
  border-top: 1px solid #eee;
  padding: 8px 12px;
}
.toolbar {
  margin-bottom: 8px;
}
.picker-mini {
  color: #666;
  font-size: 13px;
}
.inputbar {
  display: flex;
  align-items: flex-end;
}
.chat-input {
  flex: 1;
  min-height: 40px;
  max-height: 110px;
  margin-bottom: 0;
  margin-right: 8px;
}
.send {
  width: 72px;
  line-height: 40px;
  font-size: 14px;
  flex-shrink: 0;
}
</style>
