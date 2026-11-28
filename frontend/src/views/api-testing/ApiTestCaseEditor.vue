<template>
  <main class="api-case-editor">
    <header class="page-heading">
      <el-button text @click="backToList">{{ tx('backToCases', '返回用例列表') }}</el-button>
      <div class="heading-actions">
        <el-button v-if="caseRecord" :disabled="!canRun" :loading="running" @click="runCase">{{ tx('runCase', '执行用例') }}</el-button>
        <el-button v-if="caseRecord" :disabled="initialLoading" @click="showHistory = true">{{ tx('executionHistory', '执行历史') }}</el-button>
        <el-button type="primary" :disabled="!canSave" :loading="saving" @click="saveCase">{{ tx('saveCase', '保存用例') }}</el-button>
      </div>
    </header>
    <h1>{{ isEditing ? tx('editCaseTitle', '编辑接口测试用例') : tx('createCaseTitle', '新建接口测试用例') }}</h1>
    <el-alert v-if="pageError" :title="pageError" type="error" show-icon :closable="false" class="notice" />
    <el-alert v-if="prefillNotice" :title="prefillNotice" type="warning" show-icon :closable="false" class="notice" />
    <el-alert v-if="caseRecord && isDirty" :title="tx('saveBeforeRun', '请先保存修改，再执行此用例。')" type="info" show-icon :closable="false" class="notice" />
    <div v-if="initialLoading" v-loading="true" class="loading-space" />
    <template v-else>
      <section class="form-section">
        <h2>{{ tx('caseInformation', '基本信息') }}</h2>
        <div class="form-grid">
          <el-form-item :label="tx('project', 'API 项目')" required>
            <el-select v-model="form.project" :disabled="switchingProject || !projects.length" @change="onProjectChange">
              <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
            </el-select>
          </el-form-item>
          <el-form-item :label="tx('caseName', '用例名称')" required>
            <el-input v-model="form.name" maxlength="200" />
          </el-form-item>
          <el-form-item :label="tx('priorityLabel', '优先级')">
            <el-select v-model="form.priority">
              <el-option v-for="item in priorities" :key="item.value" :label="tx(item.key, item.fallback)" :value="item.value" />
            </el-select>
          </el-form-item>
          <el-form-item :label="tx('caseStatusLabel', '状态')">
            <el-select v-model="form.status">
              <el-option v-for="item in statuses" :key="item.value" :label="tx(item.key, item.fallback)" :value="item.value" />
            </el-select>
          </el-form-item>
          <el-form-item class="span-two" :label="tx('description', '描述')">
            <el-input v-model="form.description" type="textarea" :rows="2" />
          </el-form-item>
          <el-form-item class="span-two" :label="tx('preconditions', '前置条件')">
            <el-input v-model="form.preconditions" type="textarea" :rows="2" />
          </el-form-item>
          <el-form-item class="span-two" :label="tx('expectedResult', '预期结果')">
            <el-input v-model="form.expected_result" type="textarea" :rows="2" />
          </el-form-item>
          <el-form-item class="span-two" :label="tx('tags', '标签')">
            <el-select v-model="form.tags" multiple filterable allow-create default-first-option class="wide" />
          </el-form-item>
          <el-form-item class="span-two" :label="tx('initialVariables', '初始变量（JSON 对象）')">
            <el-input v-model="variablesText" type="textarea" :rows="4" spellcheck="false" />
          </el-form-item>
          <el-form-item :label="tx('executionEnvironment', '执行环境')">
            <el-select v-model="form.environment" clearable :loading="optionsLoading.environments" :placeholder="tx('noEnvironment', '使用用例默认环境')">
              <el-option v-for="environment in environments" :key="environment.id" :label="environment.name" :value="environment.id" />
            </el-select>
          </el-form-item>
          <el-form-item :label="tx('stopOnFailure', '步骤失败后停止')">
            <el-switch v-model="form.stop_on_failure" />
          </el-form-item>
          <el-form-item class="span-two" :label="tx('linkedGenericCase', '关联通用测试用例')">
            <el-select v-model="form.linked_testcase" clearable filterable :loading="optionsLoading.linkedCases" :placeholder="tx('selectLinkedCase', '不关联')">
              <el-option v-for="item in linkedCases" :key="item.id" :label="linkedCaseLabel(item)" :value="item.id" />
            </el-select>
          </el-form-item>
        </div>
        <el-alert v-if="optionErrors.environments" :title="optionErrors.environments" type="error" :closable="false" show-icon>
          <template #default><el-button link @click="retryOptions">{{ tx('retryOptions', '重新加载') }}</el-button></template>
        </el-alert>
        <el-alert v-if="optionErrors.linkedCases" :title="optionErrors.linkedCases" type="error" :closable="false" show-icon>
          <template #default><el-button link @click="retryOptions">{{ tx('retryOptions', '重新加载') }}</el-button></template>
        </el-alert>
      </section>

      <section class="form-section steps-section">
        <div class="section-heading">
          <div>
            <h2>{{ tx('caseSteps', '执行步骤') }}</h2>
            <span class="step-count">{{ steps.length }} / 50</span>
          </div>
          <el-button :disabled="steps.length >= 50 || optionsLoading.requests || Boolean(optionErrors.requests) || !form.project" @click="addStep">{{ tx('addStep', '添加步骤') }}</el-button>
        </div>
        <el-alert v-if="optionErrors.requests" :title="optionErrors.requests" type="error" show-icon :closable="false">
          <template #default><el-button link @click="loadProjectOptions(form.project)">{{ tx('retryOptions', '重新加载') }}</el-button></template>
        </el-alert>
        <el-empty v-if="!steps.length" :description="tx('noSteps', '尚未添加请求步骤')" :image-size="56" />
        <article v-for="(step, index) in steps" :key="step.key" class="step-editor">
          <header class="step-heading">
            <div class="step-title"><strong>{{ tx('step', '步骤') }} {{ index + 1 }}</strong><span v-if="step.name">{{ step.name }}</span></div>
            <div class="step-actions">
              <el-switch v-model="step.enabled" :active-text="tx('enabled', '启用')" />
              <el-button text :disabled="index === 0" :aria-label="tx('moveStepUp', '上移步骤')" @click="moveStep(index, -1)">↑</el-button>
              <el-button text :disabled="index === steps.length - 1" :aria-label="tx('moveStepDown', '下移步骤')" @click="moveStep(index, 1)">↓</el-button>
              <el-button text type="danger" :aria-label="tx('removeStep', '删除步骤')" @click="removeStep(index)">{{ tx('remove', '删除') }}</el-button>
            </div>
          </header>
          <el-alert v-if="stepErrors[index]" :title="stepErrors[index]" type="error" :closable="false" show-icon class="step-error" />
          <div class="step-fields">
            <el-form-item :label="tx('savedHttpRequest', '已保存 HTTP 请求')" required>
              <el-select v-model="step.request" filterable :loading="optionsLoading.requests" :placeholder="tx('selectRequest', '选择当前项目中的请求')" @change="value => changeStepRequest(step, value)">
                <el-option v-for="requestOption in requests" :key="requestOption.id" :label="`${requestOption.method} ${requestOption.name}`" :value="requestOption.id" />
              </el-select>
            </el-form-item>
            <el-form-item :label="tx('stepName', '步骤名称')">
              <el-input v-model="step.name" maxlength="200" />
            </el-form-item>
          </div>
          <div class="override-grid">
            <el-form-item :label="tx('paramsOverride', '参数覆盖（JSON 对象）')">
              <el-input v-model="step.overrideText.params" type="textarea" :rows="3" spellcheck="false" />
            </el-form-item>
            <el-form-item :label="tx('headersOverride', '请求头覆盖（JSON 对象）')">
              <el-input v-model="step.overrideText.headers" type="textarea" :rows="3" spellcheck="false" />
            </el-form-item>
            <el-form-item :label="tx('bodyOverride', '请求体覆盖（JSON 对象）')">
              <el-input v-model="step.overrideText.body" type="textarea" :rows="3" spellcheck="false" />
            </el-form-item>
          </div>
          <div class="assertion-heading">
            <el-checkbox :model-value="step.assertions !== null" @change="value => toggleAssertions(step, value)">{{ tx('overrideAssertions', '覆盖请求定义中的断言') }}</el-checkbox>
            <span v-if="step.assertions === null" class="inherit-note">{{ tx('inheritAssertions', '当前继承已保存请求的断言') }}</span>
          </div>
          <AssertionEditor v-if="step.assertions !== null" v-model="step.assertions" />
          <el-form-item :label="tx('extractors', '响应变量提取器（JSON 数组）')">
            <el-input v-model="step.extractorsText" type="textarea" :rows="3" spellcheck="false" />
          </el-form-item>
        </article>
      </section>

      <div class="form-footer">
        <el-button @click="backToList">{{ tx('cancel', '取消') }}</el-button>
        <el-button v-if="caseRecord" :disabled="!canRun" :loading="running" @click="runCase">{{ tx('runCase', '执行用例') }}</el-button>
        <el-button type="primary" :disabled="!canSave" :loading="saving" @click="saveCase">{{ tx('saveCase', '保存用例') }}</el-button>
      </div>
    </template>

    <ApiCaseRunPanel
      v-model:visible="showRunPanel"
      :execution="currentExecution"
      @update:execution="currentExecution = $event"
      @history="openHistory"
      @poll-limit="handlePollLimit"
    />
    <ApiCaseExecutionHistory
      v-model:visible="showHistory"
      :test-case-id="caseRecord?.id"
      @select="viewExecution"
    />
  </main>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/utils/api'
