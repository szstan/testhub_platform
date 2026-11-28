import assert from 'node:assert/strict'
import { test } from 'node:test'
import { nextTick, reactive, ref } from 'vue'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'
import * as routeHelpers from '../src/utils/ui-project-context.mjs'
import * as caseResultHelpers from '../src/utils/ui-case-execution.mjs'

const { loadUiProjectOptions, selectUiProject, uiWorkbenchReturnLocation } = routeHelpers
const projectList = [{ id: 1, name: 'Store' }, { id: 2, name: 'Checkout' }]

function projectScope(project = '') {
  return {
    projects: ref(projectList), projectId: ref(project), projectsLoading: ref(false),
    projectsError: ref(false), invalidProject: ref(false), reloadProjects() {},
  }
}

const icons = (...names) => Object.fromEntries(names.map(name => [name, {}]))
const flush = async () => { await new Promise(resolve => setImmediate(resolve)); await nextTick() }
const drfCasePage = (records, { page }) => {
  const pageSize = 20
  const offset = (page - 1) * pageSize
  if (page > 1 && offset >= records.length) {
    throw Object.assign(new Error('Invalid page.'), { response: { status: 404 } })
  }
  return { data: {
    count: records.length,
    next: offset + pageSize < records.length ? `/?page=${page + 1}` : null,
    results: records.slice(offset, offset + pageSize).map(testCase => ({ ...testCase })),
  } }
}

function projectMocks(scope) {
  return {
    '@/composables/useUiProjectContext': { useUiProjectContext: () => scope },
    '@/utils/ui-project-context.mjs': routeHelpers,
    '@/utils/ui-case-execution.mjs': caseResultHelpers,
  }
}

test('an explicit unavailable project requires reselection and a stale remembered project is ignored', () => {
  assert.deepEqual(selectUiProject(projectList, '2', '1'), { id: 2, invalid: false })
  assert.deepEqual(selectUiProject(projectList, '3', '1'), { id: '', invalid: true })
  assert.deepEqual(selectUiProject(projectList, ['1', '2'], '1'), { id: '', invalid: true })
  assert.deepEqual(selectUiProject(projectList, undefined, '3'), { id: '', invalid: false })
  assert.deepEqual(selectUiProject([projectList[1]], undefined, '3'), { id: 2, invalid: false })
})

test('project options include later pages using local API pagination', async () => {
  const calls = []
  const projects = await loadUiProjectOptions(async params => {
    calls.push(params.page)
    return { data: params.page === 1 ? { next: 'ignored', results: [{ id: '1', name: 'Store' }] } : { next: null, results: [projectList[1]] } }
  })
  assert.deepEqual(projects, projectList)
  assert.deepEqual(calls, [1, 2])
})

test('sign-in retains the exact workbench record using allowlisted routes and validated IDs', () => {
  assert.deepEqual(uiWorkbenchReturnLocation('UiExecutions', { project: '02', executionId: '40', anything: 'ignored' }), {
    name: 'UiExecutions', query: { project: '2', executionId: '40' },
  })
  assert.deepEqual(uiWorkbenchReturnLocation('UiTestCases', { project: '2', setup: 'case' }), {
    name: 'UiTestCases', query: { project: '2', setup: 'case' },
  })
  assert.equal(uiWorkbenchReturnLocation('/login', {}), null)
  assert.deepEqual(uiWorkbenchReturnLocation('UiReports', { project: ['2', '3'], executionId: '-2' }), { name: 'UiReports', query: {} })
})

test('dashboard data requests fail independently and stale results cannot replace a switched project', async t => {
  const scope = projectScope(1)
  const statsForFirst = deferred()
  const runsForFirst = deferred()
  const feedQueries = []
  const result = { id: 42, kind: 'suite', name: 'Checkout', status: 'failed', failed_step_number: 3 }
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/dashboard/Dashboard.vue', {
    ...projectMocks(scope),
    '@element-plus/icons-vue': icons('ArrowRight', 'CaretRight', 'Loading', 'Plus', 'QuestionFilled', 'VideoPlay', 'Refresh'),
    '@/api/ui_automation': {
      getDashboardStats: ({ project }) => project === 1 ? statsForFirst.promise : Promise.resolve({ data: { test_case_count: 4, suite_entity_count: 2 } }),
      getDashboardRecentExecutions: params => {
        feedQueries.push(plain(params))
        return params.project === 1 ? runsForFirst.promise : Promise.resolve({ data: { results: [result] } })
      },
      getOperationRecords: async () => { throw new Error('Unavailable') },
    },
  })
  t.after(dispose)
  scope.projectId.value = 2
  await flush()
  await state.loadActivity()
  state.executionFilter.value = 'failures'
  await flush()
  statsForFirst.resolve({ data: { test_case_count: 100, suite_entity_count: 20 } })
  runsForFirst.resolve({ data: { results: [] } })
  await flush()
  assert.equal(state.activityError.value, true)
  assert.equal(state.assetError.value, false)
  assert.equal(state.suiteCount.value, 2)
  assert.deepEqual(plain(state.executions.value), [result])
  assert.deepEqual(feedQueries.at(-1), { project: 2, status: 'failed' })
  assert.deepEqual(plain(state.executionRoute(result)), { name: 'UiReports', query: { project: '2', executionId: '42' } })
})

