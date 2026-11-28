<template>
  <div class="page-container">
    <div class="page-header">
      <h1 class="page-title">{{ $t('uiAutomation.suite.title') }}</h1>
      <el-select v-model="projectId" :aria-label="$t('uiAutomation.common.selectProject')" :placeholder="$t('uiAutomation.common.selectProject')" :loading="projectsLoading" :disabled="projectsLoading" style="width: 200px; margin-right: 15px">
        <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
      </el-select>
      <el-button type="primary" :disabled="!projectId" @click="handleNewSuite">
        <el-icon><Plus /></el-icon>
        {{ $t('uiAutomation.suite.newSuite') }}
      </el-button>
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

    <div class="card-container">
      <div class="filter-bar">
        <el-row :gutter="20">
          <el-col :span="6">
            <el-input
              v-model="searchText"
              :placeholder="$t('uiAutomation.suite.searchPlaceholder')"
              clearable
              @input="handleSearch"
            >
              <template #prefix>
                <el-icon><Search /></el-icon>
              </template>
            </el-input>
          </el-col>
        </el-row>
      </div>

      <el-table :data="suites" v-loading="loading" style="width: 100%">
        <el-table-column type="selection" width="55" />
        <el-table-column prop="name" :label="$t('uiAutomation.suite.suiteName')" min-width="200">
          <template #default="{ row }">
            <el-link @click="editSuite(row.id)" type="primary">
              {{ row.name }}
            </el-link>
          </template>
        </el-table-column>
        <el-table-column prop="description" :label="$t('uiAutomation.common.description')" min-width="200" show-overflow-tooltip />
        <el-table-column :label="$t('uiAutomation.suite.testCaseCount')" width="120">
          <template #default="{ row }">
            {{ row.test_case_count || 0 }}
          </template>
        </el-table-column>
        <el-table-column :label="$t('uiAutomation.suite.executionStatus')" width="100">
          <template #default="{ row }">
            <el-tag :type="getExecutionStatusTag(row.execution_status)">
              {{ getExecutionStatusText(row.execution_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('uiAutomation.suite.passedCount')" width="90">
          <template #default="{ row }">
            <span style="color: #67c23a; font-weight: bold;">{{ row.passed_count || 0 }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="$t('uiAutomation.suite.failedCount')" width="90">
          <template #default="{ row }">
            <span style="color: #f56c6c; font-weight: bold;">{{ row.failed_count || 0 }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" :label="$t('uiAutomation.common.createTime')" width="180" :formatter="formatDate" />
        <el-table-column prop="updated_at" :label="$t('uiAutomation.common.updateTime')" width="180" :formatter="formatDate" />
        <el-table-column :label="$t('uiAutomation.common.operation')" width="240" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" @click="editSuite(row.id)">
              <el-icon><Edit /></el-icon>
              {{ $t('uiAutomation.common.edit') }}
            </el-button>
            <el-button size="small" type="success" @click="runSuite(row)" :loading="isSuiteSubmitting(row.id)" :disabled="isSuiteSubmitting(row.id)">
              <el-icon><RefreshRight /></el-icon>
              {{ $t('uiAutomation.common.run') }}
            </el-button>
            <el-button size="small" type="danger" @click="deleteSuite(row.id)">
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

    <!-- 创建/编辑套件对话框 -->
    <el-dialog
      v-model="showCreateDialog"
      :title="isEditing ? $t('uiAutomation.suite.editSuite') : $t('uiAutomation.suite.createSuite')"
      width="900px"
      :close-on-click-modal="false"
    >
      <el-form ref="createFormRef" :model="createForm" :rules="formRules" label-width="100px">
        <el-form-item :label="$t('uiAutomation.suite.suiteName')" prop="name">
          <el-input v-model="createForm.name" :placeholder="$t('uiAutomation.suite.rules.nameRequired')" />
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.common.description')" prop="description">
          <el-input v-model="createForm.description" type="textarea" :placeholder="$t('uiAutomation.common.description')" />
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.suite.testCases')">
          <div class="test-case-selector">
            <div class="selector-panel">
              <div class="panel-header">
                <h4>{{ $t('uiAutomation.suite.availableCases') }}</h4>
                <el-input
                  v-model="testCaseSearchText"
                  :placeholder="$t('uiAutomation.suite.searchCases')"
                  size="small"
                  clearable
                  style="width: 200px;"
                >
                  <template #prefix>
                    <el-icon><Search /></el-icon>
                  </template>
                </el-input>
              </div>
              <div class="panel-content">
                <el-table
                  :data="filteredAvailableTestCases"
                  height="300"
                  @row-click="handleTestCaseRowClick"
                  :row-class-name="getTestCaseRowClassName"
                >
                  <el-table-column prop="name" :label="$t('uiAutomation.suite.caseName')" min-width="150" show-overflow-tooltip />
                  <el-table-column prop="priority" :label="$t('uiAutomation.suite.priority')" width="80">
                    <template #default="{ row }">
                      <el-tag size="small" :type="getPriorityTag(row.priority)">
                        {{ getPriorityText(row.priority) }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column prop="status" :label="$t('uiAutomation.common.status')" width="80">
                    <template #default="{ row }">
                      <el-tag size="small" :type="getCaseStatusTag(row.status)">
                        {{ getCaseStatusText(row.status) }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column :label="$t('uiAutomation.common.operation')" width="80">
                    <template #default="{ row }">
                      <el-button size="small" text @click.stop="addTestCase(row)">
                        <el-icon><ArrowRight /></el-icon>
                      </el-button>
                    </template>
                  </el-table-column>
                </el-table>
              </div>
            </div>

            <div class="selector-panel">
              <div class="panel-header">
                <h4>{{ $t('uiAutomation.suite.selectedCases') }} ({{ selectedTestCases.length }})</h4>
              </div>
              <div class="panel-content">
                <el-table
                  :data="selectedTestCases"
                  height="300"
                >
                  <el-table-column prop="name" :label="$t('uiAutomation.suite.caseName')" min-width="150" show-overflow-tooltip />
                  <el-table-column prop="priority" :label="$t('uiAutomation.suite.priority')" width="80">
                    <template #default="{ row }">
                      <el-tag size="small" :type="getPriorityTag(row.priority)">
                        {{ getPriorityText(row.priority) }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column :label="$t('uiAutomation.common.operation')" width="120">
                    <template #default="{ row, $index }">
                      <el-button
                        size="small"
                        text
                        @click="moveUp($index)"
                        :disabled="$index === 0"
                      >
                        <el-icon><Top /></el-icon>
                      </el-button>
                      <el-button
                        size="small"
                        text
                        @click="moveDown($index)"
                        :disabled="$index === selectedTestCases.length - 1"
                      >
                        <el-icon><Bottom /></el-icon>
                      </el-button>
                      <el-button
                        size="small"
                        text
                        type="danger"
                        @click="removeTestCase($index)"
                      >
                        <el-icon><Delete /></el-icon>
                      </el-button>
                    </template>
                  </el-table-column>
                </el-table>
              </div>
            </div>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="cancelCreate">{{ $t('uiAutomation.common.cancel') }}</el-button>
          <el-button type="primary" @click="handleCreate" :loading="saving">{{ $t('uiAutomation.common.confirm') }}</el-button>
        </span>
      </template>
    </el-dialog>

    <!-- 运行配置对话框 -->
    <el-dialog v-model="showRunDialog" :title="$t('uiAutomation.suite.runConfig')" width="600px" :close-on-click-modal="false">
      <el-alert v-if="readinessLoading" :title="t('uiAutomation.suite.readinessLoading')" type="info" :closable="false" show-icon />
      <el-alert v-else-if="readinessLoadError" :title="readinessLoadError" type="warning" :closable="false" show-icon />
      <template v-else-if="readiness">
        <el-alert
          v-if="readiness.base_url_ready === false || readiness.base_url_message"
          :title="readiness.base_url_message || t('uiAutomation.suite.baseUrlUnavailable')"
          :type="readiness.base_url_ready === false ? 'warning' : 'info'"
          :closable="false"
          show-icon
          class="readiness-alert"
        />
        <div class="readiness-options" role="status">
          <div v-for="engine in readiness.engines || []" :key="engine.value" class="readiness-engine">
            <strong>{{ engineLabel(engine.value) }}</strong>
            <span>{{ engine.available ? t('uiAutomation.suite.capabilityAvailable') : engine.message || t('uiAutomation.suite.capabilityUnavailable') }}</span>
            <ul v-if="engine.browsers?.length">
              <li v-for="browser in engine.browsers" :key="browser.value">
                {{ browserLabel(browser.value) }}: {{ browser.available ? t('uiAutomation.suite.capabilityAvailable') : browser.message || t('uiAutomation.suite.capabilityUnavailable') }}
              </li>
            </ul>
          </div>
        </div>
      </template>
      <el-alert v-if="submissionState === 'unknown'" :title="t('uiAutomation.suite.submissionUnknown')" type="warning" :closable="false" show-icon class="readiness-alert" />
      <el-form :model="runConfig" label-width="120px">
        <el-form-item :label="$t('uiAutomation.suite.testEngine')">
          <el-select v-model="runConfig.engine" :placeholder="$t('uiAutomation.suite.testEngine')" :disabled="running || Boolean(currentSubmission) || readinessLoading || !readiness" @change="onRunEngineChange">
            <el-option v-for="option in engineOptions" :key="option.value" :label="option.label" :value="option.value" :disabled="!option.available">
              <span>{{ option.label }}</span>
              <span v-if="option.message" class="option-reason">{{ option.message }}</span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.suite.browser')">
          <el-select v-model="runConfig.browser" :placeholder="$t('uiAutomation.suite.browser')" :disabled="running || Boolean(currentSubmission) || readinessLoading || !readiness">
            <el-option v-for="option in browserOptions" :key="option.value" :label="option.label" :value="option.value" :disabled="!option.available">
              <span>{{ option.label }}</span>
              <span v-if="option.message" class="option-reason">{{ option.message }}</span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.suite.executionMode')">
          <el-radio-group v-model="runConfig.headless" :disabled="running || Boolean(currentSubmission)">
            <el-radio :label="false">{{ $t('uiAutomation.suite.headedMode') }}</el-radio>
            <el-radio :label="true">{{ $t('uiAutomation.suite.headlessMode') }}</el-radio>
          </el-radio-group>
          <p class="headless-note">{{ t('uiAutomation.suite.headlessRecommended') }}</p>
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button :disabled="running" @click="showRunDialog = false">{{ $t('uiAutomation.common.cancel') }}</el-button>
          <el-button
            type="primary"
            @click="confirmRunSuite"
            :loading="running"
            :disabled="running || (!currentSubmission && (readinessLoading || !readiness || Boolean(readinessErrorMessage)))"
          >
            {{ t(submissionState === 'unknown' ? 'uiAutomation.suite.retrySameRequest' : 'uiAutomation.suite.startExecution') }}
          </el-button>
        </span>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Plus, Search, Edit, Delete, RefreshRight,
  ArrowRight, Top, Bottom
} from '@element-plus/icons-vue'
import {
  getTestSuites,
  createTestSuite,
  updateTestSuite,
  deleteTestSuite,
  getTestCases,
  getTestSuiteTestCases,
  addTestCaseToTestSuite,
  removeTestCaseFromTestSuite,
  runTestSuite,
  getExecutionReadiness,
  getSuiteSubmission
} from '@/api/ui_automation'
import { useRouter } from 'vue-router'
import { useUiProjectContext } from '@/composables/useUiProjectContext'
import { normalizeUiId, uiExecutionLocation } from '@/utils/ui-project-context.mjs'
import { newUiSubmissionId, uiPendingRunKey, readUiPendingRun as readStoredSuiteRun, writeUiPendingRun as writeStoredSuiteRun, clearUiPendingRun as clearStoredSuiteRun } from '@/utils/ui-execution-submission.mjs'
import { uiEngineOptions, uiBrowserOptions, uiReadinessError } from '@/utils/ui-execution-options.mjs'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()
const router = useRouter()
const {
  projects,
  projectId,
  projectsLoading,
  projectsError,
  invalidProject,
  reloadProjects,
  userId
} = useUiProjectContext()

// 响应式数据
const suites = ref([])
const loading = ref(false)
const searchText = ref('')
const total = ref(0)
let projectScopeVersion = 0
let suiteRequestVersion = 0
let readinessRequestVersion = 0
const pagination = reactive({
  currentPage: 1,
  pageSize: 20
})

// 对话框控制
const showCreateDialog = ref(false)
const showRunDialog = ref(false)
const isEditing = ref(false)
const currentSuiteId = ref(null)
const saving = ref(false)
const running = ref(false)
const submittingSuiteIds = ref(new Set())

// 表单数据
const createForm = reactive({
  name: '',
  description: ''
})

// 表单验证规则 - 使用 computed 实现动态国际化
const formRules = computed(() => ({
  name: [{ required: true, message: t('uiAutomation.suite.rules.nameRequired'), trigger: 'blur' }]
}))

// 测试用例相关
const availableTestCases = ref([])
const selectedTestCases = ref([])
const testCaseSearchText = ref('')

// 运行配置
const runConfig = reactive({
  engine: 'playwright',
  browser: 'chrome',
  headless: true
})
const currentRunningSuite = ref(null)
const readiness = ref(null)
const readinessLoading = ref(false)
const readinessLoadError = ref('')
const submissionState = ref('idle')
const currentSubmission = ref(null)
const pendingSuiteMemory = new Map()
const readUiPendingRun = key => pendingSuiteMemory.get(key) || readStoredSuiteRun(key)
const writeUiPendingRun = (key, record) => {
  pendingSuiteMemory.set(key, { ...record })
  writeStoredSuiteRun(key, record)
}
const clearUiPendingRun = key => {
  pendingSuiteMemory.delete(key)
  clearStoredSuiteRun(key)
}
const readinessErrorMessage = computed(() => {
  if (currentSubmission.value || !readiness.value) return ''
  try {
    const result = uiReadinessError(readiness.value, runConfig.engine, runConfig.browser)
    return typeof result === 'string' ? result : result?.message || ''
  } catch {
    return t('uiAutomation.suite.capabilityUnavailable')
  }
})

function optionValue(option) {
  return typeof option === 'string' ? option : option?.value || option?.engine || option?.browser || ''
}

function normalizeCapabilityOptions(options, capabilities) {
  if (!Array.isArray(options)) return []
  return options.map(option => {
    const value = optionValue(option)
    if (!value) return null
    const capability = capabilities.find(item => String(item.value) === String(value))
    return {
      value,
      label: typeof option === 'object' && option.label ? option.label : engineOrBrowserLabel(value),
      available: capability?.available ?? (typeof option === 'object' ? option.available : undefined) ?? true,
      message: capability?.message || (typeof option === 'object' ? option.message : '') || ''
    }
  }).filter(Boolean)
}

function engineOrBrowserLabel(value) {
  return ({ playwright: 'Playwright', selenium: 'Selenium', chrome: 'Chrome', firefox: 'Firefox', edge: 'Edge', safari: 'Safari' })[value] || value
}

const engineOptions = computed(() => normalizeCapabilityOptions(
  uiEngineOptions(readiness.value), readiness.value?.engines || []
))
const browserCapabilities = computed(() => readiness.value?.engines?.find(engine => String(engine.value) === String(runConfig.engine))?.browsers || [])
const browserOptions = computed(() => normalizeCapabilityOptions(
  uiBrowserOptions(readiness.value, runConfig.engine), browserCapabilities.value
))

function engineLabel(value) { return engineOrBrowserLabel(value) }
function browserLabel(value) { return engineOrBrowserLabel(value) }

function chooseAvailableRunOptions() {
  const availableEngine = engineOptions.value.find(option => option.available) || engineOptions.value[0]
  if (availableEngine && !engineOptions.value.find(option => option.value === runConfig.engine && option.available)) {
    runConfig.engine = availableEngine.value
  }
  const availableBrowser = browserOptions.value.find(option => option.available) || browserOptions.value[0]
  if (availableBrowser && !browserOptions.value.find(option => option.value === runConfig.browser && option.available)) {
    runConfig.browser = availableBrowser.value
  }
}

function onRunEngineChange() {
  const availableBrowser = browserOptions.value.find(option => option.available) || browserOptions.value[0]
  if (availableBrowser) runConfig.browser = availableBrowser.value
}

const isPositiveId = (value) => Boolean(normalizeUiId(value))

const responseList = (response) => {
  const body = response?.data?.data ?? response?.data
  if (Array.isArray(body)) return body
  if (Array.isArray(body?.results)) return body.results
  return null
}

const responseBody = response => response?.data?.data ?? response?.data ?? response
const suitePendingKey = (suiteId, project = projectId.value) => uiPendingRunKey(
  'suite', userId?.value ?? userId, project, suiteId
)
const currentSuitePendingKey = () => currentRunningSuite.value
  ? suitePendingKey(currentRunningSuite.value.id)
  : ''
const submissionClientId = record => record?.client_request_id || record?.clientRequestId || ''
const submissionExecutionId = record => normalizeUiId(record?.execution_id || record?.executionId || record?.execution?.id)

const isSuiteSubmitting = (id) => submittingSuiteIds.value.has(`${normalizeUiId(projectId.value)}:${normalizeUiId(id)}`)

// 计算属性 - 过滤后的可用测试用例
const filteredAvailableTestCases = computed(() => {
  if (!testCaseSearchText.value) {
    return availableTestCases.value
  }
  return availableTestCases.value.filter(tc =>
    tc.name.toLowerCase().includes(testCaseSearchText.value.toLowerCase()) ||
    (tc.description && tc.description.toLowerCase().includes(testCaseSearchText.value.toLowerCase()))
  )
})

// 加载测试套件列表
const loadSuites = async () => {
  const requestedProjectId = projectId.value
  const scopeVersion = projectScopeVersion
  const requestVersion = ++suiteRequestVersion
  if (!isPositiveId(requestedProjectId)) {
    suites.value = []
    total.value = 0
    loading.value = false
    return
  }

  loading.value = true
  try {
    const response = await getTestSuites({
      project: requestedProjectId,
      page: pagination.currentPage,
      page_size: pagination.pageSize,
      search: searchText.value
    })
    const resultList = responseList(response)
    if (scopeVersion !== projectScopeVersion || requestVersion !== suiteRequestVersion || String(projectId.value) !== String(requestedProjectId)) return
    if (!resultList) throw new Error('Invalid suite list response')

    const scopedSuites = resultList.filter(suite => {
      if (!suite || typeof suite !== 'object' || !isPositiveId(suite.id)) return false
      if (suite.project === undefined || suite.project === null) return true
      const returnedProjectId = typeof suite.project === 'object' ? suite.project.id : suite.project
      return String(returnedProjectId) === String(requestedProjectId)
    })
    suites.value = scopedSuites
    const count = response?.data?.count
    total.value = Number.isSafeInteger(Number(count)) && Number(count) >= 0 ? Number(count) : scopedSuites.length
  } catch (error) {
    if (scopeVersion === projectScopeVersion && requestVersion === suiteRequestVersion) {
      console.error('获取测试套件列表失败:', error)
      ElMessage.error(t('uiAutomation.suite.messages.loadFailed'))
    }
  } finally {
    if (scopeVersion === projectScopeVersion && requestVersion === suiteRequestVersion) loading.value = false
  }
}

// 加载可用测试用例
const loadAvailableTestCases = async () => {
  const requestedProjectId = projectId.value
  const scopeVersion = projectScopeVersion
  if (!isPositiveId(requestedProjectId)) {
    availableTestCases.value = []
    return
  }

  try {
    const response = await getTestCases({
      project: requestedProjectId,
      page_size: 1000
    })
    const resultList = responseList(response)
    if (scopeVersion !== projectScopeVersion || String(projectId.value) !== String(requestedProjectId)) return
    if (!resultList) throw new Error('Invalid test case list response')
    availableTestCases.value = resultList.filter(testCase => {
      if (!testCase || !isPositiveId(testCase.id)) return false
      if (testCase.project === undefined || testCase.project === null) return true
      const returnedProjectId = typeof testCase.project === 'object' ? testCase.project.id : testCase.project
      return String(returnedProjectId) === String(requestedProjectId)
    })
  } catch (error) {
    if (scopeVersion === projectScopeVersion && String(projectId.value) === String(requestedProjectId)) {
      console.error('获取测试用例列表失败:', error)
      ElMessage.error(t('uiAutomation.suite.messages.loadCasesFailed'))
    }
  }
}

// 搜索处理
const handleSearch = async () => {
  pagination.currentPage = 1
  await loadSuites()
}

// 分页处理
const handleSizeChange = async () => {
  pagination.currentPage = 1
  await loadSuites()
}

const handleCurrentChange = async () => {
  await loadSuites()
}

// 新增套件
const handleCreate = async () => {
  if (!createForm.name) {
    ElMessage.warning(t('uiAutomation.suite.messages.inputName'))
    return
  }

  if (!projectId.value) {
    ElMessage.warning(t('uiAutomation.suite.messages.selectProject'))
    return
  }

  saving.value = true
  try {
    const suiteData = {
      project: projectId.value,
      name: createForm.name,
      description: createForm.description
    }

    let suiteId
    if (isEditing.value) {
      // 更新套件
      await updateTestSuite(currentSuiteId.value, suiteData)
      suiteId = currentSuiteId.value
      ElMessage.success(t('uiAutomation.suite.messages.updateSuccess'))
    } else {
      // 创建套件
      const response = await createTestSuite(suiteData)
      suiteId = response.data.id
      ElMessage.success(t('uiAutomation.suite.messages.createSuccess'))
    }

    // 保存测试用例关联
    if (selectedTestCases.value.length > 0) {
      // 清除旧的关联（如果是编辑模式）
      if (isEditing.value) {
        const existingTestCases = await getTestSuiteTestCases(suiteId)
        for (const tc of existingTestCases.data) {
          await removeTestCaseFromTestSuite(suiteId, tc.test_case.id)
        }
      }

      // 添加新的关联
      for (let i = 0; i < selectedTestCases.value.length; i++) {
        await addTestCaseToTestSuite(suiteId, {
          test_case_id: selectedTestCases.value[i].id,
          order: i
        })
      }
    }

    showCreateDialog.value = false
    await loadSuites()
    resetForm()
  } catch (error) {
    console.error('保存测试套件失败:', error)
    ElMessage.error(t('uiAutomation.suite.messages.saveFailed'))
  } finally {
    saving.value = false
  }
}

// 编辑套件
const editSuite = async (id) => {
  const scopeVersion = projectScopeVersion
  try {
    // 加载套件详情
    const suites_data = suites.value.find(s => s.id === id)
    if (!suites_data) return

    currentSuiteId.value = id
    isEditing.value = true
    createForm.name = suites_data.name
    createForm.description = suites_data.description

    // 加载已选测试用例
    const response = await getTestSuiteTestCases(id)
    const caseList = response?.data?.data ?? response?.data
    if (scopeVersion !== projectScopeVersion || !Array.isArray(caseList)) return
    selectedTestCases.value = caseList
      .filter(item => item?.test_case && isPositiveId(item.test_case.id))
      .map(item => item.test_case)
      .sort((a, b) => a.order - b.order)

    // 加载可用测试用例
    await loadAvailableTestCases()
    if (scopeVersion !== projectScopeVersion) return

    showCreateDialog.value = true
  } catch (error) {
    console.error('加载套件详情失败:', error)
    ElMessage.error(t('uiAutomation.suite.messages.loadDetailFailed'))
  }
}

// 删除套件
const deleteSuite = async (id) => {
  try {
    await ElMessageBox.confirm(t('uiAutomation.suite.messages.deleteConfirm'), t('uiAutomation.messages.confirm.tip'), {
      confirmButtonText: t('uiAutomation.common.confirm'),
      cancelButtonText: t('uiAutomation.common.cancel'),
      type: 'warning'
    })

    await deleteTestSuite(id)
    ElMessage.success(t('uiAutomation.suite.messages.deleteSuccess'))
    await loadSuites()
  } catch (error) {
    if (error !== 'cancel') {
      console.error('删除测试套件失败:', error)
      ElMessage.error(t('uiAutomation.suite.messages.deleteFailed'))
    }
  }
}

function applyReadinessDefaults() {
  if (currentSubmission.value) {
    const saved = currentSubmission.value
    if (saved.engine) runConfig.engine = saved.engine
    if (saved.browser) runConfig.browser = saved.browser
    if (typeof saved.headless === 'boolean') runConfig.headless = saved.headless
    return
  }
  chooseAvailableRunOptions()
  runConfig.headless = readiness.value?.recommended_headless !== false
}

function restorePendingSubmission(suite) {
  let record = null
  try {
    record = readUiPendingRun(suitePendingKey(suite.id))
  } catch {
    readinessLoadError.value = t('uiAutomation.suite.pendingRequestLoadFailed')
  }
  if (!record || (record.suite_id && String(record.suite_id) !== String(suite.id))) {
    currentSubmission.value = null
    submissionState.value = 'idle'
    return
  }
  currentSubmission.value = record
  submissionState.value = 'unknown'
}

async function prepareRunDialog(suite) {
  const requestedProject = String(projectId.value)
  const requestedActor = normalizeUiId(userId?.value ?? userId)
  const scope = projectScopeVersion
  const request = ++readinessRequestVersion
  const isCurrent = () => request === readinessRequestVersion &&
    scope === projectScopeVersion &&
    String(projectId.value) === requestedProject &&
    normalizeUiId(userId?.value ?? userId) === requestedActor &&
    currentRunningSuite.value?.id === suite.id
  readiness.value = null
  readinessLoading.value = true
  readinessLoadError.value = ''
  submissionState.value = 'idle'
  restorePendingSubmission(suite)
  try {
    const response = await getExecutionReadiness(requestedProject)
    if (!isCurrent()) return
    const body = responseBody(response)
    if (!body || typeof body !== 'object' || (body.project_id && String(body.project_id) !== requestedProject)) throw new Error('Invalid execution readiness response')
    readiness.value = body
    applyReadinessDefaults()
  } catch (error) {
    if (isCurrent()) {
      readinessLoadError.value = error.response?.data?.message || error.message || t('uiAutomation.suite.readinessLoadFailed')
    }
  } finally {
    if (isCurrent()) readinessLoading.value = false
  }
}

function markSubmissionUnknown(key, record, requestId) {
  const unknownRecord = { ...record, client_request_id: requestId, status: 'unknown' }
  try { writeUiPendingRun(key, unknownRecord) } catch {}
  currentSubmission.value = unknownRecord
  submissionState.value = 'unknown'
  ElMessage.warning(t('uiAutomation.suite.submissionUnknown'))
}

function persistLateSuiteAcceptance(key, record, requestId, executionId) {
  const current = readUiPendingRun(key)
  if (current && submissionClientId(current) !== requestId) return
  try { writeUiPendingRun(key, { ...record, client_request_id: requestId, execution_id: executionId }) } catch {}
}

function readSubmissionExecutionId(response) {
  const body = responseBody(response)
  if (body?.accepted === false) return ''
  return normalizeUiId(body?.execution_id || body?.execution?.id || body?.data?.execution_id)
}

async function lookupSuiteSubmission(suiteId, requestId, requestedProject, requestedActor, scope) {
  const isCurrent = () => scope === projectScopeVersion &&
    String(projectId.value) === requestedProject &&
    normalizeUiId(userId?.value ?? userId) === requestedActor
  try {
    const response = await getSuiteSubmission(suiteId, { client_request_id: requestId })
    const executionId = readSubmissionExecutionId(response)
    if (!isCurrent()) return { stale: true, ...(executionId ? { executionId } : {}) }
    return executionId ? { executionId } : { unknown: true }
  } catch (error) {
    if (!isCurrent()) return { stale: true }
    if (Number(error.response?.status) === 404) return { notFound: true }
    return { unknown: true }
  }
}

async function openAcceptedSuiteExecution(executionId, key, requestedProject, requestedActor, scope) {
  if (!isPositiveId(executionId) || scope !== projectScopeVersion || String(projectId.value) !== requestedProject || normalizeUiId(userId?.value ?? userId) !== requestedActor) return false
  try { clearUiPendingRun(key) } catch {}
  currentSubmission.value = null
  submissionState.value = 'accepted'
  ElMessage.success(t('uiAutomation.suite.messages.startSuccess'))
  showRunDialog.value = false
  currentRunningSuite.value = null
  await router.push(uiExecutionLocation('UiReports', requestedProject, executionId))
  return true
}

function isDefinitiveSubmissionRejection(error, body) {
  const status = Number(error.response?.status)
  return body?.accepted === false && [400, 403, 503].includes(status)
}

const runSuite = async (suite) => {
  if (!isPositiveId(projectId.value)) {
    ElMessage.warning(t('uiAutomation.suite.messages.selectProject'))
    return
  }
  if ((!suite.test_case_count || suite.test_case_count === 0) && !readUiPendingRun(suitePendingKey(suite.id))) {
    ElMessage.warning(t('uiAutomation.suite.messages.noCases'))
    return
  }
  if (isSuiteSubmitting(suite.id)) return

  currentRunningSuite.value = suite
  showRunDialog.value = true
  await prepareRunDialog(suite)
}

const confirmRunSuite = async () => {
  const suite = currentRunningSuite.value
  if (!suite || !isPositiveId(suite.id) || !isPositiveId(projectId.value)) return
  if (running.value || isSuiteSubmitting(suite.id)) return
  if (!readiness.value && !currentSubmission.value) return

  const readinessIssue = readinessErrorMessage.value
  if (readinessIssue) {
    ElMessage.error(readinessIssue)
    return
  }

  const requestedProject = String(projectId.value)
  const requestedActor = normalizeUiId(userId?.value ?? userId)
  const scope = projectScopeVersion
  const key = currentSuitePendingKey()
  if (!key) {
    ElMessage.error(t('uiAutomation.suite.pendingRequestSaveFailed'))
    return
  }
  const submittingKey = `${normalizeUiId(requestedProject)}:${normalizeUiId(suite.id)}`
  let record = currentSubmission.value
  let requestId = submissionClientId(record)
  if (!requestId) requestId = newUiSubmissionId()
  record = { ...(record || {}), suite_id: String(suite.id), project_id: requestedProject, client_request_id: requestId, status: 'submitting',
    engine: record?.engine || runConfig.engine, browser: record?.browser || runConfig.browser,
    headless: typeof record?.headless === 'boolean' ? record.headless : runConfig.headless }
  try {
    writeUiPendingRun(key, record)
    if (submissionClientId(readUiPendingRun(key)) !== requestId) throw new Error('Pending request could not be persisted')
  } catch {
    ElMessage.error(t('uiAutomation.suite.pendingRequestSaveFailed'))
    return
  }

  submittingSuiteIds.value.add(submittingKey)
  running.value = true
  submissionState.value = 'submitting'
  try {
    if (currentSubmission.value) {
      submissionState.value = 'checking'
      const existing = await lookupSuiteSubmission(suite.id, requestId, requestedProject, requestedActor, scope)
      if (existing.stale) {
        if (existing.executionId) persistLateSuiteAcceptance(key, record, requestId, existing.executionId)
        return
      }
      if (existing.executionId) {
        await openAcceptedSuiteExecution(existing.executionId, key, requestedProject, requestedActor, scope)
        return
      }
      if (!existing.notFound) {
        markSubmissionUnknown(key, record, requestId)
        return
      }
    }

    record.status = 'submitting'
    currentSubmission.value = record
    try {
      const response = await runTestSuite(suite.id, {
        use_ai: false,
        engine: record.engine,
        browser: record.browser,
        headless: record.headless,
        client_request_id: requestId
      })
      const body = responseBody(response)
      const executionId = body?.accepted === false ? '' : readSubmissionExecutionId(response)
      if (scope !== projectScopeVersion || String(projectId.value) !== requestedProject || normalizeUiId(userId?.value ?? userId) !== requestedActor) {
        if (executionId) persistLateSuiteAcceptance(key, record, requestId, executionId)
        return
      }
      if (executionId) {
        await openAcceptedSuiteExecution(executionId, key, requestedProject, requestedActor, scope)
        return
      }
      if (body?.accepted === false) {
        try { clearUiPendingRun(key) } catch {}
        currentSubmission.value = null
        submissionState.value = 'rejected'
        ElMessage.error(body.message || body.error || t('uiAutomation.suite.messages.executeFailed'))
        return
      }
    } catch (error) {
      if (scope !== projectScopeVersion || String(projectId.value) !== requestedProject || normalizeUiId(userId?.value ?? userId) !== requestedActor) return
      const body = error.response?.data?.data ?? error.response?.data ?? {}
      if (isDefinitiveSubmissionRejection(error, body)) {
        try { clearUiPendingRun(key) } catch {}
        currentSubmission.value = null
        submissionState.value = 'rejected'
        ElMessage.error(body.message || body.error || t('uiAutomation.suite.messages.executeFailed'))
        return
      }
    }

    const recovered = await lookupSuiteSubmission(suite.id, requestId, requestedProject, requestedActor, scope)
    if (recovered.stale) {
      if (recovered.executionId) persistLateSuiteAcceptance(key, record, requestId, recovered.executionId)
      return
    }
    if (recovered.executionId) {
      await openAcceptedSuiteExecution(recovered.executionId, key, requestedProject, requestedActor, scope)
      return
    }
    markSubmissionUnknown(key, record, requestId)
  } finally {
    submittingSuiteIds.value.delete(submittingKey)
    if (scope === projectScopeVersion) running.value = false
  }
}

// 测试用例管理方法
const handleTestCaseRowClick = (row) => {
  // 双击添加测试用例
  addTestCase(row)
}

const getTestCaseRowClassName = ({ row }) => {
  // 如果已选中，添加特殊样式
  return selectedTestCases.value.some(tc => tc.id === row.id) ? 'selected-row' : ''
}

const addTestCase = (testCase) => {
  // 检查是否已存在
  if (selectedTestCases.value.some(tc => tc.id === testCase.id)) {
    ElMessage.warning(t('uiAutomation.suite.messages.caseAdded'))
    return
  }
  selectedTestCases.value.push({ ...testCase })
}

const removeTestCase = (index) => {
  selectedTestCases.value.splice(index, 1)
}

const moveUp = (index) => {
  if (index > 0) {
    const temp = selectedTestCases.value[index]
    selectedTestCases.value[index] = selectedTestCases.value[index - 1]
    selectedTestCases.value[index - 1] = temp
  }
}

const moveDown = (index) => {
  if (index < selectedTestCases.value.length - 1) {
    const temp = selectedTestCases.value[index]
    selectedTestCases.value[index] = selectedTestCases.value[index + 1]
    selectedTestCases.value[index + 1] = temp
  }
}

// 重置表单
const resetForm = () => {
  createForm.name = ''
  createForm.description = ''
  selectedTestCases.value = []
  testCaseSearchText.value = ''
  isEditing.value = false
  currentSuiteId.value = null
}

// 取消创建
const cancelCreate = () => {
  showCreateDialog.value = false
  resetForm()
}

// 新增套件按钮点击
const handleCreateButtonClick = async () => {
  resetForm()
  await loadAvailableTestCases()
  showCreateDialog.value = true
}

// 辅助方法
const formatDate = (row, column, cellValue) => {
  if (!cellValue) return ''
  return new Date(cellValue).toLocaleString()
}

const getExecutionStatusTag = (status) => {
  const statusMap = {
    'not_run': 'info',
    'passed': 'success',
    'failed': 'danger',
    'running': 'warning'
  }
  return statusMap[status] || 'info'
}

const getExecutionStatusText = (status) => {
  const statusKey = {
    'not_run': 'notRun',
    'passed': 'passed',
    'failed': 'failed',
    'running': 'running'
  }[status]
  return statusKey ? t(`uiAutomation.status.${statusKey}`) : t('uiAutomation.status.unknown')
}

const getPriorityTag = (priority) => {
  const priorityMap = {
    'high': 'danger',
    'medium': 'warning',
    'low': 'info'
  }
  return priorityMap[priority] || 'info'
}

const getPriorityText = (priority) => {
  const priorityKey = {
    'high': 'high',
    'medium': 'medium',
    'low': 'low'
  }[priority]
  return priorityKey ? t(`uiAutomation.priority.${priorityKey}`) : t('uiAutomation.status.unknown')
}

const getCaseStatusTag = (status) => {
  const statusMap = {
    'draft': 'info',
    'ready': 'primary',
    'running': 'warning',
    'passed': 'success',
    'failed': 'danger'
  }
  return statusMap[status] || 'info'
}

const getCaseStatusText = (status) => {
  const statusKey = {
    'draft': 'draft',
    'ready': 'ready',
    'running': 'running',
    'passed': 'passed',
    'failed': 'failed'
  }[status]
  return statusKey ? t(`uiAutomation.status.${statusKey}`) : t('uiAutomation.status.unknown')
}

// 项目范围变化时丢弃旧响应并清理页面状态
watch([projectId, userId], ([nextProjectId]) => {
  projectScopeVersion += 1
  suiteRequestVersion += 1
  readinessRequestVersion += 1
  pagination.currentPage = 1
  suites.value = []
  total.value = 0
  availableTestCases.value = []
  selectedTestCases.value = []
  loading.value = false
  showCreateDialog.value = false
  showRunDialog.value = false
  currentRunningSuite.value = null
  readiness.value = null
  readinessLoading.value = false
  readinessLoadError.value = ''
  currentSubmission.value = null
  submissionState.value = 'idle'
  submittingSuiteIds.value = new Set()
  running.value = false
  if (isPositiveId(nextProjectId)) loadSuites()
}, { immediate: true })

onBeforeUnmount(() => {
  pendingSuiteMemory.clear()
  projectScopeVersion += 1
  suiteRequestVersion += 1
  readinessRequestVersion += 1
})

// 修改新增套件按钮点击事件
const handleNewSuite = async () => {
  if (!projectId.value) return
  const scope = projectScopeVersion
  resetForm()
  await loadAvailableTestCases()
  if (scope === projectScopeVersion) showCreateDialog.value = true
}
</script>

<style scoped lang="scss">
.page-container {
  padding: 20px;
  background: #f5f5f5;
  min-height: 100vh;
}

.page-header {
  display: flex;
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
}

.project-alert {
  margin: 0 0 16px;
}

.readiness-alert {
  margin-bottom: 12px;
}

.readiness-options {
  display: grid;
  gap: 8px;
  max-height: 160px;
  overflow: auto;
  margin-bottom: 14px;
  color: var(--el-text-color-regular);
  font-size: 12px;
}

.readiness-engine {
  display: grid;
  grid-template-columns: minmax(80px, auto) minmax(0, 1fr);
  gap: 4px 10px;
}

.readiness-engine ul {
  grid-column: 2;
  margin: 0;
  padding-left: 16px;
}

.option-reason {
  float: right;
  max-width: 240px;
  overflow: hidden;
  color: var(--el-text-color-secondary);
  text-overflow: ellipsis;
}

.headless-note {
  flex-basis: 100%;
  margin: 5px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.card-container {
  background: white;
  padding: 20px;
  border-radius: 4px;
}

.filter-bar {
  margin-bottom: 20px;
}

.pagination-container {
  margin-top: 20px;
  display: flex;
  justify-content: flex-end;
}

// 测试用例选择器样式
.test-case-selector {
  display: flex;
  gap: 20px;
  width: 100%;
}

.selector-panel {
  flex: 1;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  overflow: hidden;
}

.panel-header {
  background: #f5f7fa;
  padding: 12px 15px;
  border-bottom: 1px solid #dcdfe6;
  display: flex;
  justify-content: space-between;
  align-items: center;

  h4 {
    margin: 0;
    font-size: 14px;
    color: #303133;
  }
}

.panel-content {
  padding: 10px;
}


:deep(.selected-row) {
  background-color: #f0f9ff !important;
}

:deep(.el-table__row) {
  cursor: pointer;

  &:hover {
    background-color: #f5f7fa;
  }
}

.mode-description {
  margin-top: 8px;

  .description-text {
    font-size: 12px;
    color: #909399;
    line-height: 1.5;
  }
}
</style>
