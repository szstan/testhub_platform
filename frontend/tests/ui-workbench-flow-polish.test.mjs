import assert from 'node:assert/strict'
import { test } from 'node:test'
import { nextTick, reactive, ref } from 'vue'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'
import * as routeHelpers from '../src/utils/ui-project-context.mjs'
import { readUiPendingRun, uiPendingRunKey } from '../src/utils/ui-execution-submission.mjs'

const icons = (...names) => Object.fromEntries(names.map(name => [name, {}]))
const flush = async () => { await new Promise(resolve => setImmediate(resolve)); await nextTick() }
const projects = [
  { id: 1, name: 'Store', base_url: 'https://store.example.test' },
  { id: 2, name: 'Checkout', base_url: 'https://checkout.example.test' },
]

function projectScope(project = 1, user = 7) {
  return {
    projects: ref(projects), projectId: ref(project), userId: ref(user),
    projectsLoading: ref(false), projectsError: ref(false), invalidProject: ref(false),
    reloadProjects() {},
  }
}

function projectMocks(scope) {
  return {
    '@/composables/useUiProjectContext': { useUiProjectContext: () => scope },
    '@/utils/ui-project-context.mjs': routeHelpers,
  }
}

function installSessionStorage(t) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  const values = new Map()
  const sessionStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: sessionStorage })
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, 'sessionStorage', previous)
    else delete globalThis.sessionStorage
  })
  return values
}

const readiness = project => ({
  project_id: String(project),
  recommended_headless: true,
  base_url_ready: true,
  engines: [{ value: 'playwright', available: true, browsers: [{ value: 'chrome', available: true }] }],
})

const suiteApi = overrides => ({
  getTestSuites: async () => ({ data: { results: [], count: 0 } }),
  createTestSuite: async () => ({ data: {} }),
  updateTestSuite: async () => ({ data: {} }),
  deleteTestSuite: async () => ({ data: {} }),
  getTestCases: async () => ({ data: { results: [], count: 0 } }),
  getTestSuiteTestCases: async () => ({ data: [] }),
  addTestCaseToTestSuite: async () => ({ data: {} }),
  removeTestCaseFromTestSuite: async () => ({ data: {} }),
  getExecutionReadiness: async project => ({ data: readiness(project) }),
  getSuiteSubmission: async () => { throw Object.assign(new Error('not found'), { response: { status: 404 } }) },
  runTestSuite: async () => ({ data: { accepted: true, execution_id: 71, status: 'pending', kind: 'suite' } }),
  ...overrides,
})

async function suitePage(t, scope, overrides) {
  const pushes = []
  const loaded = await loadSfcSetup('src/views/ui-automation/suites/SuiteList.vue', {
    ...projectMocks(scope),
    '@element-plus/icons-vue': icons('Plus', 'Search', 'Edit', 'Delete', 'RefreshRight', 'ArrowRight', 'Top', 'Bottom'),
    'vue-router': { useRouter: () => ({ push: async location => pushes.push(plain(location)) }) },
    '@/api/ui_automation': suiteApi(overrides),
  })
  t.after(loaded.dispose)
  await flush()
  return { ...loaded, pushes }
}

async function dashboardPage(t, scope, recent) {
  const loaded = await loadSfcSetup('src/views/ui-automation/dashboard/Dashboard.vue', {
    ...projectMocks(scope),
    '@element-plus/icons-vue': icons('ArrowRight', 'CaretRight', 'Loading', 'Plus', 'QuestionFilled', 'VideoPlay', 'Refresh'),
    '@/api/ui_automation': {
      getDashboardStats: async () => ({ data: { test_case_count: 2, suite_entity_count: 1 } }),
      getOperationRecords: async () => ({ data: { results: [] } }),
      getDashboardRecentExecutions: recent,
    },
  })
  t.after(loaded.dispose)
  await flush()
  return loaded
}

