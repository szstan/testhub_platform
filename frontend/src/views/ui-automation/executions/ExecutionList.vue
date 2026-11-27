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
        <el-button v-else-if="['pending', 'running'].includes(currentExecution.status)" class="status-refresh" @click="loadExecutionDetail">
          {{ $t('uiAutomation.workbenchExecution.refreshStatus') }}
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
        <el-button v-if="failure" :disabled="rerunning" @click="showRerunDialog(currentExecution)">
          {{ $t('uiAutomation.common.rerun') }}
        </el-button>
        <el-button @click="onDetailVisibility(false)">{{ $t('uiAutomation.common.close') }}</el-button>
      </template>
    </el-dialog>

    <!-- 重跑测试用例对话框 -->
    <el-dialog v-model="showRerunDialogVisible" :title="$t('uiAutomation.execution.rerunTitle')" width="min(500px, calc(100vw - 32px))" append-to-body>
      <el-form :model="rerunFormData" label-width="100px">
        <el-form-item :label="$t('uiAutomation.execution.testEngine')">
          <el-radio-group v-model="rerunFormData.engine">
            <el-radio label="playwright">Playwright</el-radio>
            <el-radio label="selenium">Selenium</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.execution.browserFilter')">
          <el-select v-model="rerunFormData.browser" style="width: 100%">
            <el-option label="Chrome" value="chrome" />
            <el-option label="Firefox" value="firefox" />
            <el-option label="Safari" value="safari" />
            <el-option label="Edge" value="edge" />
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
        <el-button type="primary" @click="handleRerun" :loading="rerunning">{{ $t('uiAutomation.execution.confirmRerun') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onScopeDispose, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, WarningFilled, Refresh } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import { useUiProjectContext } from '@/composables/useUiProjectContext'
import { normalizeUiId, uiExecutionLocation } from '@/utils/ui-project-context.mjs'
import { caseExecutionFailure, readCaseExecutionLog } from '@/utils/ui-case-execution.mjs'
import {
  getTestCaseExecutions,
  getTestCaseExecutionDetail,
  deleteTestCaseExecution,
  batchDeleteTestCaseExecutions,
  runTestCase
} from '@/api/ui_automation'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const { projects, projectId, projectsLoading, projectsError, invalidProject, reloadProjects } = useUiProjectContext()
const executions = ref([])
const loading = ref(false)
const listError = ref(false)
const total = ref(0)
let listRequest = 0
let detailRequest = 0
let projectGeneration = 0
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
const rerunFormData = reactive({
  testCaseId: null,
  engine: 'playwright',
  browser: 'chrome',
  headless: false
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
    'error': t('uiAutomation.status.error')
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
    if (request !== listRequest || projectId.value !== project) return
    const rows = Array.isArray(data) ? data : data?.results
    if (!Array.isArray(rows)) throw new Error('Invalid UI execution list')
    executions.value = rows
    total.value = data.count ?? rows.length
  } catch {
    if (request === listRequest && projectId.value === project) listError.value = true
  } finally {
    if (request === listRequest) loading.value = false
  }
}

async function loadExecutionDetail() {
  const request = ++detailRequest
  const project = projectId.value
  const id = normalizeUiId(executionQuery.value)
  currentExecution.value = null
  detailState.value = 'idle'
  failedImages.value = new Set()
  activeTab.value = 'logs'
  if (!showDetailDialog.value || !project || !id) return
  detailState.value = 'loading'
  try {
    const { data } = await getTestCaseExecutionDetail(id)
    if (request !== detailRequest || projectId.value !== project) return
    if (normalizeUiId(data?.project) === project && normalizeUiId(data?.id) === id) {
      currentExecution.value = data
    }
    detailState.value = 'idle'
  } catch (error) {
    if (request !== detailRequest || projectId.value !== project) return
    detailState.value = [403, 404].includes(error.response?.status) ? 'idle' : 'error'
  }
}

function onDetailVisibility(visible) {
  if (visible || !showDetailDialog.value) return
  const query = { ...route.query }
  delete query.executionId
  void router.replace({ name: 'UiExecutions', query, hash: route.hash })
}

function resetProjectScope() {
  projectGeneration += 1
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
const showRerunDialog = (execution) => {
  if (rerunning.value || !execution || normalizeUiId(execution.project) !== projectId.value) return
  rerunFormData.testCaseId = execution.test_case
  rerunFormData.engine = execution.engine || 'playwright'
  rerunFormData.browser = execution.browser || 'chrome'
  rerunFormData.headless = execution.headless || false
  showRerunDialogVisible.value = true
}

// 执行重跑
const handleRerun = async () => {
  if (!rerunFormData.testCaseId) {
    ElMessage.error(t('uiAutomation.execution.messages.invalidCaseId'))
    return
  }

  if (rerunning.value || !projectId.value) return
  const project = projectId.value
  const generation = projectGeneration
  const isCurrent = () => generation === projectGeneration && projectId.value === project && route.name === 'UiExecutions'
  rerunning.value = true
  let result
  try {
    const response = await runTestCase(rerunFormData.testCaseId, {
      project_id: project,
      engine: rerunFormData.engine,
      browser: rerunFormData.browser,
      headless: rerunFormData.headless
    })
    result = response.data
    if (!isCurrent()) return
    if (result.success) {
      ElMessage.success(t('uiAutomation.execution.messages.rerunSuccess'))
    } else {
      ElMessage.warning(t('uiAutomation.execution.messages.rerunCompleteWithFailure') + ': ' + (result.errors?.[0]?.message || t('uiAutomation.execution.messages.viewDetails')))
    }
  } catch (error) {
    result = error.response?.data
    if (!isCurrent()) return
    ElMessage.error(t('uiAutomation.execution.messages.rerunFailed') + ': ' + (result?.message || error.message || t('uiAutomation.messages.error.unknown')))
  } finally {
    rerunning.value = false
    if (isCurrent()) {
      showRerunDialogVisible.value = false
      void loadExecutions()
      const id = normalizeUiId(result?.execution_id)
      if (id) void router.push(detailLocation(id))
    }
  }
}

watch(projectId, resetProjectScope, { immediate: true })
watch([projectId, executionQuery], loadExecutionDetail, { immediate: true })
onScopeDispose(() => {
  listRequest += 1
  detailRequest += 1
  projectGeneration += 1
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
