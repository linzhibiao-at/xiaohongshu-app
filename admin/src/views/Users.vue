<script setup>
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import http, { session } from '../api'

const rows = ref([])
const q = ref('')
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
const selected = ref([])
const dialog = ref(false)
const form = reactive({ username: '', password: '', role: 'user' })
const accountsDialog = ref(false)
const accountsUser = ref('')
const accounts = ref([])
const pwdDialog = ref(false)
const pwdForm = reactive({ id: 0, username: '', password: '' })
const isAdmin = session.user?.role === 'admin'

// Operators may only touch plain users; nobody may delete themselves.
function canReset(row) {
  return isAdmin || row.role === 'user'
}

function canDelete(row) {
  return row.id !== session.user.id && canReset(row)
}

async function load() {
  const data = await http.get('/api/admin/users', {
    params: { q: q.value, page: page.value, page_size: pageSize.value },
  })
  rows.value = data.items
  total.value = data.total
  selected.value = []
}

function search() {
  page.value = 1
  load()
}

function onSizeChange() {
  page.value = 1
  load()
}

async function create() {
  await http.post('/api/admin/users', form)
  dialog.value = false
  page.value = 1
  ElMessage.success('账号已创建')
  await load()
}

function openCreate() {
  Object.assign(form, { username: '', password: '', role: 'user' })
  dialog.value = true
}

async function patch(row, body) {
  try {
    Object.assign(row, await http.patch(`/api/admin/users/${row.id}`, body))
    ElMessage.success('已更新')
  } catch {
    await load() // revert the optimistic control state
  }
}

function openPassword(row) {
  Object.assign(pwdForm, { id: row.id, username: row.username, password: '' })
  pwdDialog.value = true
}

async function resetPassword() {
  if (pwdForm.password && pwdForm.password.length < 6) return ElMessage.warning('密码至少 6 位')
  const { password, generated } = await http.post(
    `/api/admin/users/${pwdForm.id}/reset-password`,
    pwdForm.password ? { password: pwdForm.password } : {},
  )
  pwdDialog.value = false
  await ElMessageBox.alert(
    `${generated ? '已自动生成 6 位数字密码' : '已设置为新密码'}：${password}（仅显示一次，请立即转告用户）`,
    '密码已重置',
    { confirmButtonText: '我已记录' },
  )
}

async function removeUsers(ids, label) {
  await ElMessageBox.confirm(
    `确定删除${label}？只删除登录账号，其小红书账号、笔记、起号方案、互动记录等数据会保留。`,
    '删除用户',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
  )
  const { deleted } = await http.post('/api/admin/users/delete', { ids })
  if (page.value > 1 && rows.value.length === deleted) page.value -= 1
  ElMessage.success(`已删除 ${deleted} 个用户`)
  await load()
}

async function openAccounts(row) {
  accountsUser.value = row.username
  accounts.value = await http.get(`/api/admin/users/${row.id}/accounts`)
  accountsDialog.value = true
}

onMounted(load)
</script>