test('suite retry after refresh freezes the original engine/browser/headless together with its UUID', async t => {
  installSessionStorage(t)
  const scope = projectScope(2, 9)
  const suite = { id: 50, test_case_count: 1 }
  const posts = []
  const api = {
    getExecutionReadiness: async project => ({ data: { ...readiness(project), engines: [{ value: 'playwright', available: true, browsers: [{ value: 'chrome', available: true }, { value: 'firefox', available: true }] }] } }),
    runTestSuite: async (_id, body) => { posts.push(plain(body)); if (posts.length === 1) throw new Error('lost reply'); return { data: { accepted: true, execution_id: 90 } } },
  }
  const first = await suitePage(t, scope, api)
  await first.state.runSuite(suite)
  first.state.runConfig.browser = 'firefox'
  first.state.runConfig.headless = false
  await first.state.confirmRunSuite()
  const saved = readUiPendingRun(uiPendingRunKey('suite', 9, 2, 50))
  assert.equal(saved.browser, 'firefox')
  assert.equal(saved.headless, false)
  first.dispose()
  const second = await suitePage(t, scope, api)
  await second.state.runSuite(suite)
  assert.equal(second.state.runConfig.browser, 'firefox')
  assert.equal(second.state.runConfig.headless, false)
  second.state.runConfig.browser = 'chrome'
  second.state.runConfig.headless = true
  await second.state.confirmRunSuite()
  assert.equal(posts.length, 2)
  assert.deepEqual(posts[1], posts[0])
})

test('suite submissions use the same in-memory identity when sessionStorage is blocked', async t => {
  installSessionStorage(t)
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, get() { throw new Error('storage blocked') } })
  const scope = projectScope(2, 9)
  const posts = []
  const suite = { id: 51, test_case_count: 1 }
  const h = await suitePage(t, scope, { runTestSuite: async (_id, body) => {
    posts.push(plain(body)); if (posts.length === 1) throw new Error('lost reply')
    return { data: { accepted: true, execution_id: 91 } }
  } })
  await h.state.runSuite(suite)
  await h.state.confirmRunSuite()
  assert.equal(posts.length, 1)
  assert.equal(h.state.submissionState.value, 'unknown')
  await h.state.runSuite(suite)
  await h.state.confirmRunSuite()
  assert.equal(posts.length, 2)
  assert.equal(posts[1].client_request_id, posts[0].client_request_id)
  assert.deepEqual(h.pushes, [{ name: 'UiReports', query: { project: '2', executionId: '91' } }])
})

test('dashboard terminal cache is keyed by kind and ID, and resets with project scope', async t => {
  const scope = projectScope(2, 9)
  let rows = [{ id: 10, kind: 'case', status: 'failed', name: 'Case failure' }, { id: 10, kind: 'suite', status: 'running' }]
  const h = await dashboardPage(t, scope, async () => ({ data: { results: rows } }))
  rows = [{ id: 10, kind: 'case', status: 'running', name: 'stale' }, { id: 10, kind: 'suite', status: 'passed' }]
  await h.state.loadExecutions()
  assert.equal(h.state.executions.value[0].status, 'failed')
  assert.equal(h.state.executions.value[0].name, 'Case failure')
  assert.equal(h.state.executions.value[1].status, 'passed')
  scope.projectId.value = 1
  await flush()
  assert.equal(h.state.executions.value[0].status, 'running')
})

for (const status of [401, 403, 404]) {
  test(`dashboard clears sensitive cached rows/time on ${status} and does not resurrect the old terminal record`, async t => {
    const scope = projectScope(2, 9)
    let denied = false
    let recordStatus = 'failed'
    const h = await dashboardPage(t, scope, async () => {
      if (denied) throw Object.assign(new Error('unavailable'), { response: { status } })
      return { data: { results: [{ id: 10, kind: 'case', status: recordStatus, name: 'private execution' }] } }
    })
    assert.equal(h.state.executions.value.length, 1)
    denied = true
    await h.state.loadExecutions()
    assert.deepEqual(plain(h.state.executions.value), [])
    assert.equal(h.state.executionsUpdatedAt.value, null)
    denied = false
    recordStatus = 'running'
    await h.state.loadExecutions()
    assert.equal(h.state.executions.value[0].status, 'running')
  })
}

