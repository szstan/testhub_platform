import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, test } from 'node:test'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'
import { openAppExecutionReport } from '../src/utils/app-automation-report.mjs'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const instances = []
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

function popupFixture() {
  const navigations = []
  let closed = false
  return {
    popup: { opener: {}, location: { replace: url => navigations.push(url) }, close: () => { closed = true } },
    navigations,
    isClosed: () => closed,
  }
}

test('report opens a tab immediately and navigates only with a freshly fetched capability', async () => {
  const detail = deferred()
  const fixture = popupFixture()
  const calls = []
  const operation = openAppExecutionReport(12, {
    openWindow: (url, target) => { calls.push([url, target]); return fixture.popup },
    fetchExecution: id => { calls.push(['detail', id]); return detail.promise },
  })
  assert.deepEqual(calls, [['about:blank', '_blank'], ['detail', 12]])
  assert.equal(fixture.popup.opener, null)
  assert.deepEqual(fixture.navigations, [])
  detail.resolve({ data: { id: 12, report_path: '', report_access_token: 'fresh-fixture-token' } })
  assert.deepEqual(await operation, { opened: true })
  assert.deepEqual(fixture.navigations, ['/api/app-automation/executions/12/report/fresh-fixture-token/'])
})

test('expired or missing report capability is never reused and unauthorized detail closes the tab', async () => {
  for (const fetchExecution of [
    async () => ({ data: { id: 12, report_path: '', report_access_token: '' } }),
    async () => { throw new Error('unauthorized') },
    async () => ({ data: { id: 99, report_access_token: 'wrong-fixture' } }),
  ]) {
    const fixture = popupFixture()
    const result = await openAppExecutionReport(12, { openWindow: () => fixture.popup, fetchExecution })
    assert.equal(result.opened, false)
    assert.equal(fixture.isClosed(), true)
    assert.deepEqual(fixture.navigations, [])
  }
})

test('blocked popups fail before fetching report data', async () => {
  let fetches = 0
  const result = await openAppExecutionReport(12, {
    openWindow: () => null,
    fetchExecution: async () => { fetches++; return {} },
  })
  assert.deepEqual(result, { opened: false, reason: 'blocked' })
  assert.equal(fetches, 0)
})

test('suite resolver is evaluated after the tab opens and uses a fresh execution detail', async () => {
  const fixture = popupFixture()
  const events = []
  const result = await openAppExecutionReport(async () => { events.push('resolve'); return 23 }, {
    openWindow: () => { events.push('open'); return fixture.popup },
    fetchExecution: async id => { events.push(`detail:${id}`); return { data: { id, report_access_token: 'fresh-fixture' } } },
  })
  assert.deepEqual(events, ['open', 'resolve', 'detail:23'])
  assert.equal(result.opened, true)
})

const helpers = {
  getExecutionStatusType: () => 'info', getExecutionStatusText: value => value,
  getDisplayStatus: () => ({ type: 'info', text: '' }), formatDateTime: value => value,
}

async function loadReportView(path, apiName, api, reportAccess, extra = {}) {
  const mocks = {
    [apiName]: api,
    [path.includes('/reports/') ? '@/utils/app-automation-helpers.js' : '@/utils/app-automation-helpers']: helpers,
    '@/utils/app-automation-report.mjs': reportAccess,
    ...extra,
  }
  const instance = await loadSfcSetup(path, mocks, {
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  })
  instances.push(instance)
  return instance
}