import {
  createApiTestCase,
  executeApiTestCase,
  getApiTestCase,
  updateApiTestCase
} from '@/api/api-test-cases'
import { fetchAllApiPages } from '@/utils/api-test-case-pagination.mjs'
import AssertionEditor from './components/AssertionEditor.vue'
import ApiCaseRunPanel from './components/ApiCaseRunPanel.vue'
import ApiCaseExecutionHistory from './components/ApiCaseExecutionHistory.vue'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const projects = ref([])
const environments = ref([])
const requests = ref([])
const linkedCases = ref([])
const steps = ref([])
const caseRecord = ref(null)
const form = reactive({
  project: null,
  name: '',
  description: '',
  preconditions: '',
  expected_result: '',
  priority: 'medium',
  status: 'draft',
  tags: [],
  environment: null,
  stop_on_failure: true,
  linked_testcase: null
})
const variablesText = ref('{}')
const initialLoading = ref(true)
const saving = ref(false)
const running = ref(false)
const switchingProject = ref(false)
const pageError = ref('')
const prefillNotice = ref('')
const optionErrors = reactive({ requests: '', environments: '', linkedCases: '' })
const optionsLoading = reactive({ requests: false, environments: false, linkedCases: false })
const stepErrors = ref({})
const showRunPanel = ref(false)
const showHistory = ref(false)
const currentExecution = ref(null)
const pollLimitReached = ref(false)
let alive = true
let initVersion = 0
let optionsVersion = 0
let linkedCasesVersion = 0
let projectSwitchVersion = 0
let retryAttempt = null
let currentProjectId = null

