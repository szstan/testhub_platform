import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'

const instances = []
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))
const response = rows => ({ data: { count: rows.length, next: null, results: rows } })
const suite = (id = 10, project = 1, extra = {}) => ({
  id, project, name: `Suite ${id}`, environment: null,
  suite_requests: [{ id: 1, order: 0, enabled: true, request: { id: 11, name: 'Login', method: 'POST', url: '/login' }, assertions: [{ type: 'status_code', value: 200 }] }],
  suite_cases: [], ...extra
})

async function setup(overrides = {}, initial = suite()) {
  const calls = []
  const current = { suite: initial }
  const api = {
    get: async (url, config) => {
      calls.push({ method: 'get', url, config: plain(config || {}) })
      if (overrides.get) return overrides.get(url, config)
      if (/test-suites\/\d+\/$/.test(url)) return { data: plain(current.suite) }
      if (url.endsWith('/test-suites/')) return response([plain(current.suite)])
      if (url.endsWith('/projects/')) return response([{ id: 1, name: 'HTTP', project_type: 'HTTP' }])
      return response([])
    },
    post: async (url, data, config) => {
      calls.push({ method: 'post', url, data: plain(data || {}), config: plain(config || {}) })
      return overrides.post ? overrides.post(url, data, config) : { data: {} }
    },
    patch: async (url, data) => {
      calls.push({ method: 'patch', url, data: plain(data) })
      return overrides.patch ? overrides.patch(url, data) : { data: {} }
    },
    delete: async url => {
      calls.push({ method: 'delete', url })
      return overrides.delete ? overrides.delete(url) : { data: {} }
    }
  }
  const instance = await loadSfcSetup('src/views/api-testing/AutomationTesting.vue', {
    '@/utils/api': { default: api },
    './components/AssertionEditor.vue': { default: {} }
  })
  instances.push(instance)
  instance.state.projects.value = [{ id: 1, project_type: 'HTTP', name: 'Project 1' }]
  instance.state.selectedProject.value = initial.project
  instance.state.selectedSuite.value = plain(initial)
  instance.state.testSuites.value = [plain(initial)]
  return { ...instance, calls, current }
}

test('legacy request suites and new case relations share stable keys and deterministic ordering', async () => {
  const { state } = await setup({}, suite(10, 1, {
    suite_cases: [{ id: 1, order: 0, enabled: true, test_case: { id: 91, name: 'Negative login', step_count: 2 } }]
  }))
  assert.deepEqual(plain(state.suiteItems.value.map(item => [item.key, item.kind, item.name])), [
    ['request:1', 'request', 'Login'], ['case:1', 'case', 'Negative login']
  ])
  state.selectedSuite.value.suite_requests[0].order = 4
  assert.equal(state.suiteItems.value[0].kind, 'case')
})

test('legacy suite assertions normalize value without changing the original before save', async () => {
  const { state, calls } = await setup()
  state.editAssertions(state.suiteItems.value[0])
  assert.equal(state.showAssertionsDialog.value, true)
  assert.deepEqual(plain(state.assertionDraft.value), [{ type: 'status_code', expected: 200 }])
  state.assertionDraft.value[0].expected = 401
  assert.equal(state.selectedSuite.value.suite_requests[0].assertions[0].value, 200)
  await state.saveAssertions()
  assert.deepEqual(calls.find(call => call.method === 'patch'), {
    method: 'patch', url: '/api-testing/test-suite-requests/1/', data: { assertions: [{ type: 'status_code', expected: 401 }] }
  })
  assert.equal(state.showAssertionsDialog.value, false)
})

test('assertion save failure retains the editable draft and prevents duplicate submissions', async () => {
  const pending = deferred()
  const { state, calls } = await setup({ patch: () => pending.promise })
  state.editAssertions(state.suiteItems.value[0])
  state.assertionDraft.value = [{ type: 'json_path', json_path: '$.ok', expected: 'false' }]
  const saving = state.saveAssertions()
  await state.saveAssertions()
  assert.equal(calls.filter(call => call.method === 'patch').length, 1)
  pending.reject({ response: { data: { assertions: ['Invalid expected value'] } } })
  await saving
  assert.equal(state.showAssertionsDialog.value, true)
  assert.equal(state.savingAssertions.value, false)
  assert.match(state.dialogError.value, /Invalid expected value/)
  assert.equal(state.assertionDraft.value[0].expected, 'false')
})

test('closing context while saving assertions cannot overwrite the newly selected suite', async () => {
  const pending = deferred()
  const { state, calls } = await setup({ patch: () => pending.promise })
  state.editAssertions(state.suiteItems.value[0])
  const saving = state.saveAssertions()
  state.selectedProject.value = 2
  await state.onProjectChange()
  state.selectedSuite.value = suite(20, 2)
  pending.resolve({ data: {} })
  await saving
  assert.equal(state.selectedSuite.value.id, 20)
  assert.equal(calls.filter(call => call.method === 'get' && call.url === '/api-testing/test-suites/20/').length, 0)
})