test('the execution page opens an exact ID even outside the list and rejects cross-project results', async t => {
  const scope = projectScope(2)
  const route = reactive({ name: 'UiExecutions', query: { project: '2', executionId: '42' } })
  const ids = []
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/executions/ExecutionList.vue', {
    ...projectMocks(scope),
    '@element-plus/icons-vue': icons('Search', 'WarningFilled', 'Refresh'),
    'vue-router': { useRoute: () => route, useRouter: () => ({}) },
    '@/api/ui_automation': {
      getTestCaseExecutions: async () => ({ data: { results: [], count: 0 } }),
      getTestCaseExecutionDetail: async id => {
        ids.push(id)
        return { data: { id, project: id === 42 ? 2 : 1, status: 'failed', execution_logs: 'Browser unavailable', screenshots: [] } }
      },
      deleteTestCaseExecution() {}, batchDeleteTestCaseExecutions() {}, runTestCase() {},
    },
  })
  t.after(dispose)
  await flush()
  assert.equal(state.currentExecution.value.id, 42)
  assert.equal(state.failure.value.message, 'Browser unavailable')
  route.query.executionId = '60'
  await flush()
  assert.equal(state.currentExecution.value, null)
  assert.deepEqual(ids, [42, 60])
})

test('suite report polls the exact run, pauses on a temporary error and resumes with failure details', async t => {
  const scope = projectScope(2)
  const route = reactive({ name: 'UiReports', query: { project: '2', executionId: '91' } })
  const timers = []
  const detailIds = []
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/reports/ReportList.vue', {
    ...projectMocks(scope),
    '@element-plus/icons-vue': icons('Refresh', 'Document', 'Delete', 'WarningFilled'),
    'vue-router': { useRoute: () => route, useRouter: () => ({}) },
    '@/api/ui_automation': {
      getTestExecutions: async () => ({ data: { results: [{ id: 91, project: 2, status: 'PENDING', test_suite_name: 'Checkout' }], count: 1 } }),
      getTestExecutionDetail: async id => {
        detailIds.push(Number(id))
        if (detailIds.length === 2) throw new Error('Temporary load failure')
        const failed = detailIds.length > 2
        return { data: { id: Number(id), project: 2, status: failed ? 'FAILED' : 'RUNNING', result_data: { test_cases: failed ? [
          { test_case_name: 'Checkout', status: 'error', steps: [{ step_number: 3, success: false, error: 'Wrong order status' }] },
        ] : [] } } }
      },
      deleteTestExecution() {},
    },
  }, { setTimeout: run => { timers.push(run); return timers.length }, clearTimeout() {} })
  t.after(dispose)
  await flush()
  assert.equal(state.currentReport.value.status, 'RUNNING')
  assert.equal(state.reports.value[0].status, 'RUNNING')
  assert.equal(state.trackingState.value, 'polling')
  await timers.shift()()
  assert.equal(state.currentReport.value.status, 'RUNNING')
  assert.equal(state.trackingState.value, 'paused')
  state.resumeTracking()
  await flush()
  assert.equal(state.currentReport.value.status, 'FAILED')
  assert.equal(state.failureSummary.value.step.step_number, 3)
  assert.equal(state.failureSummary.value.error, 'Wrong order status')
  assert.equal(state.reports.value[0].status, 'FAILED')
  assert.equal(state.trackingState.value, 'idle')
  assert.deepEqual(detailIds, [91, 91, 91])
})

test('failure summary identifies the first failed step and prefers evidence from that step', () => {
  const { caseExecutionFailure, readCaseExecutionLog } = caseResultHelpers
  const record = { status: 'failed', screenshots: [{ step_number: 1, url: 'other' }, { step_number: 3, url: 'matching' }] }
  const log = readCaseExecutionLog('[{"step_number":1,"success":true},{"step_number":3,"success":false,"error":"text mismatch"},{"step_number":5,"success":false}]')
  assert.deepEqual(caseExecutionFailure(record, log), { stepNumber: 3, message: 'text mismatch', screenshotIndex: 1 })
})

