<script setup>
import { onHide, onShow } from '@dcloudio/uni-app'
import { computed, ref } from 'vue'

import { api, fmtTime, goLogin, isLoggedIn } from '../../api'

const loggedIn = ref(false)
const tab = ref('chat')
const sessions = ref([])
const plans = ref([])
let timer = null

// 对话超过这个条数时，列表只留最近几条，其余收进「聊天历史」面板。
const MAX_RECENT = 5
const historyOpen = ref(false)
const recent = computed(() => sessions.value.slice(0, MAX_RECENT))
const hiddenCount = computed(() => Math.max(0, sessions.value.length - MAX_RECENT))

// 左滑露出删除按钮：只维护一个打开项，拖动距离直接转成 translateX。
const SWIPE_WIDTH = 76
const swipeId = ref(null)
const swipeOffset = ref(0)
const dragging = ref(false)
let touch = null
let swipedAt = 0

function offsetOf(s) {
  return swipeId.value === s.id ? swipeOffset.value : 0
}

function closeSwipe() {
  swipeId.value = null
  swipeOffset.value = 0
}

function onTouchStart(e, s) {
  if (swipeId.value !== null && swipeId.value !== s.id) closeSwipe()
  const t = e.touches && e.touches[0]
  if (!t) return
  touch = {
    id: s.id,
    x: t.clientX,
    y: t.clientY,
    base: swipeId.value === s.id ? swipeOffset.value : 0,
    locked: false,
  }
  dragging.value = true
}

function onTouchMove(e, s) {
  if (!touch || touch.id !== s.id) return
  const t = e.touches && e.touches[0]
  if (!t) return
  const dx = t.clientX - touch.x
  const dy = t.clientY - touch.y
  if (!touch.locked) {
    if (Math.abs(dx) < 6 && Math.abs(dy) < 6) return
    // 竖向手势交给页面滚动，只有明确的横向拖动才接管。
    if (Math.abs(dy) >= Math.abs(dx)) {
      touch = null
      dragging.value = false
      return
    }
    touch.locked = true
  }
  swipeId.value = s.id
  swipeOffset.value = Math.min(8, Math.max(-SWIPE_WIDTH - 12, touch.base + dx))
}

function onTouchEnd(e, s) {
  if (!touch || touch.id !== s.id) return
  const locked = touch.locked
  touch = null
  dragging.value = false
  if (!locked) return
  swipedAt = Date.now()
  if (swipeOffset.value <= -SWIPE_WIDTH / 2) {
    swipeId.value = s.id
    swipeOffset.value = -SWIPE_WIDTH
  } else {
    closeSwipe()
  }
}

function onCardClick(s) {
  // 刚完成滑动时浏览器也会派发 click，这里吞掉以免误进对话。
  if (Date.now() - swipedAt < 350) return
  if (swipeId.value !== null) return closeSwipe()
  openSession(s)
}

function closeHistory() {
  historyOpen.value = false
  closeSwipe()
}

const LABEL = { pending: '等待中', running: '生成中', done: '已完成', failed: '失败' }
const TAG = { pending: '', running: 'tag-warn', done: 'tag-ok', failed: 'tag-bad' }

async function loadSessions() {
  sessions.value = await api.get('/api/plan-sessions', { silent: true })
  if (swipeId.value !== null && !sessions.value.some((s) => s.id === swipeId.value)) closeSwipe()
}

async function loadPlans() {
  plans.value = await api.get('/api/plans', { silent: true })
  const busy = plans.value.some((p) => ['pending', 'running'].includes(p.status))
  clearTimeout(timer)
  if (busy) timer = setTimeout(loadPlans, 3000)
}

onShow(async () => {
  // 「方案」对访客开放浏览；真正对话/生成时才要求登录。
  loggedIn.value = isLoggedIn()
  if (!loggedIn.value) {
    sessions.value = []
    plans.value = []
    return
  }
  await Promise.all([loadSessions(), loadPlans()])
})
onHide(() => clearTimeout(timer))

function newChat() {
  if (!loggedIn.value) return goLogin()
  uni.navigateTo({ url: '/pages/plan/chat' })
}

function openSession(s) {
  if (!loggedIn.value) return goLogin()
  historyOpen.value = false
  closeSwipe()
  uni.navigateTo({ url: `/pages/plan/chat?id=${s.id}` })
}

