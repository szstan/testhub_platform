import assert from 'node:assert/strict'
import { test } from 'node:test'
import { nextTick, reactive, ref } from 'vue'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'
import * as routeHelpers from '../src/utils/ui-project-context.mjs'
import * as caseHelpers from '../src/utils/ui-case-execution.mjs'
import { readUiPendingRun, uiPendingRunKey } from '../src/utils/ui-execution-submission.mjs'

const flush = async () => { await new Promise(resolve => setImmediate(resolve)); await nextTick() }
const offline = () => new Error('Connection lost')
const missing = () => Object.assign(new Error('Not found'), { response: { status: 404 } })
const icons = (...names) => Object.fromEntries(names.map(name => [name, {}]))

function projectScope(projectId = 2, userId = 1) {
  return {
    projects: ref([{ id: 2, name: 'Project 2' }, { id: 3, name: 'Project 3' }]),
    projectId: ref(projectId), userId: ref(userId), projectsLoading: ref(false),
    projectsError: ref(false), invalidProject: ref(false), reloadProjects() {},
  }
}

function blockSessionStorage(t) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, get() { throw new Error('sessionStorage blocked') } })
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, 'sessionStorage', previous)
    else delete globalThis.sessionStorage
  })
}

async function executionHarness(t, { storageBlocked = false, routeQuery = {}, apiOverrides = {}, scope = projectScope() } = {}) {
  if (storageBlocked) blockSessionStorage(t)
  const route = reactive({ name: 'UiExecutions', query: { project: String(scope.projectId.value), ...routeQuery } })
  const pushes = []
  const calls = { posts: [], lookups: [], details: [] }
  const readiness = {
    base_url_ready: true,
    recommended_headless: true,
    engines: ['playwright', 'selenium'].map(value => ({
      value, available: true,
      browsers: ['chrome', 'firefox', 'edge'].map(browser => ({ value: browser, available: true })),
    })),
  }
  const api = {
    getTestCaseExecutions: async () => ({ data: { results: [], count: 0 } }),
    getTestCaseExecutionDetail: async id => ({ data: { id, project: scope.projectId.value, test_case: 11, status: 'failed', execution_logs: 'failure' } }),
    deleteTestCaseExecution: async () => ({}),
    batchDeleteTestCaseExecutions: async () => ({}),
    getTestCaseSubmission: async () => { throw missing() },
    abortTestCaseExecution: async () => ({ data: { status: 'aborted' } }),
    getExecutionReadiness: async project => ({ data: { ...readiness, project_id: project } }),
    runTestCase: async () => ({ data: { accepted: true, execution_id: 101 } }),
    ...apiOverrides,
  }
  const loaded = await loadSfcSetup('src/views/ui-automation/executions/ExecutionList.vue', {
    '@/composables/useUiProjectContext': { useUiProjectContext: () => scope },
    '@/utils/ui-project-context.mjs': routeHelpers,
    '@/utils/ui-case-execution.mjs': caseHelpers,
    'vue-router': {
      useRoute: () => route,
      useRouter: () => ({
        push: async location => pushes.push(plain(location)),
        replace: async location => { route.name = location.name; route.query = { ...location.query } },
      }),
    },
    '@element-plus/icons-vue': icons('Search', 'WarningFilled', 'Refresh'),
    '@/api/ui_automation': Object.fromEntries(Object.keys(api).map(name => [name, (...args) => {
      if (name === 'runTestCase') calls.posts.push(plain(args))
      if (name === 'getTestCaseSubmission') calls.lookups.push(plain(args))
      if (name === 'getTestCaseExecutionDetail') calls.details.push(plain(args))
      return api[name](...args)
    }])),
  })
  t.after(loaded.dispose)
  await flush()
  return { ...loaded, api, calls, scope, route, pushes }
}

function reportDocument(hidden) {
  const listeners = new Map()
  return {
    hidden,
    listeners,
    addEventListener(type, listener) {
      const rows = listeners.get(type) || new Set()
      rows.add(listener)
      listeners.set(type, rows)
    },
    removeEventListener(type, listener) { listeners.get(type)?.delete(listener) },
    emit(type) { for (const listener of listeners.get(type) || []) listener() },
  }
}

