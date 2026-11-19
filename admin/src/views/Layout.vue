<script setup>
import { useRoute, useRouter } from 'vue-router'

import { session, setSession } from '../api'

const route = useRoute()
const router = useRouter()
const menus = [
  { path: '/dashboard', label: '数据概览' },
  { path: '/users', label: '用户管理' },
  { path: '/plans', label: '起号方案' },
  { path: '/interactions', label: '互动任务' },
  { path: '/plugins', label: '人格与 Skills' },
  { path: '/system', label: '系统运维', admin: true },
]

function logout() {
  setSession('', null)
  router.push('/login')
}
</script>

<template>
  <el-container style="min-height: 100vh">
    <el-aside width="200px" class="aside">
      <div class="brand">小红书运营平台</div>
      <el-menu :default-active="route.path" router>
        <template v-for="m in menus" :key="m.path">
          <el-menu-item v-if="!m.admin || session.user?.role === 'admin'" :index="m.path">{{ m.label }}</el-menu-item>
        </template>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="header">
        <span>{{ session.user?.username }}（{{ session.user?.role === 'admin' ? '管理员' : '运营' }}）</span>
        <el-button link type="primary" @click="logout">退出</el-button>
      </el-header>
      <el-main style="background: #f5f6f8">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.aside { border-right: 1px solid #ebeef5; }
.aside .el-menu { border-right: none; }
.brand { padding: 18px 20px; font-weight: 600; color: #ff2442; }
.header { display: flex; justify-content: flex-end; align-items: center; gap: 12px; border-bottom: 1px solid #ebeef5; }
</style>
