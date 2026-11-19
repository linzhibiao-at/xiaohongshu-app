<script setup>
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import http from '../api'

const data = ref({ personas: [], skills: [], custom_personas: [] })
const detail = ref(null)
const dialog = ref(false)
const folderInput = ref(null)
const uploading = ref(false)
const form = reactive({ key: '', name: '', description: '', system_prompt: '', skills: [] })
const MIN_PROMPT = 10 // 与后端 PersonaIn.system_prompt 的 min_length 保持一致

async function load() {
  data.value = await http.get('/api/admin/plugins')
}

async function toggle(kind, name, enabled) {
  await http.post('/api/admin/plugins/toggle', { kind, name, enabled })
  ElMessage.success(enabled ? '已启用' : '已停用')
  await load()
}

function openCreate() {
  Object.assign(form, { key: '', name: '', description: '', system_prompt: '', skills: [] })
  dialog.value = true
}

async function create() {
  const key = form.key.trim()
  const name = form.name.trim()
  const prompt = form.system_prompt.trim()
  if (!/^[a-z0-9_]{2,64}$/.test(key)) return ElMessage.warning('Key 只能用小写字母、数字、下划线，至少 2 位')
  if (!name) return ElMessage.warning('请填写名称')
  if (prompt.length < MIN_PROMPT) return ElMessage.warning(`系统提示词至少 ${MIN_PROMPT} 个字`)
  await http.post('/api/admin/personas', { ...form, key, name, system_prompt: prompt })
  dialog.value = false
  ElMessage.success('已发布')
  await load()
}

async function remove(row) {
  await ElMessageBox.confirm(`确定删除人格「${row.name}」？`, '删除', { type: 'warning' })
  await http.delete(`/api/admin/personas/${row.key}`)
  await load()
}

const IGNORED_DIRS = ['.git', '.venv', 'node_modules', '__pycache__']
const MAX_FILE_BYTES = 5 * 1024 * 1024

function isSkipped(file) {
  const parts = (file.webkitRelativePath || file.name).split('/')
  const dirs = parts.slice(0, -1)
  return (
    dirs.some((d) => IGNORED_DIRS.includes(d)) ||
    parts[parts.length - 1] === '.DS_Store' ||
    file.size > MAX_FILE_BYTES
  )
}

async function uploadFolder(files, overwrite = false) {
  const body = new FormData()
  for (const file of files) {
    const rel = file.webkitRelativePath || file.name
    body.append('files', file, rel)
    body.append('paths', rel)
  }
  body.append('overwrite', overwrite ? 'true' : 'false')
  uploading.value = true
  try {
    await http.post('/api/admin/skills', body)
    ElMessage.success('上传成功')
    await load()
  } catch (err) {
    if (err.response?.status === 409 && !overwrite) {
      await ElMessageBox.confirm('已存在同名技能，是否覆盖？', '覆盖确认', { type: 'warning' })
      return await uploadFolder(files, true)
    }
    throw err
  } finally {
    uploading.value = false
  }
}

async function onFolderPicked(event) {
  const all = Array.from(event.target.files || [])
  event.target.value = ''
  const kept = all.filter((f) => !isSkipped(f))
  if (!kept.length) return ElMessage.warning('没有可上传的文件（目录中需要包含 SKILL.md）')
  if (kept.length < all.length) ElMessage.warning(`已跳过 ${all.length - kept.length} 个被忽略或超过 5MB 的文件`)
  try {
    await uploadFolder(kept)
  } catch {
    // 拦截器已经提示了失败原因
  }
}

async function removeSkill(row) {
  await ElMessageBox.confirm(`确定删除技能「${row.name}」？`, '删除', { type: 'warning' })
  await http.delete(`/api/admin/skills/${row.name}`)
  ElMessage.success('已删除')
  await load()
}

onMounted(load)
</script>

