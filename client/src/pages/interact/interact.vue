<script setup>
import { onHide, onShow } from '@dcloudio/uni-app'
import { computed, reactive, ref } from 'vue'

import { api, fmtTime, requireLogin } from '../../api'

const accounts = ref([])
const tasks = ref([])
const quota = ref(null)
const form = reactive({ accountIdx: 0, target_note: '', like: true, collect: true, comment: false, comment_text: '', note_context: '' })
const submitting = ref(false)
const drafts = reactive({}) // task id -> edited comment text
let timer = null

const account = computed(() => accounts.value[form.accountIdx])
const ACTION = { like: '点赞', collect: '收藏', comment: '评论' }
const LABEL = {
  pending_approval: '待你确认', queued: '排队中', running: '执行中', done: '已完成',
  failed: '失败', rejected: '已拒绝', cancelled: '已取消',
}
const TAG = { pending_approval: 'tag-warn', queued: '', running: 'tag-warn', done: 'tag-ok', failed: 'tag-bad', rejected: '', cancelled: '' }

async function loadTasks() {
  tasks.value = await api.get('/api/interactions', { silent: true })
  if (account.value) {
    quota.value = await api.get(`/api/accounts/${account.value.id}/interaction-quota`, { silent: true })
  }
  clearTimeout(timer)
  if (tasks.value.some((t) => ['queued', 'running'].includes(t.status))) timer = setTimeout(loadTasks, 4000)
}

onShow(async () => {
  if (!requireLogin()) return
  accounts.value = await api.get('/api/accounts')
  if (form.accountIdx >= accounts.value.length) form.accountIdx = 0
  await loadTasks()
})
onHide(() => clearTimeout(timer))

async function submit() {
  const actions = ['like', 'collect', 'comment'].filter((a) => form[a])
  if (!form.target_note.trim()) return uni.showToast({ title: '请填写目标笔记链接或 ID', icon: 'none' })
  if (!actions.length) return uni.showToast({ title: '至少选择一种互动', icon: 'none' })
  if (form.comment && !form.comment_text.trim() && !form.note_context.trim()) {
    return uni.showToast({ title: '请填写评论，或粘贴笔记内容让 AI 起草', icon: 'none' })
  }
  submitting.value = true
  try {
    await api.post('/api/interactions', {
      account_id: account.value.id,
      target_note: form.target_note.trim(),
      actions,
      comment_text: form.comment ? form.comment_text : '',
      note_context: form.comment ? form.note_context : '',
    })
    Object.assign(form, { target_note: '', comment_text: '', note_context: '' })
    uni.showToast({ title: '已创建', icon: 'none' })
    await loadTasks()
  } finally {
    submitting.value = false
  }
}

async function approve(task) {
  await api.post(`/api/interactions/${task.id}/approve`, { comment_text: drafts[task.id] ?? task.comment_text })
  await loadTasks()
}

async function cancel(task) {
  await api.post(`/api/interactions/${task.id}/cancel`)
  await loadTasks()
}

function pick(e) {
  form.accountIdx = Number(e.detail.value)
  loadTasks()
}
</script>

<template>
  <view class="page">
    <view v-if="!accounts.length" class="card empty">请先在「账号」页添加小红书账号</view>
    <template v-else>
      <view class="card">
        <view class="title">新建互动</view>
        <picker :range="accounts.map((a) => a.nickname)" :value="form.accountIdx" @change="pick">
          <view class="picker">使用账号：{{ account.nickname }}</view>
        </picker>
        <view v-if="quota" class="muted" style="margin-bottom: 10px">
          今日已执行 {{ quota.used_today }} / {{ quota.daily_limit }} 次，两次互动至少间隔 {{ quota.min_interval_seconds }} 秒
        </view>
        <input v-model="form.target_note" class="input" placeholder="目标笔记链接或笔记 ID" />
        <view class="row" style="margin-bottom: 10px">
          <label><switch :checked="form.like" color="#ff2442" @change="(e) => (form.like = e.detail.value)" /> 点赞</label>
          <label><switch :checked="form.collect" color="#ff2442" @change="(e) => (form.collect = e.detail.value)" /> 收藏</label>
          <label><switch :checked="form.comment" color="#ff2442" @change="(e) => (form.comment = e.detail.value)" /> 评论</label>
        </view>
        <template v-if="form.comment">
          <textarea v-model="form.comment_text" class="textarea" placeholder="评论内容（留空则由 AI 起草）" />
          <textarea
            v-if="!form.comment_text.trim()" v-model="form.note_context" class="textarea"
            placeholder="粘贴这篇笔记的标题和正文，AI 会据此起草一条评论"
          />
          <view class="muted" style="margin-bottom: 10px">
            {{ account.auto_interaction ? '该账号已开启评论免确认，评论会直接排队发出。' : '评论会先进入「待你确认」，确认后才会发出。' }}
          </view>
        </template>
        <button class="btn" :disabled="submitting" @click="submit">{{ submitting ? '提交中…' : '提交' }}</button>
      </view>

      <view v-if="!tasks.length" class="empty">还没有互动任务</view>
      <view v-for="t in tasks" :key="t.id" class="card">
        <view class="row">
          <text>{{ ACTION[t.action] }} · {{ t.target_note }}</text>
          <text class="tag" :class="TAG[t.status]">{{ LABEL[t.status] }}</text>
        </view>
        <template v-if="t.status === 'pending_approval'">
          <textarea
            class="textarea" style="margin-top: 10px" :value="drafts[t.id] ?? t.comment_text"
            @input="(e) => (drafts[t.id] = e.detail.value)"
          />
          <view style="text-align: right">
            <button class="btn btn-ghost btn-sm" @click="cancel(t)">不发</button>
            <button class="btn btn-sm" @click="approve(t)">确认发出</button>
          </view>
        </template>
        <template v-else>
          <view v-if="t.comment_text" style="margin-top: 8px">「{{ t.comment_text }}」</view>
          <view v-if="t.error" class="tag tag-bad" style="margin-top: 8px">{{ t.error }}</view>
          <view class="row" style="margin-top: 8px">
            <text class="muted">{{ fmtTime(t.executed_at || t.created_at) }}</text>
            <button v-if="t.status === 'queued'" class="btn btn-ghost btn-sm" @click="cancel(t)">取消</button>
          </view>
        </template>
      </view>
    </template>
  </view>
</template>
