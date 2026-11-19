<script setup>
import { computed, onMounted, ref } from 'vue'

import http from '../api'

const stats = ref(null)
const STATUS = {
  pending_approval: '待用户确认', queued: '排队中', running: '执行中', done: '已完成',
  failed: '失败', rejected: '用户拒绝', cancelled: '已取消',
}
const cards = computed(() => stats.value && [
  ['用户总数', stats.value.users],
  ['近 7 天新增用户', stats.value.new_users_7d],
  ['绑定的小红书账号', stats.value.xhs_accounts],
  ['已同步笔记', stats.value.notes],
  ['起号方案', stats.value.plans],
  ['生成失败的方案', stats.value.plans_failed],
  ['互动队列长度', stats.value.queue_length],
])

onMounted(async () => { stats.value = await http.get('/api/admin/stats') })
</script>

<template>
  <div v-if="stats">
    <el-row :gutter="16">
      <el-col v-for="[label, value] in cards" :key="label" :span="6" style="margin-bottom: 16px">
        <el-card shadow="never">
          <el-statistic :title="label" :value="value" />
        </el-card>
      </el-col>
    </el-row>
    <el-card shadow="never" header="互动任务状态分布">
      <el-empty v-if="!Object.keys(stats.interaction_tasks).length" description="暂无互动任务" :image-size="60" />
      <el-descriptions v-else :column="4" border>
        <el-descriptions-item v-for="(count, status) in stats.interaction_tasks" :key="status" :label="STATUS[status] || status">
          {{ count }}
        </el-descriptions-item>
      </el-descriptions>
    </el-card>
  </div>
</template>
