<template>
  <section class="view-shell">
    <div class="toolbar">
      <el-input v-model="query" clearable placeholder="搜索 ID / provider / display name" />
      <el-button type="primary" :disabled="isBusy" @click="openCreate">新增数据源</el-button>
    </div>

    <el-table v-loading="loading" :data="items" border height="calc(100vh - 220px)">
      <el-table-column prop="id" label="ID" min-width="170" />
      <el-table-column prop="display_name" label="Name" min-width="180" />
      <el-table-column prop="provider" label="Provider" width="120" />
      <el-table-column label="Active" width="100">
        <template #default="{ row }">
          <el-tag :type="row.active ? 'success' : 'info'">
            {{ row.active ? 'active' : 'standby' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="Enabled" width="110">
        <template #default="{ row }">
          <el-switch
            v-model="row.enabled"
            :loading="mutatingId === row.id"
            :disabled="isBusy"
            @change="toggleEnabled(row)"
          />
        </template>
      </el-table-column>
      <el-table-column label="Token Env" min-width="150">
        <template #default="{ row }">
          {{ tokenEnvVar(row) }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="230" fixed="right">
        <template #default="{ row }">
          <el-button
            link
            type="success"
            :disabled="isBusy || row.active || !row.enabled"
            :loading="mutatingId === row.id"
            @click="activate(row)"
          >
            激活
          </el-button>
          <el-button link type="primary" :disabled="isBusy" @click="openEdit(row)">
            编辑
          </el-button>
          <el-button
            link
            type="danger"
            :loading="mutatingId === row.id"
            :disabled="isBusy || row.active"
            @click="remove(row)"
          >
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑数据源' : '新增数据源'"
      width="760px"
      :style="{ maxWidth: 'calc(100vw - 32px)' }"
      :close-on-click-modal="!submitting"
      :close-on-press-escape="!submitting"
      :before-close="handleDialogClose"
    >
      <el-form
        label-width="160px"
        :model="form"
        :disabled="submitting"
        :class="{ 'is-submitting': submitting }"
      >
        <el-form-item label="ID">
          <el-input v-model="form.id" :disabled="Boolean(editingId)" />
        </el-form-item>
        <el-form-item label="Display Name">
          <el-input v-model="form.display_name" />
        </el-form-item>
        <el-form-item label="Provider">
          <el-tag type="info">tushare</el-tag>
        </el-form-item>
        <el-form-item label="Token Env Var">
          <el-input v-model="settingsForm.token_env_var" placeholder="例如 TUSHARE_TOKEN" />
        </el-form-item>
        <el-form-item label="Enabled APIs">
          <div class="api-selector">
            <el-select
              v-model="settingsForm.enabled_api_names"
              multiple
              filterable
              allow-create
              default-first-option
              placeholder="选择或输入已开通的 Tushare 接口"
            >
              <el-option
                v-for="apiName in supportedApiNames"
                :key="apiName"
                :label="apiName"
                :value="apiName"
              />
            </el-select>
            <div v-if="unmappedApiNames.length" class="unmapped-apis">
              <el-tag v-for="apiName in unmappedApiNames" :key="apiName" type="warning" effect="plain">
                {{ apiName }}：尚未映射
              </el-tag>
            </div>
          </div>
        </el-form-item>
        <el-form-item label="Enabled">
          <el-switch v-model="form.enabled" />
        </el-form-item>
        <el-form-item label="Active">
          <el-switch v-model="form.active" :disabled="!form.enabled" />
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
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { apiErrorMessage } from '../api/client'
import {
  activateMarketDataProvider,
  createMarketDataProvider,
  deleteMarketDataProvider,
  listMarketDataProviders,
  updateMarketDataProvider
} from '../api/marketDataProviders'
import type { MarketDataProviderConfig } from '../api/types'

const supportedApiNames = [
  'stock_basic',
  'stock_company',
  'daily',
  'daily_basic',
  'income',
  'cashflow',
  'fina_indicator',
  'anns_d'
]

interface TushareSettingsForm {
  token_env_var: string
  enabled_api_names: string[]
  [key: string]: unknown
}

const loading = ref(false)
const query = ref('')
const items = ref<MarketDataProviderConfig[]>([])
const dialogVisible = ref(false)
const editingId = ref<string | null>(null)
const submitting = ref(false)
const mutatingId = ref<string | null>(null)

type MarketDataProviderForm = Omit<MarketDataProviderConfig, 'created_at' | 'updated_at'>

const form = reactive<MarketDataProviderForm>({
  id: '',
  provider: 'tushare',
  display_name: '',
  enabled: true,
  active: false,
  settings: {},
  description: ''
})

const settingsForm = reactive<TushareSettingsForm>({
  token_env_var: 'TUSHARE_TOKEN',
  enabled_api_names: []
})

const unmappedApiNames = computed(() =>
  settingsForm.enabled_api_names.filter((apiName) => !supportedApiNames.includes(apiName))
)
const isBusy = computed(() => submitting.value || Boolean(mutatingId.value))

let timer = 0
watch(query, () => {
  window.clearTimeout(timer)
  timer = window.setTimeout(load, 250)
})

watch(
  () => form.enabled,
  (enabled) => {
    if (!enabled) form.active = false
  }
)

onMounted(load)

async function load() {
  loading.value = true
  try {
    items.value = (await listMarketDataProviders(query.value)).items
  } catch (error) {
    ElMessage.error(apiErrorMessage(error))
  } finally {
    loading.value = false
  }
}

function resetForm() {
  Object.assign(form, {
    id: '',
    provider: 'tushare',
    display_name: '',
    enabled: true,
    active: false,
    settings: {},
    description: ''
  })
  resetTushareSettings()
}

function resetTushareSettings(settings: Record<string, unknown> = {}) {
  for (const key of Object.keys(settingsForm)) {
    delete settingsForm[key]
  }
  Object.assign(settingsForm, {
    ...settings,
    token_env_var:
      typeof settings.token_env_var === 'string' && settings.token_env_var.trim()
        ? settings.token_env_var.trim()
        : 'TUSHARE_TOKEN',
    enabled_api_names: normalizeApiNames(settings.enabled_api_names)
  })
}

function openCreate() {
  if (isBusy.value) return
  editingId.value = null
  resetForm()
  dialogVisible.value = true
}

function openEdit(row: MarketDataProviderConfig) {
  if (isBusy.value) return
  editingId.value = row.id
  Object.assign(form, {
    id: row.id,
    provider: 'tushare',
    display_name: row.display_name,
    enabled: row.enabled,
    active: row.active,
    settings: { ...row.settings },
    description: row.description ?? ''
  })
  resetTushareSettings(row.settings)
  dialogVisible.value = true
}

async function submit() {
  if (submitting.value) return
  const tokenEnvVar = settingsForm.token_env_var.trim()
  const enabledApiNames = normalizeApiNames(settingsForm.enabled_api_names)
  if (!tokenEnvVar) {
    ElMessage.error('请填写 Tushare Token 环境变量名')
    return
  }
  if (!enabledApiNames.length) {
    ElMessage.error('请至少配置一个已开通的 Tushare 接口')
    return
  }
  submitting.value = true
  const settings = {
    ...settingsForm,
    token_env_var: tokenEnvVar,
    enabled_api_names: enabledApiNames
  }
  const payload: MarketDataProviderForm = {
    id: form.id,
    provider: 'tushare',
    display_name: form.display_name,
    enabled: form.enabled,
    active: form.enabled ? form.active : false,
    settings,
    description: form.description
  }
  try {
    if (editingId.value) {
      await updateMarketDataProvider(editingId.value, payload)
    } else {
      await createMarketDataProvider(payload)
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

async function toggleEnabled(row: MarketDataProviderConfig) {
  if (isBusy.value) return
  const nextEnabled = row.enabled
  mutatingId.value = row.id
  try {
    await updateMarketDataProvider(row.id, { enabled: nextEnabled })
    ElMessage.success(nextEnabled ? '已启用' : '已停用')
  } catch (error) {
    row.enabled = !nextEnabled
    ElMessage.error(apiErrorMessage(error))
  } finally {
    mutatingId.value = null
  }
}

async function activate(row: MarketDataProviderConfig) {
  if (isBusy.value) return
  mutatingId.value = row.id
  try {
    await activateMarketDataProvider(row.id)
    ElMessage.success('已激活')
    await load()
  } catch (error) {
    ElMessage.error(apiErrorMessage(error))
  } finally {
    mutatingId.value = null
  }
}

async function remove(row: MarketDataProviderConfig) {
  if (isBusy.value) return
  try {
    await ElMessageBox.confirm(`确认删除 ${row.id}？`, '删除数据源', { type: 'warning' })
  } catch {
    return
  }
  mutatingId.value = row.id
  try {
    await deleteMarketDataProvider(row.id)
    ElMessage.success('已删除')
    await load()
  } catch (error) {
    ElMessage.error(apiErrorMessage(error))
  } finally {
    mutatingId.value = null
  }
}

function tokenEnvVar(row: MarketDataProviderConfig) {
  const value = row.settings?.token_env_var
  return typeof value === 'string' ? value : '-'
}

function handleDialogClose(done: () => void) {
  if (!submitting.value) done()
}

function normalizeApiNames(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  const names: string[] = []
  const seen = new Set<string>()
  for (const item of value) {
    if (typeof item !== 'string') continue
    const name = item.trim()
    if (name && !seen.has(name)) {
      names.push(name)
      seen.add(name)
    }
  }
  return names
}
</script>

<style scoped>
.api-selector {
  width: 100%;
}

.api-selector :deep(.el-select) {
  width: 100%;
}

.unmapped-apis {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}

.is-submitting {
  pointer-events: none;
}
</style>
