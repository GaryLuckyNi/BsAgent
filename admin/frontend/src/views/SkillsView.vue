<template>
  <section class="view-shell">
    <div class="toolbar">
      <el-input v-model="query" clearable placeholder="搜索 name / version / summary" />
      <el-button type="primary" :disabled="isBusy" @click="openCreate">新增 Skill</el-button>
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
        <el-form-item label="证据要求">
          <div class="evidence-requirements">
            <div class="evidence-requirements__table">
              <el-table :data="evidenceRequirements" border size="small" style="min-width: 760px">
                <el-table-column label="Capability" min-width="220">
                  <template #default="{ row }">
                    <el-input v-model="row.capability" placeholder="例如 stock.market_snapshot" />
                  </template>
                </el-table-column>
                <el-table-column label="必需字段" min-width="220">
                  <template #default="{ row }">
                    <el-input v-model="row.requiredFields" placeholder="逗号分隔，例如 price, pe_ttm" />
                  </template>
                </el-table-column>
                <el-table-column label="可选字段" min-width="220">
                  <template #default="{ row }">
                    <el-input v-model="row.optionalFields" placeholder="逗号分隔" />
                  </template>
                </el-table-column>
                <el-table-column label="操作" width="72" align="center">
                  <template #default="{ $index }">
                    <el-tooltip content="删除该证据要求" placement="top">
                      <el-button
                        circle
                        plain
                        type="danger"
                        :icon="Delete"
                        aria-label="删除该证据要求"
                        :disabled="submitting"
                        @click="removeEvidenceRequirement($index)"
                      />
                    </el-tooltip>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            <el-button
              plain
              type="primary"
              :icon="Plus"
              :disabled="submitting"
              @click="addEvidenceRequirement"
            >
              添加证据要求
            </el-button>
          </div>
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
import { Delete, Plus } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { apiErrorMessage } from '../api/client'
import { createSkill, deleteSkill, listSkills, updateSkill } from '../api/skills'
import type { SkillEvidenceContract, SkillProfile, ToolEvidenceRequirement } from '../api/types'
import StringListInput from '../components/StringListInput.vue'

interface EvidenceRequirementDraft {
  capability: string
  requiredFields: string
  optionalFields: string
}

const loading = ref(false)
const query = ref('')
const items = ref<SkillProfile[]>([])
const dialogVisible = ref(false)
const editingName = ref<string | null>(null)
const submitting = ref(false)
const mutatingName = ref<string | null>(null)
const evidenceRequirements = ref<EvidenceRequirementDraft[]>([])

type SkillProfileForm = Omit<SkillProfile, 'created_at' | 'updated_at'>

const form = reactive<SkillProfileForm>({
  name: '',
  version: '1.0.0',
  summary: '',
  supported_intents: [],
  supported_domains: [],
  supported_sub_intents: [],
  allowed_tools: [],
  evidence_contract: { requirements: [] },
  risk_level: 'medium',
  enabled: true
})
const isBusy = computed(() => submitting.value || Boolean(mutatingName.value))

let timer = 0
watch(query, () => {
  window.clearTimeout(timer)
  timer = window.setTimeout(load, 250)
})

onMounted(load)

async function load() {
  loading.value = true
  try {
    items.value = (await listSkills(query.value)).items.map(normalizeSkill)
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
    evidence_contract: { requirements: [] },
    risk_level: 'medium',
    enabled: true
  })
  evidenceRequirements.value = []
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
  const skill = normalizeSkill(row)
  Object.assign(form, {
    name: skill.name,
    version: skill.version,
    summary: skill.summary,
    supported_intents: [...skill.supported_intents],
    supported_domains: [...skill.supported_domains],
    supported_sub_intents: [...skill.supported_sub_intents],
    allowed_tools: [...skill.allowed_tools],
    evidence_contract: { requirements: [...skill.evidence_contract.requirements] },
    risk_level: skill.risk_level,
    enabled: skill.enabled
  })
  evidenceRequirements.value = skill.evidence_contract.requirements.map((requirement) => ({
    capability: requirement.capability,
    requiredFields: requirement.required_fields.join(', '),
    optionalFields: requirement.optional_fields.join(', ')
  }))
  dialogVisible.value = true
}

async function submit() {
  if (submitting.value) return
  const evidenceContract = buildEvidenceContract()
  if (!evidenceContract) return
  submitting.value = true
  const payload: SkillProfileForm = {
    name: form.name,
    version: form.version,
    summary: form.summary,
    supported_intents: [...form.supported_intents],
    supported_domains: [...form.supported_domains],
    supported_sub_intents: [...form.supported_sub_intents],
    allowed_tools: [...form.allowed_tools],
    evidence_contract: evidenceContract,
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

function addEvidenceRequirement() {
  evidenceRequirements.value.push({
    capability: '',
    requiredFields: '',
    optionalFields: ''
  })
}

function removeEvidenceRequirement(index: number) {
  evidenceRequirements.value.splice(index, 1)
}

function buildEvidenceContract(): SkillEvidenceContract | null {
  const requirements: ToolEvidenceRequirement[] = []
  const capabilities = new Set<string>()
  for (const draft of evidenceRequirements.value) {
    const capability = draft.capability.trim()
    const requiredFields = normalizeFields(draft.requiredFields)
    const optionalFields = normalizeFields(draft.optionalFields)
    if (!capability) {
      ElMessage.error('请填写证据 Capability')
      return null
    }
    if (!/^stock\.[a-z0-9_]+$/.test(capability)) {
      ElMessage.error('证据 Capability 必须符合 stock.xxx 格式')
      return null
    }
    if (capabilities.has(capability)) {
      ElMessage.error('同一证据契约中 Capability 不能重复')
      return null
    }
    if (requiredFields.some((field) => optionalFields.includes(field))) {
      ElMessage.error('必需字段与可选字段不能重复')
      return null
    }
    capabilities.add(capability)
    requirements.push({
      capability,
      required_fields: requiredFields,
      optional_fields: optionalFields
    })
  }
  return { requirements }
}

function normalizeFields(value: string): string[] {
  const fields: string[] = []
  const seen = new Set<string>()
  for (const item of value.split(',')) {
    const field = item.trim()
    if (field && !seen.has(field)) {
      fields.push(field)
      seen.add(field)
    }
  }
  return fields
}

function normalizeSkill(skill: SkillProfile): SkillProfile {
  return {
    ...skill,
    evidence_contract: normalizeEvidenceContract(skill.evidence_contract)
  }
}

function normalizeEvidenceContract(value: unknown): SkillEvidenceContract {
  if (!isRecord(value) || !Array.isArray(value.requirements)) {
    return { requirements: [] }
  }
  return {
    requirements: value.requirements.flatMap((value) => {
      if (!isRecord(value) || typeof value.capability !== 'string') return []
      return [
        {
          capability: value.capability,
          required_fields: normalizeStringArray(value.required_fields),
          optional_fields: normalizeStringArray(value.optional_fields)
        }
      ]
    })
  }
}

function normalizeStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function handleDialogClose(done: () => void) {
  if (!submitting.value) done()
}
</script>

<style scoped>
.evidence-requirements {
  display: grid;
  width: 100%;
  gap: 12px;
}

.evidence-requirements__table {
  overflow-x: auto;
}

.is-submitting {
  pointer-events: none;
}
</style>