test('dashboard refreshes active runs, pauses while hidden, rejects stale projects, and retains known results on error', async t => {
  const scope = projectScope(1)
  const staleProjectOne = deferred()
  const calls = []
  const counts = new Map()
  const listeners = new Map()
  const timers = []
  const document = {
    visibilityState: 'visible',
    addEventListener: (name, callback) => listeners.set(`document:${name}`, callback),
    removeEventListener: name => listeners.delete(`document:${name}`),
  }
  const window = {
    addEventListener: (name, callback) => listeners.set(`window:${name}`, callback),
    removeEventListener: name => listeners.delete(`window:${name}`),
  }
  const { state, mount, dispose } = await loadSfcSetup('src/views/ui-automation/dashboard/Dashboard.vue', {
    ...projectMocks(scope),
    '@element-plus/icons-vue': icons('ArrowRight', 'CaretRight', 'Loading', 'Plus', 'QuestionFilled', 'VideoPlay', 'Refresh'),
    '@/api/ui_automation': {
      getDashboardStats: async () => ({ data: { test_case_count: 2, suite_entity_count: 1 } }),
      getOperationRecords: async () => ({ data: { results: [] } }),
      getDashboardRecentExecutions: async params => {
        calls.push(plain(params))
        const count = (counts.get(String(params.project)) || 0) + 1
        counts.set(String(params.project), count)
        if (String(params.project) === '1') {
          if (count <= 2) return { data: { results: [{ id: 10, kind: 'suite', name: 'Active', status: 'running' }] } }
          if (count === 3) return { data: { results: [{ id: 10, kind: 'suite', name: 'Finished', status: 'passed' }] } }
          return staleProjectOne.promise
        }
        if (count === 1) return { data: { results: [{ id: 20, kind: 'case', name: 'Checkout', status: 'failed' }] } }
        throw new Error('temporary list outage')
      },
    },
  }, {
    document,
    window,
    setTimeout: (callback, delay) => {
      const timer = { callback, delay, active: true }
      timers.push(timer)
      return timer
    },
    clearTimeout: timer => { if (timer) timer.active = false },
  })
  t.after(dispose)
  await flush()
  await mount()

  assert.equal(state.executions.value[0].status, 'running')
  const firstPoll = timers.find(timer => timer.active)
  assert.equal(firstPoll.delay, 5000)
  firstPoll.active = false
  await firstPoll.callback()
  await flush()
  assert.equal(state.executions.value[0].status, 'running')

  const hiddenPoll = timers.findLast(timer => timer.active)
  document.visibilityState = 'hidden'
  listeners.get('document:visibilitychange')()
  assert.equal(hiddenPoll.active, false)
  document.visibilityState = 'visible'
  listeners.get('document:visibilitychange')()
  await flush()
  assert.equal(state.executions.value[0].status, 'passed')
  assert.ok(state.executionsUpdatedAt.value)

  const staleRequest = state.loadExecutions()
  scope.projectId.value = 2
  await flush()
  assert.equal(state.executions.value[0].id, 20)
  staleProjectOne.resolve({ data: { results: [{ id: 11, kind: 'suite', name: 'Stale Store result', status: 'failed' }] } })
  await staleRequest
  assert.equal(state.executions.value[0].id, 20)

  const lastUpdated = state.executionsUpdatedAt.value
  await state.loadExecutions()
  assert.equal(state.executionError.value, true)
  assert.equal(state.executions.value[0].id, 20)
  assert.equal(state.executionsUpdatedAt.value, lastUpdated)
  assert.deepEqual(calls.at(-1), { project: 2 })
})

test('suite submission survives a transport disconnect and resumes with the same UUID after reload', async t => {
  installSessionStorage(t)
  const scope = projectScope(2, 9)
  const pushes = []
  const lookups = []
  const posts = []
  const accepted = new Map()
  const suite = { id: 44, test_case_count: 2 }
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/suites/SuiteList.vue', {
    ...projectMocks(scope),
    '@element-plus/icons-vue': icons('Plus', 'Search', 'Edit', 'Delete', 'RefreshRight', 'ArrowRight', 'Top', 'Bottom'),
    'vue-router': { useRouter: () => ({ push: async location => pushes.push(plain(location)) }) },
    '@/api/ui_automation': suiteApi({
      getSuiteSubmission: async (_id, params) => {
        lookups.push(params.client_request_id)
        const record = accepted.get(params.client_request_id)
        if (!record) throw Object.assign(new Error('not found'), { response: { status: 404 } })
        return { data: { accepted: true, execution_id: record.executionId, status: 'pending', kind: 'suite' } }
      },
      runTestSuite: async (_id, payload) => {
        posts.push(plain(payload))
        throw new Error('connection closed before response')
      },
    }),
  })
  t.after(dispose)
  await flush()
  await state.runSuite(suite)
  assert.equal(state.readiness.value.recommended_headless, true)
  assert.equal(state.runConfig.headless, true)
  assert.equal(state.engineOptions.value[0].available, true)

  await state.confirmRunSuite()
  assert.equal(state.submissionState.value, 'unknown')
  assert.equal(posts.length, 1)
  const requestId = posts[0].client_request_id
  const pendingKey = uiPendingRunKey('suite', 9, 2, 44)
  assert.equal(readUiPendingRun(pendingKey).client_request_id, requestId)

  accepted.set(requestId, { executionId: 77 })
  await state.runSuite(suite)
  assert.equal(state.submissionState.value, 'unknown')
  await state.confirmRunSuite()
  assert.equal(posts.length, 1)
  assert.deepEqual(lookups, [requestId, requestId])
  assert.deepEqual(pushes, [{ name: 'UiReports', query: { project: '2', executionId: '77' } }])
  assert.equal(state.submissionState.value, 'accepted')
})

