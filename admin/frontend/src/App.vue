<template>
  <el-container class="app-shell">
    <el-aside width="240px" class="sidebar">
      <div class="brand">
        <span class="brand__title">BsAgent Admin</span>
        <span class="brand__subtitle">配置中心</span>
      </div>
      <el-menu :default-active="activeView" @select="activeView = $event">
        <el-menu-item index="business-intents">业务意图</el-menu-item>
        <el-menu-item index="skills">Skills</el-menu-item>
        <el-menu-item index="market-data-providers">市场数据源</el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="header">
        <div>
          <h1>{{ pageTitle }}</h1>
          <p>{{ pageDescription }}</p>
        </div>
      </el-header>
      <el-main class="main">
        <BusinessIntentsView v-if="activeView === 'business-intents'" />
        <SkillsView v-else-if="activeView === 'skills'" />
        <MarketDataProvidersView v-else />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import BusinessIntentsView from './views/BusinessIntentsView.vue'
import MarketDataProvidersView from './views/MarketDataProvidersView.vue'
import SkillsView from './views/SkillsView.vue'

const activeView = ref('business-intents')

const pageTitle = computed(() => {
  if (activeView.value === 'business-intents') {
    return '业务意图配置'
  }
  if (activeView.value === 'skills') {
    return 'Skill 配置'
  }
  return '市场数据源配置'
})
const pageDescription = computed(() =>
  activeView.value === 'business-intents'
    ? '维护 intent_type、domain、subdomain 和常见表达。'
    : activeView.value === 'skills'
      ? '维护 Skill 工作流说明和适用范围。'
      : '维护 MCP market data provider 的激活配置和安全密钥引用。'
)
</script>