test('adding cases uses case ids and the new suite endpoint once', async () => {
  const pending = deferred()
  const { state, calls } = await setup({ post: () => pending.promise })
  state.showAddDialog.value = true
  state.selectedKind.value = 'case'
  state.selectedCaseIds.value = [8, 9, 8]
  const adding = state.addSelectedItems()
  await state.addSelectedItems()
  assert.deepEqual(calls.filter(call => call.method === 'post'), [{ method: 'post', url: '/api-testing/test-suites/10/add-cases/', data: { case_ids: [8, 9] }, config: {} }])
  pending.resolve({ data: {} })
  await adding
  assert.equal(state.showAddDialog.value, false)
})

test('request additions still use the existing request contract and reject empty selections', async () => {
  const { state, calls } = await setup()
  state.selectedKind.value = 'request'
  await state.addSelectedItems()
  assert.equal(calls.length, 0)
  assert.match(state.dialogError.value, /selectAtLeastOneItem/)
  state.selectedRequestIds.value = [5]
  await state.addSelectedItems()
  assert.equal(calls.find(call => call.method === 'post').url, '/api-testing/test-suites/10/add-requests/')
  assert.deepEqual(calls.find(call => call.method === 'post').data, { request_ids: [5] })
})

test('mixed reordering sends request and case ids from the persisted backend contract', async () => {
  const initial = suite(10, 1, { suite_cases: [{ id: 2, order: 1, enabled: true, test_case: { id: 99, name: 'Case' } }] })
  let context
  context = await setup({ post: async (_url, data) => {
    assert.deepEqual(plain(data.items), [{ kind: 'case', id: 99 }, { kind: 'request', id: 11 }])
    context.current.suite.suite_cases[0].order = 0
    context.current.suite.suite_requests[0].order = 1
    return { data: {} }
  } }, initial)
  await context.state.moveItem(1, -1)
  assert.deepEqual(plain(context.state.suiteItems.value.map(item => item.key)), ['case:2', 'request:1'])
  assert.equal(context.state.mutating.value, false)
})

test('copying a mixed suite delegates one localized, length-limited request to the atomic endpoint', async () => {
  const initial = suite(10, 1, {
    name: 'x'.repeat(198),
    suite_requests: [{ id: 1, order: 2, enabled: true, request: { id: 11 }, assertions: [] }],
    suite_cases: [{ id: 2, order: 0, enabled: false, test_case: { id: 99 } }]
  })
  const { state, calls } = await setup({}, initial)
  await state.duplicateSuite(state.selectedSuite.value)
  const posts = calls.filter(call => call.method === 'post')
  assert.equal(posts.length, 1)
  assert.equal(posts[0].url, '/api-testing/test-suites/10/duplicate/')
  assert.deepEqual(posts[0].data, { name: `${initial.name} - apiTesting.common.copy`.slice(0, 200) })
  assert.equal(posts[0].data.name.length, 200)
  assert.equal(calls.some(call => call.method === 'get' && /test-suites\/\d+\/$/.test(call.url)), false)
})

test('suite copy failure reports the server error without issuing follow-up mutations', async () => {
  const { state, calls } = await setup({ post: async () => {
    throw { response: { data: { detail: 'source members are invalid' } } }
  } })
  await state.duplicateSuite(state.selectedSuite.value)
  assert.equal(calls.filter(call => call.method === 'post').length, 1)
  assert.match(state.mutationError.value, /source members are invalid/)
  assert.equal(state.mutating.value, false)
  assert.equal(calls.some(call => call.method === 'get' && call.url.endsWith('/test-suites/')), false)
})

test('failed reordering preserves server order and exposes an actionable error', async () => {
  const initial = suite(10, 1, { suite_cases: [{ id: 2, order: 1, enabled: true, test_case: { id: 99, name: 'Case' } }] })
  const { state } = await setup({ post: async () => { throw new Error('concurrent membership changed') } }, initial)
  await state.moveItem(1, -1)
  assert.deepEqual(plain(state.suiteItems.value.map(item => item.key)), ['request:1', 'case:2'])
  assert.match(state.mutationError.value, /membership changed/)
})