test('saved case changes survive stale list snapshots without replaying pending route restore', async t => {
  const scope = projectScope(1)
  const route = reactive({ name: 'UiTestCases', query: { project: '1', caseId: '11' } })
  const initialList = deferred()
  const staleRefresh = deferred()
  const originalCase = { id: 11, name: 'Original', steps: [{ id: 111, action_type: 'click', element: 1, description: 'Original step' }] }
  const createdCase = { id: 12, name: 'Created', project: 1, steps: [] }
  const records = [originalCase]
  let listCalls = 0
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/test-cases/TestCaseManager.vue', {
    ...projectMocks(scope),
    'vue-router': { useRoute: () => route, useRouter: () => ({ replace: async () => {} }) },
    '@element-plus/icons-vue': icons('Search', 'Plus', 'Edit', 'Delete', 'Check', 'CaretRight', 'ArrowUp', 'ArrowDown', 'Rank', 'Picture', 'Warning', 'View', 'ZoomIn', 'WarningFilled', 'MagicStick'),
    'vuedraggable': { default: {} },
    '@/components/DataFactorySelector.vue': { default: {} },
    '@/api/ui_automation': {
      getTestCases: async () => ++listCalls === 1 ? initialList.promise : staleRefresh.promise,
      getElements: async () => ({ data: { results: [] } }),
      updateTestCase: async (id, data) => {
        const index = records.findIndex(item => item.id === id)
        records[index] = { ...records[index], ...data }
        return { data: { ...records[index] } }
      },
      createTestCase: async data => {
        records.push(createdCase)
        return { data: { ...createdCase, ...data } }
      },
      deleteTestCase: async id => { records.splice(records.findIndex(item => item.id === id), 1) },
      copyTestCase: async () => ({ data: { ...createdCase, id: 13, name: 'Copy' } }),
      runTestCase() {}, getLocatorStrategies() {},
    },
    '@/api/data-factory': { getVariableFunctions: async () => ({ data: [] }) },
  })
  t.after(dispose)
  await flush()

  state.openCreateCase()
  state.testCaseForm.name = 'Created'
  await state.saveTestCaseForm()
  assert.equal(state.selectedTestCase.value.id, 12)

  initialList.resolve({ data: { results: [{ ...originalCase, steps: originalCase.steps.map(step => ({ ...step })) }] } })
  await flush()
  assert.deepEqual(plain(state.testCases.value.map(item => item.id)), [12, 11])
  assert.equal(state.selectedTestCase.value.id, 12)

  route.query.caseId = '12'
  await flush()
  assert.equal(state.selectedTestCase.value.id, 12)
  route.query.caseId = '11'
  await flush()
  assert.equal(state.selectedTestCase.value.id, 11)

  const refresh = state.loadTestCases()
  state.currentSteps.value[0].description = 'Saved step'
  await state.saveTestCase()
  state.editTestCase(state.selectedTestCase.value)
  state.testCaseForm.name = 'Updated'
  await state.saveTestCaseForm()
  await state.copyTestCase(createdCase)
  await state.deleteTestCase(createdCase)

  staleRefresh.resolve({ data: { results: [
    { ...originalCase, steps: originalCase.steps.map(step => ({ ...step })) },
    { ...createdCase },
  ] } })
  await refresh
  await flush()
  const updated = state.testCases.value.find(item => item.id === 11)
  assert.equal(updated.name, 'Updated')
  assert.equal(updated.steps[0].description, 'Saved step')
  assert.equal(state.testCases.value.some(item => item.id === 12), false)
  assert.equal(state.testCases.value.some(item => item.id === 13), true)
  assert.equal(state.selectedTestCase.value.id, 11)
})