async function reportHarness(t, { document = reportDocument(false), scope = projectScope(), apiOverrides = {}, timers = null } = {}) {
  const route = reactive({ name: 'UiReports', query: { project: String(scope.projectId.value), executionId: '42' } })
  const calls = { details: [] }
  let timerId = 0
  const scheduled = timers || new Map()
  const api = {
    getTestExecutions: async () => ({ data: { count: 0, results: [] } }),
    getTestExecutionDetail: async id => ({ data: { id, project: scope.projectId.value, status: 'RUNNING', result_data: { test_cases: [] } } }),
    deleteTestExecution: async () => ({}),
    abortTestExecution: async () => ({ data: { status: 'ABORTED' } }),
    ...apiOverrides,
  }
  const loaded = await loadSfcSetup('src/views/ui-automation/reports/ReportList.vue', {
    '@/composables/useUiProjectContext': { useUiProjectContext: () => scope },
    '@/utils/ui-project-context.mjs': routeHelpers,
    'vue-router': {
      useRoute: () => route,
      useRouter: () => ({ push: async () => {}, replace: async location => { route.name = location.name; route.query = { ...location.query } } }),
    },
    '@element-plus/icons-vue': icons('Refresh', 'Document', 'Delete', 'WarningFilled'),
    '@/api/ui_automation': Object.fromEntries(Object.keys(api).map(name => [name, (...args) => {
      if (name === 'getTestExecutionDetail') calls.details.push(plain(args))
      return api[name](...args)
    }])),
  }, {
    document,
    setTimeout(callback, delay) { const id = ++timerId; scheduled.set(id, { callback, delay }); return id },
    clearTimeout(id) { scheduled.delete(id) },
  })
  t.after(loaded.dispose)
  await flush()
  return { ...loaded, api, calls, scope, route, document, timers: scheduled }
}

async function caseEditor(t) {
  const records = [
    { id: 11, project: 2, name: 'Case A', steps: [{ id: 1, step_number: 1, action_type: 'wait', wait_time: 10, description: 'Step A' }] },
    { id: 12, project: 2, name: 'Case B', steps: [{ id: 2, step_number: 1, action_type: 'wait', wait_time: 10, description: 'Step B' }] },
  ]
  const scope = projectScope()
  const route = reactive({ name: 'UiTestCases', query: { project: '2', caseId: '11', fromExecutionId: '99', fromKind: 'case', stepNumber: '1' } })
  const submissions = []
  const loaded = await loadSfcSetup('src/views/ui-automation/test-cases/TestCaseManager.vue', {
    '@/composables/useUiProjectContext': { useUiProjectContext: () => scope },
    '@/utils/ui-project-context.mjs': routeHelpers,
    '@/utils/ui-case-execution.mjs': caseHelpers,
    'vue-router': {
      useRoute: () => route,
      useRouter: () => ({
        push: async () => {},
        replace: async location => { route.name = location.name; route.query = { ...location.query }; route.hash = location.hash },
      }),
    },
    '@element-plus/icons-vue': icons('Search', 'Plus', 'Edit', 'Delete', 'Check', 'CaretRight', 'ArrowUp', 'ArrowDown', 'Rank', 'Picture', 'Warning', 'View', 'ZoomIn', 'WarningFilled', 'MagicStick'),
    vuedraggable: { default: {} },
    '@/components/DataFactorySelector.vue': { default: {} },
    '@/api/data-factory': { getVariableFunctions: async () => ({ data: [] }) },
    '@/api/ui_automation': {
      getTestCases: async () => ({ data: { count: records.length, next: null, results: records } }),
      getElements: async () => ({ data: { count: 0, next: null, results: [] } }),
      getLocatorStrategies: async () => ({ data: [] }),
      getExecutionReadiness: async project => ({ data: { project_id: project, base_url_ready: true, engines: ['playwright', 'selenium'].map(value => ({ value, available: true, browsers: ['chrome', 'firefox'].map(browser => ({ value: browser, available: true })) })) } }),
      getTestCaseSubmission: async () => { throw missing() },
      getTestCaseExecutionDetail: async () => { throw offline() },
      abortTestCaseExecution: async () => ({ data: { status: 'aborted' } }),
      updateTestCase: async (id, data) => ({ data: { ...records.find(row => row.id === id), ...data } }),
      createTestCase: async data => ({ data: { ...data, id: 13, steps: [] } }),
      deleteTestCase: async () => ({}),
      copyTestCase: async () => ({}),
      runTestCase: async (id, payload) => {
        submissions.push({ id, payload: plain(payload) })
        throw Object.assign(new Error('Rejected source'), { response: { status: 404, data: { accepted: false, failure_stage: 'preflight', error: 'Rejected source' } } })
      },
    },
  })
  t.after(loaded.dispose)
  await flush()
  return { ...loaded, scope, route, records, submissions }
}