const priorities = [
  { value: 'low', key: 'priority.low', fallback: '低' },
  { value: 'medium', key: 'priority.medium', fallback: '中' },
  { value: 'high', key: 'priority.high', fallback: '高' },
  { value: 'critical', key: 'priority.critical', fallback: '紧急' }
]
const statuses = [
  { value: 'draft', key: 'caseStatus.draft', fallback: '草稿' },
  { value: 'active', key: 'caseStatus.active', fallback: '启用' },
  { value: 'deprecated', key: 'caseStatus.deprecated', fallback: '废弃' }
]
const isEditing = computed(() => Boolean(route.params?.id))
const canSave = computed(() => !initialLoading.value && !saving.value && !switchingProject.value && Boolean(form.project) && !optionsLoading.requests && !optionsLoading.environments && !optionErrors.requests && !optionErrors.environments)
const canRun = computed(() => Boolean(caseRecord.value?.id) && !running.value && !saving.value && !isDirty.value)
const savedSnapshot = ref('')
const isDirty = computed(() => stateSnapshot() !== savedSnapshot.value)

function tx(key, fallback, values) {
  const fullKey = `apiTesting.cases.${key}`
  const translated = values ? t(fullKey, values) : t(fullKey)
  return translated === fullKey ? fallback : translated
}
function responseData(response) { return response?.data ?? response }
function errorText(error) {
  const data = error?.response?.data
  const detail = data?.detail || data?.error
  return typeof detail === 'string' ? detail : data ? JSON.stringify(data) : error?.message || tx('operationFailed', '操作失败，请重试')
}
function clone(value) { return JSON.parse(JSON.stringify(value)) }
function asObject(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${label} ${tx('mustBeJsonObject', '必须是 JSON 对象')}`)
  return value
}
function parseJsonObject(text, label) {
  if (!String(text || '').trim()) return undefined
  let value
  try { value = JSON.parse(text) } catch { throw new Error(`${label} ${tx('invalidJson', 'JSON 格式无效')}`) }
  return asObject(value, label)
}
function normalizeAssertions(value) {
  if (!Array.isArray(value)) return []
  return value.map(item => {
    const assertion = { ...item }
    if (assertion.expected === undefined) {
      if (assertion.type === 'status_code' && assertion.value !== undefined) assertion.expected = assertion.value
      else if (assertion.type === 'header' && assertion.expected_value !== undefined) assertion.expected = assertion.expected_value
    }
    delete assertion.value
    delete assertion.expected_value
    return assertion
  })
}
function jsonText(value, fallback) {
  try { return JSON.stringify(value ?? fallback, null, 2) } catch { return JSON.stringify(fallback, null, 2) }
}
function makeStep(data = {}) {
  const overrides = data.request_overrides && typeof data.request_overrides === 'object' ? data.request_overrides : {}
  const assertionOverride = data.assertions != null
  return {
    key: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
    request: data.request == null ? null : Number(data.request),
    name: data.name || data.request_name || '',
    enabled: data.enabled !== false,
    assertionOverride,
    assertions: assertionOverride ? normalizeAssertions(data.assertions) : null,
    overrideText: {
      params: jsonText(overrides.params, {}),
      headers: jsonText(overrides.headers, {}),
      body: Object.prototype.hasOwnProperty.call(overrides, 'body') ? jsonText(overrides.body, null) : ''
    },
    extractorsText: jsonText(data.extractors, [])
  }
}
function stateSnapshot() {
  return JSON.stringify({
    form: { ...form, tags: [...form.tags] },
    variablesText: variablesText.value,
    steps: steps.value.map(step => ({
      request: step.request, name: step.name, enabled: step.enabled,
      assertions: step.assertions, overrideText: step.overrideText, extractorsText: step.extractorsText
    }))
  })
}
function linkedCaseLabel(item) {
  return `#${item.id} · ${item.title || tx('unnamedCase', '未命名用例')}${item.project_name ? ` · ${item.project_name}` : item.project ? ` · ${tx('genericProject', '项目')} ${item.project}` : ''}`
}

