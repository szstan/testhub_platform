<template>
  <el-dialog
    :model-value="visible"
    :title="tx('executionDetails', '执行详情')"
    width="min(1100px, 96vw)"
    top="5vh"
    @update:model-value="value => emit('update:visible', value)"
  >
    <template v-if="currentExecution">
      <div class="execution-heading">
        <div class="execution-state">
          <el-tag :type="statusType(currentExecution.status)">{{ statusLabel(currentExecution.status) }}</el-tag>
          <span>{{ tx('executionNumber', '执行记录') }} #{{ currentExecution.id }}</span>
          <span v-if="currentExecution.duration != null">{{ formatDuration(currentExecution.duration) }}</span>
        </div>
        <el-button link @click="emit('history')">{{ tx('viewHistory', '查看历史') }}</el-button>
      </div>
      <el-alert v-if="pollError" :title="pollError" type="warning" :closable="false" show-icon />
      <el-alert v-if="currentExecution.error_message" :title="currentExecution.error_message" type="error" :closable="false" show-icon class="error-message" />
      <el-descriptions :column="4" border class="execution-summary">
        <el-descriptions-item :label="tx('totalSteps', '步骤数')">{{ currentExecution.total_steps ?? 0 }}</el-descriptions-item>
        <el-descriptions-item :label="tx('passedSteps', '通过')">{{ currentExecution.passed_steps ?? 0 }}</el-descriptions-item>
        <el-descriptions-item :label="tx('failedSteps', '失败')">{{ currentExecution.failed_steps ?? 0 }}</el-descriptions-item>
        <el-descriptions-item :label="tx('skippedSteps', '跳过')">{{ currentExecution.skipped_steps ?? 0 }}</el-descriptions-item>
      </el-descriptions>
      <el-table :data="currentExecution.results || []" row-key="order" class="step-results">
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="assertion-results">
              <div v-for="(assertion, index) in row.assertions_results || []" :key="index" class="assertion-result">
                <el-tag size="small" :type="assertion.passed ? 'success' : 'danger'">{{ assertion.passed ? tx('passed', '通过') : tx('failed', '失败') }}</el-tag>
                <strong>{{ assertion.name || tx('assertion', '断言') }}</strong>
                <span>{{ tx('expectedValue', '期望值') }}: {{ formatValue(assertion.expected) }}</span>
                <span>{{ tx('actualValue', '实际值') }}: {{ formatValue(assertion.actual) }}</span>
                <span v-if="assertion.error" class="assertion-error">{{ assertion.error }}</span>
              </div>
              <el-empty v-if="!(row.assertions_results || []).length" :description="tx('noAssertionResults', '无断言结果')" :image-size="44" />
              <div v-if="row.extracted_variables?.length">{{ tx('extractedVariableNames', '提取的变量名称') }}: {{ row.extracted_variables.join(', ') }}</div>
              <div v-if="row.response_data" class="response-snapshot">
                <strong>{{ tx('responseSnapshot', '响应快照（已脱敏）') }}</strong>
                <pre>{{ formatValue(row.response_data) }}</pre>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="order" :label="tx('step', '步骤')" width="78">
          <template #default="{ row }">{{ Number(row.order) + 1 }}</template>
        </el-table-column>
        <el-table-column prop="name" :label="tx('stepName', '步骤名称')" min-width="170" />
        <el-table-column prop="status" :label="tx('status', '状态')" width="100">
          <template #default="{ row }"><el-tag :type="stepStatusType(row.status)">{{ stepStatusLabel(row.status) }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="status_code" :label="tx('statusCode', '状态码')" width="90" />
        <el-table-column prop="response_time" :label="tx('responseTime', '响应时间')" width="120">
          <template #default="{ row }">{{ row.response_time == null ? '-' : `${Number(row.response_time).toFixed(1)} ms` }}</template>
        </el-table-column>
        <el-table-column prop="error" :label="tx('errorMessage', '错误信息')" min-width="220" show-overflow-tooltip />
      </el-table>
      <el-empty v-if="!currentExecution.results?.length && ['COMPLETED', 'FAILED'].includes(currentExecution.status)" :description="tx('noStepResults', '没有步骤结果')" :image-size="56" />
    </template>
  </el-dialog>
</template>

<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { getApiCaseExecution } from '@/api/api-test-cases'