test('all APP report entry points request fresh details even when filesystem paths are redacted', async () => {
  const calls = []
  const detail = async id => { calls.push(['detail', id]); return { data: { id, report_access_token: 'fresh-fixture' } } }
  const reportAccess = {
    openAppExecutionReport: async (item, options) => {
      const id = typeof item === 'function' ? await item() : item
      calls.push(['open', id])
      await options.fetchExecution(id)
      return { opened: true }
    },
  }
  const userStore = { user: { id: 7 }, getSessionGeneration: () => 1 }
  const deviceHelpers = {
    canSelectAppExecutionDevice: () => true, getAppExecutionErrorMessage: () => '',
    normalizeAppDevice: value => value, normalizeAppDeviceList: () => [], prepareAppDeviceForExecution: async () => ({}),
  }
  const commonApi = {
    getExecutionList: async () => ({ data: { results: [] } }), getExecutionDetail: detail,
    stopExecution: async () => ({}), deleteExecution: async () => ({}), getAppProjects: async () => ({ data: { results: [] } }),
  }
  const caseApi = {
    ...commonApi, getTestCaseList: async () => ({}), deleteTestCase: async () => ({}), executeTestCase: async () => ({}),
    batchExecuteTestCases: async () => ({}), getPackageList: async () => ({}), getWsStatus: async () => ({}),
    getDeviceList: async () => ({}), getAppDevice: async () => ({}),
  }
  const cases = await loadReportView('src/views/app-automation/test-cases/TestCaseList.vue', '@/api/app-automation', caseApi, reportAccess, {
    'vue-router': { useRouter: () => ({ push() {} }) }, '@element-plus/icons-vue': { Refresh: {}, Search: {} },
    '@/utils/websocket': { buildWebSocketUrl: () => '', issueWebSocketTicket: async () => '' },
    '@/utils/app-automation-device-execution.mjs': deviceHelpers, '@/stores/user': { useUserStore: () => userStore },
  })
  await cases.state.viewReport({ id: 12, report_path: '' })

  const suiteApi = {
    ...commonApi, getTestSuiteList: async () => ({}), getTestSuiteDetail: async () => ({}), createTestSuite: async () => ({}),
    updateTestSuite: async () => ({}), deleteTestSuite: async () => ({}), addTestCasesToSuite: async () => ({}),
    removeTestCaseFromSuite: async () => ({}), updateSuiteTestCaseOrder: async () => ({}), runTestSuite: async () => ({}),
    getTestSuiteExecutions: async () => ({ data: { data: [{ id: 12, report_path: '', report_access_token: 'old-fixture' }] } }),
    getTestCaseList: async () => ({}), getDeviceList: async () => ({}), getPackageList: async () => ({}), getAppDevice: async () => ({}),
  }
  const suites = await loadReportView('src/views/app-automation/suites/SuiteList.vue', '@/api/app-automation', suiteApi, reportAccess, {
    '@element-plus/icons-vue': { Plus: {}, Refresh: {}, Search: {}, Check: {}, Close: {}, Rank: {} },
    'vuedraggable': { default: {} }, '@/utils/app-automation-device-execution.mjs': deviceHelpers,
    '@/stores/user': { useUserStore: () => userStore },
  })
  await suites.state.viewReport({ id: 12, report_path: '' })

  const executions = await loadReportView('src/views/app-automation/executions/ExecutionList.vue', '@/api/app-automation', commonApi, reportAccess, {
    'vue-router': { useRouter: () => ({ push() {} }), useRoute: () => ({ query: {} }) },
    '@element-plus/icons-vue': { Search: {}, Refresh: {} },
    '@/api/app-step-diagnostics': { getFlowFailureScreenshot: async () => ({ data: new Blob() }) },
    '@/stores/user': { useUserStore: () => userStore },
  })
  await executions.state.viewReport({ id: 12, report_path: '' })

  const reports = await loadReportView('src/views/app-automation/reports/ReportList.vue', '@/api/app-automation.js', suiteApi, reportAccess, {
    '@element-plus/icons-vue': { Search: {}, DataAnalysis: {} },
  })
  await reports.state.viewAllureReport({ id: 12, report_path: '' })
  await reports.state.viewSuiteAllureReport({ id: 5 })
  assert.deepEqual(plain(calls), Array.from({ length: 5 }, () => [['open', 12], ['detail', 12]]).flat())
})

test('report buttons no longer gate on the redacted filesystem path', async () => {
  for (const file of ['executions/ExecutionList.vue', 'reports/ReportList.vue', 'suites/SuiteList.vue', 'test-cases/TestCaseList.vue']) {
    const source = await readFile(resolve(root, 'src/views/app-automation', file), 'utf8')
    assert.doesNotMatch(source, /v-if="(?:row|selectedCase)\.report_path"|if \(!execution\.report_path\)/)
  }
})
