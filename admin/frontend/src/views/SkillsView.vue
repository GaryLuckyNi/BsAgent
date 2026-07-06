<template>
  <section class="view-shell">
    <div class="toolbar">
      <el-input v-model="query" clearable placeholder="搜索 name / version / summary" />
      <el-button type="primary" :disabled="submitting" @click="openCreate">新增 Skill</el-button>
    </div>

    <el-table v-loading="loading" :data="items" border height="calc(100vh - 220px)">
      <el-table-column prop="name" label="Name" min-width="180" />
      <el-table-column prop="version" label="Version" width="120" />
      <el-table-column prop="summary" label="Summary" min-width="260" />
      <el-table-column prop="risk_level" label="Risk" width="100" />
      <el-table-column label="Enabled" width="110">
        <template #default="{ row }">
          <el-switch
            v-model="row.enabled"
            :loading="mutatingName === row.name"
            :disabled="Boolean(mutatingName)"
            @change="toggleEnabled(row)"
          />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="160" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" :disabled="Boolean(mutatingName)" @click="openEdit(row)">
            编辑
          </el-button>
          <el-button
            link
            type="danger"
            :loading="mutatingName === row.name"
            :disabled="Boolean(mutatingName)"
            @click="remove(row)"
          >
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogVisible" :title="editingName ? '编辑 Skill' : '新增 Skill'" width="720px">
      <el-form label-width="150px" :model="form">
        <el-form-item label="Name">
          <el-input v-model="form.name" :disabled="Boolean(editingName)" />
        </el-form-item>
        <el-form-item label="Version">
          <el-input v-model="form.version" />
        </el-form-item>
        <el-form-item label="Summary">
          <el-input v-model="form.summary" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="Supported Intents">
          <StringListInput v-model="form.supported_intents" />
        </el-form-item>
        <el-form-item label="Supported Domains">
          <StringListInput v-model="form.supported_domains" />
        </el-form-item>
        <el-form-item label="Supported Sub Intents">
          <StringListInput v-model="form.supported_sub_intents" />
        </el-form-item>
        <el-form-item label="Allowed Tools">
          <StringListInput v-model="form.allowed_tools" />
        </el-form-item>
        <el-form-item label="Risk">
          <el-select v-model="form.risk_level">
            <el-option label="low" value="low" />
            <el-option label="medium" value="medium" />
            <el-option label="high" value="high" />
          </el-select>
        </el-form-item>
        <el-form-item label="Enabled">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="submitting" @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref, watch } from 'vue'
import { apiErrorMessage } from '../api/client'
import { createSkill, deleteSkill, listSkills, updateSkill } from '../api/skills'
import type { SkillProfile } from '../api/types'
import StringListInput from '../components/StringListInput.vue'

const loading = ref(false)
const query = ref('')
const items = ref<SkillProfile[]>([])
const dialogVisible = ref(false)
const editingName = ref<string | null>(null)
const submitting = ref(false)
const mutatingName = ref<string | null>(null)

const form = reactive<SkillProfile>({
  name: '',
  version: '1.0.0',
  summary: '',
  supported_intents: [],
  supported_domains: [],
  supported_sub_intents: [],
  allowed_tools: [],
  risk_level: 'medium',
  enabled: true
})

let timer = 0
watch(query, () => {
  window.clearTimeout(timer)
  timer = window.setTimeout(load, 250)
})

onMounted(load)

async function load() {
  loading.value = true
  try {
    items.value = (await listSkills(query.value)).items
  } catch (error) {
    ElMessage.error(apiErrorMessage(error))
  } finally {
    loading.value = false
  }
}

function resetForm() {
  Object.assign(form, {
    name: '',
    version: '1.0.0',
    summary: '',
    supported_intents: [],
    supported_domains: [],
    supported_sub_intents: [],
    allowed_tools: [],
    risk_level: 'medium',
    enabled: true
  })
}

function openCreate() {
  editingName.value = null
  resetForm()
  dialogVisible.value = true
}

function openEdit(row: SkillProfile) {
  editingName.value = row.name
  Object.assign(form, JSON.parse(JSON.stringify(row)))
  dialogVisible.value = true
}

async function submit() {
  submitting.value = true
  try {
    if (editingName.value) {
      await updateSkill(editingName.value, form)
    } else {
      await createSkill(form)
    }
    dialogVisible.value = false
    ElMessage.success('已保存')
    await load()
  } catch (error) {
    ElMessage.error(apiErrorMessage(error))
  } finally {
    submitting.value = false
  }
}

async function toggleEnabled(row: SkillProfile) {
  const nextEnabled = row.enabled
  mutatingName.value = row.name
  try {
    await updateSkill(row.name, { enabled: nextEnabled })
    ElMessage.success(nextEnabled ? '已启用' : '已停用')
  } catch (error) {
    row.enabled = !nextEnabled
    ElMessage.error(apiErrorMessage(error))
  } finally {
    mutatingName.value = null
  }
}

async function remove(row: SkillProfile) {
  try {
    await ElMessageBox.confirm(`确认删除 ${row.name}？`, '删除 Skill', { type: 'warning' })
  } catch {
    return
  }
  mutatingName.value = row.name
  try {
    await deleteSkill(row.name)
    ElMessage.success('已删除')
    await load()
  } catch (error) {
    ElMessage.error(apiErrorMessage(error))
  } finally {
    mutatingName.value = null
  }
}
</script>