test('a delayed suite acceptance updates only its original pending identity after a project switch', async t => {
  installSessionStorage(t)
  const scope = projectScope(2, 9)
  const post = deferred()
  const pushes = []
  const posts = []
  const suite = { id: 45, test_case_count: 1 }
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/suites/SuiteList.vue', {
    ...projectMocks(scope),
    '@element-plus/icons-vue': icons('Plus', 'Search', 'Edit', 'Delete', 'RefreshRight', 'ArrowRight', 'Top', 'Bottom'),
    'vue-router': { useRouter: () => ({ push: async location => pushes.push(plain(location)) }) },
    '@/api/ui_automation': suiteApi({
      runTestSuite: async (_id, payload) => { posts.push(plain(payload)); return post.promise },
    }),
  })
  t.after(dispose)
  await flush()
  await state.runSuite(suite)
  const submission = state.confirmRunSuite()
  await flush()
  const requestId = posts[0].client_request_id
  const oldKey = uiPendingRunKey('suite', 9, 2, 45)

  scope.projectId.value = 1
  await flush()
  post.resolve({ data: { accepted: true, execution_id: 88, status: 'pending', kind: 'suite' } })
  await submission

  assert.deepEqual(pushes, [])
  assert.equal(readUiPendingRun(oldKey).client_request_id, requestId)
  assert.equal(readUiPendingRun(oldKey).execution_id, 88)
  assert.equal(state.currentRunningSuite.value, null)
  assert.equal(state.running.value, false)
  assert.equal(state.readinessLoading.value, false)
})

test('a delayed suite acceptance updates only its original actor-scoped pending identity', async t => {
  installSessionStorage(t)
  const scope = projectScope(2, 9)
  const post = deferred()
  const pushes = []
  const posts = []
  const suite = { id: 46, test_case_count: 1 }
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/suites/SuiteList.vue', {
    ...projectMocks(scope),
    '@element-plus/icons-vue': icons('Plus', 'Search', 'Edit', 'Delete', 'RefreshRight', 'ArrowRight', 'Top', 'Bottom'),
    'vue-router': { useRouter: () => ({ push: async location => pushes.push(plain(location)) }) },
    '@/api/ui_automation': suiteApi({
      runTestSuite: async (_id, payload) => { posts.push(plain(payload)); return post.promise },
    }),
  })
  t.after(dispose)
  await flush()
  await state.runSuite(suite)
  const submission = state.confirmRunSuite()
  await flush()
  const requestId = posts[0].client_request_id
  const oldKey = uiPendingRunKey('suite', 9, 2, 46)

  scope.userId.value = 10
  await flush()
  assert.equal(state.running.value, false)
  assert.equal(state.readinessLoading.value, false)
  post.resolve({ data: { accepted: true, execution_id: 89, status: 'pending', kind: 'suite' } })
  await submission

  assert.deepEqual(pushes, [])
  assert.equal(readUiPendingRun(oldKey).client_request_id, requestId)
  assert.equal(readUiPendingRun(oldKey).execution_id, 89)
  assert.equal(readUiPendingRun(uiPendingRunKey('suite', 10, 2, 46)), null)
})