function removeSession(s) {
  uni.showModal({
    title: '删除对话',
    content: `「${s.title || '新对话'}」的聊天记录将一并删除，无法恢复。`,
    confirmText: '删除',
    confirmColor: '#d6213a',
    success: async ({ confirm }) => {
      if (!confirm) return
      try {
        await api.del(`/api/plan-sessions/${s.id}`)
      } catch (e) {
        return
      }
      sessions.value = sessions.value.filter((x) => x.id !== s.id)
      closeSwipe()
      if (!sessions.value.length) historyOpen.value = false
      uni.showToast({ title: '已删除', icon: 'none' })
    },
  })
}

function open(p) {
  uni.navigateTo({ url: `/pages/plan/detail?id=${p.id}` })
}
</script>

<template>
  <view class="page">
    <view class="tabs">
      <view class="tab" :class="{ 'tab-on': tab === 'chat' }" @click="tab = 'chat'">对话</view>
      <view class="tab" :class="{ 'tab-on': tab === 'plan' }" @click="tab = 'plan'">方案</view>
    </view>

    <template v-if="tab === 'chat'">
      <view class="card intro">
        <view class="title" style="margin-bottom: 6px">和起号专家聊聊</view>
        <view class="muted">说说你的赛道、账号现状和目标，专家会追问关键信息，聊清楚后一键生成起号方案。</view>
        <button class="btn" style="margin-top: 12px" @click="newChat">+ 新建对话</button>
      </view>

      <view v-if="!loggedIn" class="empty" @click="goLogin">登录后开始对话</view>
      <view v-else-if="!sessions.length" class="empty">还没有对话</view>
      <template v-else>
        <view class="row list-head">
          <text class="muted">最近对话 · 左滑可删除</text>
          <view v-if="hiddenCount" class="history" @click="historyOpen = true">
            <view class="icon-clock"></view>
            <text class="history-count">{{ sessions.length }}</text>
          </view>
        </view>
        <view
          v-for="s in recent"
          :key="s.id"
          class="sess-swipe"
          :class="{ dragging }"
          @touchstart="onTouchStart($event, s)"
          @touchmove="onTouchMove($event, s)"
          @touchend="onTouchEnd($event, s)"
          @touchcancel="onTouchEnd($event, s)"
        >
          <view class="swipe-del" @click.stop="removeSession(s)">删除</view>
          <view
            class="card swipe-body"
            :style="{ transform: `translateX(${offsetOf(s)}px)` }"
            @click="onCardClick(s)"
          >
            <view class="row">
              <text class="title sess-title">{{ s.title || '新对话' }}</text>
              <text v-if="s.plan_id" class="tag tag-ok" style="margin-right: 0; flex-shrink: 0">已生成方案</text>
            </view>
            <view class="muted" style="margin-top: 6px">{{ fmtTime(s.updated_at) }}</view>
          </view>
        </view>
      </template>
    </template>

    <template v-else>
      <view v-if="!loggedIn" class="empty" @click="goLogin">登录后可查看你生成的方案</view>
      <view v-else-if="!plans.length" class="empty">还没有方案</view>
      <view v-for="p in plans" :key="p.id" class="card" @click="open(p)">
        <view class="row">
          <text class="title" style="margin: 0">{{ p.niche }}</text>
          <text class="tag" :class="TAG[p.status]">{{ LABEL[p.status] }}</text>
        </view>
        <view style="margin: 6px 0">{{ p.goal }}</view>
        <view class="muted">{{ fmtTime(p.created_at) }}</view>
      </view>
    </template>

    <view v-if="historyOpen" class="mask" @click="closeHistory">
      <view class="sheet" @click.stop>
        <view class="row sheet-head">
          <text class="title" style="margin: 0">聊天历史（{{ sessions.length }}）</text>
          <text class="muted sheet-close" @click="closeHistory">关闭</text>
        </view>
        <scroll-view class="sheet-list" scroll-y>
          <view
            v-for="s in sessions"
            :key="s.id"
            class="hist-swipe"
            :class="{ dragging }"
            @touchstart="onTouchStart($event, s)"
            @touchmove="onTouchMove($event, s)"
            @touchend="onTouchEnd($event, s)"
            @touchcancel="onTouchEnd($event, s)"
          >
            <view class="swipe-del" @click.stop="removeSession(s)">删除</view>
            <view
              class="hist-body"
              :style="{ transform: `translateX(${offsetOf(s)}px)` }"
              @click="onCardClick(s)"
            >
              <view class="hist-main">
                <text class="hist-title">{{ s.title || '新对话' }}</text>
                <text class="muted">{{ fmtTime(s.updated_at) }}</text>
              </view>
              <text v-if="s.plan_id" class="tag tag-ok" style="margin-right: 0; flex-shrink: 0">已生成方案</text>
            </view>
          </view>
          <view v-if="!sessions.length" class="empty">还没有对话</view>
        </scroll-view>
      </view>
    </view>
  </view>
