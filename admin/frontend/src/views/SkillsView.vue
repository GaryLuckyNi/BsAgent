<template>
  <section class="view-shell">
    <div class="toolbar">
      <el-input v-model="query" clearable placeholder="搜索 name / version / summary" />
      <el-button type="primary" :disabled="isBusy" @click="openCreate">新增 Skill</el-button>
    </div>

    <el-table class="skills-table" v-loading="loading" :data="items" border height="calc(100vh - 220px)">
      <el-table-column prop="name" label="Name" min-width="180" show-overflow-tooltip />
      <el-table-column prop="version" label="Version" width="120" show-overflow-tooltip />
      <el-table-column prop="summary" label="Summary" min-width="260" show-overflow-tooltip :tooltip-options="{ popperClass: 'skill-summary-tooltip' }" />
      <el-table-column prop="risk_level" label="Risk" width="100" />
      <el-table-column label="Enabled" width="110">
        <template #default="{ row }">
          <el-switch
            v-model="row.enabled"
            :loading="mutatingName === row.name"
            :disabled="isBusy"
            @change="toggleEnabled(row)"
          />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="160" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" :disabled="isBusy" @click="openEdit(row)">
            编辑
          </el-button>
          <el-button
            link
            type="danger"
            :loading="mutatingName === row.name"
            :disabled="isBusy"
            @click="remove(row)"
          >
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="dialogVisible"
      :title="editingName ? '编辑 Skill' : '新增 Skill'"
      width="900px"
      :style="{ maxWidth: 'calc(100vw - 32px)' }"
      :close-on-click-modal="!submitting"
      :close-on-press-escape="!submitting"
      :before-close="handleDialogClose"
    >
      <el-form
        label-width="150px"
        :model="form"
        :disabled="submitting"
        :class="{ 'is-submitting': submitting }"
      >
        <el-form-item label="Name">
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item label="Version">
          <el-input v-model="form.version" />
        </el-form-item>
        <el-form-item label="Summary">
          <el-input v-model="form.summary" type="textarea" :rows="10"
            placeholder="按顺序写明每一步使用的工具、参数要求、必需返回数据、缺失处理，以及最终输出格式。指定工具建议使用完整名称。" />
        </el-form-item>
        <el-form-item v-if="catalogError" label="业务意图目录">
          <span role="alert">{{ catalogError }}</span>
          <el-button link type="primary" @click="loadCatalog">重新加载</el-button>
        </el-form-item>
        <el-form-item label="Allowed Intent">
          <el-select v-model="form.supported_intents" multiple filterable clearable
            aria-label="Allowed Intent" placeholder="选择业务意图" :loading="catalogLoading"
            :disabled="catalogLoading || Boolean(catalogError)" @change="onIntentsChange">
            <el-option v-for="option in intentOptions" :key="option.value" v-bind="option" />
          </el-select>
        </el-form-item>
        <el-form-item label="Allowed Domain">
          <el-select v-model="form.supported_domains" multiple filterable clearable
            aria-label="Allowed Domain" placeholder="先选择意图，再选择关联领域"
            :disabled="catalogLoading || Boolean(catalogError) || !form.supported_intents.length"
            @change="onDomainsChange">
            <el-option v-for="option in domainOptions" :key="option.value" v-bind="option" />
          </el-select>
        </el-form-item>
        <el-form-item label="Allowed Subdomain">
          <el-select v-model="form.supported_sub_intents" multiple filterable clearable
            aria-label="Allowed Subdomain" placeholder="先选择领域，再选择关联子领域"
            :disabled="catalogLoading || Boolean(catalogError) || !form.supported_domains.length">
            <el-option v-for="option in subdomainOptions" :key="option.value" v-bind="option" />
          </el-select>
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
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { apiErrorMessage } from '../api/client'
import { listBusinessIntents } from '../api/businessIntents'
import { createSkill, deleteSkill, listSkills, updateSkill } from '../api/skills'
import type { BusinessIntentProfile, SkillProfile } from '../api/types'

const catalog = ref<BusinessIntentProfile[]>([])
const catalogLoading = ref(false)
const catalogError = ref('')
const loading = ref(false)
const query = ref('')
const items = ref<SkillProfile[]>([])
const dialogVisible = ref(false)
const editingName = ref<string | null>(null)
const submitting = ref(false)
const mutatingName = ref<string | null>(null)

type SkillProfileForm = Omit<SkillProfile, 'created_at' | 'updated_at'>

