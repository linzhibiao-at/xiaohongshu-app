<script setup>
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'

import http from '../api'

const rows = ref([])
const status = ref('')
const LABEL = {
  pending_approval: '待用户确认', queued: '排队中', running: '执行中', done: '已完成',
  failed: '失败', rejected: '用户拒绝', cancelled: '已取消',
}
const TAG = { pending_approval: 'warning', queued: 'info', running: 'warning', done: 'success', failed: 'danger', rejected: 'info', cancelled: 'info' }
const ACTION = { like: '点赞', collect: '收藏', comment: '评论' }

async function load() {
  rows.value = await http.get('/api/admin/interactions', { params: status.value ? { status: status.value } : {} })
}

async function cancel(row) {
  await ElMessageBox.confirm(`确定取消任务 #${row.id}？`, '取消任务', { type: 'warning' })
  Object.assign(row, await http.post(`/api/admin/interactions/${row.id}/cancel`))
  ElMessage.success('已取消')
}

onMounted(load)
</script>

<template>
  <el-card shadow="never">
    <div style="margin-bottom: 12px; display: flex; gap: 8px">
      <el-select v-model="status" placeholder="全部状态" clearable style="width: 160px" @change="load">
        <el-option v-for="(label, value) in LABEL" :key="value" :label="label" :value="value" />
      </el-select>
      <el-button @click="load">刷新</el-button>
    </div>
    <el-table :data="rows" border>
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="account_id" label="账号 ID" width="90" />
      <el-table-column label="动作" width="80">
        <template #default="{ row }">{{ ACTION[row.action] }}</template>
      </el-table-column>
      <el-table-column prop="target_note" label="目标笔记" show-overflow-tooltip />
      <el-table-column prop="comment_text" label="评论内容" show-overflow-tooltip />
      <el-table-column label="状态" width="120">
        <template #default="{ row }"><el-tag :type="TAG[row.status]">{{ LABEL[row.status] }}</el-tag></template>
      </el-table-column>
      <el-table-column prop="error" label="失败原因" show-overflow-tooltip />
      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">{{ row.created_at?.replace('T', ' ').slice(0, 19) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90">
        <template #default="{ row }">
          <el-button v-if="['pending_approval', 'queued'].includes(row.status)" link type="danger" @click="cancel(row)">取消</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>
</template>