</template>

<style scoped>
.tabs {
  display: flex;
  background: #fff;
  border-radius: 10px;
  padding: 4px;
  margin-bottom: 12px;
}
.tab {
  flex: 1;
  text-align: center;
  line-height: 34px;
  border-radius: 8px;
  color: #666;
}
.tab-on {
  background: #ff2442;
  color: #fff;
  font-weight: 600;
}
.intro .muted {
  line-height: 1.5;
}
.list-head {
  margin: 0 2px 8px;
}
.sess-title {
  flex: 1;
  min-width: 0;
  margin: 0;
  padding-right: 8px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sess-swipe {
  position: relative;
  margin-bottom: 12px;
  border-radius: 10px;
  overflow: hidden;
  background: #ff3b30;
  touch-action: pan-y;
  -webkit-user-select: none;
  user-select: none;
}
.sess-swipe .card {
  margin-bottom: 0;
  border-radius: 10px;
}
.swipe-body,
.hist-body {
  position: relative;
  transition: transform 0.2s ease;
  will-change: transform;
}
.sess-swipe.dragging .swipe-body,
.hist-swipe.dragging .hist-body {
  transition: none;
}
.swipe-del {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 76px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 13px;
  background: #ff3b30;
}
/* Qoder 风格的时钟图标：纯 CSS 绘制，避免引入图片资源。 */
.history {
  display: flex;
  align-items: center;
  padding: 2px 0 2px 6px;
}
.history:active {
  opacity: 0.6;
}
.icon-clock {
  position: relative;
  width: 20px;
  height: 20px;
  box-sizing: border-box;
  border: 2px solid #444;
  border-radius: 50%;
}
.icon-clock::before,
.icon-clock::after {
  content: '';
  position: absolute;
  background: #444;
  border-radius: 1px;
}
.icon-clock::before {
  left: 7px;
  top: 3px;
  width: 2px;
  height: 5px;
}
.icon-clock::after {
  left: 8px;
  top: 7px;
  width: 5px;
  height: 2px;
}
.history-count {
  min-width: 16px;
  height: 16px;
  line-height: 16px;
  padding: 0 4px;
  margin-left: 4px;
  border-radius: 8px;
  background: #f0f1f3;
  color: #666;
  font-size: 11px;
  text-align: center;
  box-sizing: border-box;
}
.mask {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  left: 0;
  z-index: 99;
  display: flex;
  align-items: flex-end;
  background: rgba(0, 0, 0, 0.45);
}
.sheet {
  width: 100%;
  max-height: 70vh;
  display: flex;
  flex-direction: column;
  background: #fff;
  border-radius: 14px 14px 0 0;
  padding: 16px 16px calc(16px + env(safe-area-inset-bottom));
  box-sizing: border-box;
}
.sheet-head {
  margin-bottom: 4px;
}
.sheet-close {
  padding: 4px 0 4px 12px;
}
.sheet-list {
  height: 55vh;
}
.hist-swipe {
  position: relative;
  overflow: hidden;
  background: #ff3b30;
  touch-action: pan-y;
  -webkit-user-select: none;
  user-select: none;
}
.hist-body {
  display: flex;
  align-items: center;
  padding: 12px 0;
  background: #fff;
  border-bottom: 1px solid #f2f3f5;
}
.hist-body:active {
  background: #fafafa;
}
.hist-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.hist-title {
  font-size: 14px;
  color: #222;
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
