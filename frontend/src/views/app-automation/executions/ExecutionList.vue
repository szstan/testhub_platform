<template>
  <div class="execution-list">
    <!-- 工具栏 -->
    <el-card class="toolbar" shadow="never">
      <el-row :gutter="20">
        <el-col :span="4">
          <el-select v-model="projectFilter" :placeholder="$t('appAutomation.execution.allProjects')" clearable filterable @change="loadExecutions">
            <el-option v-for="p in projectList" :key="p.id" :label="p.name" :value="p.id" />
          </el-select>
        </el-col>
        <el-col :span="8">
          <el-input
            v-model="searchQuery"
            :placeholder="$t('appAutomation.execution.searchPlaceholder')"
            clearable
            @clear="loadExecutions"
            @keyup.enter="loadExecutions"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
        </el-col>
        <el-col :span="12" class="text-right">
          <el-select
            v-model="statusFilter"
            :placeholder="$t('appAutomation.execution.statusFilter')"
            clearable
            style="width: 150px; margin-right: 10px"
            @change="loadExecutions"
          >
            <el-option :label="$t('appAutomation.common.all')" value="" />
            <el-option :label="$t('appAutomation.status.pending')" value="pending" />
            <el-option :label="$t('appAutomation.status.running')" value="running" />
            <el-option :label="$t('appAutomation.status.completed')" value="completed" />
            <el-option :label="$t('appAutomation.status.error')" value="error" />
            <el-option :label="$t('appAutomation.status.stopped')" value="stopped" />
          </el-select>
          <el-button @click="loadExecutions">
            <el-icon><Refresh /></el-icon>
            {{ $t('appAutomation.common.refresh') }}
          </el-button>
        </el-col>
      </el-row>
    </el-card>
    
    <!-- 执行记录列表 -->
    <el-card class="table-card" shadow="never">
      <el-table
        v-loading="loading"
        :data="executions"
        stripe
        style="width: 100%"
      >
        <el-table-column prop="case_name" :label="$t('appAutomation.execution.testCase')" min-width="180" />
        <el-table-column prop="device_name" :label="$t('appAutomation.execution.device')" width="150" />
        <el-table-column :label="$t('appAutomation.common.status')" width="100">
          <template #default="{ row }">
            <el-tag :type="getDisplayStatus(row.status, row.result).type" size="small">
              {{ getDisplayStatus(row.status, row.result).text }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('appAutomation.execution.progress')" width="150">
          <template #default="{ row }">
            <el-progress
              :percentage="row.progress || 0"
              :status="row.status === 'error' ? 'exception' : row.result === 'failed' ? 'exception' : row.result === 'passed' ? 'success' : undefined"
            />
          </template>
        </el-table-column>
        <el-table-column :label="$t('appAutomation.execution.stepStats')" width="180">
          <template #default="{ row }">
            <div class="step-stats">
              <span class="stat-item success">{{ $t('appAutomation.status.passed') }}: {{ row.passed_steps || 0 }}</span>
              <span class="stat-item danger">{{ $t('appAutomation.status.failed') }}: {{ row.failed_steps || 0 }}</span>
              <span class="stat-item">{{ $t('appAutomation.execution.total') }}: {{ row.total_steps || 0 }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="$t('appAutomation.execution.duration')" width="100">
          <template #default="{ row }">
            {{ formatDuration(row.duration) }}
          </template>
        </el-table-column>
        <el-table-column prop="user_name" :label="$t('appAutomation.execution.executor')" width="100" />
        <el-table-column :label="$t('appAutomation.execution.startTime')" width="160">
          <template #default="{ row }">
            {{ formatDateTime(row.started_at) }}
          </template>
        </el-table-column>
        <el-table-column :label="$t('appAutomation.execution.endTime')" width="160">
          <template #default="{ row }">
            {{ row.finished_at ? formatDateTime(row.finished_at) : '-' }}
          </template>
        </el-table-column>
        <el-table-column :label="$t('appAutomation.common.operation')" width="180" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="(row.status === 'pending' || row.status === 'running') && !row.stop_requested"
              type="warning"
              size="small"
              text
              :loading="stoppingExecutionIds.has(row.id)"
              :disabled="stoppingExecutionIds.has(row.id)"
              @click="stopExecution(row)"
            >
              {{ $t('appAutomation.common.stop') }}
            </el-button>
            <el-button
              v-if="row.report_access_token"
              type="primary"
              size="small"
              text
              @click="viewReport(row)"
            >
              {{ $t('appAutomation.execution.viewReport') }}
            </el-button>
            <el-button
              v-if="row.error_message"
              type="danger"
              size="small"
              text
              @click="viewError(row)"
            >
              {{ $t('appAutomation.execution.viewError') }}
            </el-button>
            <el-button
              v-if="row.failed_steps || row.status === 'error' || row.result === 'failed'"
              type="danger"
              size="small"
              text
              @click="viewDiagnostics(row)"
            >
              {{ $t('appAutomation.execution.viewDiagnostics') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      
      <!-- 分页 -->
      <div class="pagination">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          @size-change="loadExecutions"
          @current-change="loadExecutions"
        />
      </div>
    </el-card>
    
    <!-- 错误信息对话框 -->
    <el-dialog
      v-model="errorDialogVisible"
      :title="$t('appAutomation.execution.errorInfo')"
      width="600px"
    >
      <div class="error-content">
        <pre>{{ currentError }}</pre>
      </div>
      <template #footer>
        <el-button type="primary" @click="errorDialogVisible = false">
          {{ $t('appAutomation.common.close') }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="diagnosticDialogVisible"
      :title="$t('appAutomation.execution.diagnosticsTitle')"
      width="680px"
      @closed="clearDiagnostic"
    >
      <div v-loading="diagnosticLoading" class="diagnostic-content">
        <template v-if="diagnosticExecution">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item :label="$t('appAutomation.execution.executionState')">
              <el-tag size="small">{{ $t(`appAutomation.execution.state.${diagnosticExecution.execution_state}`) }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item :label="$t('appAutomation.execution.engine')">
              {{ $t(`appAutomation.execution.engineValue.${diagnosticExecution.engine || 'legacy'}`) }}
            </el-descriptions-item>
            <el-descriptions-item :label="$t('appAutomation.execution.caseRevision')">
              {{ diagnosticExecution.case_revision || '-' }}
            </el-descriptions-item>
            <el-descriptions-item :label="$t('appAutomation.execution.deviceRecovery')">
              {{ diagnosticExecution.device_action_pending
                ? $t('appAutomation.execution.deviceRecoveryPending')
                : $t('appAutomation.execution.deviceRecoveryClear') }}
            </el-descriptions-item>
          </el-descriptions>

          <section v-if="diagnosticExecution.flow_failure" class="failure-diagnostic">
            <h3>{{ $t('appAutomation.execution.failureDetails') }}</h3>
            <p>
              <strong>{{ $t('appAutomation.execution.classification') }}:</strong>
              {{ $t(`appAutomation.execution.classificationValue.${diagnosticExecution.flow_failure.classification}`) }}
            </p>
            <p><strong>{{ $t('appAutomation.execution.failedStep') }}:</strong> {{ diagnosticExecution.flow_failure.step_name || '-' }}</p>
            <p><strong>{{ $t('appAutomation.execution.stepPath') }}:</strong> {{ diagnosticExecution.flow_failure.step_path || '-' }}</p>
            <p><strong>{{ $t('appAutomation.execution.stepDuration') }}:</strong> {{ diagnosticExecution.flow_failure.duration_ms }} ms</p>
            <p v-if="diagnosticExecution.flow_failure.match_count !== null">
              <strong>{{ $t('appAutomation.execution.matchCount') }}:</strong> {{ diagnosticExecution.flow_failure.match_count }}
            </p>
            <p><strong>{{ $t('appAutomation.execution.failureCode') }}:</strong> {{ diagnosticExecution.flow_failure.code }}</p>
            <el-alert
              v-if="diagnosticExecution.flow_failure.case_revision_changed"
              type="warning"
              :closable="false"
              :title="$t('appAutomation.execution.caseRevisionChanged')"
              :description="diagnosticExecution.flow_failure.step_path || $t('appAutomation.execution.stepPathUnavailable')"
            />
            <img
              v-if="diagnosticScreenshotUrl"
              class="diagnostic-screenshot"
              :src="diagnosticScreenshotUrl"
              :alt="$t('appAutomation.execution.failureScreenshot')"
            >
          </section>
          <p v-else class="diagnostic-empty">{{ $t('appAutomation.execution.noStructuredFailure') }}</p>
        </template>
      </div>
      <template #footer>
        <el-button
          v-if="diagnosticExecution?.test_case"
          type="primary"
          @click="openFailureStep"
        >
          {{ $t('appAutomation.execution.openTestCase') }}
        </el-button>
        <el-button @click="diagnosticDialogVisible = false">
          {{ $t('appAutomation.common.close') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getExecutionList,
  getExecutionDetail,
  stopExecution as apiStopExecution,
  getAppProjects
} from '@/api/app-automation'
import { Search, Refresh } from '@element-plus/icons-vue'
import { getExecutionStatusType, getExecutionStatusText, getDisplayStatus, formatDateTime } from '@/utils/app-automation-helpers'
import { openAppExecutionReport } from '@/utils/app-automation-report.mjs'
import { getFlowFailureScreenshot } from '@/api/app-step-diagnostics'
import { useRouter, useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useUserStore } from '@/stores/user'

const { t } = useI18n()
const router = useRouter()
const route = useRoute()
const userStore = useUserStore()

const loading = ref(false)
const executions = ref([])
const stoppingExecutionIds = ref(new Set())
const searchQuery = ref('')
const statusFilter = ref('')
const projectFilter = ref(null)
const projectList = ref([])
const currentPage = ref(1)
const pageSize = ref(20)
const total = ref(0)

const errorDialogVisible = ref(false)
const currentError = ref('')
const diagnosticDialogVisible = ref(false)
const diagnosticLoading = ref(false)
const diagnosticExecution = ref(null)
const diagnosticScreenshotUrl = ref('')
let diagnosticScreenshotObjectUrl = null
let diagnosticRequestId = 0

let refreshTimer = null

const loadExecutions = async () => {
  loading.value = true
  try {
    const params = {
      page: currentPage.value,
      page_size: pageSize.value,
      search: searchQuery.value,
      status: statusFilter.value
    }
    if (projectFilter.value) params.project = projectFilter.value
    const res = await getExecutionList(params)
    executions.value = res.data.results || []
    total.value = res.data.count || 0
  } catch (error) {
    ElMessage.error(t('appAutomation.execution.loadFailed') + ': ' + (error.message || t('appAutomation.execution.unknownError')))
  } finally {
    loading.value = false
  }
}

const stopExecution = async (execution) => {
  if (!execution?.id || !['pending', 'running'].includes(execution.status) || execution.stop_requested) return
  if (stoppingExecutionIds.value.has(execution.id)) return
  stoppingExecutionIds.value.add(execution.id)
  try {
    await ElMessageBox.confirm(
      t('appAutomation.execution.stopConfirm'),
      t('appAutomation.execution.confirmStop'),
      {
        confirmButtonText: t('appAutomation.common.confirm'),
        cancelButtonText: t('appAutomation.common.cancel'),
        type: 'warning'
      }
    )

    const res = await apiStopExecution(execution.id)
    if (res.data.success) {
      ElMessage.success(t(res.data.stopping
        ? 'appAutomation.execution.stoppingExecution'
        : 'appAutomation.execution.executionStopped'))
      loadExecutions()
    } else {
      ElMessage.error(res.data.message || t('appAutomation.execution.stopFailed'))
    }
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error(error?.response?.data?.message || error.message || t('appAutomation.execution.stopFailed'))
    }
  } finally {
    stoppingExecutionIds.value.delete(execution.id)
  }
}

const viewReport = async (execution) => {
  const result = await openAppExecutionReport(execution?.id, { fetchExecution: getExecutionDetail })
  if (!result.opened) {
    const key = result.reason === 'blocked' ? 'reportPopupBlocked' : result.reason === 'failed' ? 'reportOpenFailed' : 'reportUnavailable'
    ElMessage.warning(t(`appAutomation.common.${key}`))
  }
}

const viewError = (execution) => {
  currentError.value = execution.error_message
  errorDialogVisible.value = true
}

const clearDiagnostic = () => {
  diagnosticRequestId += 1
  if (diagnosticScreenshotObjectUrl) {
    URL.revokeObjectURL(diagnosticScreenshotObjectUrl)
    diagnosticScreenshotObjectUrl = null
  }
  diagnosticScreenshotUrl.value = ''
  diagnosticExecution.value = null
  diagnosticLoading.value = false
}

const sessionSnapshot = () => ({
  userId: userStore.user?.id ?? userStore.user?.pk ?? null,
  generation: userStore.getSessionGeneration?.(),
})
const sessionMatches = snapshot => String(userStore.user?.id ?? userStore.user?.pk ?? '') === String(snapshot.userId ?? '')
  && userStore.getSessionGeneration?.() === snapshot.generation

const viewDiagnostics = async (execution) => {
  if (!execution?.id) return
  const snapshot = sessionSnapshot()
  if (snapshot.userId === null) return
  const requestId = ++diagnosticRequestId
  if (diagnosticScreenshotObjectUrl) {
    URL.revokeObjectURL(diagnosticScreenshotObjectUrl)
    diagnosticScreenshotObjectUrl = null
  }
  diagnosticScreenshotUrl.value = ''
  diagnosticExecution.value = null
  diagnosticDialogVisible.value = true
  diagnosticLoading.value = true
  try {
    const response = await getExecutionDetail(execution.id)
    if (requestId !== diagnosticRequestId) return
    if (!sessionMatches(snapshot)) {
      diagnosticDialogVisible.value = false
      clearDiagnostic()
      return
    }
    diagnosticExecution.value = response.data
    if (response.data?.flow_failure?.has_screenshot) {
      try {
        const screenshot = await getFlowFailureScreenshot(execution.id)
        if (requestId !== diagnosticRequestId) return
        if (!sessionMatches(snapshot)) {
          diagnosticDialogVisible.value = false
          clearDiagnostic()
          return
        }
        diagnosticScreenshotObjectUrl = URL.createObjectURL(screenshot.data)
        diagnosticScreenshotUrl.value = diagnosticScreenshotObjectUrl
      } catch {
        ElMessage.warning(t('appAutomation.execution.screenshotUnavailable'))
      }
    }
  } catch {
    if (requestId === diagnosticRequestId) {
      ElMessage.error(t('appAutomation.execution.diagnosticsLoadFailed'))
      diagnosticDialogVisible.value = false
    }
  } finally {
    if (requestId === diagnosticRequestId) diagnosticLoading.value = false
  }
}

const openFailureStep = () => {
  const execution = diagnosticExecution.value
  if (!execution?.test_case) return
  const failure = execution.flow_failure
  const query = {
    case_id: String(execution.test_case),
    execution_id: String(execution.id),
  }
  if (failure?.step_path) query.step_path = failure.step_path
  if (failure?.case_revision) query.case_revision = failure.case_revision
  if (!failure?.case_revision_changed && Number.isInteger(failure?.step_index)) {
    query.step_index = String(failure.step_index)
  }
  router.push({ name: 'AppSceneBuilder', query })
}

// getDisplayStatus 已从 helpers 导入

const formatDuration = (seconds) => {
  if (!seconds) return '-'
  if (seconds < 60) return t('appAutomation.execution.durationSeconds', { n: Math.floor(seconds) })
  const minutes = Math.floor(seconds / 60)
  const secs = Math.floor(seconds % 60)
  return t('appAutomation.execution.durationMinutesSeconds', { m: minutes, s: secs })
}

// 自动刷新执行中的记录
const startAutoRefresh = () => {
  refreshTimer = setInterval(() => {
    // 如果有执行中的记录，自动刷新
    const hasRunning = executions.value.some(e => ['running', 'pending'].includes(e.status))
    if (hasRunning) {
      loadExecutions()
    }
  }, 5000) // 每5秒刷新一次
}

const stopAutoRefresh = () => {
  if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
}

onMounted(() => {
  getAppProjects({ page_size: 100 }).then(res => { projectList.value = res.data.results || res.data || [] }).catch(() => {})
  loadExecutions()
  startAutoRefresh()
})

watch(() => route.query.execution_id, executionId => {
  if (typeof executionId === 'string' && /^\d+$/.test(executionId)) {
    viewDiagnostics({ id: executionId })
  }
}, { immediate: true })

watch(() => [userStore.user?.id ?? userStore.user?.pk ?? null, userStore.accessToken], () => {
  diagnosticDialogVisible.value = false
  clearDiagnostic()
})

onUnmounted(() => {
  stopAutoRefresh()
  clearDiagnostic()
})
</script>

<style scoped lang="scss">
.execution-list {
  padding: 20px;
}

.toolbar {
  margin-bottom: 20px;
  
  .text-right {
    text-align: right;
  }
}

.table-card {
  .pagination {
    margin-top: 20px;
    display: flex;
    justify-content: flex-end;
  }
}

.step-stats {
  display: flex;
  gap: 8px;
  font-size: 12px;
  
  .stat-item {
    &.success { color: #67c23a; }
    &.danger { color: #f56c6c; }
  }
}

.error-content {
  max-height: 400px;
  overflow-y: auto;
  
  pre {
    background: #f5f7fa;
    padding: 15px;
    border-radius: 4px;
    font-family: 'Courier New', Courier, monospace;
    font-size: 13px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-wrap: break-word;
  }
}

.diagnostic-content {
  min-height: 80px;
}

.failure-diagnostic {
  margin-top: 18px;

  h3 {
    margin: 0 0 10px;
    font-size: 15px;
  }

  p {
    margin: 7px 0;
    overflow-wrap: anywhere;
  }
}

.diagnostic-empty {
  color: var(--el-text-color-secondary);
}

.diagnostic-screenshot {
  display: block;
  max-width: 100%;
  max-height: 380px;
  margin: 14px auto 0;
  object-fit: contain;
  background: var(--el-fill-color-light);
}
</style>
