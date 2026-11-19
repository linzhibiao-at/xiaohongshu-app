<script setup>
import { onShow } from '@dcloudio/uni-app'
import { computed, ref } from 'vue'

import { api, goLogin, isLoggedIn, mdToHtml, pct } from '../../api'

const loggedIn = ref(false)
const accounts = ref([])
const idx = ref(0)
const report = ref(null)
const diagnosis = ref('')
const busy = ref('')
const account = computed(() => accounts.value[idx.value])

// 笔记链接分析：访客可用，不依赖账号
const noteUrl = ref('')
const noteResult = ref(null)
const analyzing = ref(false)

const noteMetrics = computed(() => {
  const r = noteResult.value
  if (!r) return []
  return [
    ['曝光', r.metrics.impressions], ['阅读', r.metrics.views], ['点赞', r.metrics.likes],
    ['收藏', r.metrics.collects], ['评论', r.metrics.comments],
    ['点击率', pct(r.rates.ctr)], ['互动率', pct(r.rates.engagement_rate)], ['收藏率', pct(r.rates.collect_rate)],
    ['评论率', pct(r.rates.comment_rate)],
  ]
})

const summary = computed(() => {
  const r = report.value
  if (!r) return []
  return [
    ['曝光', r.totals.impressions], ['阅读', r.totals.views], ['点赞', r.totals.likes],
    ['收藏', r.totals.collects], ['评论', r.totals.comments], ['涨粉', r.totals.new_followers],
    ['点击率', pct(r.ctr)], ['互动率', pct(r.engagement_rate)], ['涨粉率', pct(r.follow_rate)],
  ]
})
const trendMax = computed(() => Math.max(1, ...(report.value?.trend || []).map((t) => t.views)))

async function load() {
  diagnosis.value = ''
  report.value = account.value ? await api.get(`/api/accounts/${account.value.id}/analytics`) : null
}

async function analyzeNote() {
  if (!noteUrl.value.trim()) return uni.showToast({ title: '请粘贴笔记链接', icon: 'none' })
  analyzing.value = true
  try {
    noteResult.value = await api.post('/api/notes/analyze', { url: noteUrl.value.trim() })
  } catch {
    // toast already shown
  } finally {
    analyzing.value = false
  }
}

onShow(async () => {
  loggedIn.value = isLoggedIn()
  if (!loggedIn.value) {
    accounts.value = []
    report.value = null
    return
  }
  accounts.value = await api.get('/api/accounts')
  if (idx.value >= accounts.value.length) idx.value = 0
  await load()
})

async function sync() {
  busy.value = 'sync'
  try {
    const { synced } = await api.post(`/api/accounts/${account.value.id}/sync`)
    uni.showToast({ title: `已同步 ${synced} 篇笔记`, icon: 'none' })
    await load()
  } finally {
    busy.value = ''
  }
}

async function diagnose() {
  busy.value = 'diagnose'
  try {
    const res = await api.post(`/api/accounts/${account.value.id}/analytics/diagnose`)
    diagnosis.value = res.diagnosis
  } finally {
    busy.value = ''
  }
}

function pick(e) {
  idx.value = Number(e.detail.value)
  load()
}
</script>