const form = reactive<SkillProfileForm>({
  name: '',
  version: '1.0.0',
  summary: '',
  supported_intents: [],
  supported_domains: [],
  supported_sub_intents: [],
  risk_level: 'medium',
  enabled: true
})
const isBusy = computed(() => submitting.value || Boolean(mutatingName.value))
const intentValues = computed(() => unique(catalog.value.map((item) => item.intent_type)))
const relatedIntents = computed(() => catalog.value.filter((item) => form.supported_intents.includes(item.intent_type)))
const domainValues = computed(() => unique(relatedIntents.value.map((item) => item.domain)))
const subdomainValues = computed(() => unique(relatedIntents.value
  .filter((item) => form.supported_domains.includes(item.domain))
  .map((item) => item.subdomain)))
const intentOptions = computed(() => selectionOptions(intentValues.value, form.supported_intents))
const domainOptions = computed(() => selectionOptions(domainValues.value, form.supported_domains))
const subdomainOptions = computed(() => selectionOptions(subdomainValues.value, form.supported_sub_intents))

function unique(values: string[]): string[] {
  return [...new Set(values.filter(Boolean))]
}

// 保留历史配置的可见性；仅在用户修改上级选择时清理失效的下级值。
function selectionOptions(values: string[], selected: string[]) {
  return unique([...values, ...selected]).map((value) => ({
    value,
    label: values.includes(value) ? value : `${value}（不在当前关联目录中）`,
    disabled: !values.includes(value)
  }))
}

function onIntentsChange() {
  form.supported_domains = form.supported_domains.filter((value) => domainValues.value.includes(value))
  onDomainsChange()
}

function onDomainsChange() {
  form.supported_sub_intents = form.supported_sub_intents.filter((value) => subdomainValues.value.includes(value))
}

async function loadCatalog() {
  catalogLoading.value = true
  catalogError.value = ''
  try {
    catalog.value = (await listBusinessIntents()).items
  } catch (error) {
    catalogError.value = `意图目录加载失败：${apiErrorMessage(error)}`
  } finally {
    catalogLoading.value = false
  }
}

let timer = 0
watch(query, () => {
  window.clearTimeout(timer)
  timer = window.setTimeout(load, 250)
})

onMounted(() => {
  void load()
  void loadCatalog()
})

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
    risk_level: 'medium',
    enabled: true
  })
}

function openCreate() {
  if (isBusy.value) return
  editingName.value = null
  resetForm()
  dialogVisible.value = true
}

function openEdit(row: SkillProfile) {
  if (isBusy.value) return
  editingName.value = row.name
  const skill = row
  Object.assign(form, {
    name: skill.name,
    version: skill.version,
    summary: skill.summary,
    supported_intents: [...skill.supported_intents],
    supported_domains: [...skill.supported_domains],
    supported_sub_intents: [...skill.supported_sub_intents],
    risk_level: skill.risk_level,
    enabled: skill.enabled
  })
  dialogVisible.value = true
}

async function submit() {
  if (submitting.value) return
  const targetName = form.name.trim()
  if (!targetName) {
    ElMessage.error('Skill 名称不能为空')
    return
  }
  const duplicate = items.value.some((item) => (
    item.name === targetName && item.name !== editingName.value
  ))
  if (duplicate) {
    ElMessage.error('Skill 名称已存在')
    return
  }
  form.name = targetName
  submitting.value = true
  const payload: SkillProfileForm = {
    name: targetName,
    version: form.version,
    summary: form.summary,
    supported_intents: [...form.supported_intents],
    supported_domains: [...form.supported_domains],
    supported_sub_intents: [...form.supported_sub_intents],
    risk_level: form.risk_level,
    enabled: form.enabled
  }
  try {
    if (editingName.value) {
      await updateSkill(editingName.value, payload)
    } else {
      await createSkill(payload)
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
  if (isBusy.value) return
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
  if (isBusy.value) return
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

function handleDialogClose(done: () => void) {
  if (!submitting.value) done()
}
</script>

<style>
.skill-summary-tooltip {
  width: min(420px, calc(100vw - 32px));
  max-width: min(420px, calc(100vw - 32px));
  display: -webkit-box;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.55;
}
</style>

<style scoped>
.skills-table :deep(.el-table__row) { height: 52px; }
.skills-table :deep(.el-table__body .cell) {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.is-submitting {
  pointer-events: none;
}
</style>