test('repair origin stays with the initially restored case and clears only after a dirty-guarded switch', async t => {
  const h = await caseEditor(t)
  assert.equal(h.state.selectedTestCase.value.id, 11)
  assert.equal(h.state.sourceExecutionLocation.value.query.executionId, '99')
  assert.equal(h.state.repairStep.value, 1)

  h.state.currentSteps.value[0].wait_time = 20
  const cancelled = h.state.selectTestCase(h.records[1])
  assert.equal(h.state.selectedTestCase.value.id, 11)
  await h.state.chooseUnsaved('cancel')
  assert.equal(await cancelled, false)
  assert.equal(h.route.query.fromExecutionId, '99')

  const switching = h.state.selectTestCase(h.records[1])
  await h.state.chooseUnsaved('discard')
  assert.equal(await switching, true)
  await flush()
  assert.equal(h.state.selectedTestCase.value.id, 12)
  assert.equal(h.route.query.caseId, '12')
  for (const key of ['fromExecutionId', 'fromKind', 'stepNumber']) assert.equal(key in h.route.query, false)
  assert.equal(h.state.canRunSelectedCase.value, true)

  await h.state.runTestCase(h.records[1])
  assert.equal(h.submissions[0].id, 12)
  assert.equal('rerun_of' in h.submissions[0].payload, false)
})

test('rerun identity and configuration survive dialog reopen when sessionStorage is blocked', async t => {
  let lookupCount = 0
  const h = await executionHarness(t, {
    storageBlocked: true,
    routeQuery: {},
    apiOverrides: {
      getTestCaseExecutions: async () => ({ data: { results: [], count: 0 } }),
      getExecutionReadiness: async project => ({ data: { project_id: project, base_url_ready: true, engines: ['playwright', 'selenium'].map(value => ({ value, available: true, browsers: ['chrome', 'firefox', 'edge'].map(browser => ({ value: browser, available: true })) })) } }),
      runTestCase: async (_id, payload) => {
        if (h?.calls.posts.length === 1) throw offline()
        return { data: { accepted: true, execution_id: 101 } }
      },
      getTestCaseSubmission: async () => {
        lookupCount += 1
        if (lookupCount === 1) throw offline()
        throw missing()
      },
    },
  })
  const record = { id: 50, project: 2, test_case: 11, status: 'failed', engine: 'playwright', browser: 'chrome', headless: true }
  await h.state.showRerunDialog(record)
  h.state.rerunFormData.engine = 'selenium'
  h.state.rerunFormData.browser = 'firefox'
  h.state.rerunFormData.headless = false
  await h.state.handleRerun()
  assert.equal(h.state.rerunUnknown.value, true)
  const key = uiPendingRunKey('case', 1, 2, 11)
  const pending = h.state.rerunPendingByScope.get(key)
  assert.ok(pending?.client_request_id)
  assert.equal(readUiPendingRun(key), null)

  h.state.showRerunDialogVisible.value = false
  await h.state.showRerunDialog(record)
  assert.equal(h.state.rerunUnknown.value, true)
  assert.equal(h.state.rerunFormData.engine, 'selenium')
  assert.equal(h.state.rerunFormData.browser, 'firefox')
  assert.equal(h.state.rerunFormData.headless, false)
  await h.state.handleRerun()

  assert.equal(h.calls.posts.length, 2)
  assert.equal(h.calls.posts[0][1].client_request_id, h.calls.posts[1][1].client_request_id)
  assert.deepEqual(h.calls.posts.map(([, payload]) => [payload.engine, payload.browser, payload.headless]), [
    ['selenium', 'firefox', false], ['selenium', 'firefox', false],
  ])
  assert.equal(h.calls.lookups.length, 2)
  assert.equal(h.calls.lookups[0][1].client_request_id, h.calls.posts[0][1].client_request_id)
  assert.equal(h.calls.lookups[1][1].client_request_id, h.calls.posts[0][1].client_request_id)
})