const props = defineProps({
  visible: { type: Boolean, default: false },
  execution: { type: Object, default: null },
  pollIntervalMs: { type: Number, default: 1200 },
  pollLimit: { type: Number, default: 60 }
})
const emit = defineEmits(['update:visible', 'update:execution', 'terminal', 'history', 'poll-error', 'poll-limit'])
const { t } = useI18n()
const currentExecution = ref(props.execution ? { ...props.execution } : null)
const pollError = ref('')
let timer = null
let generation = 0
let attempts = 0
let alive = true

function tx(key, fallback) {
  const fullKey = `apiTesting.cases.${key}`
  const translated = t(fullKey)
  return translated === fullKey ? fallback : translated
}

function responseData(response) {
  return response?.data ?? response
}

function stopPolling() {
  generation += 1
  if (timer !== null) clearTimeout(timer)
  timer = null
}

function isRunning(execution) {
  return execution && ['PENDING', 'RUNNING'].includes(execution.status)
}

function schedulePoll(token, executionId) {
  timer = setTimeout(() => {
    timer = null
    poll(token, executionId)
  }, Math.max(0, props.pollIntervalMs))
}

async function poll(token, executionId) {
  if (!alive || token !== generation || !props.visible) return
  attempts += 1
  try {
    const response = await getApiCaseExecution(executionId)
    if (!alive || token !== generation || !props.visible) return
    const latest = responseData(response)
    currentExecution.value = latest
    pollError.value = ''
    emit('update:execution', latest)
    if (!isRunning(latest)) {
      emit('terminal', latest)
      return
    }
  } catch (error) {
    if (!alive || token !== generation || !props.visible) return
    pollError.value = error?.response?.data?.detail || error?.message || tx('pollFailed', '读取执行状态失败')
    emit('poll-error', error)
  }
  if (attempts >= props.pollLimit) {
    emit('poll-limit', currentExecution.value)
    return
  }
  schedulePoll(token, executionId)
}

function startPolling() {
  stopPolling()
  attempts = 0
  pollError.value = ''
  currentExecution.value = props.execution ? { ...props.execution } : null
  if (!props.visible || !isRunning(currentExecution.value) || currentExecution.value.id == null) return
  schedulePoll(generation, currentExecution.value.id)
}

watch(() => props.execution, value => {
  currentExecution.value = value ? { ...value } : null
}, { deep: true })
watch([() => props.visible, () => props.execution?.id], startPolling, { immediate: true })
watch(() => props.execution?.status, status => {
  if (!['PENDING', 'RUNNING'].includes(status)) stopPolling()
})

function statusType(status) {
  return ({ COMPLETED: 'success', FAILED: 'danger', RUNNING: 'warning' })[status] || 'info'
}
function statusLabel(status) {
  return tx(`executionStatus.${String(status || 'PENDING').toLowerCase()}`, ({ PENDING: '等待中', RUNNING: '执行中', COMPLETED: '已完成', FAILED: '失败' })[status] || String(status || '等待中'))
}
function stepStatusType(status) {
  return ({ passed: 'success', failed: 'danger', skipped: 'info' })[status] || 'warning'
}
function stepStatusLabel(status) {
  return tx(`stepStatus.${status || 'pending'}`, ({ passed: '通过', failed: '失败', skipped: '跳过' })[status] || '等待中')
}
function formatDuration(value) {
  if (value == null) return '-'
  const number = Number(value)
  return Number.isFinite(number) ? `${(number * 1000).toFixed(1)} ms` : '-'
}
function formatValue(value) {
  if (value == null) return '-'
  return typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)
}

onBeforeUnmount(() => {
  alive = false
  stopPolling()
})
</script>

<style scoped>
.execution-heading, .execution-state { display: flex; align-items: center; gap: 12px; }
.execution-heading { justify-content: space-between; margin-bottom: 14px; }
.execution-state { flex-wrap: wrap; color: var(--el-text-color-secondary); }
.execution-summary { margin-bottom: 14px; }
.step-results { margin-top: 14px; }
.error-message { margin-top: 12px; }
.assertion-results { display: flex; flex-direction: column; gap: 8px; padding: 4px 10px; }
.response-snapshot pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 360px; overflow: auto; padding: 12px; background: var(--el-fill-color-light); border-radius: 4px; }
.assertion-result { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.assertion-error { color: var(--el-color-danger); }
@media (max-width: 680px) { .execution-state { align-items: flex-start; flex-direction: column; gap: 6px; } }
</style>