test('case list retries a real DRF offset walk after a local head insertion', async t => {
  const scope = projectScope(1)
  const route = reactive({ name: 'UiTestCases', query: { project: '1' } })
  const records = Array.from({ length: 21 }, (_, index) => ({ id: index + 1, name: `Case ${index + 1}`, project: 1, steps: [] }))
  const requests = []
  const pausedPageTwo = deferred()
  let pausePageTwo = false
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/test-cases/TestCaseManager.vue', {
    ...projectMocks(scope),
    'vue-router': { useRoute: () => route, useRouter: () => ({ replace: async () => {} }) },
    '@element-plus/icons-vue': icons('Search', 'Plus', 'Edit', 'Delete', 'Check', 'CaretRight', 'ArrowUp', 'ArrowDown', 'Rank', 'Picture', 'Warning', 'View', 'ZoomIn', 'WarningFilled', 'MagicStick'),
    'vuedraggable': { default: {} },
    '@/components/DataFactorySelector.vue': { default: {} },
    '@/api/ui_automation': {
      getTestCases: async params => {
        requests.push(params.page)
        if (pausePageTwo && params.page === 2) {
          pausePageTwo = false
          await pausedPageTwo.promise
        }
        return drfCasePage(records, params)
      },
      getElements: async () => ({ data: { results: [] } }),
      createTestCase: async data => {
        const created = { ...data, id: 22, steps: [] }
        records.unshift(created)
        return { data: { ...created } }
      },
      getLocatorStrategies() {}, updateTestCase() {}, deleteTestCase() {}, copyTestCase() {}, runTestCase() {},
    },
    '@/api/data-factory': { getVariableFunctions: async () => ({ data: [] }) },
  })
  t.after(dispose)
  await flush()
  assert.deepEqual(plain(state.testCases.value.map(testCase => testCase.id)), records.map(testCase => testCase.id))

  pausePageTwo = true
  const refresh = state.loadTestCases()
  await flush()
  assert.deepEqual(requests.slice(-2), [1, 2])

  state.openCreateCase()
  state.testCaseForm.name = 'Inserted at head'
  await state.saveTestCaseForm()
  pausedPageTwo.resolve()
  await refresh
  await flush()

  const ids = plain(state.testCases.value.map(testCase => testCase.id))
  assert.deepEqual(ids, records.map(testCase => testCase.id))
  assert.equal(new Set(ids).size, ids.length)
  assert.equal(state.selectedTestCase.value.id, 22)
  assert.equal(state.casesError.value, false)
  assert.deepEqual(requests, [1, 2, 1, 2, 1, 2])
})

test('case list retries a real DRF offset walk after a local head deletion', async t => {
  const scope = projectScope(1)
  const route = reactive({ name: 'UiTestCases', query: { project: '1' } })
  const records = Array.from({ length: 21 }, (_, index) => ({ id: index + 1, name: `Case ${index + 1}`, project: 1, steps: [] }))
  const requests = []
  const pausedPageTwo = deferred()
  let pausePageTwo = false
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/test-cases/TestCaseManager.vue', {
    ...projectMocks(scope),
    'vue-router': { useRoute: () => route, useRouter: () => ({ replace: async () => {} }) },
    '@element-plus/icons-vue': icons('Search', 'Plus', 'Edit', 'Delete', 'Check', 'CaretRight', 'ArrowUp', 'ArrowDown', 'Rank', 'Picture', 'Warning', 'View', 'ZoomIn', 'WarningFilled', 'MagicStick'),
    'vuedraggable': { default: {} },
    '@/components/DataFactorySelector.vue': { default: {} },
    '@/api/ui_automation': {
      getTestCases: async params => {
        requests.push(params.page)
        if (pausePageTwo && params.page === 2) {
          pausePageTwo = false
          await pausedPageTwo.promise
        }
        return drfCasePage(records, params)
      },
      getElements: async () => ({ data: { results: [] } }),
      deleteTestCase: async id => { records.splice(records.findIndex(testCase => testCase.id === id), 1) },
      getLocatorStrategies() {}, updateTestCase() {}, createTestCase() {}, copyTestCase() {}, runTestCase() {},
    },
    '@/api/data-factory': { getVariableFunctions: async () => ({ data: [] }) },
  })
  t.after(dispose)
  await flush()
  assert.deepEqual(plain(state.testCases.value.map(testCase => testCase.id)), records.map(testCase => testCase.id))

  const deletedId = records[0].id
  pausePageTwo = true
  const refresh = state.loadTestCases()
  await flush()
  assert.deepEqual(requests.slice(-2), [1, 2])

  await state.deleteTestCase(records[0])
  pausedPageTwo.resolve()
  await refresh
  await flush()

  const ids = plain(state.testCases.value.map(testCase => testCase.id))
  assert.deepEqual(ids, records.map(testCase => testCase.id))
  assert.equal(new Set(ids).size, ids.length)
  assert.equal(ids.includes(deletedId), false)
  assert.equal(ids.includes(21), true)
  assert.equal(state.casesError.value, false)
  assert.deepEqual(requests, [1, 2, 1, 2, 1])
})

