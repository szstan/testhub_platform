import assert from 'node:assert/strict'
import { test } from 'node:test'
import { nextTick, reactive, ref } from 'vue'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'
import * as contextHelpers from '../src/utils/ui-project-context.mjs'
import * as caseHelpers from '../src/utils/ui-case-execution.mjs'
import { readUiPendingRun, uiPendingRunKey } from '../src/utils/ui-execution-submission.mjs'

const flush = async () => { await new Promise(resolve => setImmediate(resolve)); await nextTick() }
const offline = () => new Error('Connection lost')

async function executions(t) {
  const rows = new Map()
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: {
    getItem: key => rows.get(key), setItem: (key, value) => rows.set(key, value), removeItem: key => rows.delete(key),
  } })
  t.after(() => { if (descriptor) Object.defineProperty(globalThis, 'sessionStorage', descriptor); else delete globalThis.sessionStorage })
  const scope = { projects: ref([{ id: 2 }, { id: 3 }]), projectId: ref(2), userId: ref(1), projectsLoading: ref(false), projectsError: ref(false), invalidProject: ref(false), reloadProjects() {} }
  const route = reactive({ name: 'UiExecutions', query: { project: '2', executionId: '100' } })
  const record = { id: 100, project: 2, test_case: 11, status: 'running', engine: 'playwright', browser: 'chrome', headless: true, execution_logs: '' }
  const pushes = []
  const posts = []
  const api = {
    getTestCaseExecutions: async () => ({ data: { results: [{ ...record }], count: 1 } }),
    getTestCaseExecutionDetail: async id => ({ data: { ...record, id } }),
    deleteTestCaseExecution() {}, batchDeleteTestCaseExecutions() {},
    abortTestCaseExecution: async () => { record.status = 'aborted'; return { data: { status: 'aborted' } } },
    runTestCase: async () => ({ data: { accepted: true, execution_id: 101 } }),
    getTestCaseSubmission: async () => { throw Object.assign(offline(), { response: { status: 404 } }) },
  }
  const loaded = await loadSfcSetup('src/views/ui-automation/executions/ExecutionList.vue', {
    '@/composables/useUiProjectContext': { useUiProjectContext: () => scope },
    '@/utils/ui-project-context.mjs': contextHelpers, '@/utils/ui-case-execution.mjs': caseHelpers,
    '@element-plus/icons-vue': { Search: {}, WarningFilled: {}, Refresh: {} },
    'vue-router': { useRoute: () => route, useRouter: () => ({ push: async location => { pushes.push(plain(location)) }, replace: async () => {} }) },
    '@/api/ui_automation': Object.fromEntries(Object.keys(api).map(name => [name, (...args) => {
      if (name === 'runTestCase') posts.push(plain(args))
      return api[name](...args)
    }])),
  })
  t.after(loaded.dispose)
  await flush()
  return { ...loaded, scope, route, record, api, pushes, posts, rows }
}

test('rerun handles an explicit 503 dispatch refusal without entering unknown recovery', async t => {
  const h = await executions(t)
  let lookups = 0
  h.api.runTestCase = async () => { throw Object.assign(offline(), { response: { status: 503, data: { accepted: false, failure_stage: 'dispatch', message: 'Queue unavailable' } } }) }
  h.api.getTestCaseSubmission = async () => { lookups += 1; throw offline() }
  await h.state.showRerunDialog(h.record)
  await h.state.handleRerun()
  assert.equal(h.state.rerunUnknown.value, false)
  assert.equal(readUiPendingRun(uiPendingRunKey('case', 1, 2, 11)), null)
  assert.equal(lookups, 0)
  assert.equal(h.pushes.length, 0)
  assert.ok(h.messages.some(row => row.type === 'error' && String(row.text).includes('Queue unavailable')))
})

test('execution detail polls an active run and terminal detail survives a stale list row', async t => {
  const h = await executions(t)
  assert.equal(h.state.currentExecution.value.status, 'running')
  assert.equal(h.timers.length, 1)
  h.record.status = 'passed'
  await h.timers[0]()
  assert.equal(h.state.currentExecution.value.status, 'passed')
  h.record.status = 'running'
  await h.state.loadExecutions()
  await h.state.loadExecutionDetail(true)
  assert.equal(h.state.executions.value[0].status, 'passed')
  assert.equal(h.state.currentExecution.value.status, 'passed')
})

