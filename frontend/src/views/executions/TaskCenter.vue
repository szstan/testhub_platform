<template>
  <div class="execution-task-center">
    <div class="page-header">
      <div>
        <h1>{{ $t('execution.taskCenter.title') }}</h1>
        <p class="page-subtitle">{{ $t('execution.taskCenter.subtitle') }}</p>
      </div>
      <el-button @click="router.push({ name: 'ExecutionFailureReview' })">
        {{ $t('execution.failureReview.nav') }}
      </el-button>
      <el-button type="primary" @click="router.push({ name: 'ExecutionQualityLoop' })">
        {{ $t('execution.qualityLoop.nav') }}
      </el-button>
      <el-button :icon="Refresh" :loading="loading" @click="loadJobs">
        {{ $t('common.refresh') }}
      </el-button>
    </div>

    <el-card shadow="never" class="filter-card">
      <el-form :inline="true" @submit.prevent="applyFilters">
        <el-form-item :label="$t('execution.taskCenter.search')">
          <el-input
            v-model="filters.search"
            clearable
            :placeholder="$t('execution.taskCenter.searchPlaceholder')"
            @keyup.enter="applyFilters"
          />
        </el-form-item>
        <el-form-item :label="$t('execution.taskCenter.status')">
          <el-select v-model="filters.status" clearable :placeholder="$t('execution.taskCenter.allStatuses')">
            <el-option v-for="item in statusOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('execution.taskCenter.source')">
          <el-select v-model="filters.source_type" clearable :placeholder="$t('execution.taskCenter.allSources')">
            <el-option v-for="item in sourceOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :icon="Search" @click="applyFilters">
            {{ $t('common.search') }}
          </el-button>
          <el-button @click="resetFilters">{{ $t('common.reset') }}</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never" class="table-card">
      <el-table :data="jobs" v-loading="loading" row-key="id" @row-click="openTimeline">
        <el-table-column prop="id" label="ID" width="80" />
        <el-table-column :label="$t('execution.taskCenter.source')" min-width="150">
          <template #default="scope">
            <el-tag size="small" effect="plain">{{ sourceLabel(scope.row.source_type) }}</el-tag>
            <span class="source-id">{{ scope.row.source_id || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="build_reference" :label="$t('execution.taskCenter.build')" min-width="150" show-overflow-tooltip />
        <el-table-column :label="$t('execution.taskCenter.status')" width="150">
          <template #default="scope">
            <el-tag :type="statusType(scope.row.status)">{{ statusLabel(scope.row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('execution.taskCenter.qualityGate')" width="120">
          <template #default="scope">
            <el-tag v-if="scope.row.quality_gate_result?.configured" size="small" :type="scope.row.quality_gate_result.passed ? 'success' : 'danger'">
              {{ scope.row.quality_gate_result.passed ? $t('execution.taskCenter.gatePassed') : $t('execution.taskCenter.gateFailed') }}
            </el-tag>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column prop="attempt" :label="$t('execution.taskCenter.attempts')" width="95" />
        <el-table-column prop="priority" :label="$t('execution.taskCenter.priority')" width="100" />
        <el-table-column prop="created_at" :label="$t('execution.taskCenter.createdAt')" width="180">
          <template #default="scope">{{ formatDate(scope.row.created_at) }}</template>
        </el-table-column>
        <el-table-column :label="$t('execution.taskCenter.actions')" width="240" fixed="right">
          <template #default="scope">
            <el-button link type="primary" @click.stop="openTimeline(scope.row)">
              {{ $t('execution.taskCenter.view') }}
            </el-button>
            <el-button
              v-if="canCancel(scope.row)"
              link
              type="warning"
              @click.stop="cancelJob(scope.row)"
            >
              {{ $t('execution.taskCenter.cancel') }}
            </el-button>
            <el-button
              v-if="canRetry(scope.row)"
              link
              type="success"
              @click.stop="retryJob(scope.row)"
            >
              {{ $t('execution.taskCenter.retry') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && jobs.length === 0" :description="$t('execution.taskCenter.empty')" />
      <div class="pagination">
        <el-pagination
          v-model:current-page="pagination.page"
          v-model:page-size="pagination.pageSize"
          :page-sizes="[10, 20, 50, 100]"
          :total="pagination.total"
          layout="total, sizes, prev, pager, next, jumper"
          @size-change="loadJobs"
          @current-change="loadJobs"
        />
      </div>
    </el-card>

    <el-drawer v-model="timelineVisible" :title="$t('execution.taskCenter.detail')" size="720px">
      <el-skeleton v-if="timelineLoading" :rows="8" animated />
      <template v-else-if="timeline">
        <el-descriptions :column="2" border class="timeline-summary">
          <el-descriptions-item label="ID">{{ timeline.job.id }}</el-descriptions-item>
          <el-descriptions-item :label="$t('execution.taskCenter.source')">{{ sourceLabel(timeline.job.source_type) }}</el-descriptions-item>
          <el-descriptions-item :label="$t('execution.taskCenter.finalStatus')">{{ statusLabel(timeline.job.final_status || timeline.job.status) }}</el-descriptions-item>
          <el-descriptions-item :label="$t('execution.taskCenter.qualityGate')">
            <el-tag v-if="timeline.job.quality_gate_result?.configured" size="small" :type="timeline.job.quality_gate_result.passed ? 'success' : 'danger'">
              {{ timeline.job.quality_gate_result.passed ? $t('execution.taskCenter.gatePassed') : $t('execution.taskCenter.gateFailed') }}
            </el-tag>
            <span v-else>-</span>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('execution.taskCenter.finalResult')">{{ JSON.stringify(timeline.job.final_result || {}) }}</el-descriptions-item>
          <el-descriptions-item :label="$t('execution.taskCenter.error')">{{ timeline.job.error_message || '-' }}</el-descriptions-item>
        </el-descriptions>
        <el-tabs v-model="activeTab" class="timeline-tabs">
          <el-tab-pane :label="$t('execution.taskCenter.attempts')" name="attempts">
            <el-table :data="timeline.attempts" size="small">
              <el-table-column prop="attempt_number" label="#" width="60" />
              <el-table-column prop="status" :label="$t('execution.taskCenter.status')">
                <template #default="scope"><el-tag size="small" :type="statusType(scope.row.status)">{{ statusLabel(scope.row.status) }}</el-tag></template>
              </el-table-column>
              <el-table-column prop="worker" :label="$t('execution.taskCenter.worker')" />
              <el-table-column prop="error_message" :label="$t('execution.taskCenter.error')" show-overflow-tooltip />
              <el-table-column prop="retry_reason" :label="$t('execution.taskCenter.retryReason')" show-overflow-tooltip />
              <el-table-column prop="is_final" :label="$t('execution.taskCenter.finalAttempt')" width="110">
                <template #default="scope"><el-tag v-if="scope.row.is_final" size="small" type="success">{{ $t('execution.taskCenter.yes') }}</el-tag><span v-else>-</span></template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
          <el-tab-pane :label="$t('execution.taskCenter.steps')" name="steps">
            <el-table :data="timeline.steps" size="small">
              <el-table-column prop="case_key" label="Case" min-width="140" />
              <el-table-column prop="step_key" label="Step" min-width="140" />
              <el-table-column prop="status" :label="$t('execution.taskCenter.status')">
                <template #default="scope"><el-tag size="small" :type="statusType(scope.row.status)">{{ statusLabel(scope.row.status) }}</el-tag></template>
              </el-table-column>
              <el-table-column prop="duration_ms" label="ms" width="80" />
            </el-table>
          </el-tab-pane>
          <el-tab-pane :label="$t('execution.taskCenter.artifacts')" name="artifacts">
            <el-table :data="timeline.artifacts" size="small">
              <el-table-column prop="media_type" :label="$t('execution.taskCenter.type')" width="150" />
              <el-table-column prop="uri" :label="$t('execution.taskCenter.uri')" show-overflow-tooltip />
              <el-table-column prop="retention_policy" :label="$t('execution.taskCenter.retention')" width="110" />
            </el-table>
          </el-tab-pane>
          <el-tab-pane :label="$t('execution.taskCenter.aiSuggestions')" name="ai">
            <el-table :data="timeline.ai_suggestions || []" size="small">
              <el-table-column prop="kind" :label="$t('execution.taskCenter.kind')" min-width="150" />
              <el-table-column prop="model_name" :label="$t('execution.taskCenter.model')" min-width="140" />
              <el-table-column prop="status" :label="$t('execution.taskCenter.aiStatus')" width="110" />
              <el-table-column prop="suggestion_hash" :label="$t('execution.taskCenter.hash')" show-overflow-tooltip />
            </el-table>
            <el-empty v-if="!(timeline.ai_suggestions || []).length" :description="$t('execution.taskCenter.noAiSuggestions')" />
          </el-tab-pane>
        </el-tabs>
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Search } from '@element-plus/icons-vue'
import api from '@/utils/api'
import {
  cancelExecutionJob,
  getExecutionJobTimeline,
  listExecutionJobs,
  retryExecutionJob,
} from '@/api/executions'

const router = useRouter()
const jobs = ref([])
const loading = ref(false)
const timeline = ref(null)
const timelineVisible = ref(false)
const timelineLoading = ref(false)
const activeTab = ref('attempts')
const pollTimer = ref(null)
const pagination = ref({ page: 1, pageSize: 20, total: 0 })
const filters = ref({ search: '', status: '', source_type: '' })

const statusOptions = [
  { value: 'pending', label: 'Pending' },
  { value: 'queued', label: 'Queued' },
  { value: 'running', label: 'Running' },
  { value: 'cancel_requested', label: 'Cancel requested' },
  { value: 'cancelled', label: 'Cancelled' },
  { value: 'passed', label: 'Passed' },
  { value: 'succeeded', label: 'Succeeded' },
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed' },
  { value: 'blocked', label: 'Blocked' },
  { value: 'unsupported', label: 'Unsupported' },
]
const sourceOptions = [
  { value: 'api', label: 'API' },
  { value: 'ui', label: 'UI' },
  { value: 'app', label: 'APP' },
  { value: 'performance', label: 'Performance' },
  { value: 'ci', label: 'CI' },
  { value: 'manual', label: 'Manual' },
]
const sourceLabels = Object.fromEntries(sourceOptions.map(item => [item.value, item.label]))
const terminalStatuses = new Set(['cancelled', 'passed', 'succeeded', 'completed', 'failed', 'blocked', 'unsupported'])

const loadJobs = async () => {
  loading.value = true
  try {
    const response = await listExecutionJobs({
      page: pagination.value.page,
      page_size: pagination.value.pageSize,
      search: filters.value.search || undefined,
      status: filters.value.status || undefined,
      source_type: filters.value.source_type || undefined,
      ordering: '-created_at',
    })
    const data = response.data || {}
    jobs.value = data.results || (Array.isArray(data) ? data : [])
    pagination.value.total = data.count ?? jobs.value.length
    refreshPolling()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载统一执行任务失败')
  } finally {
    loading.value = false
  }
}

const applyFilters = () => {
  pagination.value.page = 1
  loadJobs()
}

const resetFilters = () => {
  filters.value = { search: '', status: '', source_type: '' }
  applyFilters()
}

const openTimeline = async (row) => {
  timelineVisible.value = true
  timelineLoading.value = true
  timeline.value = null
  activeTab.value = 'attempts'
  try {
    const response = await getExecutionJobTimeline(row.id)
    timeline.value = response.data
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载执行详情失败')
  } finally {
    timelineLoading.value = false
  }
}

const cancelJob = async (row) => {
  try {
    await ElMessageBox.confirm('确定取消该执行任务吗？', '确认操作', { type: 'warning' })
    await cancelExecutionJob(row.id, 'cancelled from task center')
    ElMessage.success('取消请求已提交')
    await loadJobs()
    if (timelineVisible.value) await openTimeline(row)
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(error.response?.data?.detail || '取消执行失败')
  }
}

const retryJob = async (row) => {
  try {
    await retryExecutionJob(row.id)
    ElMessage.success('重试任务已排队')
    await loadJobs()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '重试执行失败')
  }
}

const refreshPolling = () => {
  if (pollTimer.value) clearInterval(pollTimer.value)
  if (jobs.value.some(row => !terminalStatuses.has(row.status))) {
    pollTimer.value = setInterval(loadJobs, 5000)
  }
}

const sourceLabel = value => sourceLabels[value] || value || '-'
const statusLabel = value => statusOptions.find(item => item.value === value)?.label || value || '-'
const statusType = value => ({
  passed: 'success', succeeded: 'success', completed: 'success',
  failed: 'danger', blocked: 'danger', unsupported: 'info',
  cancelled: 'info', cancel_requested: 'warning', running: 'warning', queued: 'warning', pending: 'info',
}[value] || 'info')
const canCancel = row => !terminalStatuses.has(row.status)
const canRetry = row => ['failed', 'blocked', 'cancelled', 'unsupported'].includes(row.status)
const formatDate = value => value ? new Date(value).toLocaleString() : '-'

onMounted(loadJobs)
onBeforeUnmount(() => {
  if (pollTimer.value) clearInterval(pollTimer.value)
})
</script>

<style scoped>
.execution-task-center { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 18px; }
.page-header h1 { margin: 0; font-size: 24px; color: var(--el-text-color-primary); }
.page-subtitle { margin: 8px 0 0; color: var(--el-text-color-secondary); }
.filter-card, .table-card { margin-bottom: 16px; }
.source-id { margin-left: 8px; color: var(--el-text-color-secondary); }
.pagination { display: flex; justify-content: flex-end; padding-top: 18px; }
.timeline-summary { margin-bottom: 18px; }
.timeline-tabs { min-height: 280px; }
</style>