<template>
  <el-card shadow="never">
    <div style="margin-bottom: 12px; display: flex; gap: 8px">
      <el-input v-model="q" placeholder="搜索用户名" clearable style="width: 240px" @keyup.enter="search" @clear="search" />
      <el-button type="primary" @click="search">查询</el-button>
      <el-button type="primary" @click="openCreate">新建用户</el-button>
      <el-button
        type="danger" :disabled="!selected.length"
        @click="removeUsers(selected.map((r) => r.id), `选中的 ${selected.length} 个用户`)"
      >
        批量删除
      </el-button>
    </div>
    <el-table :data="rows" border @selection-change="(v) => (selected = v)">
      <el-table-column type="selection" width="46" :selectable="canDelete" />
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="username" label="用户名" />
      <el-table-column label="角色" width="160">
        <template #default="{ row }">
          <el-select
            v-if="isAdmin && row.id !== session.user.id"
            :model-value="row.role" size="small" @change="(v) => patch(row, { role: v })"
          >
            <el-option label="普通用户" value="user" />
            <el-option label="运营" value="operator" />
            <el-option label="管理员" value="admin" />
          </el-select>
          <span v-else>{{ { user: '普通用户', operator: '运营', admin: '管理员' }[row.role] }}</span>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="90">
        <template #default="{ row }">
          <el-switch
            :model-value="row.is_active"
            :disabled="row.id === session.user.id || (!isAdmin && row.role !== 'user')"
            @change="(v) => patch(row, { is_active: v })"
          />
        </template>
      </el-table-column>
      <el-table-column label="注册时间" width="180">
        <template #default="{ row }">{{ row.created_at?.replace('T', ' ').slice(0, 19) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="300">
        <template #default="{ row }">
          <el-button link type="primary" :disabled="!canReset(row)" @click="openAccounts(row)">查看小红书账号</el-button>
          <el-button link type="primary" :disabled="!canReset(row)" @click="openPassword(row)">重置密码</el-button>
          <el-button link type="danger" :disabled="!canDelete(row)" @click="removeUsers([row.id], `用户「${row.username}」`)">
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <div style="margin-top: 12px; display: flex; justify-content: flex-end">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[10, 20, 50]"
        layout="total, sizes, prev, pager, next"
        @current-change="load"
        @size-change="onSizeChange"
      />
    </div>
  </el-card>

  <el-dialog v-model="dialog" title="新建用户" width="420px">
    <el-form label-width="70px">
      <el-form-item label="用户名">
        <el-input v-model="form.username" placeholder="至少 3 位" autocomplete="off" />
      </el-form-item>
      <el-form-item label="密码">
        <el-input v-model="form.password" type="password" show-password placeholder="至少 6 位" autocomplete="new-password" />
      </el-form-item>
      <el-form-item label="角色">
        <el-select v-model="form.role" style="width: 100%" :disabled="!isAdmin">
          <el-option label="普通用户" value="user" />
          <el-option label="运营" value="operator" />
          <el-option label="管理员" value="admin" />
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialog = false">取消</el-button>
      <el-button type="primary" @click="create">创建</el-button>
    </template>
  </el-dialog>

  <el-dialog v-model="pwdDialog" :title="`重置密码 · ${pwdForm.username}`" width="420px">
    <el-form label-width="90px">
      <el-form-item label="新密码">
        <el-input
          v-model="pwdForm.password" type="password" show-password
          placeholder="留空则自动生成 6 位数字密码" autocomplete="new-password"
          @keyup.enter="resetPassword"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="pwdDialog = false">取消</el-button>
      <el-button type="primary" @click="resetPassword">确认重置</el-button>
    </template>
  </el-dialog>

  <el-dialog v-model="accountsDialog" :title="`小红书账号 · ${accountsUser}`" width="760px">
    <el-table :data="accounts" border empty-text="该用户尚未绑定小红书账号">
      <el-table-column prop="xhs_id" label="小红书 ID" width="170" show-overflow-tooltip />
      <el-table-column prop="nickname" label="昵称" width="140" show-overflow-tooltip />
      <el-table-column prop="niche" label="赛道" width="110" />
      <el-table-column prop="followers" label="粉丝数" width="90" />
      <el-table-column prop="connector" label="连接器" width="100" />
      <el-table-column label="自动互动" width="90">
        <template #default="{ row }">{{ row.auto_interaction ? '是' : '否' }}</template>
      </el-table-column>
      <el-table-column label="绑定时间" width="170">
        <template #default="{ row }">{{ row.created_at?.replace('T', ' ').slice(0, 19) }}</template>
      </el-table-column>
    </el-table>
    <template #footer>
      <el-button type="primary" @click="accountsDialog = false">关闭</el-button>
    </template>
  </el-dialog>
</template>
