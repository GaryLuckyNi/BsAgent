<template>
  <section class="view-shell">
    <div class="toolbar">
      <el-input v-model="query" clearable placeholder="搜索 ID / domain / sub_intent" />
      <el-button type="primary" :disabled="submitting" @click="openCreate">新增业务意图</el-button>
    </div>

    <el-table v-loading="loading" :data="items" border height="calc(100vh - 220px)">
      <el-table-column prop="id" label="ID" min-width="220" />
      <el-table-column prop="intent" label="Intent" width="150" />
      <el-table-column prop="domain" label="Domain" width="170" />
      <el-table-column prop="sub_intent" label="Sub Intent" width="190" />
      <el-table-column prop="candidate_skill" label="Skill" width="150" />
      <el-table-column prop="risk_level" label="Risk" width="100" />
      <el-table-column label="Enabled" width="110">
        <template #default="{ row }">
          <el-switch
            v-model="row.enabled"
            :loading="mutatingId === row.id"
            :disabled="Boolean(mutatingId)"
            @change="toggleEnabled(row)"
          />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="160" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" :disabled="Boolean(mutatingId)" @click="openEdit(row)">
            编辑
          </el-button>
          <el-button
            link
            type="danger"
            :loading="mutatingId === row.id"
            :disabled="Boolean(mutatingId)"
            @click="remove(row)"
          >
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogVisible" :title="editingId ? '编辑业务意图' : '新增业务意图'" width="720px">
      <el-form label-width="150px" :model="form">
        <el-form-item label="ID">
          <el-input v-model="form.id" :disabled="Boolean(editingId)" />
        </el-form-item>
        <el-form-item label="Intent">
          <el-select v-model="form.intent">
            <el-option v-for="item in intentOptions" :key="item" :label="item" :value="item" />
          </el-select>
        </el-form-item>
        <el-form-item label="Domain">
          <el-input v-model="form.domain" />
        </el-form-item>
        <el-form-item label="Sub Intent">
          <el-input v-model="form.sub_intent" />
        </el-form-item>
        <el-form-item label="Candidate Skill">
          <el-input v-model="form.candidate_skill" />
        </el-form-item>
        <el-form-item label="Risk">
          <el-select v-model="form.risk_level">
            <el-option label="low" value="low" />
            <el-option label="medium" value="medium" />
            <el-option label="high" value="high" />
          </el-select>
        </el-form-item>
        <el-form-item label="Trigger Keywords">
          <StringListInput v-model="form.trigger_keywords" />
        </el-form-item>
        <el-form-item label="Trigger Examples">
          <StringListInput v-model="form.trigger_examples" />
        </el-form-item>
        <el-form-item label="Capabilities">
          <StringListInput v-model="form.required_capabilities" />
        </el-form-item>
        <el-form-item label="Enabled">
          <el-switch v-model="form.enabled" />
        </el-form-item>
        <el-form-item label="Description">
          <el-input v-model="form.description" type="textarea" :rows="3" />
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
import {
  createBusinessIntent,
  deleteBusinessIntent,
  listBusinessIntents,
  updateBusinessIntent
} from '../api/businessIntents'
import { apiErrorMessage } from '../api/client'
import type { BusinessIntentProfile, IntentKind } from '../api/types'
import StringListInput from '../components/StringListInput.vue'

const intentOptions: IntentKind[] = [
  'knowledge_qa',
  'memory_query',
  'tool_action',
  'task_workflow',
  'conversation',
  'clarification_needed',
  'unsupported'
]

const loading = ref(false)
const query = ref('')
const items = ref<BusinessIntentProfile[]>([])
const dialogVisible = ref(false)
const editingId = ref<string | null>(null)
const submitting = ref(false)
const mutatingId = ref<string | null>(null)

const form = reactive<BusinessIntentProfile>({
  id: '',
  intent: 'knowledge_qa',
  domain: '',
  sub_intent: '',
  candidate_skill: '',
  risk_level: 'medium',
  trigger_keywords: [],
  trigger_examples: [],
  required_capabilities: [],
  enabled: true,
  description: ''
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
    items.value = (await listBusinessIntents(query.value)).items
  } catch (error) {
    ElMessage.error(apiErrorMessage(error))
  } finally {
    loading.value = false
  }
}

function resetForm() {
  Object.assign(form, {
    id: '',
    intent: 'knowledge_qa',
    domain: '',
    sub_intent: '',
    candidate_skill: '',
    risk_level: 'medium',
    trigger_keywords: [],
    trigger_examples: [],
    required_capabilities: [],
    enabled: true,
    description: ''
  })
}

function openCreate() {
  editingId.value = null
  resetForm()
  dialogVisible.value = true
}

function openEdit(row: BusinessIntentProfile) {
  editingId.value = row.id
  Object.assign(form, JSON.parse(JSON.stringify(row)))
  dialogVisible.value = true
}

async function submit() {
  submitting.value = true
  try {
    if (editingId.value) {
      await updateBusinessIntent(editingId.value, form)
    } else {
      await createBusinessIntent(form)
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

async function toggleEnabled(row: BusinessIntentProfile) {
  const nextEnabled = row.enabled
  mutatingId.value = row.id
  try {
    await updateBusinessIntent(row.id, { enabled: nextEnabled })
    ElMessage.success(nextEnabled ? '已启用' : '已停用')
  } catch (error) {
    row.enabled = !nextEnabled
    ElMessage.error(apiErrorMessage(error))
  } finally {
    mutatingId.value = null
  }
}

async function remove(row: BusinessIntentProfile) {
  try {
    await ElMessageBox.confirm(`确认删除 ${row.id}？`, '删除业务意图', { type: 'warning' })
  } catch {
    return
  }
  mutatingId.value = row.id
  try {
    await deleteBusinessIntent(row.id)
    ElMessage.success('已删除')
    await load()
  } catch (error) {
    ElMessage.error(apiErrorMessage(error))
  } finally {
    mutatingId.value = null
  }
}
</script>