test('locator checks send explicit mode and map positive-but-unknown server results to UNKNOWN', async t => {
  const scope = projectScope(2)
  const calls = []
  const results = [
    { is_valid: true, validation_status: 'UNKNOWN', validation_message: 'Live policy rejected the target.', validation_mode: 'live', match_count: 0, checked_at: '2026-11-27T10:00:00Z' },
    { is_valid: true, format_valid: true, validation_status: 'UNKNOWN', validation_message: 'Format only does not probe the page.', validation_mode: 'format', match_count: null, checked_at: '2026-11-27T10:01:00Z' },
  ]
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/elements/ElementManagerEnhanced.vue', {
    ...projectMocks(scope),
    '@element-plus/icons-vue': icons('Plus', 'FolderAdd', 'Document', 'Search', 'Edit', 'Delete', 'Folder', 'Operation', 'DocumentCopy', 'ArrowDown'),
    'vue-router': { useRoute: () => ({ query: {} }) },
    '@/api/ui_automation': {
      createElement: async () => ({ data: {} }),
      getElementDetail: async () => ({ data: {} }),
      updateElement: async () => ({ data: {} }),
      deleteElement: async () => ({ data: {} }),
      getElementTree: async () => ({ data: [] }),
      getElementGroupTree: async () => ({ data: [] }),
      getElementGroups: async () => ({ data: [] }),
      createElementGroup: async () => ({ data: {} }),
      updateElementGroup: async () => ({ data: {} }),
      deleteElementGroup: async () => ({ data: {} }),
      getLocatorStrategies: async () => ({ data: [] }),
      generateElementSuggestions: async () => ({ data: { suggestions: [] } }),
      validateElementLocator: async (id, payload) => {
        calls.push({ id, payload: plain(payload) })
        return { data: results.shift() }
      },
    },
  })
  t.after(dispose)
  await flush()
  state.selectedElement.value = { id: 31, locator_value: '#checkout', locator_strategy_id: 7 }
  state.locatorValidationMode.value = 'live'
  assert.equal(state.locatorValidationTargetUrl.value, 'https://checkout.example.test')
  await state.validateElement()
  assert.deepEqual(calls[0], { id: 31, payload: { mode: 'live', engine: 'playwright', browser: 'chrome', expected_locator_value: '#checkout', expected_locator_strategy_id: 7, target_url: 'https://checkout.example.test' } })
  assert.equal(state.locatorValidationResult.value.validation_status, 'UNKNOWN')
  assert.equal(state.locatorValidationResult.value.match_count, 0)
  assert.equal(state.locatorValidationResult.value.validation_mode, 'live')

  state.locatorValidationMode.value = 'format'
  await state.validateElement()
  assert.deepEqual(calls[1], { id: 31, payload: { mode: 'format', engine: 'playwright', browser: 'chrome', expected_locator_value: '#checkout', expected_locator_strategy_id: 7 } })
  assert.equal(state.locatorValidationResult.value.validation_status, 'UNKNOWN')
  assert.equal(state.locatorValidationResult.value.match_count, null)
  assert.equal(state.locatorValidationResult.value.validation_mode, 'format')
  state.selectedElement.value.locator_strategy_id = 8
  assert.equal(state.locatorValidationResult.value, null, 'changing the strategy must invalidate the old result')
})

test('failed suite reports link the failing step to the case editor and expose cancellation for active suite runs', async t => {
  const scope = projectScope(2)
  const route = reactive({ name: 'UiReports', query: { project: '2' } })
  const pushes = []
  const aborted = []
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/reports/ReportList.vue', {
    ...projectMocks(scope),
    '@element-plus/icons-vue': icons('Refresh', 'Document', 'Delete', 'WarningFilled'),
    'vue-router': {
      useRoute: () => route,
      useRouter: () => ({ push: async location => pushes.push(plain(location)), replace: async () => {} }),
    },
    '@/api/ui_automation': {
      getTestExecutions: async () => ({ data: { results: [], count: 0 } }),
      getTestExecutionDetail: async () => { throw new Error('not used') },
      abortTestExecution: async id => { aborted.push(id); return { data: { accepted: true } } },
      deleteTestExecution: async () => {},
    },
  })
  t.after(dispose)
  await flush()

  state.currentReport.value = {
    id: 55,
    project: 2,
    status: 'FAILED',
    rerun_of: 40,
    rerun_kind: 'suite',
    result_data: { test_cases: [{
      test_case_id: 12,
      test_case_name: 'Checkout total',
      success: false,
      steps: [{ step_number: 4, success: false, description: 'Total is visible' }],
    }] },
  }
  assert.deepEqual(plain(state.failedCaseEditorLocation.value), {
    name: 'UiTestCases',
    query: { project: '2', caseId: '12', stepNumber: '4', fromExecutionId: '55', fromKind: 'suite' },
  })
  assert.deepEqual(plain(state.rerunSource.value.location), { name: 'UiReports', query: { project: '2', executionId: '40' } })
  await state.openFailedCaseInEditor()
  assert.deepEqual(pushes[0], plain(state.failedCaseEditorLocation.value))

  await state.abortExecution({ id: 91, project: 2, status: 'RUNNING' })
  assert.deepEqual(aborted, [91])
})