test('late rerun acknowledgements update only their actor/project/case identity', async t => {
  const first = deferred()
  const second = deferred()
  let postCount = 0
  const h = await executionHarness(t, {
    storageBlocked: true,
    apiOverrides: {
      runTestCase: async () => {
        postCount += 1
        if (postCount === 1) return first.promise
        if (postCount === 2) return second.promise
        throw offline()
      },
      getTestCaseSubmission: async () => { throw offline() },
    },
  })
  const record = { id: 50, project: 2, test_case: 11, status: 'failed', engine: 'playwright', browser: 'chrome', headless: true }
  await h.state.showRerunDialog(record)
  const oldRun = h.state.handleRerun()
  const oldKey = uiPendingRunKey('case', 1, 2, 11)
  const oldIdentity = h.state.rerunPendingByScope.get(oldKey)

  h.scope.projectId.value = 3
  h.route.query.project = '3'
  await flush()
  const otherProjectRecord = { ...record, project: 3 }
  await h.state.showRerunDialog(otherProjectRecord)
  const newRun = h.state.handleRerun()
  const projectKey = uiPendingRunKey('case', 1, 3, 11)
  const projectIdentity = h.state.rerunPendingByScope.get(projectKey)
  assert.notEqual(projectIdentity.client_request_id, oldIdentity.client_request_id)

  first.resolve({ data: { accepted: true, execution_id: 101 } })
  await oldRun
  assert.equal(oldIdentity.execution_id, 101)
  assert.equal(projectIdentity.execution_id, undefined)
  assert.deepEqual(h.pushes, [])

  second.resolve({ data: { accepted: true, execution_id: 202 } })
  await newRun
  assert.equal(projectIdentity.execution_id, 202)
  assert.deepEqual(h.pushes.at(-1), { name: 'UiExecutions', query: { project: '3', executionId: '202' } })

  h.scope.userId.value = 2
  await flush()
  await h.state.showRerunDialog(otherProjectRecord)
  await h.state.handleRerun()
  const actorKey = uiPendingRunKey('case', 2, 3, 11)
  const actorIdentity = h.state.rerunPendingByScope.get(actorKey)
  assert.ok(actorIdentity?.client_request_id)
  assert.notEqual(actorIdentity.client_request_id, projectIdentity.client_request_id)
  assert.equal(h.state.rerunPendingByScope.get(projectKey), projectIdentity)
})

test('browser history restores a valid repair context after switching away', async t => {
  const h = await caseEditor(t)
  h.state.selectTestCase(h.records[1])
  await flush()
  h.route.query = { project: '2', caseId: '11', fromExecutionId: '99', fromKind: 'case', stepNumber: '1' }
  await flush()
  assert.equal(h.state.selectedTestCase.value.id, 11)
  assert.equal(h.state.sourceExecutionLocation.value?.query.executionId, '99')
  assert.equal(h.state.repairStep.value, 1)
})

test('known rerun rejection clears pending identity and releases the busy button', async t => {
  const h = await executionHarness(t, {
    storageBlocked: true,
    apiOverrides: { runTestCase: async () => { throw Object.assign(offline(), { response: { status: 503, data: { accepted: false, failure_stage: 'dispatch', message: 'queue offline' } } }) } },
  })
  const record = { id: 50, project: 2, test_case: 11, status: 'failed', engine: 'playwright', browser: 'chrome', headless: true }
  await h.state.showRerunDialog(record)
  await h.state.handleRerun()
  assert.equal(h.state.rerunPendingByScope.size, 0)
  assert.equal(h.state.rerunning.value, false)
})

test('revisiting a terminal summary never labels stale running evidence as final', async t => {
  let stale = false
  const h = await executionHarness(t, {
    routeQuery: { executionId: '1' },
    apiOverrides: { getTestCaseExecutionDetail: async id => ({ data: {
      id: Number(id), project: 2, test_case: 11, status: stale && Number(id) === 1 ? 'running' : 'passed',
      execution_logs: stale ? 'partial running log' : 'complete terminal log', screenshots: [{ url: 'data:image/png;base64,fixture' }],
    } }) },
  })
  h.route.query.executionId = '2'
  await flush()
  stale = true
  h.route.query.executionId = '1'
  await flush()
  assert.equal(h.state.currentExecution.value.status, 'passed')
  assert.equal(h.state.currentExecution.value.execution_logs, '')
  assert.deepEqual(plain(h.state.currentExecution.value.screenshots), [])
  assert.equal(h.state.detailTrackingPaused.value, true)
})