async function loadInitial() {
  const token = ++initVersion
  initialLoading.value = true
  pageError.value = ''
  prefillNotice.value = ''
  try {
    const [projectRows, detail] = await Promise.all([
      fetchAllApiPages(api.get, '/api-testing/projects/', {}),
      isEditing.value ? getApiTestCase(route.params.id).then(responseData) : Promise.resolve(null)
    ])
    if (!alive || token !== initVersion) return
    projects.value = projectRows.filter(project => project.project_type !== 'WEBSOCKET').map(project => ({ id: Number(project.id), name: project.name }))
    if (detail) hydrateCase(detail)
    const queryProject = route.query?.project
    const queryProjectRow = projects.value.find(project => Number(project.id) === Number(queryProject))
    const desiredProject = detail?.project ?? queryProjectRow?.id ?? projects.value[0]?.id ?? null
    if (queryProject && !queryProjectRow && !detail) {
      prefillNotice.value = tx('requestProjectUnavailable', '来源项目不可用，请选择有权限的 API 项目。')
    }
    if (desiredProject == null) {
      pageError.value = tx('noApiProjects', '没有可用的 HTTP API 项目。')
      return
    }
    if (!projects.value.some(project => Number(project.id) === Number(desiredProject))) {
      pageError.value = tx('caseProjectUnavailable', '用例项目不可用或无权访问。')
      return
    }
    form.project = Number(desiredProject)
    currentProjectId = Number(desiredProject)
    await Promise.all([loadProjectOptions(form.project), loadLinkedCases(token)])
    if (!alive || token !== initVersion) return
    if (route.query?.request_id && !detail) await prefillRequest(route.query.request_id, form.project, token)
    savedSnapshot.value = stateSnapshot()
  } catch (error) {
    if (alive && token === initVersion) pageError.value = errorText(error)
  } finally {
    if (alive && token === initVersion) initialLoading.value = false
  }
}