test('caseId restores once and does not override deliberate selections during save or list refresh', async t => {
  const scope = projectScope('')
  const route = reactive({ name: 'UiTestCases', query: { project: '1', caseId: '1' } })
  const pendingRefresh = deferred()
  const records = [
    { id: 1, name: 'A', steps: [{ id: 11, action_type: 'click', element: 1 }] },
    { id: 2, name: 'B', steps: [{ id: 21, action_type: 'click', element: 2 }] },
  ]
  let listCalls = 0
  let nextId = 3
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/test-cases/TestCaseManager.vue', {
    ...projectMocks(scope),
    'vue-router': { useRoute: () => route, useRouter: () => ({ replace: async () => {} }) },
    '@element-plus/icons-vue': icons('Search', 'Plus', 'Edit', 'Delete', 'Check', 'CaretRight', 'ArrowUp', 'ArrowDown', 'Rank', 'Picture', 'Warning', 'View', 'ZoomIn', 'WarningFilled', 'MagicStick'),
    'vuedraggable': { default: {} },
    '@/components/DataFactorySelector.vue': { default: {} },
    '@/api/ui_automation': {
      getTestCases: async () => {
        listCalls += 1
        if (listCalls === 2) return pendingRefresh.promise
        return { data: { results: records.map(item => ({ ...item })) } }
      },
      getElements: async () => ({ data: { results: [] } }),
      updateTestCase: async (id, data) => {
        const index = records.findIndex(item => item.id === id)
        records[index] = { ...records[index], ...data }
        return { data: { ...records[index] } }
      },
      createTestCase: async data => {
        const created = { ...data, id: nextId++, steps: [] }
        records.push(created)
        return { data: { ...created } }
      },
      deleteTestCase() {}, runTestCase() {}, copyTestCase() {}, getLocatorStrategies() {},
    },
    '@/api/data-factory': { getVariableFunctions: async () => ({ data: [] }) },
  })
  t.after(dispose)
  await flush()
  assert.equal(state.selectedTestCase.value, null)
  scope.projectId.value = 1
  await flush()

  assert.equal(state.selectedTestCase.value.id, 1)
  state.selectTestCase(records[1])
  await state.saveTestCase()
  assert.equal(state.selectedTestCase.value.id, 2)

  state.editTestCase(state.selectedTestCase.value)
  state.testCaseForm.name = 'B updated'
  await state.saveTestCaseForm()
  assert.equal(state.selectedTestCase.value.id, 2)
  assert.equal(state.selectedTestCase.value.name, 'B updated')

  const refresh = state.loadTestCases()
  state.openCreateCase()
  state.testCaseForm.name = 'C'
  await state.saveTestCaseForm()
  assert.equal(state.selectedTestCase.value.id, 3)

  pendingRefresh.resolve({ data: { results: records.map(item => ({ ...item })) } })
  await refresh
  await flush()
  assert.equal(state.selectedTestCase.value.id, 3)
  assert.equal(state.testCases.value.some(item => item.id === 3), true)

  route.query.caseId = '3'
  await flush()
  assert.equal(state.selectedTestCase.value.id, 3)
})