<template>
  <el-card shadow="never" header="内置人格插件（plugins/personas/*.yaml）" style="margin-bottom: 16px">
    <el-table :data="data.personas" border>
      <el-table-column prop="key" label="Key" width="180" />
      <el-table-column prop="name" label="名称" width="140" />
      <el-table-column prop="description" label="说明" show-overflow-tooltip />
      <el-table-column label="Skills">
        <template #default="{ row }"><el-tag v-for="s in row.skills" :key="s" style="margin-right: 4px">{{ s }}</el-tag></template>
      </el-table-column>
      <el-table-column label="启用" width="80">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled" @change="(v) => toggle('persona', row.key, v)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="90">
        <template #default="{ row }">
          <el-button link type="primary" @click="detail = { title: row.name, text: row.system_prompt }">提示词</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>

  <el-card shadow="never" style="margin-bottom: 16px">
    <template #header>
      <div style="display: flex; justify-content: space-between; align-items: center">
        <span>Skills 插件（内置 + 上传）</span>
        <el-button type="primary" size="small" :loading="uploading" @click="folderInput.click()">上传 Skill 文件夹</el-button>
      </div>
    </template>
    <input ref="folderInput" type="file" webkitdirectory multiple style="display: none" @change="onFolderPicked" />
    <el-table :data="data.skills" border>
      <el-table-column prop="name" label="名称" width="180" />
      <el-table-column prop="description" label="说明" show-overflow-tooltip />
      <el-table-column label="来源" width="90">
        <template #default="{ row }">
          <el-tag :type="row.source === 'uploaded' ? 'success' : 'info'" size="small">
            {{ row.source === 'uploaded' ? '上传' : '内置' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="带工具" width="90">
        <template #default="{ row }">{{ row.has_tools ? '是' : '否' }}</template>
      </el-table-column>
      <el-table-column label="启用" width="80">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled" @change="(v) => toggle('skill', row.name, v)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="140">
        <template #default="{ row }">
          <el-button link type="primary" @click="detail = { title: row.name, text: row.instructions }">说明书</el-button>
          <el-button v-if="row.source === 'uploaded'" link type="danger" @click="removeSkill(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <div class="hint">上传目录中的 tools.py 会在服务器端执行，请仅上传可信内容。</div>
  </el-card>

  <el-card shadow="never">
    <template #header>
      <div style="display: flex; justify-content: space-between; align-items: center">
        <span>自定义人格（运营发布的对所有用户可见）</span>
        <el-button type="primary" size="small" @click="openCreate">发布公共人格</el-button>
      </div>
    </template>
    <el-table :data="data.custom_personas" border>
      <el-table-column prop="key" label="Key" show-overflow-tooltip />
      <el-table-column prop="name" label="名称" width="140" />
      <el-table-column label="来源" width="120">
        <template #default="{ row }">{{ row.owner_id ? `用户 #${row.owner_id}` : '运营发布' }}</template>
      </el-table-column>
      <el-table-column label="Skills">
        <template #default="{ row }"><el-tag v-for="s in row.skills" :key="s" style="margin-right: 4px">{{ s }}</el-tag></template>
      </el-table-column>
      <el-table-column label="操作" width="140">
        <template #default="{ row }">
          <el-button link type="primary" @click="detail = { title: row.name, text: row.system_prompt }">提示词</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>

  <el-dialog :model-value="!!detail" :title="detail?.title" width="640px" @close="detail = null">
    <pre class="pre">{{ detail?.text }}</pre>
  </el-dialog>

  <el-dialog v-model="dialog" title="发布公共人格" width="640px">
    <el-form label-width="90px">
      <el-form-item label="Key"><el-input v-model="form.key" placeholder="小写字母、数字、下划线" /></el-form-item>
      <el-form-item label="名称"><el-input v-model="form.name" /></el-form-item>
      <el-form-item label="说明"><el-input v-model="form.description" /></el-form-item>
      <el-form-item label="Skills">
        <el-select v-model="form.skills" multiple style="width: 100%">
          <el-option v-for="s in data.skills" :key="s.name" :label="s.name" :value="s.name" />
        </el-select>
      </el-form-item>
      <el-form-item label="系统提示词">
        <el-input v-model="form.system_prompt" type="textarea" :rows="8" placeholder="描述这个人格的身份、语气和工作原则（至少 10 个字）" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialog = false">取消</el-button>
      <el-button type="primary" @click="create">发布</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.pre { white-space: pre-wrap; word-break: break-word; font-family: inherit; line-height: 1.7; margin: 0; }
.hint { color: #909399; font-size: 12px; margin-top: 8px; }
</style>
