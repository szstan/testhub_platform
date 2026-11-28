<template>
  <div class="page-container">
    <div class="page-header">
      <h1 class="page-title">{{ $t('uiAutomation.execution.title') }}</h1>
      <el-select
        v-model="projectId"
        :aria-label="$t('uiAutomation.common.selectProject')"
        :placeholder="$t('uiAutomation.common.selectProject')"
        :disabled="projectsLoading"
        style="width: 200px"
      >
        <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
      </el-select>
    </div>

    <div v-if="projectsLoading" class="card-container" role="status">{{ $t('uiAutomation.dashboard.loading') }}</div>
    <div v-else-if="projectsError" class="card-container" role="alert">
      <p>{{ $t('uiAutomation.project.messages.loadFailed') }}</p>
      <el-button @click="reloadProjects">{{ $t('uiAutomation.workbenchExecution.retry') }}</el-button>
    </div>
    <div v-else-if="!projectId" class="card-container">
      <el-empty :description="$t(invalidProject ? 'uiAutomation.workbenchExecution.invalidProject' : projects.length ? 'uiAutomation.common.selectProject' : 'uiAutomation.workbenchExecution.noProjects')">
        <RouterLink v-if="!projects.length" :to="{ name: 'UiProjects', query: { setup: 'project' } }">
          {{ $t('uiAutomation.project.newProject') }}
        </RouterLink>
      </el-empty>
    </div>
    <div v-else class="card-container">
      <div class="filter-bar">
        <el-form :inline="true" :model="queryParams" class="demo-form-inline">
          <el-form-item :label="$t('uiAutomation.common.search')">
            <el-input
              v-model="queryParams.search"
              :placeholder="$t('uiAutomation.execution.searchPlaceholder')"
              clearable
              @keyup.enter="handleSearch"
            >
              <template #prefix>
                <el-icon><Search /></el-icon>
              </template>
            </el-input>
          </el-form-item>
          <el-form-item :label="$t('uiAutomation.common.status')">
            <el-select v-model="queryParams.status" :placeholder="$t('uiAutomation.execution.statusFilter')" clearable>
              <el-option :label="$t('uiAutomation.status.pending')" value="pending" />
              <el-option :label="$t('uiAutomation.status.running')" value="running" />
              <el-option :label="$t('uiAutomation.status.passed')" value="passed" />
              <el-option :label="$t('uiAutomation.status.failed')" value="failed" />
              <el-option :label="$t('uiAutomation.status.error')" value="error" />
              <el-option :label="$t('uiAutomation.workbenchExecution.aborted')" value="aborted" />
            </el-select>
          </el-form-item>
          <el-form-item :label="$t('uiAutomation.execution.browserFilter')">
            <el-select v-model="queryParams.browser" :placeholder="$t('uiAutomation.execution.browserFilter')" clearable>
              <el-option label="Chrome" value="chrome" />
              <el-option label="Firefox" value="firefox" />
              <el-option label="Safari" value="safari" />
              <el-option label="Edge" value="edge" />
            </el-select>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" @click="handleSearch">{{ $t('uiAutomation.common.query') }}</el-button>
            <el-button @click="resetQuery">{{ $t('uiAutomation.common.reset') }}</el-button>
            <el-button
              type="danger"
              :disabled="selectedIds.length === 0"
              @click="handleBatchDelete"
            >
              {{ $t('uiAutomation.common.batchDelete') }}
            </el-button>
          </el-form-item>
        </el-form>
      </div>

      <div v-if="listError" role="alert" class="list-error">
        <p>{{ $t('uiAutomation.execution.messages.loadFailed') }}</p>
        <el-button size="small" @click="loadExecutions">{{ $t('uiAutomation.workbenchExecution.retry') }}</el-button>
      </div>
      <el-table v-else :data="executions" v-loading="loading" style="width: 100%" @selection-change="handleSelectionChange">
        <el-table-column type="selection" width="55" align="center" />
        <el-table-column prop="id" label="ID" width="80" align="center" />
        <el-table-column prop="test_case_name" :label="$t('uiAutomation.execution.caseName')" min-width="200">
          <template #default="{ row }">
            <RouterLink class="execution-link" :to="detailLocation(row.id)">
              {{ row.test_case_name }}
            </RouterLink>
          </template>
        </el-table-column>
        <el-table-column :label="$t('uiAutomation.execution.relatedObject')" width="100" align="center">
          <template #default="{ row }">
            <el-tag v-if="!row.test_suite" type="info" size="small">{{ $t('uiAutomation.execution.case') }}</el-tag>
            <el-tag v-else type="warning" size="small">{{ $t('uiAutomation.execution.suiteTag') }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" :label="$t('uiAutomation.execution.statusFilter')" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="getStatusType(row.status)" size="small">{{ getStatusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="engine" :label="$t('uiAutomation.execution.testEngine')" width="120" align="center">
          <template #default="{ row }">
            {{ getEngineText(row.engine) }}
          </template>
        </el-table-column>
        <el-table-column prop="headless" :label="$t('uiAutomation.execution.executionMode')" width="100" align="center">
          <template #default="{ row }">
            {{ row.headless ? $t('uiAutomation.execution.headlessMode') : $t('uiAutomation.execution.headedMode') }}
          </template>
        </el-table-column>
        <el-table-column prop="browser" :label="$t('uiAutomation.execution.browserFilter')" width="100" align="center">
          <template #default="{ row }">
            {{ getBrowserText(row.browser) }}
          </template>
        </el-table-column>
        <el-table-column prop="created_by_name" :label="$t('uiAutomation.execution.executor')" width="120" align="center" />
        <el-table-column prop="started_at" :label="$t('uiAutomation.execution.startTime')" width="180" align="center">
          <template #default="{ row }">
            {{ formatDateTime(row.started_at) }}
          </template>
        </el-table-column>
        <el-table-column prop="finished_at" :label="$t('uiAutomation.execution.endTime')" width="180" align="center">
          <template #default="{ row }">
            {{ formatDateTime(row.finished_at) }}
          </template>
        </el-table-column>
        <el-table-column :label="$t('uiAutomation.execution.duration')" width="120" align="center">
          <template #default="{ row }">
            {{ formatDuration(row.execution_time) }}
          </template>
        </el-table-column>
        <el-table-column :label="$t('uiAutomation.common.operation')" width="150" fixed="right" align="center">
          <template #default="{ row }">
            <RouterLink class="execution-link" :to="detailLocation(row.id)">
              {{ $t('uiAutomation.common.details') }}
            </RouterLink>
            <el-button
              v-if="row.status === 'failed' || row.status === 'error'"
              size="small"
              type="warning"
              link
              @click="showRerunDialog(row)"
            >
              <el-icon><Refresh /></el-icon>
              {{ $t('uiAutomation.common.rerun') }}
            </el-button>
            <el-button
              link
              type="danger"
              @click="handleDelete(row)"
            >
              {{ $t('uiAutomation.common.delete') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="!listError" class="pagination-container">
        <el-pagination
          v-model:current-page="pagination.currentPage"
          v-model:page-size="pagination.pageSize"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          :total="total"
          @size-change="handleSizeChange"
          @current-change="handleCurrentChange"
        />
      </div>
    </div>

    <!-- 执行详情对话框 -->
    <el-dialog :model-value="showDetailDialog" @update:model-value="onDetailVisibility" :title="$t('uiAutomation.execution.executionDetail')" width="min(900px, calc(100vw - 32px))">
      <div v-if="projectsLoading || detailState === 'loading'" role="status">
        {{ $t('uiAutomation.dashboard.loading') }}
      </div>
      <div v-else-if="projectsError || detailState === 'error'" role="alert">
        <p>{{ $t('uiAutomation.workbenchExecution.detailError') }}</p>
        <el-button @click="projectsError ? reloadProjects() : loadExecutionDetail()">
          {{ $t('uiAutomation.workbenchExecution.retry') }}
        </el-button>
      </div>
      <el-empty v-else-if="!currentExecution" :description="$t('uiAutomation.workbenchExecution.detailUnavailable')" />
      <div v-else class="execution-detail">
        <!-- 基本信息 -->
        <el-descriptions :column="2" border>
          <el-descriptions-item :label="$t('uiAutomation.execution.caseName')">{{ currentExecution.test_case_name }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.execution.statusFilter')">
            <el-tag :type="getStatusType(currentExecution.status)">{{ getStatusText(currentExecution.status) }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.execution.browserFilter')">{{ getBrowserText(currentExecution.browser) }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.execution.executor')">{{ currentExecution.created_by_name }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.execution.startTime')">{{ formatDateTime(currentExecution.started_at) }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.execution.endTime')">{{ formatDateTime(currentExecution.finished_at) }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.execution.duration')" :span="2">{{ formatDuration(currentExecution.execution_time) }}</el-descriptions-item>
        </el-descriptions>

        <section v-if="failure" class="failure-summary" :aria-label="$t('uiAutomation.workbenchExecution.failureTitle')">
          <h3>{{ failure.stepNumber ? $t('uiAutomation.workbenchExecution.failedStep', { step: failure.stepNumber }) : $t('uiAutomation.workbenchExecution.failureTitle') }}</h3>
          <p>{{ failure.message || $t('uiAutomation.execution.noError') }}</p>
          <figure v-if="failure.screenshotIndex >= 0 && !failedImages.has(failure.screenshotIndex)">
            <img
              :src="currentExecution.screenshots[failure.screenshotIndex].url"
              :alt="currentExecution.screenshots[failure.screenshotIndex].description || $t('uiAutomation.execution.screenshot')"
              class="failure-screenshot"
              @error="failedImages.add(failure.screenshotIndex)"
            />
          </figure>
          <p v-else class="muted-text">{{ $t(failure.screenshotIndex >= 0 ? 'uiAutomation.execution.imageLoadFailed' : 'uiAutomation.execution.noScreenshots') }}</p>
        </section>
        <div v-if="detailTrackingPaused" class="case-page-state" role="status">
          {{ t(isUiRunTerminal(currentExecution.status) ? 'uiAutomation.workbenchExecution.terminalDetailsPending' : 'uiAutomation.workbenchExecution.trackingPaused') }}
          <el-button link type="primary" @click="loadExecutionDetail(true)">{{ t('uiAutomation.workbenchExecution.reconnectRun') }}</el-button>
        </div>
        <el-button v-if="['pending', 'running'].includes(currentExecution.status)" class="status-refresh" @click="loadExecutionDetail(true)">
          {{ $t('uiAutomation.workbenchExecution.refreshStatus') }}
        </el-button>
        <el-button v-if="['pending', 'running'].includes(currentExecution.status)" type="danger" plain :loading="cancelling" @click="cancelExecution">
          {{ t('uiAutomation.workbenchExecution.cancelRun') }}
        </el-button>

        <!-- 执行结果选项卡 -->
        <el-tabs v-model="activeTab" class="execution-tabs" style="margin-top: 20px;">
          <!-- 执行日志 - 所有状态都显示 -->
          <el-tab-pane :label="$t('uiAutomation.execution.executionLogs')" name="logs">
            <div class="logs-container">
              <div v-if="executionLog.steps.length">
                <div v-for="(step, index) in executionLog.steps" :key="index" class="log-item">
                  <div class="log-header">
                    <el-tag :type="step.success ? 'success' : 'danger'" size="small">
                      {{ $t('uiAutomation.execution.step') }} {{ step.step_number }}
                    </el-tag>
                    <span class="log-action">{{ getActionText(step.action_type) }}</span>
                    <span class="log-desc">{{ step.description }}</span>
                  </div>
                  <div v-if="step.error" class="log-error">
                    <el-icon><WarningFilled /></el-icon>
                    <pre class="error-message">{{ step.error }}</pre>
                  </div>
                </div>
              </div>
              <pre v-else-if="executionLog.text" class="plain-log">{{ executionLog.text }}</pre>
              <el-empty v-else :description="$t('uiAutomation.execution.noLogs')" />
            </div>
          </el-tab-pane>

          <!-- 失败截图 - 仅失败或错误状态显示 -->
          <el-tab-pane :label="$t('uiAutomation.execution.failedScreenshots')" name="screenshots" v-if="currentExecution.status === 'failed' || currentExecution.status === 'error'">
            <div class="screenshots-container">
              <div v-if="currentExecution.screenshots && currentExecution.screenshots.length > 0">
                <div v-for="(screenshot, index) in currentExecution.screenshots" :key="index" class="screenshot-item">
                  <h5>{{ screenshot?.description || `${$t('uiAutomation.execution.screenshot')} ${index + 1}` }}</h5>
                  <div v-if="screenshot?.url && !failedImages.has(index)" class="screenshot-wrapper">
                    <img
                      :src="screenshot.url"
                      :alt="screenshot.description || $t('uiAutomation.execution.screenshot')"
                      class="screenshot-img"
                      @error="failedImages.add(index)"
                    />
                  </div>
                  <div v-else class="screenshot-error">
                    <el-icon><WarningFilled /></el-icon>
                    <span>{{ failedImages.has(index) ? $t('uiAutomation.execution.imageLoadFailed') : screenshot?.error || $t('uiAutomation.execution.noScreenshots') }}</span>
                  </div>
                  <p class="screenshot-time">{{ formatDateTime(screenshot?.timestamp) }}</p>
                </div>
              </div>
              <el-empty v-else :description="$t('uiAutomation.execution.noScreenshots')" />
            </div>
          </el-tab-pane>

          <!-- 错误信息 - 仅失败或错误状态显示 -->
          <el-tab-pane :label="$t('uiAutomation.execution.errorInfo')" name="error" v-if="currentExecution.status === 'failed' || currentExecution.status === 'error'">
            <div class="errors-container">
              <div v-if="currentExecution.error_message" class="error-item">
                <div class="error-content">
                  <pre class="error-text">{{ currentExecution.error_message }}</pre>
                </div>
              </div>
              <el-empty v-else :description="$t('uiAutomation.execution.noError')" />
            </div>
          </el-tab-pane>
        </el-tabs>
      </div>
      <template #footer>
        <RouterLink v-if="currentExecution?.test_case" class="el-button el-button--primary" :to="repairLocation(currentExecution)">{{ t('uiAutomation.workbenchExecution.editFailedCase') }}</RouterLink>
        <el-button v-if="failure" :disabled="rerunning" @click="showRerunDialog(currentExecution)">
          {{ $t('uiAutomation.common.rerun') }}
        </el-button>
        <el-button @click="onDetailVisibility(false)">{{ $t('uiAutomation.common.close') }}</el-button>
      </template>
    </el-dialog>

    <!-- 重跑测试用例对话框 -->
    <el-dialog v-model="showRerunDialogVisible" :title="$t('uiAutomation.execution.rerunTitle')" width="min(500px, calc(100vw - 32px))" append-to-body>
      <p v-if="rerunUnknown" class="list-error" role="status">{{ t('uiAutomation.workbenchExecution.submissionUnknown') }}</p>
      <p v-else-if="rerunReadinessLoading" role="status">{{ t('uiAutomation.workbenchExecution.checkingReadiness') }}</p>
      <p v-else-if="rerunReadinessError || rerunReadinessReason" class="list-error" role="alert">{{ rerunReadinessError ? t('uiAutomation.workbenchExecution.readinessUnavailable') : rerunReadinessReason }}</p>
      <el-form :model="rerunFormData" :disabled="rerunning || rerunUnknown || rerunReadinessLoading" label-width="100px">
        <el-form-item :label="$t('uiAutomation.execution.testEngine')">
          <el-select v-model="rerunFormData.engine">
            <el-option v-for="option in rerunEngineOptions" :key="option.value" :label="option.label" :value="option.value" :disabled="option.disabled" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.execution.browserFilter')">
          <el-select v-model="rerunFormData.browser" style="width: 100%">
            <el-option v-for="option in rerunBrowserOptions" :key="option.value" :label="option.label" :value="option.value" :disabled="option.disabled" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.execution.executionMode')">
          <el-radio-group v-model="rerunFormData.headless">
            <el-radio :label="false">{{ $t('uiAutomation.execution.headedMode') }}</el-radio>
            <el-radio :label="true">{{ $t('uiAutomation.execution.headlessMode') }}</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showRerunDialogVisible = false">{{ $t('uiAutomation.common.cancel') }}</el-button>
        <el-button type="primary" @click="handleRerun" :loading="rerunning" :disabled="rerunReadinessLoading || (!rerunUnknown && (rerunReadinessError || Boolean(rerunReadinessReason)))">{{ t(rerunUnknown ? 'uiAutomation.workbenchExecution.retrySameSubmission' : 'uiAutomation.execution.confirmRerun') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, onScopeDispose, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, WarningFilled, Refresh } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import { useUiProjectContext } from '@/composables/useUiProjectContext'
import { normalizeUiId, uiExecutionLocation } from '@/utils/ui-project-context.mjs'
import { caseExecutionFailure, readCaseExecutionLog } from '@/utils/ui-case-execution.mjs'
import { clearUiPendingRun, isUiRunActive, isUiSubmissionRejected, newUiSubmissionId, normalizeUiRunStatus, readUiPendingRun, uiPendingRunKey, writeUiPendingRun } from '@/utils/ui-execution-submission.mjs'
import { uiBrowserOptions, uiEngineOptions, uiReadinessError } from '@/utils/ui-execution-options.mjs'
import {
  getTestCaseExecutions,
  getTestCaseExecutionDetail,
  deleteTestCaseExecution,
  batchDeleteTestCaseExecutions,
  getTestCaseSubmission,
  abortTestCaseExecution,
  getExecutionReadiness,
  runTestCase
} from '@/api/ui_automation'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const { projects, projectId, userId, projectsLoading, projectsError, invalidProject, reloadProjects } = useUiProjectContext()
const executions = ref([])
const loading = ref(false)
const listError = ref(false)
const total = ref(0)
let listRequest = 0
let detailRequest = 0
let projectGeneration = 0
let detailPollTimer = null
let cancelRequest = 0
const detailTrackingPaused = ref(false)
const cancelling = ref(false)
const confirmedDetails = new Map()
const maxConfirmedDetails = 100
const rerunPendingByScope = new Map()
let rerunScopeKey = ''
const isUiRunTerminal = status => ['passed', 'failed', 'error', 'aborted'].includes(normalizeUiRunStatus(status))
const summarizeExecution = record => {
  const summary = {}
  for (const key of ['id', 'project', 'test_case']) {
    const id = normalizeUiId(record?.[key])
    if (id) summary[key] = id
  }
  for (const key of ['status', 'test_case_name', 'browser', 'engine', 'started_at', 'finished_at', 'created_at', 'created_by_name', 'error_message']) {
    if (typeof record?.[key] === 'string') summary[key] = record[key].slice(0, key === 'error_message' ? 1000 : 300)
  }
  summary.status = normalizeUiRunStatus(record?.status)
  if (typeof record?.headless === 'boolean') summary.headless = record.headless
  if (Number.isFinite(Number(record?.execution_time))) summary.execution_time = Number(record.execution_time)
  return summary
}
const rememberConfirmedDetail = record => {
  const summary = summarizeExecution(record)
  if (!summary.id || !isUiRunTerminal(summary.status)) return null
  confirmedDetails.delete(summary.id)
  confirmedDetails.set(summary.id, summary)
  while (confirmedDetails.size > maxConfirmedDetails) confirmedDetails.delete(confirmedDetails.keys().next().value)
  return summary
}
const pagination = reactive({
  currentPage: 1,
  pageSize: 20
})

// 搜索和筛选
const queryParams = reactive({
  search: '',
  status: '',
  browser: ''
})
const selectedIds = ref([])

const executionQuery = computed(() => route.query.executionId)
const showDetailDialog = computed(() => route.name === 'UiExecutions' && executionQuery.value !== undefined)
const detailState = ref('idle')
const activeTab = ref('logs')
const currentExecution = ref(null)
const failedImages = ref(new Set())
const executionLog = computed(() => readCaseExecutionLog(currentExecution.value?.execution_logs))
const failure = computed(() => caseExecutionFailure(currentExecution.value, executionLog.value))
const detailLocation = id => uiExecutionLocation('UiExecutions', projectId.value, id)

// 重跑对话框相关
const showRerunDialogVisible = ref(false)
const rerunning = ref(false)
const rerunUnknown = ref(false)
const rerunOrigin = ref(null)
const rerunReadiness = ref(null)
const rerunReadinessLoading = ref(false)
const rerunReadinessError = ref(false)
const rerunEngineOptions = computed(() => uiEngineOptions(rerunReadiness.value))
const rerunBrowserOptions = computed(() => uiBrowserOptions(rerunReadiness.value, rerunFormData.engine))
const rerunReadinessReason = computed(() => uiReadinessError(rerunReadiness.value, rerunFormData.engine, rerunFormData.browser))
let rerunPending = null
const rerunFormData = reactive({
  testCaseId: null,
  engine: 'playwright',
  browser: 'chrome',
  headless: true
})

// 格式化日期时间
const formatDateTime = (dateString) => {
  if (!dateString) return '-'
  const date = new Date(dateString)
  if (isNaN(date.getTime())) return '-'
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  })
}

// 格式化持续时间（execution_time单位是秒）
const formatDuration = (seconds) => {
  if (!seconds && seconds !== 0) return '-'

  const totalSeconds = Math.floor(seconds)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const secs = totalSeconds % 60

  if (hours > 0) {
    return `${hours}h ${minutes}m ${secs}s`
  } else if (minutes > 0) {
    return `${minutes}m ${secs}s`
  } else {
    return `${secs}s`
  }
}

// 获取状态样式
const getStatusType = (status) => {
  const statusMap = {
    'pending': 'info',
    'running': 'warning',
    'passed': 'success',
    'failed': 'danger',
    'error': 'danger'
  }
  return statusMap[status] || 'info'
}

// 获取状态文本
const getStatusText = (status) => {
  const statusMap = {
    'pending': t('uiAutomation.status.pending'),
    'running': t('uiAutomation.status.running'),
    'passed': t('uiAutomation.status.passed'),
    'failed': t('uiAutomation.status.failed'),
    'error': t('uiAutomation.status.error'),
    'aborted': t('uiAutomation.workbenchExecution.aborted')
  }
  return statusMap[status] || status
}

// 获取浏览器文本
const getBrowserText = (browser) => {
  const browserMap = {
    'chrome': 'Chrome',
    'firefox': 'Firefox',
    'safari': 'Safari',
    'edge': 'Edge'
  }
  return browserMap[browser] || browser || 'Chrome'
}

// 获取测试引擎文本
const getEngineText = (engine) => {
  const engineMap = {
    'playwright': 'Playwright',
    'selenium': 'Selenium'
  }
  return engineMap[engine] || engine || 'Playwright'
}

// 获取操作类型文本
const getActionText = (actionType) => {
  const actionMap = {
    'click': t('uiAutomation.actionTypes.click'),
    'fill': t('uiAutomation.actionTypes.fill'),
    'getText': t('uiAutomation.actionTypes.getText'),
    'waitFor': t('uiAutomation.actionTypes.waitFor'),
    'hover': t('uiAutomation.actionTypes.hover'),
    'scroll': t('uiAutomation.actionTypes.scroll'),
    'screenshot': t('uiAutomation.actionTypes.screenshot'),
    'assert': t('uiAutomation.actionTypes.assert'),
    'wait': t('uiAutomation.actionTypes.wait')
  }
  return actionMap[actionType] || actionType
}

async function loadExecutions() {
  const request = ++listRequest
  const generation = projectGeneration
  const actor = userId?.value
  const project = projectId.value
  loading.value = Boolean(project)
  listError.value = false
  if (!project) return
  try {
    const { data } = await getTestCaseExecutions({
      page: pagination.currentPage,
      page_size: pagination.pageSize,
      ...queryParams,
      project,
    })
    if (request !== listRequest || generation !== projectGeneration || projectId.value !== project || userId?.value !== actor) return
    const rows = Array.isArray(data) ? data : data?.results
    if (!Array.isArray(rows)) throw new Error('Invalid UI execution list')
    executions.value = rows.map(row => {
      const known = confirmedDetails.get(normalizeUiId(row.id))
      return known && isUiRunTerminal(known.status) ? { ...row, ...known } : row
    })
    total.value = data.count ?? rows.length
  } catch {
    if (request === listRequest && generation === projectGeneration && projectId.value === project && userId?.value === actor) listError.value = true
  } finally {
    if (request === listRequest && generation === projectGeneration && userId?.value === actor) loading.value = false
  }
}

async function loadExecutionDetail(preserve = false) {
  const request = ++detailRequest
  const generation = projectGeneration
  const actor = userId?.value
  if (detailPollTimer !== null) globalThis.clearTimeout?.(detailPollTimer)
  detailPollTimer = null
  const project = projectId.value
  const id = normalizeUiId(executionQuery.value)
  if (!preserve) {
    currentExecution.value = null
    detailState.value = 'idle'
    failedImages.value = new Set()
    activeTab.value = 'logs'
  }
  detailTrackingPaused.value = false
  if (!showDetailDialog.value || !project || !id) return
  if (!preserve) detailState.value = 'loading'
  try {
    const { data } = await getTestCaseExecutionDetail(id)
    if (request !== detailRequest || generation !== projectGeneration || projectId.value !== project || userId?.value !== actor || normalizeUiId(executionQuery.value) !== id) return
    if (normalizeUiId(data?.project) === project && normalizeUiId(data?.id) === id) {
      const record = { ...data, status: normalizeUiRunStatus(data.status) }
      const known = confirmedDetails.get(id)
      const current = currentExecution.value
      const currentTerminal = normalizeUiId(current?.id) === id && isUiRunTerminal(current.status)
      if (known && isUiRunTerminal(known.status) && isUiRunActive(record.status)) {
        currentExecution.value = currentTerminal ? current : { ...known, execution_logs: '', screenshots: [] }
        // A summary can confirm status, but cannot turn an old active response
        // into terminal evidence after the full detail has been released.
        detailTrackingPaused.value = !currentTerminal
      } else currentExecution.value = record
      const summary = rememberConfirmedDetail(currentExecution.value)
      executions.value = executions.value.map(row => row.id === id ? { ...row, ...(summary || summarizeExecution(currentExecution.value)) } : row)
      if (isUiRunActive(currentExecution.value.status)) {
        if (!globalThis.document?.hidden) detailPollTimer = globalThis.setTimeout(() => loadExecutionDetail(true), 3000)
      } else {
        const storageKey = uiPendingRunKey('case', actor, project, currentExecution.value.test_case)
        const inMemory = rerunPendingByScope.get(storageKey)
        const saved = readUiPendingRun(storageKey)
        if (inMemory?.execution_id === id) {
          rerunPendingByScope.delete(storageKey)
          clearUiPendingRun(storageKey)
          if (rerunScopeKey === storageKey && rerunPending === inMemory) {
            rerunPending = null
            rerunUnknown.value = false
          }
        } else if (!inMemory && saved?.execution_id === id) clearUiPendingRun(storageKey)
      }
    } else currentExecution.value = null
    detailState.value = 'idle'
  } catch (error) {
    if (request !== detailRequest || generation !== projectGeneration || projectId.value !== project || userId?.value !== actor || normalizeUiId(executionQuery.value) !== id) return
    if (preserve && currentExecution.value && ![403, 404].includes(error.response?.status)) detailTrackingPaused.value = true
    else {
      currentExecution.value = null
      detailState.value = [403, 404].includes(error.response?.status) ? 'idle' : 'error'
    }
  }
}

function repairLocation(execution) {
  const step = caseExecutionFailure(execution, readCaseExecutionLog(execution.execution_logs))?.stepNumber
  return { name: 'UiTestCases', query: { project: String(projectId.value), caseId: String(execution.test_case), fromExecutionId: String(execution.id), fromKind: 'case', ...(step ? { stepNumber: String(step) } : {}) } }
}

async function cancelExecution() {
  const project = projectId.value
  const id = currentExecution.value?.id
  if (!id || cancelling.value) return
  const request = ++cancelRequest
  cancelling.value = true
  try { await abortTestCaseExecution(id) }
  catch (error) { if (request === cancelRequest) ElMessage.error(error.response?.data?.error || t('uiAutomation.workbenchExecution.detailError')) }
  finally {
    if (request === cancelRequest) {
      cancelling.value = false
      if (projectId.value === project && currentExecution.value?.id === id) await loadExecutionDetail(true)
    }
  }
}

function onDetailVisibility(visible) {
  if (visible || !showDetailDialog.value) return
  const query = { ...route.query }
  delete query.executionId
  void router.replace({ name: 'UiExecutions', query, hash: route.hash })
}

function resetProjectScope() {
  if (detailPollTimer !== null) globalThis.clearTimeout?.(detailPollTimer)
  detailPollTimer = null
  listRequest += 1
  detailRequest += 1
  confirmedDetails.clear()
  rerunPending = null
  rerunScopeKey = ''
  rerunning.value = false
  rerunUnknown.value = false
  rerunReadinessLoading.value = false
  cancelRequest += 1
  cancelling.value = false
  projectGeneration += 1
  currentExecution.value = null
  detailState.value = 'idle'
  detailTrackingPaused.value = false
  executions.value = []
  total.value = 0
  selectedIds.value = []
  showRerunDialogVisible.value = false
  resetQuery()
}

// 搜索处理
const handleSearch = () => {
  pagination.currentPage = 1
  loadExecutions()
}

// 重置查询
const resetQuery = () => {
  queryParams.search = ''
  queryParams.status = ''
  queryParams.browser = ''
  pagination.currentPage = 1
  loadExecutions()
}

// 分页处理
const handleSizeChange = (val) => {
  pagination.pageSize = val
  pagination.currentPage = 1
  loadExecutions()
}

const handleCurrentChange = (val) => {
  pagination.currentPage = val
  loadExecutions()
}

// 表格多选
const handleSelectionChange = (selection) => {
  selectedIds.value = selection.map(item => item.id)
}

// 删除单个执行记录
const handleDelete = (row) => {
  ElMessageBox.confirm(t('uiAutomation.execution.messages.deleteConfirm'), t('uiAutomation.messages.confirm.tip'), {
    confirmButtonText: t('uiAutomation.common.confirm'),
    cancelButtonText: t('uiAutomation.common.cancel'),
    type: 'warning'
  }).then(async () => {
    try {
      await deleteTestCaseExecution(row.id)
      ElMessage.success(t('uiAutomation.execution.messages.deleteSuccess'))
      loadExecutions()
    } catch (error) {
      console.error('删除失败:', error)
      ElMessage.error(t('uiAutomation.execution.messages.deleteFailed'))
    }
  })
}

// 批量删除执行记录
const handleBatchDelete = () => {
  const ids = [...selectedIds.value]
  if (ids.length === 0) return

  ElMessageBox.confirm(t('uiAutomation.execution.messages.batchDeleteConfirm', { count: ids.length }), t('uiAutomation.messages.confirm.tip'), {
    confirmButtonText: t('uiAutomation.common.confirm'),
    cancelButtonText: t('uiAutomation.common.cancel'),
    type: 'warning'
  }).then(async () => {
    try {
      await batchDeleteTestCaseExecutions(ids)
      ElMessage.success(t('uiAutomation.execution.messages.batchDeleteSuccess'))
      selectedIds.value = []
      loadExecutions()
    } catch (error) {
      console.error('批量删除失败:', error)
      ElMessage.error(t('uiAutomation.execution.messages.batchDeleteFailed'))
    }
  })
}

// 显示重跑对话框
const showRerunDialog = async execution => {
  if (rerunning.value || !execution || normalizeUiId(execution.project) !== projectId.value) return
  const project = projectId.value
  const actor = userId?.value
  const generation = projectGeneration
  rerunOrigin.value = execution.id
  rerunFormData.testCaseId = execution.test_case
  const storageKey = uiPendingRunKey('case', userId?.value, project, execution.test_case)
  if (!storageKey) return
  rerunScopeKey = storageKey
  rerunPending = rerunPendingByScope.get(storageKey) || readUiPendingRun(storageKey)
  if (rerunPending) rerunPendingByScope.set(storageKey, rerunPending)
  rerunUnknown.value = Boolean(rerunPending)
  rerunOrigin.value = rerunPending?.rerun_of || execution.id
  rerunFormData.engine = rerunPending?.engine || execution.engine || 'playwright'
  rerunFormData.browser = rerunPending?.browser || execution.browser || 'chrome'
  rerunFormData.headless = rerunPending?.headless ?? execution.headless ?? true
  showRerunDialogVisible.value = true
  rerunReadinessLoading.value = true
  rerunReadinessError.value = false
  try {
    const { data } = await getExecutionReadiness(project)
    if (projectGeneration !== generation || projectId.value !== project || userId?.value !== actor) return
    rerunReadiness.value = data
    const available = uiEngineOptions(data).filter(item => !item.disabled)
    if (!rerunPending && available.length && !available.some(item => item.value === rerunFormData.engine)) rerunFormData.engine = available[0].value
    const browsers = uiBrowserOptions(data, rerunFormData.engine).filter(item => !item.disabled)
    if (!rerunPending && browsers.length && !browsers.some(item => item.value === rerunFormData.browser)) rerunFormData.browser = browsers[0].value
  } catch { if (projectGeneration === generation && userId?.value === actor) rerunReadinessError.value = true }
  finally { if (projectGeneration === generation && userId?.value === actor) rerunReadinessLoading.value = false }
}

const handleRerun = async () => {
  const caseId = normalizeUiId(rerunFormData.testCaseId)
  if (!caseId || rerunning.value || !projectId.value || rerunReadinessLoading.value) return
  if (!rerunPending && (rerunReadinessError.value || rerunReadinessReason.value)) return
  const project = projectId.value
  const actor = userId?.value
  const generation = projectGeneration
  const storageKey = uiPendingRunKey('case', actor, project, caseId)
  if (!storageKey) return
  const pending = rerunPendingByScope.get(storageKey) || (rerunScopeKey === storageKey ? rerunPending : null) || { client_request_id: newUiSubmissionId(), engine: rerunFormData.engine, browser: rerunFormData.browser, headless: rerunFormData.headless, rerun_of: rerunOrigin.value, rerun_kind: 'case' }
  rerunPendingByScope.set(storageKey, pending)
  rerunScopeKey = storageKey
  rerunPending = pending
  const isCurrentContext = () => generation === projectGeneration && projectId.value === project && userId?.value === actor && route.name === 'UiExecutions' && rerunScopeKey === storageKey
  const isCurrent = () => isCurrentContext() && rerunPendingByScope.get(storageKey) === pending
  writeUiPendingRun(storageKey, pending)
  rerunning.value = true
  const open = async id => {
    if (rerunPendingByScope.get(storageKey) === pending) {
      pending.execution_id = id
      writeUiPendingRun(storageKey, pending)
    }
    if (!isCurrent()) return
    rerunUnknown.value = false
    showRerunDialogVisible.value = false
    await router.push(detailLocation(id))
    void loadExecutions()
  }
  try {
    if (rerunUnknown.value || pending.execution_id) {
      if (pending.execution_id) { await open(pending.execution_id); return }
      try {
        const { data } = await getTestCaseSubmission(caseId, { client_request_id: pending.client_request_id })
        const id = normalizeUiId(data?.execution_id)
        if (id) { await open(id); return }
      } catch (error) { if (error.response?.status !== 404) throw error }
    }
    const { data } = await runTestCase(caseId, { ...pending, project_id: project })
    if (isUiSubmissionRejected(data)) throw Object.assign(new Error(data.message || data.error), { response: { status: 200, data } })
    const id = normalizeUiId(data?.execution_id)
    if (id) await open(id)
    else throw new Error(t('uiAutomation.workbenchExecution.submissionUnknown'))
  } catch (error) {
    const data = error.response?.data || {}
    const id = normalizeUiId(data.execution_id)
    const rejected = isUiSubmissionRejected(data)
    if (id && !rejected) { await open(id); return }
    if (rejected && id) { await open(id); return }
    const current = isCurrent()
    const definitive = rejected || (error.response && error.response.status < 500)
    if (definitive && rerunPendingByScope.get(storageKey) === pending) {
      rerunPendingByScope.delete(storageKey)
      clearUiPendingRun(storageKey)
      if (rerunScopeKey === storageKey && rerunPending === pending) rerunPending = null
    }
    if (!current) return
    if (definitive) {
      rerunUnknown.value = false
      ElMessage.error(data.message || data.error || data.detail || t('uiAutomation.execution.messages.rerunFailed'))
    } else {
      rerunUnknown.value = true
      try {
        const { data } = await getTestCaseSubmission(caseId, { client_request_id: pending.client_request_id })
        const recovered = normalizeUiId(data?.execution_id)
        if (recovered) await open(recovered)
      } catch { /* The same persisted identity remains available for explicit retry. */ }
    }
  } finally { if (isCurrentContext()) rerunning.value = false }
}

watch([projectId, () => userId?.value], resetProjectScope, { immediate: true })
watch([projectId, () => userId?.value, executionQuery], () => loadExecutionDetail(), { immediate: true })
const onVisibility = () => {
  if (globalThis.document?.hidden) {
    if (detailPollTimer !== null) globalThis.clearTimeout?.(detailPollTimer)
    detailPollTimer = null
  } else if (currentExecution.value && isUiRunActive(currentExecution.value.status)) void loadExecutionDetail(true)
}
onMounted(() => globalThis.document?.addEventListener('visibilitychange', onVisibility))
onScopeDispose(() => {
  if (detailPollTimer !== null) globalThis.clearTimeout?.(detailPollTimer)
  globalThis.document?.removeEventListener('visibilitychange', onVisibility)
  listRequest += 1
  detailRequest += 1
  projectGeneration += 1
  currentExecution.value = null
  confirmedDetails.clear()
  rerunPendingByScope.clear()
})
</script>

<style scoped lang="scss">
.page-container {
  padding: 20px;
  height: 100%;
  overflow-y: auto;
  background: #f5f5f5;
}

.page-header {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  background: white;
  padding: 20px;
  border-radius: 4px;
}

.page-title {
  margin: 0;
  font-size: 24px;
  color: #303133;
}

.card-container {
  background-color: #fff;
  border-radius: 8px;
  padding: 20px;
  box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
}

.filter-bar {
  margin-bottom: 20px;
}

.pagination-container {
  margin-top: 20px;
  display: flex;
  justify-content: flex-end;
}

.execution-link {
  color: var(--el-color-primary);
  margin-inline-end: 8px;
  text-decoration: underline;
  text-underline-offset: 2px;
}

.list-error,
.failure-summary {
  border: 1px solid var(--el-color-danger-light-7);
  border-radius: 6px;
  padding: 12px 16px;
  background: var(--el-color-danger-light-9);
}

.failure-summary,
.status-refresh {
  margin-top: 16px;
}

.failure-summary h3,
.failure-summary figure {
  margin: 0;
}

.failure-summary p,
.plain-log {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.failure-screenshot {
  display: block;
  max-width: 100%;
  max-height: 220px;
  object-fit: contain;
}

.muted-text {
  color: var(--el-text-color-secondary);
}

.execution-detail {
  .execution-tabs {
    margin-top: 20px;
  }

  .logs-container {
    max-height: 500px;
    overflow-y: auto;
    background: #f5f7fa;
    padding: 15px;
    border-radius: 4px;

    .log-item {
      margin-bottom: 15px;
      padding: 12px;
      background: white;
      border-radius: 4px;
      border-left: 3px solid #409eff;

      &:last-child {
        margin-bottom: 0;
      }

      .log-header {
        display: flex;
        align-items: center;
        gap: 10px;
        margin-bottom: 8px;

        .log-action {
          font-weight: 500;
          color: #606266;
        }

        .log-desc {
          color: #909399;
          font-size: 14px;
        }
      }

      .log-error {
        display: flex;
        align-items: flex-start;  /* 改为 flex-start，适配多行文本 */
        gap: 8px;
        color: #f56c6c;
        background: #fef0f0;
        padding: 8px 12px;
        border-radius: 4px;
        margin-top: 8px;
        font-size: 14px;

        .error-message {
          margin: 0;
          padding: 0;
          font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
          font-size: 13px;
          line-height: 1.6;
          white-space: pre-wrap;  /* 保留换行符和空格 */
          word-break: break-word;  /* 长单词换行 */
          flex: 1;
        }

        .el-icon {
          margin-top: 2px;  /* 图标与文本顶部对齐 */
          flex-shrink: 0;  /* 图标不缩小 */
        }
      }
    }
  }

  .screenshots-container {
    max-height: 600px;
    overflow-y: auto;
    padding: 10px;

    .screenshot-item {
      margin-bottom: 30px;
      text-align: center;

      h5 {
        margin: 0 0 15px 0;
        color: #303133;
        font-size: 14px;
      }

      .screenshot-wrapper {
        position: relative;
      }

      .screenshot-img {
        max-width: 100%;
        border: 1px solid #dcdfe6;
        border-radius: 4px;
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.1);
      }

      .screenshot-error {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 12px 20px;
        background: #fef0f0;
        color: #f56c6c;
        border: 1px solid #fbc4c4;
        border-radius: 4px;
        font-size: 14px;

        .el-icon {
          font-size: 16px;
        }
      }

      .img-load-error {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 12px 20px;
        background: #fff7e6;
        color: #e6a23c;
        border: 1px solid #f5dab1;
        border-radius: 4px;
        font-size: 14px;
        margin-top: 10px;

        i {
          font-size: 16px;
        }
      }

      .screenshot-time {
        margin: 10px 0 0 0;
        color: #909399;
        font-size: 12px;
      }
    }
  }

  .errors-container {
    padding: 10px;
    height: 100%;
    overflow-y: auto;
  }

  .error-item {
    background: #fff;
    border: 2px solid #f56c6c;
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 15px;
  }

  .error-item:last-child {
    margin-bottom: 0;
  }

  .error-content {
    display: flex;
    flex-direction: column;
  }

  .error-text {
    margin: 0;
    padding: 15px;
    background: #2d2d2d;
    color: #ff6b6b;
    border-radius: 4px;
    font-family: 'Courier New', Courier, monospace;
    font-size: 13px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-wrap: break-word;
    overflow-x: auto;
  }

  .error-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 15px;
    padding-bottom: 15px;
    border-bottom: 1px solid #f5f5f5;
  }

  .error-header .el-tag {
    font-size: 16px;
    padding: 10px 15px;
    font-weight: 600;
  }
}
</style>
