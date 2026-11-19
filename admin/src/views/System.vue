<script setup>
import { onMounted, ref } from 'vue'

import http from '../api'

const sys = ref(null)
const logs = ref([])

async function load() {
  ;[sys.value, logs.value] = await Promise.all([http.get('/api/admin/system'), http.get('/api/admin/audit-logs')])
}
onMounted(load)
</script>

<template>
  <div v-if="sys">
    <el-card shadow="never" style="margin-bottom: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span>系统状态</span>
          <el-button size="small" @click="load">刷新</el-button>
        </div>
      </template>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="MySQL">
          <el-tag :type="sys.health.mysql === 'ok' ? 'success' : 'danger'">{{ sys.health.mysql }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="Redis">
          <el-tag :type="sys.health.redis === 'ok' ? 'success' : 'danger'">{{ sys.health.redis }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="互动 Worker 心跳">
          <el-tag v-if="sys.worker_last_heartbeat" type="success">{{ sys.worker_last_heartbeat }}</el-tag>
          <el-tag v-else type="danger">未运行（互动任务不会被执行）</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="互动队列长度">{{ sys.queue_length }}</el-descriptions-item>
        <el-descriptions-item label="大模型">
          {{ sys.llm.provider }} / {{ sys.llm.model }}
          <el-tag :type="sys.llm.configured ? 'success' : 'warning'" style="margin-left: 8px">
            {{ sys.llm.configured ? '已配置' : '未配置 API Key，AI 功能走模板兜底' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="E2B 沙箱">
          <el-tag :type="sys.sandbox_configured ? 'success' : 'warning'">{{ sys.sandbox_configured ? '已配置' : '未配置' }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="小红书连接器">
          {{ sys.xhs_connector }}
          <el-tag v-if="sys.xhs_connector === 'mock'" type="warning" style="margin-left: 8px">模拟数据，未连接小红书</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="已注册连接器">{{ sys.available_connectors.join('、') }}</el-descriptions-item>
        <el-descriptions-item label="单账号每日互动上限">{{ sys.limits.interaction_daily_limit }}</el-descriptions-item>
        <el-descriptions-item label="互动最小间隔">{{ sys.limits.interaction_min_interval_seconds }} 秒</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card shadow="never" header="操作审计日志">
      <el-table :data="logs" border>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="actor_id" label="操作人 ID" width="100" />
        <el-table-column prop="action" label="动作" width="180" />
        <el-table-column prop="detail" label="详情" show-overflow-tooltip />
        <el-table-column label="时间" width="180">
          <template #default="{ row }">{{ row.created_at?.replace('T', ' ').slice(0, 19) }}</template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>