function hydrateCase(data) {
  caseRecord.value = data
  Object.assign(form, {
    project: Number(data.project),
    name: data.name || '',
    description: data.description || '',
    preconditions: data.preconditions || '',
    expected_result: data.expected_result || '',
    priority: data.priority || 'medium',
    status: data.status || 'draft',
    tags: Array.isArray(data.tags) ? [...data.tags] : [],
    environment: data.environment == null ? null : Number(data.environment),
    stop_on_failure: data.stop_on_failure !== false,
    linked_testcase: data.linked_testcase == null ? null : Number(data.linked_testcase)
  })
  variablesText.value = jsonText(data.variables, {})
  steps.value = (data.steps || []).map(makeStep)
}

async function loadProjectOptions(projectId) {
  const token = ++optionsVersion
  if (projectId == null) return
  optionsLoading.requests = true
  optionsLoading.environments = true
  optionErrors.requests = ''
  optionErrors.environments = ''
  requests.value = []
  environments.value = []
  const [requestResult, environmentResult] = await Promise.allSettled([
    fetchAllApiPages(api.get, '/api-testing/requests/', { project: projectId, request_type: 'HTTP' }),
    fetchAllApiPages(api.get, '/api-testing/environments/', {})
  ])
  if (!alive || token !== optionsVersion || Number(form.project) !== Number(projectId)) return
  if (requestResult.status === 'fulfilled') {
    requests.value = requestResult.value
      .filter(item => item.request_type === 'HTTP' && item.collection != null)
      .map(item => ({ id: Number(item.id), collection: Number(item.collection), name: item.name, method: item.method, url: item.url }))
  } else {
    optionErrors.requests = errorText(requestResult.reason)
  }
  if (environmentResult.status === 'fulfilled') {
    environments.value = environmentResult.value
      .filter(item => item.scope === 'GLOBAL' || Number(item.project) === Number(projectId))
      .map(item => ({ id: Number(item.id), name: item.name, scope: item.scope, project: item.project }))
    if (form.environment != null && !environments.value.some(item => item.id === Number(form.environment))) form.environment = null
  } else {
    optionErrors.environments = errorText(environmentResult.reason)
  }
  optionsLoading.requests = false
  optionsLoading.environments = false
}

