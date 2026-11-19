<script setup>
import { onLoad } from '@dcloudio/uni-app'
import { reactive, ref } from 'vue'

import { api, exchangeWechatTicket, getAuthMethods, setAuth, startWechatLogin } from '../../api'

const form = reactive({ username: '', password: '' })
const loading = ref(false)
const exchanging = ref(false)
const wechat = ref({ enabled: false, mode: 'mock' })

function inWeChat() {
  // App 端没有 navigator，微信内浏览器判断只在 H5 有意义。
  const ua = typeof navigator === 'undefined' ? '' : navigator.userAgent
  return /MicroMessenger/i.test(ua)
}

function loginWithWechat() {
  if (wechat.value.mode === 'live' && !inWeChat()) {
    return uni.showToast({ title: '请在微信中打开链接', icon: 'none' })
  }
  startWechatLogin()
}

onLoad(async (options = {}) => {
  getAuthMethods()
    .then((m) => {
      wechat.value = m.wechat || wechat.value
    })
    .catch(() => {})
  if (options.error === 'wechat_failed') {
    uni.showToast({ title: '微信授权未完成，请重试', icon: 'none' })
  }
  if (options.ticket) {
    exchanging.value = true
    try {
      const { token, user } = await exchangeWechatTicket(options.ticket)
      setAuth(token, user)
      uni.switchTab({ url: '/pages/home/home' })
    } catch {
      // toast already shown
    } finally {
      exchanging.value = false
    }
  }
})

async function submit() {
  if (form.username.length < 3 || form.password.length < 6) {
    return uni.showToast({ title: '用户名至少 3 位，密码至少 6 位', icon: 'none' })
  }
  loading.value = true
  try {
    const { token, user } = await api.post('/api/auth/login', form)
    setAuth(token, user)
    uni.switchTab({ url: '/pages/home/home' })
  } catch {
    // toast already shown
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <view class="page">
    <view class="card" style="margin-top: 40px">
      <view class="title" style="color: #ff2442; font-size: 20px">小红书运营助手</view>
      <view class="muted" style="margin-bottom: 16px">起号方案 · 流量分析 · 互动管理 · AI 助手</view>
      <button v-if="wechat.enabled" class="btn wechat-btn" :disabled="exchanging" @click="loginWithWechat">
        {{ exchanging ? '登录中…' : '微信登录' }}
      </button>
      <view v-if="wechat.enabled" class="muted divider">或使用账号密码</view>
      <input v-model="form.username" class="input" placeholder="用户名" />
      <input v-model="form.password" class="input" password placeholder="密码" />
      <button class="btn" :disabled="loading" @click="submit">登录</button>
      <view class="muted" style="margin-top: 14px; font-size: 12px">账号由运营人员开通，如需使用请联系管理员</view>
    </view>
  </view>
</template>

<style>
.wechat-btn {
  background: #07c160;
  margin-bottom: 4px;
}
.divider {
  text-align: center;
  margin: 12px 0;
}
</style>