test('a detail outage retains the current record and reconnects without rerunning', async t => {
  const h = await executions(t)
  h.api.getTestCaseExecutionDetail = async () => { throw offline() }
  await h.state.loadExecutionDetail(true)
  assert.equal(h.state.currentExecution.value.status, 'running')
  assert.equal(h.state.detailTrackingPaused.value, true)
  h.api.getTestCaseExecutionDetail = async id => ({ data: { ...h.record, id, status: 'failed' } })
  await h.state.loadExecutionDetail(true)
  assert.equal(h.state.currentExecution.value.status, 'failed')
  assert.equal(h.state.detailTrackingPaused.value, false)
  assert.equal(h.posts.length, 0)
})

test('rerun transport ambiguity reuses the stored UUID and recovers the accepted ID', async t => {
  const h = await executions(t)
  h.record.status = 'failed'
  h.api.runTestCase = async () => { throw offline() }
  await h.state.showRerunDialog(h.record)
  await h.state.handleRerun()
  assert.equal(h.state.rerunUnknown.value, true)
  const pending = readUiPendingRun(uiPendingRunKey('case', 1, 2, 11))
  assert.ok(pending.client_request_id)
  assert.equal(pending.rerun_of, 100)
  assert.equal(pending.rerun_kind, 'case')
  h.api.getTestCaseSubmission = async (id, params) => {
    assert.equal(id, 11)
    assert.equal(params.client_request_id, pending.client_request_id)
    return { data: { execution_id: 101 } }
  }
  await h.state.handleRerun()
  assert.equal(h.posts.length, 1)
  assert.deepEqual(h.pushes, [{ name: 'UiExecutions', query: { project: '2', executionId: '101' } }])
})

test('switching projects releases rerun UI while a late acknowledgement only enriches its old identity', async t => {
  const h = await executions(t)
  const accepted = deferred()
  h.api.runTestCase = () => accepted.promise
  await h.state.showRerunDialog(h.record)
  const running = h.state.handleRerun()
  assert.equal(h.state.rerunning.value, true)
  h.scope.projectId.value = 3
  h.route.query.project = '3'
  await flush()
  assert.equal(h.state.rerunning.value, false)
  accepted.resolve({ data: { accepted: true, execution_id: 101 } })
  await running
  assert.equal(h.pushes.length, 0)
  assert.equal(readUiPendingRun(uiPendingRunKey('case', 1, 2, 11)).execution_id, 101)
  assert.equal(readUiPendingRun(uiPendingRunKey('case', 1, 3, 11)), null)
})

test('a late cancellation in the old project cannot clear cancellation state for the new execution', async t => {
  const h = await executions(t)
  const first = deferred()
  const second = deferred()
  h.api.abortTestCaseExecution = id => id === 100 ? first.promise : second.promise
  const cancellingFirst = h.state.cancelExecution()
  h.scope.projectId.value = 3
  h.route.query.project = '3'
  h.route.query.executionId = '200'
  h.record.project = 3
  await flush()
  assert.equal(h.state.cancelling.value, false)
  const cancellingSecond = h.state.cancelExecution()
  assert.equal(h.state.cancelling.value, true)
  first.resolve({ data: { status: 'aborted' } })
  await cancellingFirst
  assert.equal(h.state.cancelling.value, true)
  h.record.status = 'aborted'
  second.resolve({ data: { status: 'aborted' } })
  await cancellingSecond
  assert.equal(h.state.cancelling.value, false)
  assert.equal(h.state.currentExecution.value.id, 200)
  assert.equal(h.state.currentExecution.value.status, 'aborted')
})

test('failed execution links retain its case, first failing step and source ID for repair', async t => {
  const h = await executions(t)
  const failed = { ...h.record, status: 'failed', execution_logs: JSON.stringify({ steps: [{ step_number: 1, status: 'passed' }, { step_number: 3, status: 'failed', error: 'Missing button' }] }) }
  assert.deepEqual(plain(h.state.repairLocation(failed)), { name: 'UiTestCases', query: { project: '2', caseId: '11', stepNumber: '3', fromExecutionId: '100', fromKind: 'case' } })
})