async function loadLinkedCases(initToken = initVersion) {
  const token = ++linkedCasesVersion
  optionsLoading.linkedCases = true
  optionErrors.linkedCases = ''
  try {
    const rows = await fetchAllApiPages(api.get, '/testcases/', {})
    if (!alive || token !== linkedCasesVersion || initToken !== initVersion) return
    linkedCases.value = rows.map(item => ({ id: Number(item.id), title: item.title, project: item.project, project_name: item.project_name || item.project?.name || '' }))
  } catch (error) {
    if (alive && token === linkedCasesVersion && initToken === initVersion) optionErrors.linkedCases = errorText(error)
  } finally {
    if (alive && token === linkedCasesVersion && initToken === initVersion) optionsLoading.linkedCases = false
  }
}

async function retryOptions() {
  if (form.project != null) await loadProjectOptions(form.project)
  if (optionErrors.linkedCases) await loadLinkedCases()
}

function hasStepContent() { return steps.value.length > 0 }
async function onProjectChange(value) {
  const nextProject = Number(value)
  const previousProject = currentProjectId
  const token = ++projectSwitchVersion
  if (!Number.isFinite(nextProject) || nextProject === previousProject) {
    form.project = nextProject
    return
  }
  switchingProject.value = true
  if (hasStepContent()) {
    try {
      await ElMessageBox.confirm(
        tx('discardStepsOnProjectChange', '切换项目会移除当前请求步骤，是否继续？'),
        tx('confirmProjectChange', '确认切换项目'),
        { type: 'warning' }
      )
    } catch {
      if (alive && token === projectSwitchVersion) form.project = previousProject
      switchingProject.value = false
      return
    }
  }
  if (!alive || token !== projectSwitchVersion) return
  if (hasStepContent()) steps.value = []
  form.project = nextProject
  currentProjectId = nextProject
  form.environment = null
  stepErrors.value = {}
  await loadProjectOptions(nextProject)
  if (alive && token === projectSwitchVersion) switchingProject.value = false
}

function addStep() {
  if (steps.value.length >= 50 || optionErrors.requests || optionsLoading.requests) return
  steps.value.push(makeStep())
}
function removeStep(index) {
  steps.value.splice(index, 1)
  stepErrors.value = {}
}
function moveStep(index, direction) {
  const target = index + direction
  if (index < 0 || index >= steps.value.length || target < 0 || target >= steps.value.length) return
  const next = [...steps.value]
  ;[next[index], next[target]] = [next[target], next[index]]
  steps.value = next
  stepErrors.value = {}
}
function changeStepRequest(step, requestId) {
  const selected = requests.value.find(item => Number(item.id) === Number(requestId))
  if (selected && !step.name) step.name = selected.name
}
function toggleAssertions(step, enabled) {
  if (enabled) step.assertions = []
  else step.assertions = null
}

async function prefillRequest(requestId, projectId, initToken) {
  try {
    const requestData = responseData(await api.get(`/api-testing/requests/${requestId}/`))
    if (!alive || initToken !== initVersion) return
    if (requestData.request_type !== 'HTTP' || requestData.collection == null) throw new Error(tx('savedRequestRequired', '只能引用已保存到集合中的 HTTP 请求。'))
    const collection = responseData(await api.get(`/api-testing/collections/${requestData.collection}/`))
    if (!alive || initToken !== initVersion) return
    if (Number(collection.project) !== Number(projectId)) throw new Error(tx('requestProjectMismatch', '来源请求不属于所选 API 项目。'))
    const option = { id: Number(requestData.id), collection: Number(requestData.collection), name: requestData.name, method: requestData.method, url: requestData.url }
    if (!requests.value.some(item => item.id === option.id)) requests.value = [...requests.value, option]
    steps.value.push(makeStep({ request: option.id, request_name: option.name, name: option.name, request_overrides: {}, assertions: null, extractors: [] }))
  } catch (error) {
    if (alive && initToken === initVersion) prefillNotice.value = errorText(error)
  }
}

