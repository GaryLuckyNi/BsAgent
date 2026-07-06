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
        <SkillsView v-else />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import BusinessIntentsView from './views/BusinessIntentsView.vue'
import SkillsView from './views/SkillsView.vue'

const activeView = ref('business-intents')

const pageTitle = computed(() => (activeView.value === 'business-intents' ? '业务意图配置' : 'Skill 配置'))
const pageDescription = computed(() =>
  activeView.value === 'business-intents'
    ? '维护 domain、sub_intent、candidate_skill 和风险策略。'
    : '维护 Skill 能力声明、支持范围和工具 allowlist。'
)
</script>
