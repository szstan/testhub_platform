<template>
  <div class="automation-testing">
    <div class="header">
      <h2>{{ t('apiTesting.automation.title') }}</h2>
      <el-button type="primary" :disabled="!httpProjects.length" @click="openCreateSuite">{{ t('apiTesting.automation.createSuite') }}</el-button>
    </div>
    <el-alert v-if="pageError" :title="pageError" type="error" show-icon :closable="false" class="notice">
      <template #default><el-button link @click="loadProjects">{{ t('apiTesting.common.refresh') }}</el-button></template>
    </el-alert>
    <div class="content-layout">
      <aside class="sidebar">
        <el-select v-model="selectedProject" :placeholder="t('apiTesting.common.selectProject')" @change="onProjectChange">
          <el-option v-for="project in httpProjects" :key="project.id" :label="project.name" :value="project.id" />
        </el-select>
        <div class="section-header">
          <strong>{{ t('apiTesting.automation.testSuites') }}</strong>
          <el-button link :loading="suitesLoading" @click="loadTestSuites">{{ t('apiTesting.common.refresh') }}</el-button>
        </div>
        <el-empty v-if="!testSuites.length && !suitesLoading" :description="t('apiTesting.common.noData')" />
        <button v-for="suite in testSuites" :key="suite.id" type="button" class="suite-choice" :class="{ active: selectedSuite?.id === suite.id }" @click="selectSuite(suite)">
          <strong>{{ suite.name }}</strong>
          <span>{{ t('apiTesting.cases.suiteMemberCount', { n: (suite.suite_requests?.length || 0) + (suite.suite_cases?.length || 0) }) }}</span>
        </button>
      </aside>
      <main class="main-content" v-loading="detailLoading">
        <el-empty v-if="!selectedSuite" :description="t('apiTesting.automation.selectSuiteHint')" />
        <template v-else>
          <div class="suite-heading">
            <div><h3>{{ selectedSuite.name }}</h3><p>{{ selectedSuite.description }}</p></div>
            <div class="buttons">
              <el-button type="success" :loading="running" :disabled="mutating || hasRunningExecution || !suiteItems.some(item => item.enabled)" @click="runTestSuite(selectedSuite)">{{ t('apiTesting.automation.runTest') }}</el-button>
              <el-button :disabled="mutating || running" @click="editSuite(selectedSuite)">{{ t('apiTesting.common.edit') }}</el-button>
              <el-button :disabled="mutating || running" @click="duplicateSuite(selectedSuite)">{{ t('apiTesting.common.copy') }}</el-button>
              <el-button type="danger" plain :disabled="mutating || running" @click="deleteSuite(selectedSuite)">{{ t('apiTesting.common.delete') }}</el-button>
            </div>
          </div>
          <el-alert v-if="mutationError" :title="mutationError" type="error" :closable="false" show-icon class="notice" />
          <div class="section-header">
            <h4>{{ t('apiTesting.cases.suiteMembers') }}</h4>
            <el-button :disabled="mutating || running" @click="showAddItems">{{ t('apiTesting.cases.addSuiteItems') }}</el-button>
          </div>
          <el-table :data="suiteItems" row-key="key" v-loading="mutating">
            <el-table-column type="index" width="48" />
            <el-table-column :label="t('apiTesting.cases.memberType')" width="100">
              <template #default="{ row }"><el-tag :type="row.kind === 'case' ? 'primary' : 'info'">{{ t(`apiTesting.cases.${row.kind === 'case' ? 'caseType' : 'requestType'}`) }}</el-tag></template>
            </el-table-column>
            <el-table-column :label="t('apiTesting.common.name')" min-width="180">
              <template #default="{ row }">
                <router-link v-if="row.kind === 'case'" :to="`/api-testing/test-cases/${row.test_case.id}/edit`">{{ row.name }}</router-link>
                <span v-else>{{ row.name }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('apiTesting.cases.requestSummary')" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ row.kind === 'request' ? `${row.request.method} ${row.request.url}` : t('apiTesting.cases.stepCount', { n: row.test_case.step_count ?? row.test_case.steps?.length ?? 0 }) }}</template>
            </el-table-column>
            <el-table-column :label="t('apiTesting.automation.enabled')" width="80">
              <template #default="{ row }"><el-switch :model-value="row.enabled" :disabled="mutating || running" @change="value => updateMemberEnabled(row, value)" /></template>
            </el-table-column>
            <el-table-column :label="t('apiTesting.common.operation')" min-width="310">
              <template #default="{ row, $index }">
                <el-button v-if="row.kind === 'request'" link :disabled="mutating || running" @click="editAssertions(row)">{{ t('apiTesting.automation.editAssertions') }}</el-button>
                <el-button link :disabled="!$index || mutating || running" @click="moveItem($index, -1)">{{ t('apiTesting.cases.moveUp') }}</el-button>
                <el-button link :disabled="$index === suiteItems.length - 1 || mutating || running" @click="moveItem($index, 1)">{{ t('apiTesting.cases.moveDown') }}</el-button>
                <el-button link type="danger" :disabled="mutating || running" @click="removeMember(row)">{{ t('apiTesting.automation.remove') }}</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="section-header history-heading">
            <h4>{{ t('apiTesting.automation.executionHistory') }}</h4>
            <el-button link :loading="executionsLoading" @click="loadExecutions">{{ t('apiTesting.common.refresh') }}</el-button>
          </div>
          <el-alert v-if="executionError" :title="executionError" type="error" :closable="false" show-icon class="notice" />
          <el-table :data="executions" v-loading="executionsLoading">
            <el-table-column prop="id" label="ID" width="70" />
            <el-table-column prop="status" :label="t('apiTesting.common.status')" width="130">
              <template #default="{ row }"><el-tag :type="getStatusType(row.status)">{{ statusText(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="total_requests" :label="t('apiTesting.cases.suiteTotal')" width="100" />
            <el-table-column prop="passed_requests" :label="t('apiTesting.automation.passedCount')" width="90" />
            <el-table-column prop="failed_requests" :label="t('apiTesting.automation.failedCount')" width="90" />
            <el-table-column prop="created_at" :label="t('apiTesting.automation.executionTime')" min-width="170">
              <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
            </el-table-column>
            <el-table-column :label="t('apiTesting.common.operation')" width="110">
              <template #default="{ row }"><el-button link @click="viewExecutionDetail(row)">{{ t('apiTesting.automation.viewDetails') }}</el-button></template>
            </el-table-column>
          </el-table>
          <el-pagination v-if="executionTotal > executionPageSize" v-model:current-page="executionPage" :page-size="executionPageSize" :total="executionTotal" layout="prev, pager, next" @current-change="loadExecutions" />
        </template>
      </main>
    </div>

    <el-dialog v-model="showCreateSuiteDialog" :title="t(editingSuite ? 'apiTesting.automation.editSuite' : 'apiTesting.automation.createSuite')" width="min(600px, 95vw)" :close-on-click-modal="!submittingSuite">
      <el-form label-position="top" :model="suiteForm" @submit.prevent="submitSuiteForm">
        <el-form-item :label="t('apiTesting.automation.suiteName')" required><el-input v-model="suiteForm.name" maxlength="200" /></el-form-item>
        <el-form-item :label="t('apiTesting.automation.suiteDescription')"><el-input v-model="suiteForm.description" type="textarea" :rows="3" /></el-form-item>
        <el-form-item :label="t('apiTesting.automation.belongProject')" required>
          <el-select v-model="suiteForm.project" :disabled="Boolean(editingSuite)" @change="suiteForm.environment = null">
            <el-option v-for="project in httpProjects" :key="project.id" :label="project.name" :value="project.id" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('apiTesting.automation.executionEnvironment')">
          <el-select v-model="suiteForm.environment" clearable><el-option v-for="environment in suiteEnvironments" :key="environment.id" :label="environment.name" :value="environment.id" /></el-select>
        </el-form-item>
      </el-form>
      <el-alert v-if="dialogError" :title="dialogError" type="error" :closable="false" show-icon />
      <template #footer>
        <el-button :disabled="submittingSuite" @click="showCreateSuiteDialog = false">{{ t('apiTesting.common.cancel') }}</el-button>
        <el-button type="primary" :loading="submittingSuite" @click="submitSuiteForm">{{ t('apiTesting.common.save') }}</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showAddDialog" :title="t('apiTesting.cases.addSuiteItems')" width="min(760px, 95vw)" :close-on-click-modal="!addingItems">
      <el-tabs v-model="selectedKind">
        <el-tab-pane :label="t('apiTesting.cases.caseType')" name="case">
          <el-select v-model="selectedCaseIds" multiple filterable :loading="optionsLoading" :placeholder="t('apiTesting.cases.selectCases')" class="wide">
            <el-option v-for="item in availableCases" :key="item.id" :label="item.name" :value="item.id" :disabled="suiteItems.some(member => member.kind === 'case' && member.test_case.id === item.id)" />
          </el-select>
        </el-tab-pane>
        <el-tab-pane :label="t('apiTesting.cases.requestType')" name="request">
          <el-select v-model="selectedRequestIds" multiple filterable :loading="optionsLoading" :placeholder="t('apiTesting.cases.selectRequests')" class="wide">
            <el-option v-for="item in availableRequests" :key="item.id" :label="`${item.method} ${item.name}`" :value="item.id" :disabled="suiteItems.some(member => member.kind === 'request' && member.request.id === item.id)" />
          </el-select>
        </el-tab-pane>
      </el-tabs>
      <el-alert v-if="dialogError" :title="dialogError" type="error" :closable="false" show-icon class="notice" />
      <template #footer>
        <el-button :disabled="addingItems" @click="showAddDialog = false">{{ t('apiTesting.common.cancel') }}</el-button>
        <el-button type="primary" :disabled="optionsLoading" :loading="addingItems" @click="addSelectedItems">{{ t('apiTesting.common.create') }}</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showAssertionsDialog" :title="t('apiTesting.automation.editAssertions')" width="min(900px, 95vw)" :close-on-click-modal="!savingAssertions">
      <p>{{ t('apiTesting.cases.suiteAssertionHelp') }}</p>
      <AssertionEditor v-model="assertionDraft" />
      <el-alert v-if="dialogError" :title="dialogError" type="error" :closable="false" show-icon />
      <template #footer>
        <el-button :disabled="savingAssertions" @click="showAssertionsDialog = false">{{ t('apiTesting.common.cancel') }}</el-button>
        <el-button type="primary" :loading="savingAssertions" @click="saveAssertions">{{ t('apiTesting.common.save') }}</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showExecutionDialog" :title="t('apiTesting.automation.testExecutionResult')" width="min(1100px, 96vw)" top="5vh">
      <template v-if="currentExecution">
        <el-descriptions :column="3" border>
          <el-descriptions-item :label="t('apiTesting.common.status')">{{ statusText(currentExecution.status) }}</el-descriptions-item>
          <el-descriptions-item :label="t('apiTesting.automation.passedCount')">{{ currentExecution.passed_requests }}</el-descriptions-item>
          <el-descriptions-item :label="t('apiTesting.automation.failedCount')">{{ currentExecution.failed_requests }}</el-descriptions-item>
        </el-descriptions>
        <el-table :data="currentExecution.results || []" class="history-heading">
          <el-table-column type="expand"><template #default="{ row }"><pre class="result-json">{{ JSON.stringify(row, null, 2) }}</pre></template></el-table-column>
          <el-table-column prop="name" :label="t('apiTesting.common.name')" min-width="180" />
          <el-table-column :label="t('apiTesting.automation.result')" width="100"><template #default="{ row }"><el-tag :type="row.passed ? 'success' : 'danger'">{{ t(row.passed ? 'apiTesting.common.success' : 'apiTesting.common.failed') }}</el-tag></template></el-table-column>
          <el-table-column prop="status_code" :label="t('apiTesting.automation.statusCode')" width="100" />
          <el-table-column prop="response_time" :label="t('apiTesting.automation.responseTime')" width="120" />
          <el-table-column prop="error" :label="t('apiTesting.automation.errorMessage')" min-width="220" />
        </el-table>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import api from '@/utils/api'
import AssertionEditor from './components/AssertionEditor.vue'

const { t } = useI18n()
const projects = ref([])
const selectedProject = ref(null)
const testSuites = ref([])
const selectedSuite = ref(null)
const environments = ref([])
const executions = ref([])
const executionTotal = ref(0)
const executionPage = ref(1)
const executionPageSize = 20
const currentExecution = ref(null)
const pageError = ref('')
const mutationError = ref('')
const executionError = ref('')
const dialogError = ref('')
const suitesLoading = ref(false)
const detailLoading = ref(false)
const executionsLoading = ref(false)
const running = ref(false)
const mutating = ref(false)
const submittingSuite = ref(false)
const addingItems = ref(false)
const savingAssertions = ref(false)
const optionsLoading = ref(false)
const showCreateSuiteDialog = ref(false)
const showAddDialog = ref(false)
const showAssertionsDialog = ref(false)
const showExecutionDialog = ref(false)
const editingSuite = ref(null)
const assertionTarget = ref(null)
const assertionDraft = ref([])
const selectedKind = ref('case')
const selectedCaseIds = ref([])
const selectedRequestIds = ref([])
const availableCases = ref([])
const availableRequests = ref([])
const suiteForm = reactive({ name: '', description: '', project: null, environment: null })
let alive = true
let projectVersion = 0
let listVersion = 0
let detailVersion = 0
let historyVersion = 0
let optionsVersion = 0
let selectionVersion = 0

const httpProjects = computed(() => projects.value.filter(project => project.project_type !== 'WEBSOCKET'))
const suiteEnvironments = computed(() => environments.value.filter(environment =>
  environment.scope === 'GLOBAL' || Number(environment.project) === Number(suiteForm.project)
))
const suiteItems = computed(() => {
  const suite = selectedSuite.value || {}
  const requests = (suite.suite_requests || []).map(item => ({ ...item, kind: 'request', key: `request:${item.id}`, name: item.request?.name || '', enabled: item.enabled !== false }))
  const cases = (suite.suite_cases || []).map(item => ({ ...item, kind: 'case', key: `case:${item.id}`, name: item.test_case?.name || item.test_case_name || '', enabled: item.enabled !== false }))
  return [...requests, ...cases].sort((left, right) =>
    (Number(left.order) || 0) - (Number(right.order) || 0) || (left.kind === right.kind ? Number(left.id) - Number(right.id) : left.kind === 'request' ? -1 : 1)
  )
})
const hasRunningExecution = computed(() => executions.value.some(item => ['PENDING', 'RUNNING'].includes(item.status)))
const errorText = error => {
  const data = error.response?.data
  const detail = data?.detail || data?.error
  return typeof detail === 'string' ? detail : data ? JSON.stringify(data) : error.message || t('apiTesting.cases.operationFailed')
}
const isCurrent = (suiteId, version = selectionVersion) => alive && version === selectionVersion && selectedSuite.value?.id === suiteId
const memberPath = item => `/api-testing/${item.kind === 'case' ? 'test-suite-cases' : 'test-suite-requests'}/${item.id}/`
const clone = value => JSON.parse(JSON.stringify(value))
const getStatusType = status => ({ COMPLETED: 'success', FAILED: 'danger', RUNNING: 'warning' }[status] || 'info')
const statusText = status => t(`apiTesting.automation.status.${({ PENDING: 'pending', RUNNING: 'running', COMPLETED: 'completed', FAILED: 'failed', CANCELLED: 'cancelled' })[status] || 'pending'}`)
const formatDate = value => value ? new Date(value).toLocaleString() : '-'

async function getAll(url, params = {}) {
  const rows = new Map()
  for (let page = 1; page <= 1000; page += 1) {
    const { data } = await api.get(url, { params: { ...params, page, page_size: 100 } })
    const items = Array.isArray(data) ? data : data.results || []
    const previousCount = rows.size
    for (const item of items) rows.set(item.id, item)
    if (Array.isArray(data) || (!data.next && (data.count == null || rows.size >= data.count))) return [...rows.values()]
    if (!items.length || rows.size === previousCount) throw new Error(t('apiTesting.cases.incompleteOptions'))
  }
  throw new Error(t('apiTesting.cases.incompleteOptions'))
}

async function loadProjects() {
  const version = ++projectVersion
  pageError.value = ''
  const [projectResult, environmentResult] = await Promise.allSettled([
    getAll('/api-testing/projects/'),
    getAll('/api-testing/environments/')
  ])
  if (!alive || version !== projectVersion) return

  if (environmentResult.status === 'fulfilled') {
    environments.value = environmentResult.value
  } else {
    environments.value = []
    pageError.value = errorText(environmentResult.reason)
  }

  if (projectResult.status === 'rejected') {
    pageError.value = errorText(projectResult.reason)
    return
  }

  projects.value = projectResult.value
  if (!httpProjects.value.some(project => project.id === selectedProject.value)) selectedProject.value = httpProjects.value[0]?.id || null
  await onProjectChange()
}

async function loadTestSuites() {
  const project = selectedProject.value
  const version = ++listVersion
  if (!project) { testSuites.value = []; return }
  suitesLoading.value = true
  try {
    const rows = await getAll('/api-testing/test-suites/', { project })
    if (alive && version === listVersion && project === selectedProject.value) testSuites.value = rows
  } catch (error) {
    if (alive && version === listVersion && project === selectedProject.value) pageError.value = errorText(error)
  } finally {
    if (alive && version === listVersion) suitesLoading.value = false
  }
}

async function onProjectChange() {
  selectionVersion += 1
  detailVersion += 1
  historyVersion += 1
  optionsVersion += 1
  selectedSuite.value = null
  testSuites.value = []
  executions.value = []
  executionTotal.value = 0
  executionPage.value = 1
  detailLoading.value = false
  executionsLoading.value = false
  optionsLoading.value = false
  showAddDialog.value = false
  showAssertionsDialog.value = false
  showExecutionDialog.value = false
  mutationError.value = ''
  executionError.value = ''
  await loadTestSuites()
}

async function selectSuite(suite) {
  if (Number(suite.project) !== Number(selectedProject.value)) return
  selectionVersion += 1
  selectedSuite.value = suite
  executions.value = []
  executionPage.value = 1
  mutationError.value = ''
  executionError.value = ''
  showAddDialog.value = false
  showAssertionsDialog.value = false
  showExecutionDialog.value = false
  await Promise.all([reloadCurrentSuite(), loadExecutions()])
}

async function reloadCurrentSuite() {
  const suiteId = selectedSuite.value?.id
  const selection = selectionVersion
  const version = ++detailVersion
  if (!suiteId) return
  detailLoading.value = true
  try {
    const { data } = await api.get(`/api-testing/test-suites/${suiteId}/`)
    if (!isCurrent(suiteId, selection) || version !== detailVersion) return
    selectedSuite.value = data
    const index = testSuites.value.findIndex(item => item.id === suiteId)
    if (index >= 0) testSuites.value[index] = data
  } catch (error) {
    if (isCurrent(suiteId, selection) && version === detailVersion) mutationError.value = errorText(error)
  } finally {
    if (isCurrent(suiteId, selection) && version === detailVersion) detailLoading.value = false
  }
}

async function loadExecutions() {
  const suiteId = selectedSuite.value?.id
  const selection = selectionVersion
  const version = ++historyVersion
  if (!suiteId) return
  executionsLoading.value = true
  executionError.value = ''
  try {
    const { data } = await api.get('/api-testing/test-executions/', { params: { test_suite: suiteId, page: executionPage.value, page_size: executionPageSize } })
    if (!isCurrent(suiteId, selection) || version !== historyVersion) return
    executions.value = Array.isArray(data) ? data : data.results || []
    executionTotal.value = data.count ?? executions.value.length
  } catch (error) {
    if (isCurrent(suiteId, selection) && version === historyVersion) executionError.value = errorText(error)
  } finally {
    if (isCurrent(suiteId, selection) && version === historyVersion) executionsLoading.value = false
  }
}

function openCreateSuite() {
  editingSuite.value = null
  Object.assign(suiteForm, { name: '', description: '', project: selectedProject.value, environment: null })
  dialogError.value = ''
  showCreateSuiteDialog.value = true
}
function editSuite(suite) {
  editingSuite.value = suite
  Object.assign(suiteForm, { name: suite.name, description: suite.description || '', project: suite.project, environment: suite.environment || null })
  dialogError.value = ''
  showCreateSuiteDialog.value = true
}
async function submitSuiteForm() {
  if (submittingSuite.value) return
  if (!suiteForm.name.trim() || !suiteForm.project) { dialogError.value = t('apiTesting.cases.requiredSuiteFields'); return }
  submittingSuite.value = true
  dialogError.value = ''
  const project = selectedProject.value
  try {
    const payload = { ...suiteForm, name: suiteForm.name.trim(), environment: suiteForm.environment || null }
    if (editingSuite.value) await api.patch(`/api-testing/test-suites/${editingSuite.value.id}/`, payload)
    else await api.post('/api-testing/test-suites/', payload)
    if (!alive) return
    showCreateSuiteDialog.value = false
    if (project === selectedProject.value) {
      await loadTestSuites()
      if (selectedSuite.value) await reloadCurrentSuite()
    }
  } catch (error) {
    if (alive) dialogError.value = errorText(error)
  } finally {
    if (alive) submittingSuite.value = false
  }
}

async function duplicateSuite(suite) {
  if (mutating.value || running.value) return
  mutating.value = true
  mutationError.value = ''
  const selection = selectionVersion
  try {
    const name = `${suite.name} - ${t('apiTesting.common.copy')}`.slice(0, 200)
    await api.post(`/api-testing/test-suites/${suite.id}/duplicate/`, { name })
    if (isCurrent(suite.id, selection)) await loadTestSuites()
  } catch (error) {
    if (isCurrent(suite.id, selection)) mutationError.value = errorText(error)
  } finally {
    if (alive) mutating.value = false
  }
}

async function deleteSuite(suite) {
  if (mutating.value || running.value) return
  const selection = selectionVersion
  try {
    await ElMessageBox.confirm(t('apiTesting.automation.confirmDeleteSuite', { name: suite.name }), t('apiTesting.common.confirm'), { type: 'warning' })
    if (!isCurrent(suite.id, selection)) return
    mutating.value = true
    await api.delete(`/api-testing/test-suites/${suite.id}/`)
    if (!isCurrent(suite.id, selection)) return
    selectionVersion += 1
    selectedSuite.value = null
    executions.value = []
    await loadTestSuites()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close' && isCurrent(suite.id, selection)) mutationError.value = errorText(error)
  } finally {
    if (alive) mutating.value = false
  }
}

async function showAddItems() {
  const suiteId = selectedSuite.value?.id
  if (!suiteId) return
  const selection = selectionVersion
  const version = ++optionsVersion
  showAddDialog.value = true
  optionsLoading.value = true
  dialogError.value = ''
  selectedCaseIds.value = []
  selectedRequestIds.value = []
  availableCases.value = []
  availableRequests.value = []
  try {
    const [cases, requests] = await Promise.all([
      getAll('/api-testing/test-cases/', { project: selectedProject.value }),
      getAll('/api-testing/requests/', { project: selectedProject.value, request_type: 'HTTP' })
    ])
    if (!isCurrent(suiteId, selection) || version !== optionsVersion) return
    availableCases.value = cases.filter(item => item.status !== 'deprecated')
    availableRequests.value = requests.filter(item => item.collection && item.request_type !== 'WEBSOCKET')
  } catch (error) {
    if (isCurrent(suiteId, selection) && version === optionsVersion) dialogError.value = errorText(error)
  } finally {
    if (isCurrent(suiteId, selection) && version === optionsVersion) optionsLoading.value = false
  }
}

async function addSelectedItems() {
  if (addingItems.value || optionsLoading.value) return
  const suiteId = selectedSuite.value?.id
  const selection = selectionVersion
  const isCase = selectedKind.value === 'case'
  const ids = [...new Set(isCase ? selectedCaseIds.value : selectedRequestIds.value)]
  if (!suiteId || !ids.length) { dialogError.value = t('apiTesting.cases.selectAtLeastOneItem'); return }
  addingItems.value = true
  dialogError.value = ''
  try {
    await api.post(`/api-testing/test-suites/${suiteId}/${isCase ? 'add-cases' : 'add-requests'}/`, isCase ? { case_ids: ids } : { request_ids: ids })
    if (!isCurrent(suiteId, selection)) return
    showAddDialog.value = false
    await reloadCurrentSuite()
  } catch (error) {
    if (isCurrent(suiteId, selection)) dialogError.value = errorText(error)
  } finally {
    if (alive) addingItems.value = false
  }
}

function editAssertions(item) {
  if (item.kind === 'case') return
  assertionTarget.value = { ...item, suiteId: selectedSuite.value?.id, selection: selectionVersion }
  assertionDraft.value = clone(item.assertions || []).map(assertion => {
    if (assertion.type === 'status_code' && assertion.expected === undefined) assertion.expected = assertion.value
    delete assertion.value
    return assertion
  })
  dialogError.value = ''
  showAssertionsDialog.value = true
}
async function saveAssertions() {
  const target = assertionTarget.value
  if (!target || savingAssertions.value || !isCurrent(target.suiteId, target.selection)) return
  savingAssertions.value = true
  dialogError.value = ''
  try {
    await api.patch(memberPath(target), { assertions: clone(assertionDraft.value) })
    if (!isCurrent(target.suiteId, target.selection)) return
    showAssertionsDialog.value = false
    await reloadCurrentSuite()
  } catch (error) {
    if (isCurrent(target.suiteId, target.selection)) dialogError.value = errorText(error)
  } finally {
    if (alive) savingAssertions.value = false
  }
}

async function updateMemberEnabled(item, enabled) {
  if (mutating.value || running.value) return
  const suiteId = selectedSuite.value?.id
  const selection = selectionVersion
  mutating.value = true
  mutationError.value = ''
  try {
    await api.patch(memberPath(item), { enabled })
    if (isCurrent(suiteId, selection)) await reloadCurrentSuite()
  } catch (error) {
    if (isCurrent(suiteId, selection)) mutationError.value = errorText(error)
  } finally {
    if (alive) mutating.value = false
  }
}

async function moveItem(index, direction) {
  if (mutating.value || running.value) return
  const next = [...suiteItems.value]
  const target = index + direction
  if (target < 0 || target >= next.length) return
  ;[next[index], next[target]] = [next[target], next[index]]
  const suiteId = selectedSuite.value?.id
  const selection = selectionVersion
  mutating.value = true
  mutationError.value = ''
  try {
    await api.post(`/api-testing/test-suites/${suiteId}/reorder/`, { items: next.map(item => ({ kind: item.kind, id: item.kind === 'case' ? item.test_case.id : item.request.id })) })
    if (isCurrent(suiteId, selection)) await reloadCurrentSuite()
  } catch (error) {
    if (isCurrent(suiteId, selection)) mutationError.value = errorText(error)
  } finally {
    if (alive) mutating.value = false
  }
}

async function removeMember(item) {
  if (mutating.value || running.value) return
  const suiteId = selectedSuite.value?.id
  const selection = selectionVersion
  try {
    await ElMessageBox.confirm(t('apiTesting.automation.confirmRemoveRequest'), t('apiTesting.common.confirm'), { type: 'warning' })
    if (!isCurrent(suiteId, selection)) return
    mutating.value = true
    await api.delete(memberPath(item))
    if (isCurrent(suiteId, selection)) await reloadCurrentSuite()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close' && isCurrent(suiteId, selection)) mutationError.value = errorText(error)
  } finally {
    if (alive) mutating.value = false
  }
}

async function runTestSuite(suite) {
  if (running.value || mutating.value || hasRunningExecution.value) return
  const selection = selectionVersion
  running.value = true
  executionError.value = ''
  try {
    const { data } = await api.post(`/api-testing/test-suites/${suite.id}/execute/`, {}, { timeout: 120000 })
    if (!isCurrent(suite.id, selection)) return
    currentExecution.value = data
    showExecutionDialog.value = true
    await loadExecutions()
  } catch (error) {
    if (isCurrent(suite.id, selection)) {
      const message = `${errorText(error)} ${t('apiTesting.cases.refreshBeforeRetry')}`
      await loadExecutions()
      if (isCurrent(suite.id, selection)) executionError.value = [message, executionError.value].filter(Boolean).join(' ')
    }
  } finally {
    if (alive) running.value = false
  }
}
function viewExecutionDetail(execution) {
  currentExecution.value = execution
  showExecutionDialog.value = true
}

onMounted(loadProjects)
onBeforeUnmount(() => {
  alive = false
  projectVersion += 1
  listVersion += 1
  selectionVersion += 1
  detailVersion += 1
  historyVersion += 1
  optionsVersion += 1
})
</script>

<style scoped>
.automation-testing { display: flex; flex-direction: column; gap: 20px; min-height: 70vh; }
.header, .suite-heading, .section-header, .buttons { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
h2, h3, h4, p { margin: 0; }
.suite-heading p { margin-top: 8px; color: var(--el-text-color-secondary); }
.content-layout { display: grid; grid-template-columns: 260px minmax(0, 1fr); gap: 20px; align-items: start; }
.sidebar, .main-content { background: var(--el-bg-color); border: 1px solid var(--el-border-color); border-radius: 8px; padding: 20px; }
.sidebar { display: flex; flex-direction: column; gap: 12px; }
.main-content { min-height: 420px; min-width: 0; }
.suite-choice { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; padding: 12px; text-align: left; border: 1px solid var(--el-border-color-light); border-radius: 6px; color: var(--el-text-color-primary); background: var(--el-fill-color-blank); cursor: pointer; }
.suite-choice span { font-size: 12px; color: var(--el-text-color-secondary); }
.suite-choice.active { border-color: var(--el-color-primary); background: var(--el-color-primary-light-9); }
.section-header { margin: 18px 0 12px; }
.buttons { justify-content: flex-end; flex-wrap: wrap; }
.buttons :deep(.el-button + .el-button) { margin-left: 0; }
.notice, .history-heading { margin-top: 16px; }
.wide, .el-select { width: 100%; }
.result-json { max-height: 420px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; padding: 16px; color: var(--el-text-color-primary); background: var(--el-fill-color-light); }
@media (max-width: 1000px) { .content-layout { grid-template-columns: 1fr; } .suite-heading { align-items: flex-start; flex-direction: column; } }
</style>