function buildPayload() {
  pageError.value = ''
  stepErrors.value = {}
  if (!String(form.name || '').trim()) throw new Error(tx('caseNameRequired', '请填写用例名称。'))
  if (form.project == null) throw new Error(tx('projectRequired', '请选择 API 项目。'))
  if (steps.value.length > 50) throw new Error(tx('tooManySteps', '单个用例最多包含 50 个步骤。'))
  const variables = parseJsonObject(variablesText.value, tx('initialVariablesLabel', '初始变量')) || {}
  const serializedSteps = []
  for (let index = 0; index < steps.value.length; index += 1) {
    const step = steps.value[index]
    const label = `${tx('step', '步骤')} ${index + 1}`
    try {
      if (step.request == null) throw new Error(tx('requestRequired', '请选择请求。'))
      const requestOverrides = {}
      for (const key of ['params', 'headers']) {
        const value = parseJsonObject(step.overrideText[key], `${label} · ${key}`)
        if (value !== undefined && Object.keys(value).length) requestOverrides[key] = value
      }
      if (String(step.overrideText.body || '').trim()) {
        let body
        try { body = JSON.parse(step.overrideText.body) } catch { throw new Error(`${label} · body ${tx('invalidJson', 'JSON 格式无效')}`) }
        requestOverrides.body = body
      }
      let extractors
      try { extractors = JSON.parse(step.extractorsText || '[]') } catch { throw new Error(`${label} · ${tx('extractors', '提取器')} ${tx('invalidJson', 'JSON 格式无效')}`) }
      if (!Array.isArray(extractors)) throw new Error(`${label} · ${tx('extractors', '提取器')} ${tx('mustBeJsonArray', '必须是 JSON 数组')}`)
      serializedSteps.push({
        request: Number(step.request),
        name: String(step.name || '').trim() || `${label}`,
        order: index,
        enabled: step.enabled !== false,
        request_overrides: requestOverrides,
        assertions: step.assertions === null ? null : normalizeAssertions(step.assertions),
        extractors
      })
    } catch (error) {
      stepErrors.value = { ...stepErrors.value, [index]: error.message }
      throw error
    }
  }
  return {
    project: Number(form.project),
    name: form.name.trim(),
    description: form.description,
    preconditions: form.preconditions,
    expected_result: form.expected_result,
    priority: form.priority,
    status: form.status,
    tags: [...new Set(form.tags.map(tag => String(tag).trim()).filter(Boolean))],
    variables,
    environment: form.environment == null || form.environment === '' ? null : Number(form.environment),
    stop_on_failure: Boolean(form.stop_on_failure),
    linked_testcase: form.linked_testcase == null || form.linked_testcase === '' ? null : Number(form.linked_testcase),
    steps: serializedSteps
  }
}

async function saveCase() {
  if (!canSave.value) return
  let payload
  try { payload = buildPayload() } catch (error) { pageError.value = error.message; return }
  const submittedSnapshot = stateSnapshot()
  saving.value = true
  pageError.value = ''
  try {
    if (caseRecord.value?.id) {
      const data = responseData(await updateApiTestCase(caseRecord.value.id, payload))
      if (!alive) return
      caseRecord.value = data?.id ? data : { ...caseRecord.value, ...payload }
      savedSnapshot.value = submittedSnapshot
      retryAttempt = null
      ElMessage.success(tx('saveSucceeded', '用例已保存'))
    } else {
      const data = responseData(await createApiTestCase(payload))
      if (!alive) return
      caseRecord.value = data
      ElMessage.success(tx('createSucceeded', '用例已创建'))
      await router.push('/api-testing/test-cases')
    }
  } catch (error) {
    if (alive) pageError.value = errorText(error)
  } finally {
    if (alive) saving.value = false
  }
}