test('case enable and remove actions target their own relation without touching requests', async () => {
  const initial = suite(10, 1, { suite_cases: [{ id: 2, order: 1, enabled: true, test_case: { id: 99, name: 'Case' } }] })
  const { state, calls } = await setup({}, initial)
  await state.updateMemberEnabled(state.suiteItems.value[1], false)
  await state.removeMember(state.suiteItems.value[1])
  assert.deepEqual(calls.filter(call => call.method === 'patch' || call.method === 'delete'), [
    { method: 'patch', url: '/api-testing/test-suite-cases/2/', data: { enabled: false } },
    { method: 'delete', url: '/api-testing/test-suite-cases/2/' }
  ])
})

test('suite and selector loading reads later pages instead of silently truncating choices', async () => {
  const pages = []
  const { state } = await setup({ get: async (url, config) => {
    const page = config.params.page
    pages.push({ url, page, project: config.params.project })
    if (url.endsWith('/test-suites/')) return { data: { count: 2, results: [suite(page * 10)] } }
    if (url.endsWith('/test-cases/')) return { data: { count: 2, results: [{ id: page, name: `Case ${page}`, status: 'active' }] } }
    return response([{ id: 5, name: 'Orphan request', collection: null }, { id: 6, name: 'HTTP request', collection: 2, request_type: 'HTTP' }])
  } })
  await state.loadTestSuites()
  assert.equal(state.testSuites.value.length, 2)
  await state.showAddItems()
  assert.deepEqual(plain(state.availableCases.value.map(item => item.id)), [1, 2])
  assert.deepEqual(plain(state.availableRequests.value.map(item => item.id)), [6])
  assert.ok(pages.every(call => call.project === 1))
})

test('late suite detail and history responses cannot overwrite a new selection', async () => {
  const oldDetail = deferred()
  const oldHistory = deferred()
  const { state } = await setup({ get: (url, config) => {
    if (url === '/api-testing/test-suites/10/') return oldDetail.promise
    if (url === '/api-testing/test-suites/20/') return Promise.resolve({ data: suite(20) })
    if (config?.params?.test_suite === 10) return oldHistory.promise
    return Promise.resolve(response([{ id: 200, status: 'COMPLETED' }]))
  } })
  const oldLoading = Promise.all([state.reloadCurrentSuite(), state.loadExecutions()])
  await state.selectSuite(suite(20))
  oldDetail.resolve({ data: suite(10) })
  oldHistory.resolve(response([{ id: 100 }]))
  await oldLoading
  assert.equal(state.selectedSuite.value.id, 20)
  assert.equal(state.executions.value[0].id, 200)
  assert.equal(state.detailLoading.value, false)
  assert.equal(state.executionsLoading.value, false)
})

test('failed suite execution remains visible while historical executions refresh', async () => {
  const { state } = await setup({ post: async () => { throw new Error('gateway response lost') } })
  await state.runTestSuite(state.selectedSuite.value)
  assert.match(state.executionError.value, /gateway response lost/)
  assert.equal(state.showExecutionDialog.value, false)
  assert.equal(state.running.value, false)
})

test('running suites cannot be submitted twice and failed assertions do not show success', async () => {
  const pending = deferred()
  const { state, calls, messages } = await setup({ post: () => pending.promise })
  const running = state.runTestSuite(state.selectedSuite.value)
  await state.runTestSuite(state.selectedSuite.value)
  assert.equal(calls.filter(call => call.method === 'post').length, 1)
  pending.resolve({ data: { id: 12, status: 'FAILED', failed_requests: 1, results: [{ name: 'Login', passed: false }] } })
  await running
  assert.equal(state.currentExecution.value.status, 'FAILED')
  assert.equal(state.showExecutionDialog.value, true)
  assert.equal(messages.some(message => message.type === 'success'), false)
})

test('unmounting invalidates pending detail responses', async () => {
  const pending = deferred()
  const instance = await setup({ get: () => pending.promise })
  const loading = instance.state.reloadCurrentSuite()
  instance.dispose()
  pending.resolve({ data: suite(99) })
  await loading
  assert.equal(instance.state.selectedSuite.value.id, 10)
})

test('environment loading failure does not block projects and suites', async () => {
  const { state } = await setup({
    get: async url => {
      if (url.endsWith('/api-testing/environments/')) throw new Error('environment service unavailable')
      if (url.endsWith('/api-testing/projects/')) {
        return response([{ id: 1, name: 'HTTP project', project_type: 'HTTP' }])
      }
      if (url.endsWith('/api-testing/test-suites/')) return response([suite(31, 1)])
      return response([])
    }
  })
  state.projects.value = []
  state.selectedProject.value = null
  state.testSuites.value = []

  await state.loadProjects()

  assert.deepEqual(plain(state.projects.value.map(project => project.id)), [1])
  assert.deepEqual(plain(state.testSuites.value.map(item => item.id)), [31])
  assert.deepEqual(plain(state.environments.value), [])
  assert.match(state.pageError.value, /environment service unavailable/)
})
