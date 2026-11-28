<template>
  <div class="assertion-editor">
    <div class="assertion-heading">
      <strong>{{ tx('assertions', '断言') }}</strong>
      <el-button size="small" @click="addAssertion">{{ tx('addAssertion', '添加断言') }}</el-button>
    </div>
    <el-empty v-if="!assertions.length" :description="tx('noAssertions', '暂无断言')" :image-size="48" />
    <div v-for="(assertion, index) in assertions" :key="index" class="assertion-row">
      <el-select v-model="assertion.type" class="assertion-type" @change="value => changeType(index, value)">
        <el-option v-for="option in assertionTypes" :key="option.value" :label="tx(option.key, option.fallback)" :value="option.value" />
      </el-select>
      <el-input v-model="assertion.name" class="assertion-name" :placeholder="tx('assertionNameOptional', '断言名称（可选）')" @input="emitValue" />
      <el-input
        v-if="assertion.type === 'json_path'"
        v-model="assertion.json_path"
        class="assertion-path"
        placeholder="$.data.id"
        @input="emitValue"
      />
      <el-input
        v-if="assertion.type === 'header'"
        v-model="assertion.header_name"
        class="assertion-path"
        :placeholder="tx('headerName', '响应头名称')"
        @input="emitValue"
      />
      <el-input
        :model-value="formatExpected(assertion)"
        :type="numericTypes.includes(assertion.type) ? 'number' : 'text'"
        class="assertion-expected"
        :placeholder="tx('expectedValue', '期望值')"
        @input="value => setExpected(index, value)"
      />
      <el-button type="danger" plain size="small" :aria-label="tx('removeAssertion', '删除断言')" @click="removeAssertion(index)">{{ tx('remove', '删除') }}</el-button>
    </div>
    <p v-if="assertions.some(assertion => assertion.type === 'json_path')" class="assertion-hint">{{ tx('jsonValueHint', 'JSONPath 期望值支持 JSON 布尔值、数字、数组和对象；字符串可直接填写，也可用双引号包裹。') }}</p>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps({
  modelValue: { type: Array, default: () => [] }
})
const emit = defineEmits(['update:modelValue'])
const { t } = useI18n()
const assertionTypes = [
  { value: 'status_code', key: 'assertionStatusCode', fallback: '状态码' },
  { value: 'response_time', key: 'assertionResponseTime', fallback: '响应时间' },
  { value: 'contains', key: 'assertionContains', fallback: '响应包含' },
  { value: 'json_path', key: 'assertionJsonPath', fallback: 'JSON 路径' },
  { value: 'header', key: 'assertionHeader', fallback: '响应头' },
  { value: 'equals', key: 'assertionEquals', fallback: '响应相等' }
]
const numericTypes = ['status_code', 'response_time']
const assertions = ref(normalizeList(props.modelValue))

function tx(key, fallback) {
  const fullKey = `apiTesting.cases.${key}`
  const translated = t(fullKey)
  return translated === fullKey ? fallback : translated
}

function normalizeAssertion(value) {
  const assertion = value && typeof value === 'object' ? { ...value } : {}
  if (assertion.expected === undefined) {
    if (assertion.type === 'status_code' && assertion.value !== undefined) assertion.expected = assertion.value
    else if (assertion.type === 'header' && assertion.expected_value !== undefined) assertion.expected = assertion.expected_value
  }
  delete assertion.value
  delete assertion.expected_value
  return assertion
}

function normalizeList(value) {
  return Array.isArray(value) ? value.map(normalizeAssertion) : []
}

watch(() => props.modelValue, value => {
  assertions.value = normalizeList(value)
}, { deep: true })

function emitValue() {
  emit('update:modelValue', assertions.value.map(normalizeAssertion))
}

function addAssertion() {
  assertions.value.push({ type: 'status_code', expected: 200, name: '' })
  emitValue()
}

function removeAssertion(index) {
  assertions.value.splice(index, 1)
  emitValue()
}

function changeType(index, type) {
  const row = assertions.value[index]
  const name = row.name || ''
  Object.assign(row, { type, name, expected: numericTypes.includes(type) ? 0 : '' })
  delete row.json_path
  delete row.header_name
  delete row.comparison
  if (type === 'json_path') row.comparison = 'json'
  emitValue()
}

function formatExpected(assertion) {
  const value = assertion.expected
  if (assertion.type !== 'json_path') return typeof value === 'object' && value !== null ? JSON.stringify(value) : value ?? ''
  if (typeof value === 'string') {
    try {
      JSON.parse(value)
      return JSON.stringify(value)
    } catch {
      return value
    }
  }
  return JSON.stringify(value) ?? ''
}

function setExpected(index, value) {
  const row = assertions.value[index]
  if (row.type === 'json_path') {
    row.comparison = 'json'
    try { row.expected = JSON.parse(value) } catch { row.expected = value }
  } else {
    row.expected = numericTypes.includes(row.type) && value !== '' ? Number(value) : value
  }
  emitValue()
}
</script>

<style scoped>
.assertion-editor { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.assertion-hint { margin: 0; color: var(--el-text-color-secondary); font-size: 12px; }
.assertion-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.assertion-row { display: grid; grid-template-columns: minmax(130px, 0.8fr) minmax(130px, 1fr) minmax(150px, 1.2fr) minmax(120px, 0.8fr) auto; gap: 8px; align-items: center; }
.assertion-row:has(.assertion-path:nth-child(3)) { grid-template-columns: minmax(130px, 0.8fr) minmax(130px, 1fr) minmax(150px, 1.2fr) minmax(120px, 0.8fr) auto; }
.assertion-type, .assertion-name, .assertion-path, .assertion-expected { min-width: 0; width: 100%; }
@media (max-width: 760px) {
  .assertion-row { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .assertion-row > :last-child { justify-self: start; }
}
</style>