function idempotencyKey() {
  return globalThis.crypto?.randomUUID?.() || `api-case-${Date.now()}-${Math.random().toString(36).slice(2)}`
}
async function runCase() {
  if (!canRun.value) return
  let variables
  try { variables = parseJsonObject(variablesText.value, tx('initialVariablesLabel', '初始变量')) || {} } catch (error) { pageError.value = error.message; return }
  const caseId = caseRecord.value.id
  const environment = form.environment ?? caseRecord.value.environment ?? null
  const fingerprint = `${caseId}:${environment ?? ''}:${JSON.stringify(variables)}:${savedSnapshot.value}`
  const key = retryAttempt?.fingerprint === fingerprint ? retryAttempt.key : idempotencyKey()
  const payload = { variables, idempotency_key: key }
  if (form.environment != null) payload.environment_id = Number(form.environment)
  running.value = true
  pageError.value = ''
  try {
    currentExecution.value = responseData(await executeApiTestCase(caseId, payload))
    if (!alive) return
    retryAttempt = null
    showRunPanel.value = true
  } catch (error) {
    if (!alive) return
    const status = Number(error?.response?.status)
    const uncertain = !status || status >= 500 || status === 408
    retryAttempt = uncertain ? { fingerprint, key } : null
    pageError.value = uncertain ? `${errorText(error)} ${tx('refreshBeforeRetry', '执行状态未知，请先检查历史再重试。')}` : errorText(error)
    showHistory.value = uncertain
  } finally {
    if (alive) running.value = false
  }
}
function openHistory() { showRunPanel.value = false; showHistory.value = true }
function viewExecution(execution) { currentExecution.value = execution; showHistory.value = false; showRunPanel.value = true }
function handlePollLimit() { pollLimitReached.value = true; pageError.value = tx('pollLimitReached', '执行仍在进行，请刷新历史查看最新状态。') }
function backToList() { router.push('/api-testing/test-cases') }

onMounted(loadInitial)
onBeforeUnmount(() => {
  alive = false
  initVersion += 1
  optionsVersion += 1
  linkedCasesVersion += 1
  projectSwitchVersion += 1
})
</script>

<style scoped>
.api-case-editor { display: flex; flex-direction: column; gap: 14px; min-width: 0; padding-bottom: 24px; }
.page-heading, .heading-actions, .section-heading, .step-heading, .step-title, .step-actions, .form-footer { display: flex; align-items: center; gap: 10px; }
.page-heading, .section-heading, .step-heading { justify-content: space-between; }
.heading-actions, .step-actions { flex-wrap: wrap; }
h1 { margin: 0 0 4px; font-size: 22px; font-weight: 600; }
h2 { margin: 0; font-size: 17px; font-weight: 600; }
.form-section { display: flex; flex-direction: column; gap: 14px; padding: 16px 0; border-top: 1px solid var(--el-border-color); }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: 18px; }
.form-grid :deep(.el-form-item) { margin-bottom: 12px; }
.form-grid :deep(.el-form-item__content), .form-grid :deep(.el-select), .wide { width: 100%; min-width: 0; }
.span-two { grid-column: 1 / -1; }
.step-count, .inherit-note { color: var(--el-text-color-secondary); font-size: 13px; }
.step-editor { display: flex; flex-direction: column; gap: 12px; min-width: 0; padding: 14px 0; border-top: 1px solid var(--el-border-color-light); }
.step-title { min-width: 0; flex-wrap: wrap; }
.step-title span { color: var(--el-text-color-secondary); overflow-wrap: anywhere; }
.step-fields, .override-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.override-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.step-fields :deep(.el-select), .step-fields :deep(.el-input), .override-grid :deep(.el-textarea), .override-grid :deep(.el-form-item) { width: 100%; min-width: 0; }
.step-error { margin: 0 0 4px; }
.assertion-heading { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.form-footer { justify-content: flex-end; padding-top: 16px; border-top: 1px solid var(--el-border-color); }
.notice { margin: 0; }
.loading-space { min-height: 280px; }
@media (max-width: 760px) {
  .page-heading { align-items: flex-start; flex-direction: column; }
  .form-grid, .step-fields, .override-grid { grid-template-columns: minmax(0, 1fr); }
  .span-two { grid-column: auto; }
  .step-heading { align-items: flex-start; }
  .step-actions { justify-content: flex-end; }
}
</style>