test('case runs are owned per project and case, allowing B while A runs and fencing stale results', async t => {
  const scope = projectScope(1)
  const route = reactive({ name: 'UiTestCases', query: { project: '1' } })
  const recordsByProject = {
    1: [
      { id: 1, name: 'A', steps: [{ id: 11, action_type: 'click', element: 1 }] },
      { id: 2, name: 'B', steps: [{ id: 21, action_type: 'click', element: 2 }] },
    ],
    2: [{ id: 1, name: 'B in Checkout', steps: [{ id: 31, action_type: 'click', element: 3 }] }],
  }
  const runCalls = []
  const updates = []
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/test-cases/TestCaseManager.vue', {
    ...projectMocks(scope),
    'vue-router': { useRoute: () => route, useRouter: () => ({ replace: async () => {} }) },
    '@element-plus/icons-vue': icons('Search', 'Plus', 'Edit', 'Delete', 'Check', 'CaretRight', 'ArrowUp', 'ArrowDown', 'Rank', 'Picture', 'Warning', 'View', 'ZoomIn', 'WarningFilled', 'MagicStick'),
    'vuedraggable': { default: {} },
    '@/components/DataFactorySelector.vue': { default: {} },
    '@/api/ui_automation': {
      getTestCases: async ({ project }) => ({ data: { results: recordsByProject[project].map(item => ({ ...item })) } }),
      getElements: async () => ({ data: { results: [] } }),
      updateTestCase: async (id, data) => {
        updates.push({ id, data })
        return { data: { id, ...data } }
      },
      runTestCase: (id, params) => {
        const request = deferred()
        runCalls.push({ id, params, request })
        return request.promise
      },
      createTestCase() {}, deleteTestCase() {}, copyTestCase() {}, getLocatorStrategies() {},
    },
    '@/api/data-factory': { getVariableFunctions: async () => ({ data: [] }) },
  })
  t.after(dispose)
  await flush()

  const caseA = state.testCases.value[0]
  const caseB = state.testCases.value[1]
  state.selectTestCase(caseA)
  const runA = state.runTestCase(caseA)
  state.selectTestCase(caseB)
  const runB = state.runTestCase(caseB)
  state.selectTestCase(caseA)
  assert.equal(state.isRunning.value, true)
  await state.runTestCase(caseA)
  assert.equal(runCalls.length, 2)

  state.selectTestCase(caseB)
  runCalls[1].request.resolve({ data: { success: true, logs: 'B result', execution_id: 202 } })
  await runB
  assert.equal(state.isRunning.value, false)
  state.selectTestCase(caseA)
  assert.equal(state.isRunning.value, true)

  scope.projectId.value = 2
  await flush()
  const caseInSecondProject = state.testCases.value[0]
  state.selectTestCase(caseInSecondProject)
  assert.equal(state.isRunning.value, false)
  await state.saveTestCase()
  assert.equal(updates.at(-1).id, 1)
  const runInSecondProject = state.runTestCase(caseInSecondProject)
  assert.equal(runCalls.length, 3)
  runCalls[2].request.resolve({ data: { success: true, logs: 'Checkout result', execution_id: 301 } })
  await runInSecondProject
  assert.equal(state.executionResult.value.logs, 'Checkout result')

  runCalls[0].request.resolve({ data: { success: true, logs: 'stale Store result', execution_id: 101 } })
  await runA
  assert.equal(state.executionResult.value.logs, 'Checkout result')
  assert.equal(state.isRunning.value, false)
})

test('exact report polling and refresh reconcile only the matching row without replacing fresher state', async t => {
  const scope = projectScope(2)
  const route = reactive({ name: 'UiReports', query: { project: '2', executionId: '20' } })
  const initialList = deferred()
  const refreshList = deferred()
  const timers = []
  let listCalls = 0
  let detailCalls = 0
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/reports/ReportList.vue', {
    ...projectMocks(scope),
    '@element-plus/icons-vue': icons('Refresh', 'Document', 'Delete', 'WarningFilled'),
    'vue-router': { useRoute: () => route, useRouter: () => ({}) },
    '@/api/ui_automation': {
      getTestExecutions: async () => ++listCalls === 1 ? initialList.promise : refreshList.promise,
      getTestExecutionDetail: async id => {
        detailCalls += 1
        const status = ['RUNNING', 'SUCCESS', 'FAILED', 'SUCCESS'][detailCalls - 1]
        return { data: { id: Number(id), project: 2, status, test_suite_name: 'Checkout' } }
      },
      deleteTestExecution() {},
    },
  }, { setTimeout: callback => { timers.push(callback); return timers.length }, clearTimeout() {} })
  t.after(dispose)
  await flush()

  assert.equal(state.currentReport.value.status, 'RUNNING')
  await timers.shift()()
  assert.equal(state.currentReport.value.status, 'SUCCESS')

  initialList.resolve({ data: { results: [
    { id: 20, project: 2, status: 'PENDING', test_suite_name: 'Checkout' },
    { id: 21, project: 2, status: 'RUNNING', test_suite_name: 'Other' },
  ], count: 54 } })
  await flush()
  assert.deepEqual(state.reports.value.map(report => report.id), [20, 21])
  assert.equal(state.reports.value[0].status, 'SUCCESS')
  assert.equal(state.total.value, 54)

  const refresh = state.refreshReports()
  await flush()
  assert.equal(state.currentReport.value.status, 'FAILED')
  assert.equal(state.reports.value[0].status, 'FAILED')
  refreshList.resolve({ data: { results: [
    { id: 20, project: 2, status: 'RUNNING', test_suite_name: 'Checkout' },
    { id: 21, project: 2, status: 'RUNNING', test_suite_name: 'Other' },
  ], count: 54 } })
  await refresh
  assert.deepEqual(state.reports.value.map(report => report.id), [20, 21])
  assert.equal(state.reports.value[0].status, 'FAILED')
  assert.equal(state.total.value, 54)

  route.query.executionId = '99'
  await flush()
  assert.equal(state.currentReport.value.id, 99)
  assert.deepEqual(state.reports.value.map(report => report.id), [20, 21])
})