<template>
  <view class="page">
    <view class="card">
      <view class="row" style="margin-bottom: 10px">
        <text class="title" style="margin: 0">笔记链接分析</text>
        <text v-if="!loggedIn" class="muted" @click="goLogin">登录</text>
      </view>
      <view class="row">
        <input v-model="noteUrl" class="input" style="flex: 1; margin: 0 8px 0 0" placeholder="粘贴小红书笔记链接" />
        <button class="btn btn-sm" style="margin: 0; flex: none" :disabled="analyzing" @click="analyzeNote">
          {{ analyzing ? '分析中…' : '分析' }}
        </button>
      </view>
      <view class="muted" style="margin-top: 8px">免登录看单篇笔记的点击、收藏、评论表现</view>
    </view>

    <view v-if="noteResult" class="card">
      <view class="row">
        <text class="title" style="margin: 0">{{ noteResult.note.title }}</text>
        <text class="tag">{{ noteResult.note.note_type === 'video' ? '视频' : '图文' }}</text>
      </view>
      <view class="score">{{ noteResult.score }}<text class="muted"> 分</text></view>
      <view style="margin-bottom: 10px">{{ noteResult.verdict }}</view>
      <view class="grid">
        <view v-for="[label, value] in noteMetrics" :key="label" class="cell">
          <view class="num">{{ value }}</view>
          <view class="muted">{{ label }}</view>
        </view>
      </view>
      <view class="title" style="margin-top: 12px">改进建议</view>
      <view v-for="s in noteResult.suggestions" :key="s" class="advice">{{ s }}</view>
    </view>

    <view v-if="!loggedIn" class="card empty">
      <view>登录后可看自己账号的整体流量报表和 AI 诊断</view>
      <button class="btn" style="margin-top: 14px" @click="goLogin">登录</button>
    </view>

    <template v-else>
      <view v-if="!accounts.length" class="card empty">请先在「账号」页添加小红书账号</view>
      <template v-else>
        <view class="card">
          <picker :range="accounts.map((a) => a.nickname)" :value="idx" @change="pick">
            <view class="picker">账号：{{ account.nickname }}</view>
          </picker>
          <view v-if="account.connector === 'mock'" class="muted" style="margin-bottom: 10px">
            当前为模拟连接器，以下是演示数据，不是真实的小红书数据。
          </view>
          <button class="btn" :disabled="!!busy" @click="sync">{{ busy === 'sync' ? '同步中…' : '同步最新笔记数据' }}</button>
        </view>

        <view v-if="report && !report.note_count" class="card empty">还没有数据，点上方按钮同步</view>
        <template v-if="report && report.note_count">
          <view class="card">
            <view class="title">总览（{{ report.note_count }} 篇笔记）</view>
            <view class="grid">
              <view v-for="[label, value] in summary" :key="label" class="cell">
                <view class="num">{{ value }}</view>
                <view class="muted">{{ label }}</view>
              </view>
            </view>
          </view>

          <view class="card">
            <view class="title">阅读量趋势（按同步日）</view>
            <view v-for="t in report.trend" :key="t.date" class="bar-row">
              <text class="muted bar-label">{{ t.date.slice(5) }}</text>
              <view class="bar" :style="{ width: (t.views / trendMax) * 70 + '%' }" />
              <text class="muted">{{ t.views }}</text>
            </view>
            <view v-if="report.trend.length < 2" class="muted">每天同步一次，才能看到变化趋势。</view>
          </view>

          <view class="card">
            <view class="title">什么内容互动更好</view>
            <view class="muted">发布时段</view>
            <view style="margin: 6px 0 10px">
              <text v-for="h in report.by_publish_hour.slice(0, 4)" :key="h.hour" class="tag">{{ h.hour }} 点 · {{ pct(h.avg_engagement_rate) }}</text>
            </view>
            <view class="muted">标签</view>
            <view style="margin: 6px 0 10px">
              <text v-for="t in report.by_tag.slice(0, 5)" :key="t.tag" class="tag">#{{ t.tag }} · {{ pct(t.avg_engagement_rate) }}</text>
            </view>
            <view class="muted">笔记类型</view>
            <view style="margin-top: 6px">
              <text v-for="t in report.by_type" :key="t.note_type" class="tag">{{ t.note_type === 'video' ? '视频' : '图文' }} · {{ pct(t.avg_engagement_rate) }}</text>
            </view>
          </view>

          <view class="card">
            <view class="title">AI 流量诊断</view>
            <rich-text v-if="diagnosis" :nodes="mdToHtml(diagnosis)" style="line-height: 1.7" />
            <button class="btn btn-ghost" style="margin-top: 8px" :disabled="!!busy" @click="diagnose">
              {{ busy === 'diagnose' ? '分析中，请稍候…' : diagnosis ? '重新诊断' : '让数据分析师诊断' }}
            </button>
          </view>

          <view class="card">
            <view class="title">笔记明细（按阅读量）</view>
            <view v-for="n in report.notes" :key="n.id" class="note">
              <view>{{ n.title }}</view>
              <view class="muted" style="margin-top: 4px">
                曝光 {{ n.impressions }} · 阅读 {{ n.views }} · 点击率 {{ pct(n.ctr) }} · 互动率 {{ pct(n.engagement_rate) }}
              </view>
              <view class="muted">赞 {{ n.likes }} · 藏 {{ n.collects }} · 评 {{ n.comments }} · 涨粉 {{ n.new_followers }}</view>
            </view>
          </view>
        </template>
      </template>
    </template>
  </view>
</template>

<style scoped>
.grid { display: flex; flex-wrap: wrap; }
.cell { width: 33.33%; text-align: center; padding: 8px 0; }
.num { font-size: 18px; font-weight: 600; }
.bar-row { display: flex; align-items: center; margin-bottom: 6px; }
.bar-label { width: 44px; }
.bar { height: 10px; background: #ff2442; border-radius: 5px; margin-right: 8px; min-width: 2px; }
.note { padding: 10px 0; border-bottom: 1px solid #f0f1f3; }
.note:last-child { border-bottom: none; }
.score { font-size: 30px; font-weight: 600; color: #ff2442; margin: 8px 0 4px; }
.advice { color: #555; line-height: 1.6; margin-bottom: 8px; }
</style>
