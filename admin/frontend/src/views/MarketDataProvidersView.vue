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
      <el-table-column label="MCP 地址" min-width="280">
        <template #default="{ row }">
          {{ mcpServerUrl(row) }}
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
        <el-form-item label="Tushare MCP URL">
          <el-input
            v-model="settingsForm.mcp_server_url"
            type="password"
            show-password
            autocomplete="off"
            :placeholder="
              editingId
                ? '留空表示保留当前地址'
                : 'https://api.tushare.pro/mcp/token=&lt;mcp-key&gt;'
            "
          />
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

interface TushareSettingsForm {
  mcp_server_url: string
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
  mcp_server_url: ''
})

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

function resetTushareSettings() {
  settingsForm.mcp_server_url = ''
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
  resetTushareSettings()
  dialogVisible.value = true
}

async function submit() {
  if (submitting.value) return
  const mcpServerUrl = settingsForm.mcp_server_url.trim()
  if (!editingId.value && !mcpServerUrl) {
    ElMessage.error('请填写 Tushare MCP URL')
    return
  }
  submitting.value = true
  const settings = mcpServerUrl ? { mcp_server_url: mcpServerUrl } : {}
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

function mcpServerUrl(row: MarketDataProviderConfig) {
  return row.settings.mcp_server_url || '-'
}

function handleDialogClose(done: () => void) {
  if (!submitting.value) done()
}

</script>

<style scoped>
.is-submitting {
  pointer-events: none;
}
</style>
