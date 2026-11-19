<script setup>
import { onShow } from '@dcloudio/uni-app'
import { nextTick, ref } from 'vue'

import { api, mdToHtml, requireLogin } from '../../api'

const personas = ref([])
const idx = ref(0)
const messages = ref([]) // { role, text }
const input = ref('')
const sending = ref(false)
const status = ref(null)
const scrollTo = ref('')

onShow(async () => {
  if (!requireLogin()) return
  ;[personas.value, status.value] = await Promise.all([api.get('/api/personas'), api.get('/api/agent/status')])
  if (idx.value >= personas.value.length) idx.value = 0
})

function pick(e) {
  idx.value = Number(e.detail.value)
  messages.value = []
}

async function send() {
  const text = input.value.trim()
  if (!text || sending.value) return
  messages.value.push({ role: 'user', text })
  input.value = ''
  sending.value = true
  scroll()
  try {
    const { reply } = await api.post('/api/agent/chat', { persona_key: personas.value[idx.value].key, message: text })
    messages.value.push({ role: 'assistant', text: reply })
  } catch (e) {
    messages.value.push({ role: 'error', text: e.message })
  } finally {
    sending.value = false
    scroll()
  }
}

function scroll() {
  nextTick(() => { scrollTo.value = `m${messages.value.length - 1}` })
}

function openPersonas() {
  uni.navigateTo({ url: '/pages/persona/persona' })
}
</script>

<template>
  <view class="chat">
    <view class="card top">
      <view class="row">
        <picker style="flex: 1" :range="personas.map((p) => p.name)" :value="idx" @change="pick">
          <view>当前人格：{{ personas[idx]?.name }} ▾</view>
        </picker>
        <text class="muted" @click="openPersonas">管理人格</text>
      </view>
      <view class="muted" style="margin-top: 6px">{{ personas[idx]?.description }}</view>
      <view v-if="status && !status.llm_configured" class="tag tag-warn" style="margin-top: 8px">
        平台尚未配置大模型，暂时无法对话
      </view>
    </view>

    <scroll-view scroll-y class="list" :scroll-into-view="scrollTo">
      <view v-if="!messages.length" class="empty">问问起号、选题、数据复盘的问题</view>
      <view v-for="(m, i) in messages" :id="`m${i}`" :key="i" class="msg" :class="m.role">
        <rich-text v-if="m.role === 'assistant'" :nodes="mdToHtml(m.text)" />
        <text v-else>{{ m.text }}</text>
      </view>
      <view v-if="sending" class="msg assistant muted">思考中…</view>
    </scroll-view>

    <view class="bar">
      <input v-model="input" class="input" style="flex: 1; margin: 0" placeholder="输入你的问题" confirm-type="send" @confirm="send" />
      <button class="btn btn-sm" :disabled="sending" @click="send">发送</button>
    </view>
  </view>
</template>

<style scoped>
.chat { display: flex; flex-direction: column; height: calc(100vh - var(--window-top) - var(--window-bottom)); }
.top { margin: 12px 12px 0; }
.list { flex: 1; height: 0; padding: 12px; box-sizing: border-box; }
.msg { max-width: 86%; padding: 10px 12px; border-radius: 10px; margin-bottom: 10px; line-height: 1.6; word-break: break-word; }
.msg.user { background: #ff2442; color: #fff; margin-left: auto; width: fit-content; }
.msg.assistant { background: #fff; }
.msg.error { background: #fde8ea; color: #d6213a; }
.bar { display: flex; align-items: center; padding: 8px 12px; background: #fff; }
</style>