test('a stale paginated list keeps terminal state and evidence for report A after navigating to report B', async t => {
  const scope = projectScope(2)
  const route = reactive({ name: 'UiReports', query: { project: '2', executionId: '10' } })
  const pendingList = deferred()
  const timers = []
  let detailCalls = 0
  let listCalls = 0
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/reports/ReportList.vue', {
    ...projectMocks(scope),
    '@element-plus/icons-vue': icons('Refresh', 'Document', 'Delete', 'WarningFilled'),
    'vue-router': { useRoute: () => route, useRouter: () => ({}) },
    '@/api/ui_automation': {
      getTestExecutions: async () => {
        listCalls += 1
        return listCalls === 1 ? pendingList.promise : { data: { results: [
          { id: 10, project: 2, status: 'FAILED', test_suite_name: 'A', result_data: { error_message: 'Changed terminal result' } },
          { id: 20, project: 2, status: 'RUNNING', test_suite_name: 'B' },
        ], count: 38 } }
      },
      getTestExecutionDetail: async id => {
        if (Number(id) !== 10) return { data: { id: 20, project: 2, status: 'RUNNING', test_suite_name: 'B' } }
        detailCalls += 1
        return { data: {
          id: 10, project: 2, status: detailCalls === 1 ? 'RUNNING' : 'SUCCESS',
          test_suite_name: 'A',
          ...(detailCalls > 1 ? { result_data: { test_cases: [{ test_case_name: 'A case', status: 'passed', steps: [{ step_number: 1, success: true, description: 'Completed evidence' }] }] } } : {}),
        } }
      },
      deleteTestExecution() {},
    },
  }, { setTimeout: callback => { timers.push(callback); return timers.length }, clearTimeout() {} })
  t.after(dispose)
  await flush()

  await timers.shift()()
  assert.equal(state.currentReport.value.id, 10)
  assert.equal(state.currentReport.value.status, 'SUCCESS')

  route.query.executionId = '20'
  await flush()
  assert.equal(state.currentReport.value.id, 20)

  pendingList.resolve({ data: { results: [
    { id: 10, project: 2, status: 'RUNNING', test_suite_name: 'A' },
    { id: 20, project: 2, status: 'PENDING', test_suite_name: 'B' },
  ], count: 37 } })
  await flush()
  assert.deepEqual(plain(state.reports.value.map(report => report.id)), [10, 20])
  assert.equal(state.reports.value[0].status, 'SUCCESS')
  assert.equal(state.reports.value[0].result_data.test_cases[0].steps[0].description, 'Completed evidence')
  assert.equal(state.total.value, 37)

  await state.loadReports()
  assert.equal(state.reports.value[0].status, 'FAILED')
  assert.equal(state.reports.value[0].result_data.error_message, 'Changed terminal result')
  assert.equal(state.total.value, 38)
})

test('an earlier running response and a failed detail refresh cannot roll back a completed report', async t => {
  const scope = projectScope(2)
  const route = reactive({ name: 'UiReports', query: { project: '2', executionId: '20' } })
  const delayedInitialDetail = deferred()
  const delayedRefreshDetail = deferred()
  const record = { id: 20, project: 2, status: 'SUCCESS' }
  let listCalls = 0
  let detailCalls = 0
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/reports/ReportList.vue', {
    ...projectMocks(scope),
    '@element-plus/icons-vue': icons('Refresh', 'Document', 'Delete', 'WarningFilled'),
    'vue-router': { useRoute: () => route, useRouter: () => ({}) },
    '@/api/ui_automation': {
      getTestExecutions: async () => ({ data: { results: [
        ++listCalls === 1 ? record : { ...record, status: 'RUNNING' },
      ], count: 1 } }),
      getTestExecutionDetail: async () => ++detailCalls === 1 ? delayedInitialDetail.promise : delayedRefreshDetail.promise,
      deleteTestExecution() {},
    },
  }, { setTimeout() { throw new Error('A completed run must not start polling') }, clearTimeout() {} })
  t.after(dispose)
  await flush()
  assert.equal(state.reports.value[0].status, 'SUCCESS')

  delayedInitialDetail.resolve({ data: { ...record, status: 'RUNNING' } })
  await flush()
  assert.equal(state.currentReport.value.status, 'SUCCESS')
  assert.equal(state.trackingState.value, 'idle')

  const refreshing = state.refreshReports()
  await flush()
  assert.equal(state.exactDetailState.value, 'loading')
  assert.equal(state.reports.value[0].status, 'SUCCESS')
  delayedRefreshDetail.reject(new Error('Temporary network failure'))
  await refreshing
  assert.equal(state.reports.value[0].status, 'SUCCESS')
  assert.equal(state.currentReport.value.status, 'SUCCESS')
})

