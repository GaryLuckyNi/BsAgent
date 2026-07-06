<template>
  <div class="string-list">
    <el-tag
      v-for="item in modelValue"
      :key="item"
      closable
      @close="remove(item)"
    >
      {{ item }}
    </el-tag>
    <el-input
      v-model="draft"
      class="string-list__input"
      size="small"
      @keyup.enter="append"
      @blur="append"
    />
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const props = defineProps<{
  modelValue: string[]
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string[]]
}>()

const draft = ref('')

function append() {
  const value = draft.value.trim()
  if (!value) return
  if (!props.modelValue.includes(value)) {
    emit('update:modelValue', [...props.modelValue, value])
  }
  draft.value = ''
}

function remove(item: string) {
  emit(
    'update:modelValue',
    props.modelValue.filter((value) => value !== item)
  )
}
</script>
