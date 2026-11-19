<script setup>
import { onShow } from '@dcloudio/uni-app'
import { reactive, ref } from 'vue'

import { api, requireLogin } from '../../api'

const personas = ref([])
const skills = ref([])
const editing = ref(null) // null | 'new' | persona key
const form = reactive({ name: '', description: '', system_prompt: '', skills: [] })

async function load() {
  ;[personas.value, skills.value] = await Promise.all([api.get('/api/personas'), api.get('/api/skills')])
}

onShow(() => {
  if (requireLogin()) load()
})

function edit(p) {
  editing.value = p ? p.key : 'new'
  Object.assign(form, p
    ? { name: p.name, description: p.description, system_prompt: p.system_prompt, skills: [...p.skills] }
    : { name: '', description: '', system_prompt: '', skills: [] })
}

function toggleSkill(name) {
  const i = form.skills.indexOf(name)
  if (i >= 0) form.skills.splice(i, 1)
  else form.skills.push(name)
}

async function save() {
  if (!form.name.trim() || form.system_prompt.trim().length < 10) {
    return uni.showToast({ title: '请填写名称和至少 10 个字的人格设定', icon: 'none' })
  }
  if (editing.value === 'new') await api.post('/api/personas', form)
  else await api.put(`/api/personas/${editing.value}`, form)
  editing.value = null
  await load()
}

function remove(p) {
  uni.showModal({
    title: '删除人格',
    content: `确定删除「${p.name}」？`,
    success: async (res) => {
      if (!res.confirm) return
      await api.del(`/api/personas/${p.key}`)
      await load()
    },
  })
}
</script>

<template>
  <view class="page">
    <view v-if="editing" class="card">
      <view class="title">{{ editing === 'new' ? '新建人格' : '编辑人格' }}</view>
      <input v-model="form.name" class="input" placeholder="名称，如 毒舌选题官" />
      <input v-model="form.description" class="input" placeholder="一句话说明" />
      <textarea v-model="form.system_prompt" class="textarea" style="min-height: 140px" maxlength="8000"
        placeholder="人格设定：身份、语气、工作原则。例如：你是一位说话直接的选题顾问，每次只给 3 个选题并说明理由……" />
      <view class="muted" style="margin-bottom: 6px">可使用的技能（点击选择）</view>
      <view style="margin-bottom: 12px">
        <view v-for="s in skills" :key="s.name" class="skill" :class="{ on: form.skills.includes(s.name) }" @click="toggleSkill(s.name)">
          <view>{{ s.name }}</view>
          <view class="muted">{{ s.description }}</view>
        </view>
      </view>
      <button class="btn" @click="save">保存</button>
      <button class="btn btn-ghost" style="margin-top: 10px" @click="editing = null">取消</button>
    </view>

    <template v-else>
      <view v-for="p in personas" :key="p.key" class="card">
        <view class="row">
          <text class="title" style="margin: 0">{{ p.name }}</text>
          <text class="tag">{{ p.builtin ? '内置' : p.mine ? '我的' : '公共' }}</text>
        </view>
        <view class="muted" style="margin: 6px 0">{{ p.description }}</view>
        <view><text v-for="s in p.skills" :key="s" class="tag">{{ s }}</text></view>
        <view v-if="p.mine" style="text-align: right; margin-top: 8px">
          <button class="btn btn-ghost btn-sm" @click="edit(p)">编辑</button>
          <button class="btn btn-ghost btn-sm" @click="remove(p)">删除</button>
        </view>
      </view>
      <button class="btn" @click="edit(null)">新建自定义人格</button>
    </template>
  </view>
</template>

<style scoped>
.skill { border: 1px solid #e3e5e8; border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; }
.skill.on { border-color: #ff2442; background: #fff5f6; }
</style>
