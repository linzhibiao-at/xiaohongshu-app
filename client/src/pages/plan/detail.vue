<script setup>
import { onLoad, onUnload } from '@dcloudio/uni-app'
import { computed, ref } from 'vue'

import { api, mdToHtml } from '../../api'

const plan = ref(null)
const html = computed(() => mdToHtml(plan.value?.content))
let timer = null

async function load(id) {
  plan.value = await api.get(`/api/plans/${id}`)
  if (['pending', 'running'].includes(plan.value.status)) timer = setTimeout(() => load(id), 3000)
}

onLoad((query) => load(query.id))
onUnload(() => clearTimeout(timer))

function copy() {
  uni.setClipboardData({ data: plan.value.content })
}
</script>

<template>
  <view v-if="plan" class="page">
    <view class="card">
      <view class="title">{{ plan.niche }} · {{ plan.goal }}</view>
      <view v-if="['pending', 'running'].includes(plan.status)" class="empty">方案生成中，通常需要 1-2 分钟…</view>
      <view v-else-if="plan.status === 'failed'" class="tag tag-bad">生成失败：{{ plan.error }}</view>
      <template v-else>
        <rich-text :nodes="html" style="line-height: 1.7" />
        <button class="btn btn-ghost" style="margin-top: 14px" @click="copy">复制方案</button>
      </template>
    </view>
  </view>
</template>
