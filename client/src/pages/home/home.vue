<script setup>
import { onShow } from '@dcloudio/uni-app'
import { reactive, ref } from 'vue'

import { api, isLoggedIn, logout } from '../../api'

const accounts = ref([])
const user = ref(null)
const editing = ref(null) // null | 'new' | account id
const form = reactive({ xhs_id: '', nickname: '', auto_interaction: false })

async function load() {
  accounts.value = await api.get('/api/accounts')
}

onShow(() => {
  // 「我的」需要登录：访客直接落到公开的「数据」tab。
  if (!isLoggedIn()) return uni.switchTab({ url: '/pages/analytics/analytics' })
  user.value = uni.getStorageSync('user')
  load()
})

function edit(account) {
  editing.value = account ? account.id : 'new'
  Object.assign(form, account
    ? { xhs_id: account.xhs_id, nickname: account.nickname, auto_interaction: account.auto_interaction }
    : { xhs_id: '', nickname: '', auto_interaction: false })
}

async function save() {
  if (!form.xhs_id.trim()) return uni.showToast({ title: '请填写小红书账号', icon: 'none' })
  if (!form.nickname.trim()) return uni.showToast({ title: '请填写小红书账号名称', icon: 'none' })
  const body = { xhs_id: form.xhs_id.trim(), nickname: form.nickname.trim(), auto_interaction: form.auto_interaction }
  if (editing.value === 'new') await api.post('/api/accounts', body)
  else await api.put(`/api/accounts/${editing.value}`, body)
  editing.value = null
  await load()
}

function remove(account) {
  uni.showModal({
    title: '删除账号',
    content: `确定删除「${account.nickname}」？`,
    success: async (res) => {
      if (!res.confirm) return
      await api.del(`/api/accounts/${account.id}`)
      await load()
    },
  })
}
</script>

<template>
  <view class="page">
    <view class="card row">
      <text>你好，{{ user?.username }}</text>
      <view>
        <text class="muted" @click="logout">退出登录</text>
      </view>
    </view>

    <view v-if="editing" class="card">
      <view class="title">{{ editing === 'new' ? '添加小红书账号' : '编辑账号' }}</view>
      <input v-model="form.xhs_id" class="input" placeholder="小红书账号，如 xhs_abc123 或小红书号" />
      <input v-model="form.nickname" class="input" placeholder="小红书账号名称" />
      <view class="row" style="margin-bottom: 4px">
        <text>评论免确认，直接执行</text>
        <switch :checked="form.auto_interaction" color="#ff2442" @change="(e) => (form.auto_interaction = e.detail.value)" />
      </view>
      <view class="muted" style="margin-bottom: 12px">关闭时，每条评论都会先等你确认内容再发出。点赞和收藏始终直接排队执行。</view>
      <button class="btn" @click="save">保存</button>
      <button class="btn btn-ghost" style="margin-top: 10px" @click="editing = null">取消</button>
    </view>

    <template v-else>
      <view v-if="!accounts.length" class="card empty">还没有账号，先添加一个开始运营</view>
      <view v-for="a in accounts" :key="a.id" class="card">
        <view class="row">
          <text class="title" style="margin: 0">{{ a.nickname }}</text>
        </view>
        <view class="muted" style="margin: 8px 0">小红书账号：{{ a.xhs_id || '未填写' }}</view>
        <view style="text-align: right">
          <button class="btn btn-ghost btn-sm" @click="edit(a)">编辑</button>
          <button class="btn btn-ghost btn-sm" @click="remove(a)">删除</button>
        </view>
      </view>
      <button class="btn" @click="edit(null)">添加小红书账号</button>
    </template>
  </view>
</template>