test('execution terminal cache is bounded, evidence-free, and cannot roll back current detail evidence', async t => {
  const routeQuery = { executionId: '1' }
  let staleActiveFirst = false
  const h = await executionHarness(t, {
    routeQuery,
    apiOverrides: {
      getTestCaseExecutions: async () => ({ data: { count: 1, results: [{ id: 102, project: 2, test_case: 11, status: 'running' }] } }),
      getTestCaseExecutionDetail: async id => {
        const numericId = Number(id)
        const status = numericId === 1 && staleActiveFirst ? 'running' : 'passed'
        return { data: {
          id: numericId, project: 2, test_case: 11, test_case_name: `Case ${numericId}`, status,
          execution_logs: `${status}-${numericId}-` + 'L'.repeat(5000),
          screenshots: [{ url: `data:image/png;base64,${'S'.repeat(5000)}`, description: 'evidence' }],
          error_message: 'E'.repeat(5000), engine: 'playwright', browser: 'chrome', headless: true,
          started_at: '2026-11-28T10:00:00Z', finished_at: '2026-11-28T10:01:00Z', execution_time: 60,
        } }
      },
    },
  })
  await flush()
  const terminalLogs = h.state.currentExecution.value.execution_logs
  const terminalScreenshot = h.state.currentExecution.value.screenshots[0].url
  staleActiveFirst = true
  await h.state.loadExecutionDetail(true)
  assert.equal(h.state.currentExecution.value.status, 'passed')
  assert.equal(h.state.currentExecution.value.execution_logs, terminalLogs)
  assert.equal(h.state.currentExecution.value.screenshots[0].url, terminalScreenshot)

  for (let id = 2; id <= 102; id += 1) {
    h.route.query.executionId = String(id)
    await flush()
  }
  assert.equal(h.state.currentExecution.value.id, 102)
  assert.equal(h.state.confirmedDetails.size, 100)
  for (const summary of h.state.confirmedDetails.values()) {
    assert.equal('execution_logs' in summary, false)
    assert.equal('screenshots' in summary, false)
    assert.ok((summary.error_message || '').length <= 1000)
  }

  await h.state.loadExecutions()
  assert.equal(h.state.executions.value[0].status, 'passed')
  assert.equal('execution_logs' in h.state.executions.value[0], false)
  assert.equal('screenshots' in h.state.executions.value[0], false)

  h.state.onDetailVisibility(false)
  await flush()
  assert.equal(h.state.currentExecution.value, null)
  assert.equal([...h.state.confirmedDetails.values()].some(row => 'execution_logs' in row || 'screenshots' in row), false)

  h.scope.projectId.value = 3
  h.route.query.project = '3'
  await flush()
  assert.equal(h.state.confirmedDetails.size, 0)
  assert.equal(h.state.currentExecution.value, null)
})

test('report polling pauses while hidden, resumes immediately without spending hidden budget, and unregisters its listener', async t => {
  const document = reportDocument(true)
  const timers = new Map()
  const active = { id: 42, project: 2, status: 'RUNNING', test_suite: 1, result_data: { test_cases: [] } }
  const h = await reportHarness(t, {
    document,
    timers,
    apiOverrides: {
      getTestExecutionDetail: async id => ({ data: { ...active, id } }),
    },
  })
  assert.equal(h.calls.details.length, 0)
  await h.mount()
  document.hidden = false
  document.emit('visibilitychange')
  await flush()
  assert.equal(h.calls.details.length, 1)
  assert.equal(h.state.trackingState.value, 'polling')
  assert.equal(timers.size, 1)

  const runTimer = async () => {
    const [id, timer] = timers.entries().next().value
    timers.delete(id)
    await timer.callback()
    await flush()
  }
  for (let attempt = 0; attempt < 39; attempt += 1) await runTimer()
  assert.equal(h.calls.details.length, 40)
  const [, staleTimer] = timers.entries().next().value
  document.hidden = true
  document.emit('visibilitychange')
  assert.equal(timers.size, 0)
  await staleTimer.callback()
  await flush()
  assert.equal(h.calls.details.length, 40)
  assert.equal(h.state.trackingState.value, 'paused')

  document.hidden = false
  document.emit('visibilitychange')
  await flush()
  assert.equal(h.calls.details.length, 41)
  assert.equal(h.state.trackingState.value, 'timeout')
  assert.equal(timers.size, 0)

  h.state.resumeTracking()
  await flush()
  assert.equal(h.calls.details.length, 42)
  assert.equal(h.state.trackingState.value, 'polling')
  assert.equal(timers.size, 1)
  h.dispose()
  assert.equal(document.listeners.get('visibilitychange').size, 0)
  assert.equal(timers.size, 0)
})

test('report detail requests reject stale actor responses', async t => {
  const scope = projectScope(2, 1)
  const first = deferred()
  let detailCalls = 0
  const h = await reportHarness(t, {
    scope,
    apiOverrides: {
      getTestExecutionDetail: async id => {
        detailCalls += 1
        if (detailCalls === 1) return first.promise
        return { data: { id, project: 2, status: 'FAILED', result_data: { test_cases: [] } } }
      },
    },
  })
  assert.equal(detailCalls, 1)
  scope.userId.value = 2
  await flush()
  assert.equal(detailCalls, 2)
  assert.equal(h.state.currentReport.value.status, 'FAILED')
  first.resolve({ data: { id: 42, project: 2, status: 'RUNNING', result_data: { test_cases: [] } } })
  await flush()
  assert.equal(h.state.currentReport.value.status, 'FAILED')
  assert.notEqual(h.state.trackingState.value, 'polling')
})
