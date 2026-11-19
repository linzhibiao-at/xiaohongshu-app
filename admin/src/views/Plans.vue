<script setup>
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { computed, onMounted, ref } from 'vue'

import http from '../api'

const rows = ref([])
const status = ref('')
const current = ref(null)
const html = computed(() => (current.value ? DOMPurify.sanitize(marked.parse(current.value.content || '')) : ''))
const TAG = { pending: 'info', running: 'warning', done: 'success', failed: 'danger' }
const LABEL = { pending: '等待中', running: '生成中', done: '已完成', failed: '失败' }

async function load() {
  rows.value = await http.get('/api/admin/plans', { params: status.value ? { status: status.value } : {} })
}
onMounted(load)
</script>

<template>
  <el-card shadow="never">
    <el-select v-model="status" placeholder="全部状态" clearable style="width: 160px; margin-bottom: 12px" @change="load">
      <el-option v-for="(label, value) in LABEL" :key="value" :label="label" :value="value" />
    </el-select>
    <el-table :data="rows" border>
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="niche" label="赛道" width="120" />
      <el-table-column prop="goal" label="目标" show-overflow-tooltip />
      <el-table-column prop="persona_key" label="人格" width="150" />
      <el-table-column label="状态" width="100">
        <template #default="{ row }"><el-tag :type="TAG[row.status]">{{ LABEL[row.status] }}</el-tag></template>
      </el-table-column>
      <el-table-column label="创建时间" width="180">
        <template #default="{ row }">{{ row.created_at?.replace('T', ' ').slice(0, 19) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90">
        <template #default="{ row }"><el-button link type="primary" @click="current = row">查看</el-button></template>
      </el-table-column>
    </el-table>
  </el-card>

  <el-drawer :model-value="!!current" size="50%" :title="current ? `${current.niche} · ${current.goal}` : ''" @close="current = null">
    <el-alert v-if="current?.error" type="error" :title="current.error" :closable="false" style="margin-bottom: 12px" />
    <div class="md" v-html="html" />
  </el-drawer>
</template>

<style scoped>
.md { line-height: 1.7; font-size: 14px; }
.md :deep(table) { border-collapse: collapse; }
.md :deep(th), .md :deep(td) { border: 1px solid #dcdfe6; padding: 6px 10px; }
</style>
