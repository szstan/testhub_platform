<template>
  <el-dialog
    :model-value="visible"
    :title="tx('executionHistory', '执行历史')"
    width="min(980px, 96vw)"
    @update:model-value="value => emit('update:visible', value)"
  >
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <el-table :data="rows" v-loading="loading" row-key="id" class="history-table">
      <el-table-column prop="id" label="ID" width="80" />
      <el-table-column prop="status" :label="tx('status', '状态')" width="130">
        <template #default="{ row }"><el-tag :type="statusType(row.status)">{{ statusLabel(row.status) }}</el-tag></template>
      </el-table-column>
      <el-table-column prop="test_case_name" :label="tx('caseName', '用例')" min-width="170" show-overflow-tooltip />
      <el-table-column :label="tx('stepsProgress', '步骤结果')" width="150">
        <template #default="{ row }">{{ row.passed_steps || 0 }} / {{ row.total_steps || 0 }} {{ tx('passed', '通过') }}</template>
      </el-table-column>
      <el-table-column prop="duration" :label="tx('duration', '耗时')" width="120">
        <template #default="{ row }">{{ formatDuration(row.duration) }}</template>
      </el-table-column>
      <el-table-column prop="created_at" :label="tx('executionTime', '执行时间')" min-width="170">
        <template #default="{ row }">{{ formatDate(row.created_at || row.start_time) }}</template>
      </el-table-column>
      <el-table-column :label="tx('operation', '操作')" width="110" fixed="right">
        <template #default="{ row }"><el-button link type="primary" @click="emit('select', row)">{{ tx('viewDetails', '查看详情') }}</el-button></template>
      </el-table-column>
    </el-table>
    <el-empty v-if="!loading && !rows.length && !error" :description="tx('noExecutionHistory', '暂无执行历史')" :image-size="56" />
    <el-pagination
      v-if="total > pageSize"
      v-model:current-page="page"
      :page-size="pageSize"
      :total="total"
      layout="prev, pager, next, total"
      @current-change="loadHistory"
    />
  </el-dialog>
</template>

<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { getApiTestCaseExecutions } from '@/api/api-test-cases'

const props = defineProps({
  visible: { type: Boolean, default: false },
  testCaseId: { type: [Number, String], default: null },
  pageSize: { type: Number, default: 10 }
})
const emit = defineEmits(['update:visible', 'select'])
const { t } = useI18n()
const rows = ref([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const error = ref('')
let alive = true
let version = 0

function tx(key, fallback) {
  const fullKey = `apiTesting.cases.${key}`
  const translated = t(fullKey)
  return translated === fullKey ? fallback : translated
}

function responseData(response) {
  return response?.data ?? response
}

async function loadHistory() {
  if (!props.testCaseId || !props.visible) return
  const token = ++version
  const caseId = props.testCaseId
  loading.value = true
  error.value = ''
  try {
    const response = await getApiTestCaseExecutions(caseId, { page: page.value, page_size: props.pageSize })
    if (!alive || token !== version || caseId !== props.testCaseId) return
    const data = responseData(response)
    rows.value = Array.isArray(data) ? data : data?.results || []
    total.value = Number(data?.count ?? rows.value.length)
  } catch (cause) {
    if (!alive || token !== version || caseId !== props.testCaseId) return
    error.value = cause?.response?.data?.detail || cause?.message || tx('historyLoadFailed', '加载执行历史失败')
    rows.value = []
    total.value = 0
  } finally {
    if (alive && token === version && caseId === props.testCaseId) loading.value = false
  }
}

watch(() => [props.visible, props.testCaseId], ([visible, caseId], previous = []) => {
  version += 1
  if (visible && caseId) {
    if (!previous[0] || previous[1] !== caseId) page.value = 1
    loadHistory()
  } else {
    loading.value = false
  }
}, { immediate: true })

function statusType(status) {
  return ({ COMPLETED: 'success', FAILED: 'danger', RUNNING: 'warning' })[status] || 'info'
}
function statusLabel(status) {
  const key = `executionStatus.${String(status || 'PENDING').toLowerCase()}`
  const fallback = ({ PENDING: '等待中', RUNNING: '执行中', COMPLETED: '已完成', FAILED: '失败' })[status] || String(status || '等待中')
  return tx(key, fallback)
}
function formatDuration(value) {
  if (value == null) return '-'
  const number = Number(value)
  return Number.isFinite(number) ? `${(number * 1000).toFixed(1)} ms` : '-'
}
function formatDate(value) {
  return value ? new Date(value).toLocaleString() : '-'
}

onBeforeUnmount(() => {
  alive = false
  version += 1
})
</script>

<style scoped>
.history-table { margin: 12px 0; }
.el-pagination { justify-content: flex-end; }
</style>
