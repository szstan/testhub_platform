<template>
  <div class="report-view">
    <div class="header">
      <h3>{{ $t('uiAutomation.report.title') }}</h3>
      <div class="actions">
        <el-select v-model="projectId" :aria-label="$t('uiAutomation.common.selectProject')" :placeholder="$t('uiAutomation.common.selectProject')" :loading="projectsLoading" :disabled="projectsLoading" style="width: 200px; margin-right: 15px">
          <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
        </el-select>
        <el-button type="primary" @click="refreshReports">
          <el-icon><Refresh /></el-icon>
          {{ $t('uiAutomation.report.refreshReport') }}
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="invalidProject || projectsError"
      :title="invalidProject ? t('uiAutomation.workbenchResults.projectUnavailable') : t('uiAutomation.workbenchResults.projectLoadFailed')"
      type="warning"
      show-icon
      :closable="false"
      class="project-alert"
    >
      <el-button link type="primary" :loading="projectsLoading" @click="reloadProjects">
        {{ t('uiAutomation.workbenchResults.reloadProjects') }}
      </el-button>
    </el-alert>

    <div v-if="hasExecutionLink" class="execution-link-state">
      <el-alert v-if="exactDetailState === 'loading'" :title="t('uiAutomation.workbenchResults.loadingExecution')" type="info" show-icon :closable="false" />
      <el-alert v-else-if="exactDetailState === 'invalid'" :title="t('uiAutomation.workbenchResults.invalidExecutionLink')" type="warning" show-icon :closable="false">
        <el-button link type="primary" @click="returnToReportList">{{ t('uiAutomation.workbenchResults.backToReports') }}</el-button>
      </el-alert>
      <el-alert v-else-if="exactDetailState === 'notFound'" :title="t('uiAutomation.workbenchResults.executionNotFound')" type="warning" show-icon :closable="false">
        <el-button link type="primary" @click="returnToReportList">{{ t('uiAutomation.workbenchResults.backToReports') }}</el-button>
      </el-alert>
      <el-alert v-else-if="exactDetailState === 'error'" :title="t('uiAutomation.workbenchResults.executionLoadFailed')" type="error" show-icon :closable="false">
        <el-button link type="primary" @click="retryExactReport">{{ t('uiAutomation.workbenchResults.retry') }}</el-button>
      </el-alert>
      <el-alert v-else-if="exactDetailState === 'ready'" :title="t('uiAutomation.workbenchResults.runStatus', { id: currentReport?.id, status: getStatusText(currentReport?.status) })" :type="getStatusType(currentReport?.status)" show-icon :closable="false">
        <div class="execution-link-actions">
          <span v-if="trackingState === 'polling'">{{ t('uiAutomation.workbenchResults.trackingActive') }}</span>
          <span v-else-if="trackingState === 'paused'">{{ t('uiAutomation.workbenchResults.trackingPaused') }}</span>
          <span v-else-if="trackingState === 'timeout'">{{ t('uiAutomation.workbenchResults.trackingTimedOut') }}</span>
          <el-button v-if="isRunActive(currentReport.status)" link type="warning" :loading="isAbortPending(currentReport)" @click="abortExecution(currentReport)">
            {{ t('uiAutomation.workbenchResults.abortExecution') }}
          </el-button>
          <el-button link type="primary" @click="openCurrentReport">{{ t('uiAutomation.workbenchResults.openReport') }}</el-button>
          <el-button v-if="trackingState === 'paused' || trackingState === 'timeout'" link type="primary" @click="resumeTracking">
            {{ t('uiAutomation.workbenchResults.resumeTracking') }}
          </el-button>
        </div>
      </el-alert>
    </div>

    <div v-if="projectId" class="content">
      <el-alert v-if="listError" :title="t('uiAutomation.report.messages.loadFailed')" type="error" :closable="false" show-icon>
        <el-button link @click="loadReports">{{ t('uiAutomation.workbenchResults.retry') }}</el-button>
      </el-alert>
      <el-table v-else :data="reports" v-loading="loading" style="width: 100%">
        <el-table-column prop="id" label="ID" width="80" />
        <el-table-column prop="test_suite_name" :label="$t('uiAutomation.report.testSuite')" min-width="200" />
        <el-table-column prop="status" :label="$t('uiAutomation.common.status')" width="120">
          <template #default="{ row }">
            <el-tag :type="getStatusType(row.status)">
              {{ getStatusText(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('uiAutomation.report.testEngine')" width="120">
          <template #default="{ row }">
            <el-tag size="small">{{ getEngineText(row.engine) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('uiAutomation.report.browser')" width="100">
          <template #default="{ row }">
            {{ getBrowserText(row.browser) }}
          </template>
        </el-table-column>
        <el-table-column prop="total_cases" :label="$t('uiAutomation.report.totalCases')" width="100" />
        <el-table-column prop="passed_cases" :label="$t('uiAutomation.report.passedCases')" width="100">
          <template #default="{ row }">
            <span style="color: #67c23a; font-weight: bold;">{{ row.passed_cases }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="failed_cases" :label="$t('uiAutomation.report.failedCases')" width="100">
          <template #default="{ row }">
            <span style="color: #f56c6c; font-weight: bold;">{{ row.failed_cases }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="$t('uiAutomation.report.passRate')" width="100">
          <template #default="{ row }">
            <el-progress
              :percentage="row.pass_rate"
              :color="getProgressColor(row.pass_rate)"
              :stroke-width="16"
            />
          </template>
        </el-table-column>
        <el-table-column :label="$t('uiAutomation.report.duration')" width="120">
          <template #default="{ row }">
            {{ formatDuration(row.duration) }}
          </template>
        </el-table-column>
        <el-table-column prop="executed_by_name" :label="$t('uiAutomation.report.executor')" width="120" />
        <el-table-column prop="created_at" :label="$t('uiAutomation.report.executionTime')" width="180">
          <template #default="{ row }">
            {{ formatDate(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column :label="$t('uiAutomation.common.operation')" width="250" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="viewReportDetail(row)">
              <el-icon><Document /></el-icon>
              {{ $t('uiAutomation.report.viewDetail') }}
            </el-button>
            <el-button v-if="isRunActive(row.status)" link type="warning" size="small" :loading="isAbortPending(row)" @click="abortExecution(row)">
              {{ t('uiAutomation.workbenchResults.abortExecution') }}
            </el-button>
            <el-button link type="danger" size="small" @click="deleteReport(row)">
              <el-icon><Delete /></el-icon>
              {{ $t('uiAutomation.common.delete') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-container">
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
    <el-empty v-else-if="!projectsLoading && !projectsError && !invalidProject && !hasExecutionLink" :description="$t(projects.length ? 'uiAutomation.common.selectProject' : 'uiAutomation.workbenchExecution.noProjects')" />

    <!-- 报告详情对话框 -->
    <el-dialog
      v-model="showDetailDialog"
      :title="$t('uiAutomation.report.reportDetail')"
      width="min(1100px, calc(100vw - 32px))"
      :close-on-click-modal="false"
    >
      <div v-if="currentReport" class="report-detail">
        <div v-if="trackingState !== 'idle'" class="tracking-notice">
          <p role="status">{{ t(`uiAutomation.workbenchResults.${trackingLabel}`) }}</p>
          <el-button v-if="isRunActive(currentReport.status) && trackingState !== 'polling'" link type="primary" @click="resumeTracking">
            {{ t('uiAutomation.workbenchResults.resumeTracking') }}
          </el-button>
        </div>
        <el-descriptions :column="2" border>
          <el-descriptions-item :label="$t('uiAutomation.report.reportId')">{{ currentReport.id }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.report.testSuite')">{{ currentReport.test_suite_name }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.report.executionStatus')">
            <el-tag :type="getStatusType(currentReport.status)">
              {{ getStatusText(currentReport.status) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.report.executor')">{{ currentReport.executed_by_name }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.report.testEngine')">{{ getEngineText(currentReport.engine) }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.report.browser')">{{ getBrowserText(currentReport.browser) }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.report.executionMode')">{{ currentReport.headless ? $t('uiAutomation.report.headlessMode') : $t('uiAutomation.report.headedMode') }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.report.duration')">{{ formatDuration(currentReport.duration) }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.report.startTime')">{{ formatDate(currentReport.started_at) }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.report.endTime')">{{ formatDate(currentReport.finished_at) }}</el-descriptions-item>
        </el-descriptions>

        <div v-if="rerunSource" class="rerun-origin">
          {{ t('uiAutomation.workbenchResults.rerunOrigin') }}
          <el-button link type="primary" @click="openRerunSource">#{{ rerunSource.id }}</el-button>
        </div>

        <div v-if="failureSummary" class="failure-summary">
          <h4>{{ $t('uiAutomation.workbenchResults.failureSummary') }}</h4>
          <el-descriptions :column="2" border>
            <el-descriptions-item v-if="failureSummary.caseName" :label="$t('uiAutomation.workbenchResults.failedCase')">
              {{ failureSummary.caseName }}
            </el-descriptions-item>
            <el-descriptions-item v-if="failureSummary.stage" :label="$t('uiAutomation.workbenchResults.failureStage')">
              {{ failureSummary.stage }}
            </el-descriptions-item>
            <el-descriptions-item v-if="failureSummary.step" :label="$t('uiAutomation.workbenchResults.firstFailedStep')">
              <span v-if="normalizeUiId(failureSummary.step.step_number)">{{ $t('uiAutomation.workbenchExecution.failedStep', { step: failureSummary.step.step_number }) }} · </span>
              {{ failureSummary.step.description || getActionText(failureSummary.step.action_type) }}
            </el-descriptions-item>
            <el-descriptions-item v-if="failureSummary.error" :label="$t('uiAutomation.report.errorInfo')" :span="2">
              <pre class="failure-error">{{ failureSummary.error }}</pre>
            </el-descriptions-item>
          </el-descriptions>
          <div v-if="failureSummary.screenshotUrl && !failedScreenshot" class="failure-screenshot">
            <img :src="failureSummary.screenshotUrl" :alt="$t('uiAutomation.workbenchResults.failureScreenshot')" @error="failedScreenshot = true" />
          </div>
          <p v-else>{{ $t(failedScreenshot ? 'uiAutomation.execution.imageLoadFailed' : 'uiAutomation.execution.noScreenshots') }}</p>
          <el-button v-if="failedCaseEditorLocation" link type="primary" @click="openFailedCaseInEditor">
            {{ $t('uiAutomation.workbenchResults.openFailedCase') }}
          </el-button>
          <el-button v-if="failureSummary.caseData" link type="primary" @click="viewCaseDetail(failureSummary.caseData)">
            {{ $t('uiAutomation.report.viewDetail') }}
          </el-button>
        </div>

        <div class="statistics-section">
          <h4>{{ $t('uiAutomation.report.testStatistics') }}</h4>
          <el-row :gutter="20">
            <el-col :span="6">
              <div class="stat-card">
                <div class="stat-label">{{ $t('uiAutomation.report.totalCases') }}</div>
                <div class="stat-value">{{ currentReport.total_cases }}</div>
              </div>
            </el-col>
            <el-col :span="6">
              <div class="stat-card success">
                <div class="stat-label">{{ $t('uiAutomation.report.passedCases') }}</div>
                <div class="stat-value">{{ currentReport.passed_cases }}</div>
              </div>
            </el-col>
            <el-col :span="6">
              <div class="stat-card danger">
                <div class="stat-label">{{ $t('uiAutomation.report.failedCases') }}</div>
                <div class="stat-value">{{ currentReport.failed_cases }}</div>
              </div>
            </el-col>
            <el-col :span="6">
              <div class="stat-card warning">
                <div class="stat-label">{{ $t('uiAutomation.report.skippedCases') }}</div>
                <div class="stat-value">{{ currentReport.skipped_cases }}</div>
              </div>
            </el-col>
          </el-row>

          <div class="pass-rate-chart">
            <h5>{{ $t('uiAutomation.report.passRate') }}: {{ currentReport.pass_rate }}%</h5>
            <el-progress
              :percentage="currentReport.pass_rate"
              :color="getProgressColor(currentReport.pass_rate)"
              :stroke-width="20"
            />
          </div>
        </div>

        <div class="result-section">
          <h4>{{ $t('uiAutomation.report.executionResultDetail') }}</h4>
          <el-table
            :data="getCaseExecutionList(currentReport)"
            border
            style="margin-top: 15px;"
          >
            <el-table-column type="index" :label="$t('uiAutomation.report.sequence')" width="60" />
            <el-table-column prop="test_case_name" :label="$t('uiAutomation.report.testCase')" min-width="200" />
            <el-table-column :label="$t('uiAutomation.report.executionStatus')" width="100" align="center">
              <template #default="{ row }">
                <el-tag :type="row.status === 'passed' ? 'success' : 'danger'">
                  {{ row.status === 'passed' ? $t('uiAutomation.report.casePassed') : $t('uiAutomation.report.caseFailed') }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="$t('uiAutomation.report.stepCount')" width="100" align="center">
              <template #default="{ row }">
                {{ row.steps ? row.steps.length : 0 }}
              </template>
            </el-table-column>
            <el-table-column :label="$t('uiAutomation.common.operation')" width="120" align="center">
              <template #default="{ row }">
                <el-button
                  type="primary"
                  link
                  @click="viewCaseDetail(row)"
                >
                  {{ $t('uiAutomation.report.viewDetail') }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>

        <div class="error-section" v-if="currentReport.error_message">
          <h4>{{ $t('uiAutomation.report.errorInfo') }}</h4>
          <div class="errors-container">
            <div class="error-item">
              <div class="error-content">
                <pre class="error-text">{{ currentReport.error_message }}</pre>
              </div>
            </div>
          </div>
        </div>
      </div>
      <template #footer>
        <el-button @click="showDetailDialog = false">{{ $t('uiAutomation.common.close') }}</el-button>
      </template>
    </el-dialog>

    <!-- 用例详情对话框 -->
    <el-dialog
      v-model="showCaseDetailDialog"
      :title="`${$t('uiAutomation.report.caseDetail')} - ${currentCase?.test_case_name || ''}`"
      width="min(900px, calc(100vw - 32px))"
      :close-on-click-modal="false"
      append-to-body
    >
      <div v-if="currentCase" class="case-detail">
        <!-- 用例执行成功 - 只显示执行日志 -->
        <div v-if="currentCase.status === 'passed'">
          <h4>{{ $t('uiAutomation.report.executionLogs') }}</h4>
          <div class="log-container">
            <div v-for="(step, index) in currentCase.steps" :key="index" class="log-item">
              <div class="log-header">
                <el-tag :type="step.success ? 'success' : 'danger'" size="small">
                  {{ $t('uiAutomation.report.step') }} {{ step.step_number }}
                </el-tag>
                <span class="log-action">{{ getActionText(step.action_type) }}</span>
                <span class="log-desc">{{ step.description }}</span>
              </div>
              <div v-if="step.error" class="log-error">
                <el-icon><WarningFilled /></el-icon>
                {{ step.error }}
              </div>
            </div>
          </div>
        </div>

        <!-- 用例执行失败 - 显示执行日志、失败截图、错误信息三个tab -->
        <div v-else>
          <el-tabs v-model="activeTab" type="border-card">
            <!-- 执行日志 Tab -->
            <el-tab-pane :label="$t('uiAutomation.report.executionLogs')" name="logs">
              <div class="log-container">
                <div v-for="(step, index) in currentCase.steps" :key="index" class="log-item">
                  <div class="log-header">
                    <el-tag :type="step.success ? 'success' : 'danger'" size="small">
                      {{ $t('uiAutomation.report.step') }} {{ step.step_number }}
                    </el-tag>
                    <span class="log-action">{{ getActionText(step.action_type) }}</span>
                    <span class="log-desc">{{ step.description }}</span>
                  </div>
                  <div v-if="step.error" class="log-error">
                    <el-icon><WarningFilled /></el-icon>
                    {{ step.error }}
                  </div>
                </div>
              </div>
            </el-tab-pane>

            <!-- 失败截图 Tab -->
            <el-tab-pane :label="$t('uiAutomation.report.failedScreenshots')" name="screenshots">
              <div v-if="currentCase.screenshots && currentCase.screenshots.length > 0" class="screenshot-container">
                <div v-for="(screenshot, index) in currentCase.screenshots" :key="index" class="screenshot-item">
                  <h5>{{ screenshot.description || `${$t('uiAutomation.report.screenshot')} ${index + 1}` }}</h5>
                  <img :src="screenshot.url" :alt="screenshot.description" class="screenshot-img" />
                  <p class="screenshot-time">{{ screenshot.timestamp }}</p>
                </div>
              </div>
              <el-empty v-else :description="$t('uiAutomation.report.noScreenshots')" />
            </el-tab-pane>

            <!-- 错误信息 Tab -->
            <el-tab-pane :label="$t('uiAutomation.report.errorInfo')" name="error">
              <div class="errors-container">
                <div v-if="currentCase.error" class="error-item">
                  <div class="error-content">
                    <pre class="error-text">{{ currentCase.error }}</pre>
                  </div>
                </div>
                <el-empty v-else :description="$t('uiAutomation.report.noError')" />
              </div>
            </el-tab-pane>
          </el-tabs>
        </div>
      </div>
      <template #footer>
        <el-button @click="showCaseDetailDialog = false">{{ $t('uiAutomation.common.close') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Document, Delete, WarningFilled } from '@element-plus/icons-vue'
import * as uiAutomationApi from '@/api/ui_automation'
import { getTestExecutions, getTestExecutionDetail, deleteTestExecution } from '@/api/ui_automation'
import { useUiProjectContext } from '@/composables/useUiProjectContext'
import { normalizeUiId, uiExecutionLocation } from '@/utils/ui-project-context.mjs'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const {
  projects,
  projectId,
  userId,
  projectsLoading,
  projectsError,
  invalidProject,
  reloadProjects
} = useUiProjectContext()

const reports = ref([])
const loading = ref(false)
const listError = ref(false)
const total = ref(0)
const pagination = reactive({ currentPage: 1, pageSize: 20 })
const showDetailDialog = ref(false)
const currentReport = ref(null)
const showCaseDetailDialog = ref(false)
const currentCase = ref(null)
const activeTab = ref('logs')
const exactDetailState = ref('idle')
const trackingState = ref('idle')
const hasExecutionLink = computed(() => route.name === 'UiReports' && route.query.executionId !== undefined)
const failureSummary = computed(() => buildFailureSummary(currentReport.value))
const failedCaseEditorLocation = computed(() => buildFailedCaseEditorLocation(failureSummary.value, currentReport.value, projectId.value))
const rerunSource = computed(() => buildRerunSource(currentReport.value, projectId.value))
const failedScreenshot = ref(false)
const abortingExecutionKeys = ref(new Set())
const trackingLabel = computed(() => ({ polling: 'trackingActive', paused: 'trackingPaused', timeout: 'trackingTimedOut' }[trackingState.value]))

let scopeVersion = 0
let listRequestVersion = 0
let exactRequestVersion = 0
let pollGeneration = 0
let pollTimer = null
let pollSession = null
const pollIntervalMs = 3000
const maxPolls = 40
const maxConfirmedReports = 100
let confirmedReportRevision = 0
const confirmedReports = new Map()

const isPositiveId = value => Boolean(normalizeUiId(value))

const responseBody = (response) => response?.data?.data ?? response?.data ?? response
const responseList = (response) => {
  const body = responseBody(response)
  if (Array.isArray(body)) return body
  if (Array.isArray(body?.results)) return body.results
  return null
}
const normalizeProjectId = (value) => value && typeof value === 'object' ? value.id : value
const isRunActive = status => ['PENDING', 'RUNNING'].includes(status)
const isUnavailable = error => [403, 404].includes(error.response?.status)
const reportProgress = report => ({ PENDING: 0, RUNNING: 1, SUCCESS: 2, FAILED: 2, ABORTED: 2 }[report?.status] ?? -1)
const reportKey = (project, id) => JSON.stringify([normalizeUiId(project), normalizeUiId(id)])
const abortKey = report => reportKey(projectId.value, report?.id)
const isAbortPending = report => abortingExecutionKeys.value.has(abortKey(report))

const rememberConfirmedReport = (record, scopedProjectId) => {
  const projectKey = normalizeUiId(scopedProjectId)
  const reportId = normalizeUiId(record?.id)
  if (!projectKey || !reportId || normalizeUiId(normalizeProjectId(record?.project)) !== projectKey) return
  const key = reportKey(projectKey, reportId)
  confirmedReports.delete(key)
  confirmedReports.set(key, { report: { ...record }, revision: ++confirmedReportRevision })
  // Keep only recent snapshots while this project is open.
  if (confirmedReports.size > maxConfirmedReports) {
    confirmedReports.delete(confirmedReports.keys().next().value)
  }
}

const mergeReportIntoRows = (rows, record, scopedProjectId, preferRecord = false) => {
  const projectKey = normalizeUiId(scopedProjectId)
  const reportId = normalizeUiId(record?.id)
  if (!projectKey || !reportId || String(projectId.value) !== String(scopedProjectId)) return rows
  if (normalizeUiId(normalizeProjectId(record?.project)) !== projectKey) return rows

  return rows.map(row => {
    if (normalizeUiId(row?.id) !== reportId) return row
    const rowProject = normalizeUiId(normalizeProjectId(row.project))
    if (rowProject && rowProject !== projectKey) return row
    const rowProgress = reportProgress(row)
    const recordProgress = reportProgress(record)
    if (rowProgress > recordProgress) return row
    if (recordProgress > rowProgress) return { ...row, ...record }

    const bothTerminal = rowProgress === 2 && recordProgress === 2
    const sameStatus = String(row.status).toUpperCase() === String(record.status).toUpperCase()
    if (bothTerminal && !sameStatus) return preferRecord ? { ...row, ...record } : row
    if (bothTerminal && !preferRecord) {
      const merged = { ...record, ...row }
      if (row.result_data == null && record.result_data != null) merged.result_data = record.result_data
      if (row.error_message == null && record.error_message != null) merged.error_message = record.error_message
      return merged
    }
    return preferRecord ? { ...row, ...record } : { ...record, ...row }
  })
}

const applyExactReport = (record, scopedProjectId) => {
  const previous = currentReport.value
  const confirmed = confirmedReports.get(reportKey(scopedProjectId, record.id))?.report
  if (normalizeUiId(previous?.id) === normalizeUiId(record.id) && reportProgress(previous) > reportProgress(record)) {
    record = previous
  }
  if (confirmed && reportProgress(confirmed) > reportProgress(record)) record = confirmed
  rememberConfirmedReport(record, scopedProjectId)
  reports.value = mergeReportIntoRows(reports.value, record, scopedProjectId, true)
  adoptAdvancedListReport(record, reports.value, scopedProjectId)
}

const adoptAdvancedListReport = (record, rows, scopedProjectId) => {
  const reportId = normalizeUiId(record.id)
  const row = rows.find(item => normalizeUiId(item.id) === reportId)
  if (reportProgress(row) > reportProgress(record)) {
    currentReport.value = { ...record, ...row }
    rememberConfirmedReport(currentReport.value, scopedProjectId)
  } else {
    currentReport.value = record
  }
}

const mergeCurrentExactReport = (rows, scopedProjectId, requestConfirmedRevision) => {
  const projectKey = normalizeUiId(scopedProjectId)
  const merged = rows.map(row => {
    const key = reportKey(projectKey, row.id)
    const entry = confirmedReports.get(key)
    if (!entry) return row

    const isStaleSnapshot = entry.revision > requestConfirmedRevision
    const reconciled = mergeReportIntoRows([row], entry.report, scopedProjectId, isStaleSnapshot)[0]
    const rowProgress = reportProgress(row)
    const confirmedProgress = reportProgress(entry.report)
    if (rowProgress > confirmedProgress || (!isStaleSnapshot && rowProgress === confirmedProgress)) {
      rememberConfirmedReport(reconciled, scopedProjectId)
    }
    return reconciled
  })

  const executionId = route.name === 'UiReports' ? normalizeUiId(route.query.executionId) : ''
  const current = currentReport.value
  if (executionId && normalizeUiId(current?.id) === executionId && normalizeUiId(normalizeProjectId(current.project)) === projectKey) {
    const entry = confirmedReports.get(reportKey(projectKey, executionId))
    if (entry) {
      currentReport.value = entry.report
      if (isRunActive(current.status) && !isRunActive(entry.report.status)) stopTracking()
    }
  }
  return merged
}

const stopTracking = () => {
  pollGeneration += 1
  if (pollTimer !== null) clearTimeout(pollTimer)
  pollTimer = null
  pollSession = null
  trackingState.value = 'idle'
}

const pauseTracking = () => {
  if (pollTimer !== null) clearTimeout(pollTimer)
  pollTimer = null
  if (!pollSession || !isRunActive(currentReport.value?.status)) {
    stopTracking()
    return
  }
  pollGeneration += 1
  trackingState.value = 'paused'
}

const clearExactReport = () => {
  stopTracking()
  exactRequestVersion += 1
  exactDetailState.value = 'idle'
  currentReport.value = null
  showDetailDialog.value = false
  showCaseDetailDialog.value = false
}

const loadReports = async () => {
  const requestedProjectId = projectId.value
  const requestedActorId = userId?.value
  const currentScope = scopeVersion
  const requestVersion = ++listRequestVersion
  const requestConfirmedRevision = confirmedReportRevision
  if (!isPositiveId(requestedProjectId)) {
    reports.value = []
    total.value = 0
    loading.value = false
    return
  }

  loading.value = true
  listError.value = false
  try {
    const response = await getTestExecutions({
      project: requestedProjectId,
      page: pagination.currentPage,
      page_size: pagination.pageSize
    })
    const resultList = responseList(response)
    if (currentScope !== scopeVersion || requestVersion !== listRequestVersion || String(projectId.value) !== String(requestedProjectId) || userId?.value !== requestedActorId) return
    if (!resultList) throw new Error('Invalid execution list response')

    const scopedReports = resultList.filter(report => {
      if (!report || typeof report !== 'object' || !isPositiveId(report.id)) return false
      if (report.project === undefined || report.project === null) return true
      return String(normalizeProjectId(report.project)) === String(requestedProjectId)
    })
    reports.value = mergeCurrentExactReport(scopedReports, requestedProjectId, requestConfirmedRevision)
    const body = responseBody(response)
    const count = body?.count
    total.value = Number.isSafeInteger(Number(count)) && Number(count) >= 0 ? Number(count) : reports.value.length
  } catch {
    if (currentScope === scopeVersion && requestVersion === listRequestVersion && userId?.value === requestedActorId) listError.value = true
  } finally {
    if (currentScope === scopeVersion && requestVersion === listRequestVersion && userId?.value === requestedActorId) loading.value = false
  }
}

const loadExactReport = async ({ preserveDialog = false, preserveKnown = false } = {}) => {
  const requestedExecutionId = normalizeUiId(route.query.executionId)
  const activeProjectId = projectId.value
  const currentScope = scopeVersion
  const requestVersion = ++exactRequestVersion
  const requestedActorId = userId?.value

  if (!isPositiveId(activeProjectId)) {
    stopTracking()
    return
  }
  if (!requestedExecutionId) {
    stopTracking()
    exactDetailState.value = 'invalid'
    currentReport.value = null
    showDetailDialog.value = false
    return
  }
  if (globalThis.document?.hidden) {
    exactDetailState.value = preserveKnown && currentReport.value ? 'ready' : 'idle'
    if (currentReport.value && isRunActive(currentReport.value.status)) trackingState.value = 'paused'
    return
  }

  stopTracking()
  exactDetailState.value = 'loading'
  if (!preserveKnown) currentReport.value = null
  if (!preserveDialog) showDetailDialog.value = false
  try {
    const response = await getTestExecutionDetail(requestedExecutionId)
    const record = responseBody(response)
    const returnedProjectId = normalizeUiId(normalizeProjectId(record?.project))
    if (currentScope !== scopeVersion || requestVersion !== exactRequestVersion || projectId.value !== activeProjectId || userId?.value !== requestedActorId) return
    if (normalizeUiId(record?.id) !== requestedExecutionId || returnedProjectId !== activeProjectId) {
      exactDetailState.value = 'notFound'
      currentReport.value = null
      showDetailDialog.value = false
      return
    }

    exactDetailState.value = 'ready'
    applyExactReport(record, activeProjectId)
    if (!preserveDialog || showDetailDialog.value) showDetailDialog.value = true
    if (isRunActive(currentReport.value.status)) startTracking()
  } catch (error) {
    if (currentScope !== scopeVersion || requestVersion !== exactRequestVersion || projectId.value !== activeProjectId || userId?.value !== requestedActorId) return
    if (isUnavailable(error)) {
      clearExactReport()
      exactDetailState.value = 'notFound'
    } else if (preserveKnown && currentReport.value) {
      exactDetailState.value = 'ready'
      trackingState.value = isRunActive(currentReport.value.status) ? 'paused' : 'idle'
    } else {
      exactDetailState.value = 'error'
      currentReport.value = null
      showDetailDialog.value = false
    }
  }
}

const startTracking = (immediate = false, preserveBudget = false) => {
  const record = currentReport.value
  if (!record || !isRunActive(record.status)) {
    stopTracking()
    return
  }
  const executionId = String(record.id)
  const requestedProjectId = String(projectId.value)
  const requestedActorId = userId?.value
  const key = reportKey(requestedProjectId, executionId)
  const previousAttempts = preserveBudget && pollSession?.key === key ? pollSession.attempts : 0
  stopTracking()

  const currentPollGeneration = ++pollGeneration
  const currentScope = scopeVersion
  const session = { key, attempts: previousAttempts }
  pollSession = session
  const isCurrentPoll = () => currentPollGeneration === pollGeneration &&
    pollSession === session &&
    currentScope === scopeVersion &&
    String(projectId.value) === requestedProjectId &&
    userId?.value === requestedActorId &&
    route.name === 'UiReports' &&
    normalizeUiId(route.query.executionId) === normalizeUiId(executionId)
  trackingState.value = globalThis.document?.hidden ? 'paused' : 'polling'

  const poll = async () => {
    pollTimer = null
    if (!isCurrentPoll()) return
    if (globalThis.document?.hidden) {
      pauseTracking()
      return
    }
    try {
      const response = await getTestExecutionDetail(executionId)
      const updated = responseBody(response)
      const returnedProjectId = normalizeProjectId(updated?.project)
      if (!isCurrentPoll()) return
      if (globalThis.document?.hidden) {
        pauseTracking()
        return
      }
      if (!updated || normalizeUiId(updated.id) !== normalizeUiId(executionId) || !isPositiveId(returnedProjectId) || String(returnedProjectId) !== requestedProjectId) {
        stopTracking()
        exactDetailState.value = 'notFound'
        currentReport.value = null
        showDetailDialog.value = false
        return
      }

      exactDetailState.value = 'ready'
      applyExactReport(updated, requestedProjectId)
      session.attempts += 1
      if (!isRunActive(currentReport.value.status)) {
        stopTracking()
        return
      }
      if (session.attempts >= maxPolls) {
        pollSession = null
        trackingState.value = 'timeout'
        pollTimer = null
        return
      }
      pollTimer = setTimeout(poll, pollIntervalMs)
    } catch (error) {
      if (!isCurrentPoll()) return
      if (isUnavailable(error)) {
        clearExactReport()
        exactDetailState.value = 'notFound'
      } else {
        trackingState.value = 'paused'
        pollTimer = null
      }
    }
  }

  if (globalThis.document?.hidden) return
  if (immediate) void poll()
  else pollTimer = setTimeout(poll, pollIntervalMs)
}

const onVisibilityChange = () => {
  if (globalThis.document?.hidden) {
    if (trackingState.value === 'polling') pauseTracking()
    return
  }
  if (currentReport.value) {
    if (isRunActive(currentReport.value.status) && pollSession && trackingState.value === 'paused') startTracking(true, true)
    return
  }
  if (hasExecutionLink.value && projectId.value) {
    void loadExactReport({ preserveKnown: Boolean(currentReport.value) })
  }
}

const retryExactReport = () => loadExactReport()
const resumeTracking = () => startTracking(true)
const openCurrentReport = () => {
  if (currentReport.value) showDetailDialog.value = true
}
const openRerunSource = () => {
  if (rerunSource.value) void router.push(rerunSource.value.location)
}
const openFailedCaseInEditor = () => {
  const location = failedCaseEditorLocation.value
  if (!location) return
  showCaseDetailDialog.value = false
  showDetailDialog.value = false
  void router.push(location)
}

const returnToReportList = async () => {
  const query = { ...route.query }
  delete query.executionId
  await router.replace({ name: 'UiReports', query })
}

const refreshReports = async () => {
  const exactId = route.query.executionId
  await Promise.all([
    loadReports(),
    exactId ? loadExactReport({ preserveDialog: true, preserveKnown: true }) : Promise.resolve()
  ])
  ElMessage.success(t('uiAutomation.report.messages.refreshed'))
}

const handleSizeChange = async () => {
  pagination.currentPage = 1
  await loadReports()
}
const handleCurrentChange = async () => loadReports()

const viewReportDetail = async (report) => {
  if (!isPositiveId(projectId.value) || !isPositiveId(report?.id)) return
  if (report.project !== undefined && report.project !== null && String(normalizeProjectId(report.project)) !== String(projectId.value)) return
  await router.push(uiExecutionLocation('UiReports', projectId.value, report.id))
}

const getCaseExecutionList = (report) => {
  const testCases = report?.result_data?.test_cases
  return Array.isArray(testCases) ? testCases.filter(testCase => testCase && typeof testCase === 'object') : []
}

const firstText = (...values) => {
  const value = values.find(candidate => typeof candidate === 'string' && candidate.trim())
  return value ? value.trim() : ''
}

const buildFailureSummary = (report) => {
  if (!['FAILED', 'ABORTED'].includes(report?.status)) return null
  const testCases = getCaseExecutionList(report)
  const isFailure = item => item && (item.success === false || ['failed', 'error'].includes(String(item.status).toLowerCase()))
  const failedCase = testCases.find(isFailure) || report.result_data?.failed_case || null
  const steps = Array.isArray(failedCase?.steps) ? failedCase.steps : []
  const failedStep = steps.find(isFailure)
  const resultData = report.result_data || {}
  const stage = firstText(failedCase?.failure_stage, failedCase?.failed_stage, failedCase?.stage, failedStep?.stage, resultData.failure_stage, report.failure_stage)
  const error = firstText(failedCase?.error, failedCase?.error_message, failedStep?.error, resultData.error_message, report.error_message)
  const screenshots = Array.isArray(failedCase?.screenshots) ? failedCase.screenshots : []
  const getScreenshotUrl = item => typeof item === 'string' ? item : firstText(item?.url, item?.image_url, item?.src, item?.data)
  const matched = failedStep?.step_number && screenshots.find(item => item?.step_number === failedStep.step_number && getScreenshotUrl(item))
  const screenshotUrl = getScreenshotUrl(matched) || screenshots.map(getScreenshotUrl).find(Boolean) || getScreenshotUrl(failedCase?.screenshot || failedCase?.screenshot_url)
  const caseName = firstText(failedCase?.test_case_name, failedCase?.name, resultData.failed_case_name, report.failed_case_name)
  if (!failedCase && !failedStep && !stage && !error && !screenshotUrl) return null
  return { caseData: failedCase, caseName, stage, step: failedStep, stepNumber: normalizeUiId(failedStep?.step_number || failedCase?.failed_step_number || resultData.failed_step_number), error, screenshotUrl }
}

function buildFailedCaseEditorLocation(summary, report, project) {
  const caseData = summary?.caseData
  const caseId = normalizeUiId(caseData?.test_case_id || caseData?.case_id || caseData?.test_case?.id)
  const executionId = normalizeUiId(report?.id)
  const projectIdValue = normalizeUiId(project)
  if (!caseId || !executionId || !projectIdValue) return null
  return {
    name: 'UiTestCases',
    query: {
      project: String(projectIdValue),
      caseId: String(caseId),
      ...(summary.stepNumber ? { stepNumber: String(summary.stepNumber) } : {}),
      fromExecutionId: String(executionId),
      fromKind: 'suite'
    }
  }
}

function buildRerunSource(report, project) {
  const id = normalizeUiId(report?.rerun_of)
  const kind = report?.rerun_kind
  const projectIdValue = normalizeUiId(normalizeProjectId(report?.project) || project)
  if (!id || !projectIdValue || !['case', 'suite'].includes(kind)) return null
  return {
    id,
    location: uiExecutionLocation(kind === 'suite' ? 'UiReports' : 'UiExecutions', projectIdValue, id)
  }
}

const viewCaseDetail = (caseData) => {
  if (!caseData || typeof caseData !== 'object') return
  currentCase.value = caseData
  activeTab.value = 'logs'
  showCaseDetailDialog.value = true
}

const getActionText = (actionType) => {
  const actionMap = {
    click: t('uiAutomation.actionTypes.click'),
    fill: t('uiAutomation.actionTypes.fill'),
    getText: t('uiAutomation.actionTypes.getText'),
    waitFor: t('uiAutomation.actionTypes.waitFor'),
    hover: t('uiAutomation.actionTypes.hover'),
    scroll: t('uiAutomation.actionTypes.scroll'),
    screenshot: t('uiAutomation.actionTypes.screenshot'),
    assert: t('uiAutomation.actionTypes.assert'),
    wait: t('uiAutomation.actionTypes.wait')
  }
  return actionMap[actionType] || actionType || t('uiAutomation.status.unknown')
}

const abortExecution = async (report) => {
  const executionId = normalizeUiId(report?.id)
  const requestedProject = normalizeUiId(projectId.value)
  const requestedActor = userId?.value
  const currentScope = scopeVersion
  if (!executionId || !requestedProject || !isRunActive(report?.status) || isAbortPending(report)) return
  try {
    await ElMessageBox.confirm(
      t('uiAutomation.workbenchResults.abortConfirm', { id: executionId }),
      t('uiAutomation.common.confirm'),
      { confirmButtonText: t('uiAutomation.workbenchResults.abortExecution'), cancelButtonText: t('uiAutomation.common.cancel'), type: 'warning' }
    )
    if (currentScope !== scopeVersion || normalizeUiId(projectId.value) !== requestedProject || userId?.value !== requestedActor) return
    const key = reportKey(requestedProject, executionId)
    abortingExecutionKeys.value = new Set([...abortingExecutionKeys.value, key])
    const abortRequest = Object.entries(uiAutomationApi).find(([name]) => name === 'abortTestExecution')?.[1]
    if (typeof abortRequest !== 'function') throw new Error(t('uiAutomation.workbenchResults.abortFailed'))
    await abortRequest(executionId)
    if (currentScope !== scopeVersion || normalizeUiId(projectId.value) !== requestedProject || userId?.value !== requestedActor) return
    ElMessage.success(t('uiAutomation.workbenchResults.abortRequested'))
    if (hasExecutionLink && normalizeUiId(route.query.executionId) === executionId) {
      await loadExactReport({ preserveDialog: true, preserveKnown: true })
    } else {
      await loadReports()
    }
  } catch (error) {
    if (error !== 'cancel' && currentScope === scopeVersion && userId?.value === requestedActor) {
      ElMessage.error(error.response?.data?.message || t('uiAutomation.workbenchResults.abortFailed'))
    }
  } finally {
    const key = reportKey(requestedProject, executionId)
    const next = new Set(abortingExecutionKeys.value)
    next.delete(key)
    abortingExecutionKeys.value = next
  }
}

const deleteReport = async (report) => {
  const currentScope = scopeVersion
  try {
    await ElMessageBox.confirm(
      t('uiAutomation.report.messages.deleteConfirm', { name: report.test_suite_name }),
      t('uiAutomation.report.messages.confirmDelete'),
      {
        confirmButtonText: t('uiAutomation.common.confirm'),
        cancelButtonText: t('uiAutomation.common.cancel'),
        type: 'warning'
      }
    )
    if (currentScope !== scopeVersion || !isPositiveId(report?.id)) return

    await deleteTestExecution(report.id)
    if (currentScope !== scopeVersion) return
    confirmedReports.delete(reportKey(projectId.value, report.id))
    ElMessage.success(t('uiAutomation.report.messages.deleteSuccess'))
    if (String(route.query.executionId) === String(report.id)) await returnToReportList()
    await loadReports()
  } catch (error) {
    if (error !== 'cancel' && currentScope === scopeVersion) {
      console.error('Failed to delete report:', error)
      ElMessage.error(t('uiAutomation.report.messages.deleteFailed'))
    }
  }
}

const getStatusType = (status) => {
  const typeMap = { PENDING: 'info', RUNNING: 'warning', SUCCESS: 'success', FAILED: 'danger', ABORTED: 'info' }
  return typeMap[String(status || '').toUpperCase()] || 'info'
}

const getStatusText = (status) => {
  const textMap = {
    PENDING: t('uiAutomation.report.statusPending'),
    RUNNING: t('uiAutomation.report.statusRunning'),
    SUCCESS: t('uiAutomation.report.statusSuccess'),
    FAILED: t('uiAutomation.report.statusFailed'),
    ABORTED: t('uiAutomation.report.statusAborted')
  }
  return textMap[String(status || '').toUpperCase()] || status || t('uiAutomation.status.unknown')
}

const getEngineText = (engine) => ({ playwright: 'Playwright', selenium: 'Selenium' }[engine] || engine || 'Playwright')
const getBrowserText = (browser) => ({ chrome: 'Chrome', firefox: 'Firefox', safari: 'Safari', edge: 'Edge' }[browser] || browser || 'Chrome')
const getProgressColor = (percentage) => {
  const value = Number(percentage) || 0
  if (value >= 80) return '#67c23a'
  if (value >= 60) return '#e6a23c'
  return '#f56c6c'
}
const formatDate = (dateString) => dateString ? new Date(dateString).toLocaleString() : '-'
const formatDuration = (seconds) => {
  const value = Number(seconds) || 0
  if (value < 60) return `${value.toFixed(1)}${t('uiAutomation.report.seconds')}`
  const minutes = Math.floor(value / 60)
  const remainder = (value % 60).toFixed(0)
  return `${minutes}${t('uiAutomation.report.minutes')}${remainder}${t('uiAutomation.report.seconds')}`
}

watch(() => failureSummary.value?.screenshotUrl, () => { failedScreenshot.value = false })

watch([projectId, () => userId?.value], ([nextProject]) => {
  scopeVersion += 1
  listRequestVersion += 1
  confirmedReports.clear()
  pagination.currentPage = 1
  reports.value = []
  total.value = 0
  loading.value = false
  listError.value = false
  if (nextProject) void loadReports()
}, { immediate: true })

watch([projectId, () => userId?.value, hasExecutionLink, () => route.query.executionId, projectsLoading, invalidProject], ([project, , hasLink]) => {
  clearExactReport()
  if (!hasLink) return
  if (project) void loadExactReport()
  else if (invalidProject.value || (!projectsLoading.value && !projectsError.value)) exactDetailState.value = 'invalid'
}, { immediate: true })

onMounted(() => globalThis.document?.addEventListener('visibilitychange', onVisibilityChange))
onBeforeUnmount(() => {
  globalThis.document?.removeEventListener('visibilitychange', onVisibilityChange)
  scopeVersion += 1
  listRequestVersion += 1
  exactRequestVersion += 1
  confirmedReports.clear()
  stopTracking()
})
</script>

<style scoped lang="scss">
.report-view {
  padding: 20px;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #f5f5f5;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  background: white;
  padding: 20px;
  border-radius: 4px;

  h3 {
    margin: 0;
    color: #303133;
    font-size: 24px;
  }

  .actions {
    display: flex;
    align-items: center;
  }
}

.execution-link-state {
  margin: 0 0 16px;

  .execution-link-actions {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 12px;
  }
}

.tracking-notice {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  align-items: center;
  padding: 8px 12px;
  margin-bottom: 12px;
  color: var(--el-text-color-regular);
  background: var(--el-color-info-light-9);
  border-radius: 4px;

  p {
    margin: 0;
  }
}

.project-alert {
  margin: 0 0 16px;
}

.content {
  flex: 1;
  overflow: auto;
  background: white;
  padding: 20px;
  border-radius: 4px;
}

.pagination-container {
  margin-top: 20px;
  display: flex;
  justify-content: flex-end;
}

// 报告详情样式
.report-detail {
  .rerun-origin {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 4px;
    margin-top: 12px;
    color: var(--el-text-color-secondary);
    font-size: 13px;
  }

  .failure-summary {
    margin: 24px 0 8px;
    padding: 16px;
    border: 1px solid #f3b8b8;
    border-left: 4px solid #f56c6c;
    background: #fff8f8;

    h4 {
      margin: 0 0 14px;
      color: #303133;
    }

    .failure-error {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
    }

    .failure-screenshot {
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 8px;
      margin-top: 14px;

      img {
        display: block;
        max-width: min(100%, 720px);
        max-height: 420px;
        object-fit: contain;
        border: 1px solid #dcdfe6;
      }
    }
  }

  .statistics-section {
    margin-top: 30px;

    h4 {
      margin: 0 0 20px 0;
      color: #303133;
    }

    .stat-card {
      background: #f5f7fa;
      padding: 20px;
      border-radius: 4px;
      text-align: center;

      &.success {
        background: #f0f9ff;
        border: 1px solid #b3e5fc;
      }

      &.danger {
        background: #fef0f0;
        border: 1px solid #fde2e2;
      }

      &.warning {
        background: #fdf6ec;
        border: 1px solid #faecd8;
      }

      .stat-label {
        font-size: 14px;
        color: #909399;
        margin-bottom: 10px;
      }

      .stat-value {
        font-size: 32px;
        font-weight: bold;
        color: #303133;
      }
    }

    .pass-rate-chart {
      margin-top: 30px;

      h5 {
        margin: 0 0 15px 0;
        color: #303133;
        font-size: 16px;
      }
    }
  }

  .result-section {
    margin-top: 30px;

    h4 {
      margin: 0 0 15px 0;
      color: #303133;
    }

    .result-data {
      background: #f5f7fa;
      padding: 15px;
      border-radius: 4px;
      max-height: 400px;
      overflow: auto;
      font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
      font-size: 12px;
      line-height: 1.5;
    }
  }

  .error-section {
    margin-top: 30px;

    h4 {
      margin: 0 0 15px 0;
      color: #303133;
    }
  }
}

// 统一的错误信息样式
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

// 用例详情样式
.case-detail {
  h4 {
    margin: 0 0 20px 0;
    color: #303133;
    font-size: 16px;
  }

  .log-container {
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
        align-items: center;
        gap: 8px;
        color: #f56c6c;
        background: #fef0f0;
        padding: 8px 12px;
        border-radius: 4px;
        margin-top: 8px;
        font-size: 14px;
      }
    }
  }

  .screenshot-container {
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

      .screenshot-img {
        max-width: 100%;
        border: 1px solid #dcdfe6;
        border-radius: 4px;
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.1);
      }

      .screenshot-time {
        margin: 10px 0 0 0;
        color: #909399;
        font-size: 12px;
      }
    }
  }
}
</style>