test('terminal report state survives a delayed active list row and a failed exact refresh', async t => {
  const scope = projectScope(2)
  const route = reactive({ name: 'UiReports', query: { project: '2', executionId: '40' } })
  const initialDetail = deferred()
  const refreshDetail = deferred()
  const refreshList = deferred()
  let listCalls = 0
  let detailCalls = 0
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/reports/ReportList.vue', {
    ...projectMocks(scope),
    '@element-plus/icons-vue': icons('Refresh', 'Document', 'Delete', 'WarningFilled'),
    'vue-router': { useRoute: () => route, useRouter: () => ({}) },
    '@/api/ui_automation': {
      getTestExecutions: async () => {
        listCalls += 1
        return listCalls === 1
          ? { data: { results: [
            { id: 40, project: 2, status: 'SUCCESS', test_suite_name: 'Checkout' },
            { id: 41, project: 2, status: 'PENDING', test_suite_name: 'Other' },
          ], count: 42 } }
          : refreshList.promise
      },
      getTestExecutionDetail: async () => {
        detailCalls += 1
        return detailCalls === 1 ? initialDetail.promise : refreshDetail.promise
      },
      deleteTestExecution() {},
    },
  })
  t.after(dispose)
  await flush()
  assert.equal(state.currentReport.value, null)
  assert.equal(state.reports.value[0].status, 'SUCCESS')

  initialDetail.resolve({ data: { id: 40, project: 2, status: 'RUNNING', test_suite_name: 'Checkout' } })
  await flush()
  assert.equal(state.currentReport.value.status, 'SUCCESS')
  assert.equal(state.trackingState.value, 'idle')

  const refresh = state.refreshReports()
  await flush()
  assert.equal(state.exactDetailState.value, 'loading')
  refreshList.resolve({ data: { results: [
    { id: 40, project: 2, status: 'RUNNING', test_suite_name: 'Checkout' },
    { id: 41, project: 2, status: 'PENDING', test_suite_name: 'Other' },
  ], count: 42 } })
  await flush()
  assert.equal(state.reports.value[0].status, 'SUCCESS')
  assert.equal(state.currentReport.value.status, 'SUCCESS')

  refreshDetail.reject(new Error('Exact refresh failed'))
  await refresh
  assert.equal(state.exactDetailState.value, 'ready')
  assert.equal(state.currentReport.value.status, 'SUCCESS')
  assert.equal(state.reports.value[0].status, 'SUCCESS')
  assert.equal(state.trackingState.value, 'idle')
  assert.deepEqual(state.reports.value.map(report => report.id), [40, 41])
  assert.equal(state.total.value, 42)
})

test('a pending exact poll cannot update report state after switching project', async t => {
  const scope = projectScope(1)
  const route = reactive({ name: 'UiReports', query: { project: '1', executionId: '30' } })
  const pendingPoll = deferred()
  const timers = []
  let detailCalls = 0
  const { state, dispose } = await loadSfcSetup('src/views/ui-automation/reports/ReportList.vue', {
    ...projectMocks(scope),
    '@element-plus/icons-vue': icons('Refresh', 'Document', 'Delete', 'WarningFilled'),
    'vue-router': { useRoute: () => route, useRouter: () => ({}) },
    '@/api/ui_automation': {
      getTestExecutions: async ({ project }) => ({ data: { results: [{ id: 30, project, status: 'PENDING' }], count: 1 } }),
      getTestExecutionDetail: async id => {
        detailCalls += 1
        return detailCalls === 1
          ? { data: { id: Number(id), project: 1, status: 'RUNNING' } }
          : pendingPoll.promise
      },
      deleteTestExecution() {},
    },
  }, { setTimeout: callback => { timers.push(callback); return timers.length }, clearTimeout() {} })
  t.after(dispose)
  await flush()

  const poll = timers.shift()()
  await flush()
  assert.equal(detailCalls, 2)
  delete route.query.executionId
  scope.projectId.value = 2
  await flush()
  assert.equal(state.currentReport.value, null)
  assert.deepEqual(state.reports.value, [{ id: 30, project: 2, status: 'PENDING' }])

  pendingPoll.resolve({ data: { id: 30, project: 1, status: 'SUCCESS' } })
  await poll
  await flush()
  assert.equal(state.currentReport.value, null)
  assert.deepEqual(state.reports.value, [{ id: 30, project: 2, status: 'PENDING' }])
})
