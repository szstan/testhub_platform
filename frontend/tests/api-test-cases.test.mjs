import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { afterEach, test } from 'node:test'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import { build } from 'esbuild'
import { nextTick, reactive } from 'vue'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'
import * as pagination from '../src/utils/api-test-case-pagination.mjs'
import * as pendingIdentity from '../src/utils/api-case-pending-identity.mjs'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const instances = []
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

function paged(items, page, pageSize = 1) {
  const start = (page - 1) * pageSize
  const results = items.slice(start, start + pageSize)
  return { data: { count: items.length, next: start + pageSize < items.length ? `?page=${page + 1}` : null, results } }
}

async function loadApiService(request) {
  const source = await readFile(resolve(frontend, 'src/api/api-test-cases.js'), 'utf8')
  const result = await build({
    stdin: { contents: source, resolveDir: resolve(frontend, 'src/api'), sourcefile: 'api-test-cases.js' },
    bundle: true,
    write: false,
    platform: 'node',
    format: 'cjs',
    plugins: [{
      name: 'api-request-mock',
      setup(builder) {
        builder.onResolve({ filter: /^@\/utils\/api$/ }, () => ({ path: 'api-request-mock', namespace: 'api-test' }))
        builder.onLoad({ filter: /.*/, namespace: 'api-test' }, () => ({ contents: 'export default __apiRequest', loader: 'js' }))
      }
    }]
  })
  const module = { exports: {} }
  runInNewContext(result.outputFiles[0].text, { module, exports: module.exports, __apiRequest: request })
  return module.exports
}

const defaultProjects = [{ id: 7, name: 'HTTP project', project_type: 'HTTP' }]
const defaultRequests = [{ id: 31, collection: 4, name: 'Login', method: 'POST', url: '/login', request_type: 'HTTP' }]

function executionFingerprint(id, revision = 0) {
  return `${Number(id).toString(16).padStart(56, '0')}${Number(revision).toString(16).padStart(8, '0')}`
}

function defaultApiGet(url) {
  if (url === '/api-testing/projects/') return { data: defaultProjects }
  if (url === '/api-testing/requests/') return { data: defaultRequests }
  if (url === '/api-testing/environments/') return { data: [{ id: 5, name: 'QA', scope: 'LOCAL', project: 7 }] }
  if (url === '/testcases/') return { data: [] }
  if (url === '/api-testing/requests/77/') return { data: { id: 77, name: 'Saved Login', request_type: 'HTTP', collection: 41, headers: { Authorization: 'secret-token' }, body: { type: 'json', data: { password: 'secret' } } } }
  if (url === '/api-testing/collections/41/') return { data: { id: 41, project: 7 } }
  throw new Error(`Unexpected GET ${url}`)
}

function serviceDefaults(overrides = {}) {
  return {
    listApiTestCases: async () => ({ data: { count: 0, results: [] } }),
    getApiTestCase: async () => ({ data: null }),
    createApiTestCase: async () => ({ data: { id: 201 } }),
    updateApiTestCase: async (_id, data) => ({ data: { id: 201, ...plain(data) } }),
    deleteApiTestCase: async () => ({ data: {} }),
    duplicateApiTestCase: async () => ({ data: { id: 202 } }),
    executeApiTestCase: async () => ({ data: { id: 301, status: 'PENDING' } }),
    getApiCaseExecution: async () => ({ data: { id: 301, status: 'COMPLETED' } }),
    getApiTestCaseExecutions: async () => ({ data: { count: 0, results: [] } }),
    ...overrides
  }
}

async function setupList({ get = defaultApiGet, caseApi = {}, routeQuery = {}, actorId = 1, storage, memory = new Map() } = {}) {
  const calls = []
  const api = { get: async (url, config = {}) => get(url, config) }
  const route = { params: {}, query: routeQuery }
  const navigation = []
  const userStore = { user: actorId == null ? null : { id: actorId } }
  const sessionStorage = storage || createSessionStorage()
  const pendingStore = pendingIdentity.createApiCasePendingIdentityStore({ storage: () => sessionStorage, memory })
  const instance = await loadSfcSetup('src/views/api-testing/ApiTestCaseList.vue', {
    '@/utils/api': { default: api },
    '@/api/api-test-cases': serviceDefaults({
      listApiTestCases: async params => { calls.push({ method: 'list', params: plain(params) }); return { data: { count: 0, results: [] } } },
      getApiTestCase: async id => ({ data: { id, project: 7, execution_fingerprint: executionFingerprint(id) } }),
      ...caseApi
    }),
    '@/utils/api-test-case-pagination.mjs': pagination,
    '@/utils/api-case-pending-identity.mjs': {
      apiCaseExecutionFingerprint: pendingIdentity.apiCaseExecutionFingerprint,
      createApiCasePendingIdentityStore: () => pendingStore
    },
    '@/stores/user': { useUserStore: () => userStore },
    './components/ApiCaseRunPanel.vue': { default: {} },
    './components/ApiCaseExecutionHistory.vue': { default: {} },
    'vue-router': {
      useRoute: () => route,
      useRouter: () => ({ push: location => navigation.push(location), replace: location => navigation.push(location) })
    }
  })
  instances.push(instance)
  return { ...instance, calls, navigation, route, userStore, sessionStorage, pendingStore }
}

function createSessionStorage() {
  const values = new Map()
  return {
    getItem(key) { return values.has(key) ? values.get(key) : null },
    setItem(key, value) { values.set(key, String(value)) },
    removeItem(key) { values.delete(key) },
    values
  }
}

async function setupEditor({ get = defaultApiGet, caseApi = {}, routeParams = {}, routeQuery = {}, confirm } = {}) {
  const calls = []
  const api = { get: async (url, config = {}) => { calls.push({ method: 'get', url, params: plain(config.params || {}) }); return get(url, config) } }
  const route = { params: routeParams, query: routeQuery }
  const navigation = []
  const methods = serviceDefaults({
    getApiTestCase: async () => ({ data: null }),
    createApiTestCase: async payload => { calls.push({ method: 'create', data: plain(payload) }); return { data: { id: 201, ...plain(payload) } } },
    updateApiTestCase: async (id, payload) => { calls.push({ method: 'update', id, data: plain(payload) }); return { data: { id, ...plain(payload) } } },
    executeApiTestCase: async (id, payload) => { calls.push({ method: 'execute', id, data: plain(payload) }); return { data: { id: 301, status: 'PENDING' } } },
    ...caseApi
  })
  const instance = await loadSfcSetup('src/views/api-testing/ApiTestCaseEditor.vue', {
    '@/utils/api': { default: api },
    '@/api/api-test-cases': methods,
    '@/utils/api-test-case-pagination.mjs': pagination,
    './components/AssertionEditor.vue': { default: {} },
    './components/ApiCaseRunPanel.vue': { default: {} },
    './components/ApiCaseExecutionHistory.vue': { default: {} },
    'vue-router': {
      useRoute: () => route,
      useRouter: () => ({ push: location => navigation.push(location), replace: location => navigation.push(location) })
    }
  }, { confirm })
  instances.push(instance)
  return { ...instance, calls, navigation, route, methods }
}

test('API service uses the frozen /test-cases routes and asynchronous execution resource', async () => {
  const calls = []
  const service = await loadApiService(async config => { calls.push(plain(config)); return { data: { id: 9 } } })
  const payload = { project: 7, steps: [{ request: 31, order: 0, assertions: null }] }
  await service.listApiTestCases({ project: 7, status: 'active', priority: 'high', search: 'login', page: 2, page_size: 20 })
  await service.getApiTestCase(9)
  await service.createApiTestCase(payload)
  await service.updateApiTestCase(9, payload)
  await service.deleteApiTestCase(9)
  await service.duplicateApiTestCase(9)
  await service.executeApiTestCase(9, { variables: { token: 'provided' }, idempotency_key: 'run-1' })
  await service.getApiCaseExecution(88)
  await service.getApiTestCaseExecutions(9, { page: 2, page_size: 10 })

  assert.deepEqual(calls, [
    { url: '/api-testing/test-cases/', method: 'get', params: { project: 7, status: 'active', priority: 'high', search: 'login', page: 2, page_size: 20 } },
    { url: '/api-testing/test-cases/9/', method: 'get' },
    { url: '/api-testing/test-cases/', method: 'post', data: payload },
    { url: '/api-testing/test-cases/9/', method: 'patch', data: payload },
    { url: '/api-testing/test-cases/9/', method: 'delete' },
    { url: '/api-testing/test-cases/9/duplicate/', method: 'post' },
    { url: '/api-testing/test-cases/9/execute/', method: 'post', data: { variables: { token: 'provided' }, idempotency_key: 'run-1' } },
    { url: '/api-testing/case-executions/88/', method: 'get' },
    { url: '/api-testing/test-cases/9/executions/', method: 'get', params: { page: 2, page_size: 10 } }
  ])
})

test('case list applies server filters, resets pagination, and preserves rows on failure', async () => {
  const requests = []
  let fail = false
  const instance = await setupList({
    caseApi: { listApiTestCases: async params => {
      requests.push(plain(params))
      if (fail) throw new Error('list unavailable')
      return { data: { count: 1, results: [{ id: 12, name: 'Login', project: 7, priority: 'high', status: 'active' }] } }
    } }
  })
  await instance.mount()
  assert.equal(instance.state.rows.value[0].id, 12)
  instance.state.page.value = 4
  instance.state.filters.priority = 'high'
  instance.state.filters.status = 'active'
  instance.state.filters.search = 'login'
  await instance.state.applyFilters()
  assert.deepEqual(requests.at(-1), { page: 1, page_size: 20, project: 7, priority: 'high', status: 'active', search: 'login' })
  fail = true
  await instance.state.loadCases()
  assert.equal(instance.state.rows.value[0].id, 12)
  assert.match(instance.state.pageError.value, /list unavailable/)
})

test('case list prevents duplicate execution submissions and uses the asynchronous record response', async () => {
  const pending = deferred()
  const started = deferred()
  let executions = 0
  const instance = await setupList({ caseApi: {
    listApiTestCases: async () => ({ data: { count: 1, results: [{ id: 12, name: 'Login', project: 7, environment: 5 }] } }),
    executeApiTestCase: async (id, payload) => { executions += 1; started.resolve(); instance.calls.push({ method: 'execute', id, payload: plain(payload) }); return pending.promise }
  } })
  await instance.mount()
  const row = instance.state.rows.value[0]
  const running = instance.state.runCase(row)
  await started.promise
  await instance.state.runCase(row)
  assert.equal(executions, 1)
  pending.resolve({ data: { id: 301, status: 'PENDING' } })
  await running
  assert.equal(instance.state.currentExecution.value.status, 'PENDING')
  assert.equal(instance.state.showRunPanel.value, true)
  assert.equal(instance.calls.find(call => call.method === 'execute').payload.idempotency_key.length > 0, true)
})

test('editor prefill validates saved request collection and stores only the request reference', async () => {
  const instance = await setupEditor({ routeQuery: { request_id: '77', project: '7' } })
  await instance.mount()
  assert.equal(instance.state.steps.value.length, 1)
  assert.equal(instance.state.steps.value[0].request, 77)
  assert.equal(instance.state.steps.value[0].name, 'Saved Login')
  assert.deepEqual(plain(instance.state.requests.value.find(item => item.id === 77)), {
    id: 77, collection: 41, name: 'Saved Login'
  })
  instance.state.form.name = 'New login flow'
  const payload = instance.state.buildPayload()
  assert.deepEqual(plain(payload.steps), [{
    request: 77, name: 'Saved Login', order: 0, enabled: true,
    request_overrides: {}, assertions: null, extractors: []
  }])
  assert.equal(JSON.stringify(plain(payload)).includes('secret-token'), false)
  assert.equal(JSON.stringify(plain(payload)).includes('password'), false)
})

test('editor paginates project options and validates atomic step payloads including legacy assertions', async () => {
  const projects = [{ id: 7, name: 'HTTP A', project_type: 'HTTP' }, { id: 8, name: 'HTTP B', project_type: 'HTTP' }]
  const pages = []
  const get = async (url, config = {}) => {
    if (url === '/api-testing/projects/') { pages.push(['projects', config.params.page]); return paged(projects, config.params.page) }
    if (url === '/api-testing/requests/') { pages.push([`requests-${config.params.project}`, config.params.page]); return paged([{ ...defaultRequests[0], id: config.params.project * 10 }, { ...defaultRequests[0], id: config.params.project * 10 + 1 }], config.params.page) }
    if (url === '/api-testing/environments/') { pages.push(['environments', config.params.page]); return paged([{ id: 5, name: 'A', scope: 'LOCAL', project: 7 }, { id: 6, name: 'B', scope: 'LOCAL', project: 8 }], config.params.page) }
    if (url === '/testcases/') return { data: { count: 0, next: null, results: [] } }
    throw new Error(`Unexpected ${url}`)
  }
  const initial = {
    id: 55, project: 7, name: 'Existing', description: 'desc', preconditions: 'pre', expected_result: 'ok',
    priority: 'high', status: 'active', tags: ['smoke'], variables: { token: '{{token}}' }, environment: 5,
    stop_on_failure: false, linked_testcase: null,
    steps: [{
      request: 70, request_name: 'Legacy request', name: 'First step', order: 6, enabled: false,
      request_overrides: { params: { page: '2' }, headers: { Accept: 'application/json' }, body: { type: 'json', data: { ok: true } } },
      assertions: [{ type: 'status_code', value: 401 }],
      extractors: [{ name: 'session', source: 'json_path', expression: '$.token', required: true }]
    }]
  }
  const instance = await setupEditor({ get, routeParams: { id: '55' }, caseApi: { getApiTestCase: async () => ({ data: initial }) } })
  await instance.mount()
  assert.equal(pages.filter(item => item[0] === 'projects').length, 2)
  assert.equal(instance.state.requests.value.length, 2)
  assert.deepEqual(plain(instance.state.environments.value.map(item => item.id)), [5])
  const step = instance.state.steps.value[0]
  assert.equal(step.assertions[0].expected, 401)
  assert.equal('value' in step.assertions[0], false)
  step.overrideText.params = '{"page":"3"}'
  const secondStep = instance.state.makeStep({ request: 71, name: 'Second step', assertions: null })
  instance.state.steps.value.push(secondStep)
  instance.state.moveStep(0, 1)
  const payload = instance.state.buildPayload()
  assert.deepEqual(plain(payload.steps.map(item => [item.request, item.order])), [[71, 0], [70, 1]])
  assert.deepEqual(plain(payload.steps[1]), {
    request: 70, name: 'First step', order: 1, enabled: false,
    request_overrides: { params: { page: '3' }, headers: { Accept: 'application/json' }, body: { type: 'json', data: { ok: true } } },
    assertions: [{ type: 'status_code', expected: 401 }],
    extractors: [{ name: 'session', source: 'json_path', expression: '$.token', required: true }]
  })
  assert.equal(payload.linked_testcase, null)
  assert.equal(pages.some(item => item[0] === 'requests-7'), true)
})

test('editor round trips explicit empty bodies while absent and cleared overrides inherit the saved request', async () => {
  for (const overrides of [{}, { body: {} }, { body: null }, { body: [] }, { body: { type: 'none', data: null } }]) {
    const initial = {
      id: 55, project: 7, name: 'Existing case', tags: [], variables: {},
      steps: [{ request: 31, name: 'Login', request_overrides: overrides, assertions: null, extractors: [] }]
    }
    const instance = await setupEditor({ routeParams: { id: '55' }, caseApi: { getApiTestCase: async () => ({ data: initial }) } })
    await instance.mount()
    instance.state.form.description = 'An unrelated edit'
    await instance.state.saveCase()
    const saved = instance.calls.find(call => call.method === 'update')
    assert.ok(saved)
    assert.deepEqual(saved.data.steps[0].request_overrides, overrides)
    instance.state.steps.value[0].overrideText.body = '   '
    assert.deepEqual(plain(instance.state.buildPayload().steps[0].request_overrides), {})
  }
})

test('editor JSON validation points to the step and project changes confirm before discarding references', async () => {
  const instance = await setupEditor({ confirm: async () => { throw new Error('cancel') } })
  await instance.mount()
  instance.state.form.name = 'Flow'
  const step = instance.state.makeStep({ request: 31, name: 'Login', assertions: null })
  step.overrideText.params = '{invalid'
  instance.state.steps.value.push(step)
  assert.throws(() => instance.state.buildPayload(), /步骤 1/)
  assert.match(instance.state.stepErrors.value[0], /JSON/)
  await instance.state.onProjectChange(8)
  assert.equal(instance.state.form.project, 7)
  assert.equal(instance.state.steps.value.length, 1)
})

test('confirmed project switch drops old steps and loads only the new project options', async () => {
  const get = async (url, config = {}) => {
    if (url === '/api-testing/requests/') return { data: [{ id: Number(config.params.project) * 10, collection: 4, name: `Request ${config.params.project}`, method: 'GET', request_type: 'HTTP' }] }
    if (url === '/api-testing/environments/') return { data: [{ id: 8, name: 'Env 8', scope: 'LOCAL', project: 8 }] }
    return defaultApiGet(url, config)
  }
  const instance = await setupEditor({ get })
  await instance.mount()
  instance.state.steps.value.push(instance.state.makeStep({ request: 31, name: 'Old', assertions: null }))
  await instance.state.onProjectChange(8)
  assert.equal(instance.state.form.project, 8)
  assert.equal(instance.state.steps.value.length, 0)
  assert.deepEqual(plain(instance.state.requests.value.map(item => item.id)), [80])
  assert.deepEqual(plain(instance.state.environments.value.map(item => item.id)), [8])
})

test('late request-option pages from a previous project cannot overwrite the active project', async () => {
  const oldPage = deferred()
  const get = async (url, config = {}) => {
    if (url === '/api-testing/requests/' && config.params.project === 8) return oldPage.promise
    if (url === '/api-testing/requests/' && config.params.project === 9) return { data: [{ id: 90, collection: 9, name: 'Current', method: 'GET', request_type: 'HTTP' }] }
    if (url === '/api-testing/environments/') return { data: [] }
    return defaultApiGet(url, config)
  }
  const instance = await setupEditor({ get })
  await instance.mount()
  instance.state.form.project = 8
  const oldLoad = instance.state.loadProjectOptions(8)
  instance.state.form.project = 9
  await instance.state.loadProjectOptions(9)
  oldPage.resolve({ data: [{ id: 80, collection: 8, name: 'Old', method: 'GET', request_type: 'HTTP' }] })
  await oldLoad
  assert.deepEqual(plain(instance.state.requests.value.map(item => item.id)), [90])
})

test('failed editor save preserves draft and suppresses duplicate POSTs', async () => {
  const pending = deferred()
  let submissions = 0
  const instance = await setupEditor({ caseApi: { createApiTestCase: async () => { submissions += 1; return pending.promise } } })
  await instance.mount()
  instance.state.form.name = 'Draft that must survive'
  instance.state.form.description = 'keep me'
  const saving = instance.state.saveCase()
  await instance.state.saveCase()
  assert.equal(submissions, 1)
  pending.reject(new Error('validation failed'))
  await saving
  assert.equal(instance.state.form.name, 'Draft that must survive')
  assert.equal(instance.state.form.description, 'keep me')
  assert.match(instance.state.pageError.value, /validation failed/)
  assert.equal(instance.state.saving.value, false)
})

test('editor creates and updates persisted case payloads without losing explicit links', async () => {
  let createdPayload = null
  const creating = await setupEditor({ caseApi: {
    createApiTestCase: async payload => { createdPayload = plain(payload); return { data: { id: 501, ...plain(payload) } } }
  } })
  await creating.mount()
  creating.state.form.name = 'Login flow'
  creating.state.form.tags = ['smoke', 'auth']
  creating.state.variablesText.value = '{"baseUrl":"https://example.test"}'
  creating.state.steps.value.push(creating.state.makeStep({ request: 31, name: 'Sign in', assertions: null }))
  await creating.state.saveCase()
  assert.deepEqual(createdPayload.steps, [{
    request: 31, name: 'Sign in', order: 0, enabled: true, request_overrides: {}, assertions: null, extractors: []
  }])
  assert.deepEqual(createdPayload.tags, ['smoke', 'auth'])
  assert.equal(createdPayload.linked_testcase, null)
  assert.deepEqual(creating.navigation, ['/api-testing/test-cases'])

  let updatedCall = null
  const existing = { id: 61, project: 7, name: 'Original', description: '', preconditions: '', expected_result: '', priority: 'medium', status: 'draft', tags: [], variables: {}, environment: null, stop_on_failure: true, linked_testcase: null, steps: [] }
  const editing = await setupEditor({ routeParams: { id: '61' }, caseApi: {
    getApiTestCase: async () => ({ data: existing }),
    updateApiTestCase: async (id, data) => { updatedCall = { id, data: plain(data) }; return { data: { id, ...plain(data) } } }
  } })
  await editing.mount()
  editing.state.form.name = 'Renamed'
  await editing.state.saveCase()
  assert.equal(updatedCall.id, 61)
  assert.equal(updatedCall.data.name, 'Renamed')
  assert.deepEqual(updatedCall.data.steps, [])
})

test('editor surfaces option failures instead of treating them as an empty request list', async () => {
  const get = async (url, config = {}) => {
    if (url === '/api-testing/requests/') throw new Error('requests unavailable')
    return defaultApiGet(url, config)
  }
  const instance = await setupEditor({ get })
  await instance.mount()
  assert.equal(instance.state.requests.value.length, 0)
  assert.match(instance.state.optionErrors.requests, /requests unavailable/)
  assert.equal(instance.state.canSave.value, false)
  instance.state.addStep()
  assert.equal(instance.state.steps.value.length, 0)
})

test('editor reuses idempotency key after uncertain execute failure and blocks concurrent submissions', async () => {
  const pending = deferred()
  const submissions = []
  let first = true
  const saved = { id: 55, project: 7, name: 'Saved', variables: { token: 'v' }, environment: 5, status: 'active', priority: 'medium', tags: [], steps: [] }
  const instance = await setupEditor({
    routeParams: { id: '55' },
    caseApi: {
      getApiTestCase: async () => ({ data: saved }),
      executeApiTestCase: async (id, payload) => {
        submissions.push({ id, payload: plain(payload) })
        if (first) { first = false; throw new Error('response lost') }
        return pending.promise
      }
    }
  })
  await instance.mount()
  await instance.state.runCase()
  assert.match(instance.state.pageError.value, /response lost/)
  const run = instance.state.runCase()
  await instance.state.runCase()
  assert.equal(submissions.length, 2)
  assert.equal(submissions[0].payload.idempotency_key, submissions[1].payload.idempotency_key)
  assert.equal(submissions[1].payload.environment_id, 5)
  assert.deepEqual(plain(submissions[1].payload.variables), { token: 'v' })
  pending.resolve({ data: { id: 91, status: 'PENDING' } })
  await run
  assert.equal(instance.state.currentExecution.value.status, 'PENDING')
  assert.equal(instance.state.showRunPanel.value, true)
})

test('saving changed steps starts a new execution attempt after an uncertain run', async () => {
  const submissions = []
  const saved = { id: 55, project: 7, name: 'Saved', variables: {}, steps: [{ request: 31, name: 'First', assertions: null }] }
  const instance = await setupEditor({ routeParams: { id: '55' }, caseApi: {
    getApiTestCase: async () => ({ data: saved }),
    executeApiTestCase: async (_id, payload) => { submissions.push(plain(payload)); throw new Error('response lost') }
  } })
  await instance.mount()
  await instance.state.runCase()
  instance.state.steps.value[0].assertions = [{ type: 'status_code', expected: 401 }]
  assert.equal(instance.state.canRun.value, false)
  await instance.state.saveCase()
  assert.equal(instance.state.canRun.value, true)
  await instance.state.runCase()
  assert.equal(submissions.length, 2)
  assert.notEqual(submissions[0].idempotency_key, submissions[1].idempotency_key)
})

test('edits made while a save is in flight remain dirty and cannot run as saved data', async () => {
  const pending = deferred()
  const instance = await setupEditor({ routeParams: { id: '55' }, caseApi: {
    getApiTestCase: async () => ({ data: { id: 55, project: 7, name: 'Saved', variables: {}, steps: [] } }),
    updateApiTestCase: async () => pending.promise
  } })
  await instance.mount()
  instance.state.form.name = 'Submitted'
  const saving = instance.state.saveCase()
  instance.state.form.name = 'Newer unsaved edit'
  pending.resolve({ data: { id: 55, project: 7, name: 'Submitted', variables: {}, steps: [] } })
  await saving
  assert.equal(instance.state.form.name, 'Newer unsaved edit')
  assert.equal(instance.state.isDirty.value, true)
  assert.equal(instance.state.canRun.value, false)
})

test('optional linked-case lookup failures allow saving null or existing links', async () => {
  for (const linked of [null, 812]) {
    let payload
    const instance = await setupEditor({
      get: (url, config) => { if (url === '/testcases/') throw new Error('generic cases unavailable'); return defaultApiGet(url, config) },
      routeParams: linked == null ? {} : { id: '55' },
      caseApi: {
        getApiTestCase: async () => ({ data: { id: 55, project: 7, name: 'Linked case', linked_testcase: linked, variables: {}, steps: [] } }),
        createApiTestCase: async value => { payload = plain(value); return { data: { id: 55, ...payload } } },
        updateApiTestCase: async (_id, value) => { payload = plain(value); return { data: { id: 55, ...payload } } }
      }
    })
    await instance.mount()
    instance.state.form.name = 'Save without changing the optional link'
    assert.match(instance.state.optionErrors.linkedCases, /generic cases unavailable/)
    assert.equal(instance.state.canSave.value, true)
    await instance.state.saveCase()
    assert.equal(payload.linked_testcase, linked)
  }
})

test('definitive client rejections show direct errors without uncertain-history guidance', async () => {
  for (const status of [400, 401, 403, 404, 409, 429]) {
    for (const kind of ['editor', 'list']) {
      const submissions = []
      const saved = { id: 55, project: 7, name: 'Saved', variables: {}, steps: [] }
      const caseApi = {
        getApiTestCase: async () => ({ data: saved }),
        executeApiTestCase: async (_id, payload) => {
          submissions.push(plain(payload))
          throw Object.assign(new Error('rejected'), { response: { status, data: { detail: `rejection-${status}` } } })
        }
      }
      const listCaseApi = {
        ...caseApi,
        getApiTestCase: async id => ({
          data: { ...saved, id, execution_fingerprint: executionFingerprint(id) }
        })
      }
      const instance = kind === 'editor' ? await setupEditor({ routeParams: { id: '55' }, caseApi }) : await setupList({ caseApi: listCaseApi })
      await instance.mount()
      await instance.state.runCase(saved)
      assert.equal(instance.state.showHistory.value, false)
      assert.equal(instance.state.pageError.value, `rejection-${status}`)
      await instance.state.runCase(saved)
      assert.notEqual(submissions[0].idempotency_key, submissions[1].idempotency_key)
    }
  }
})

test('list retries retain a key only while the fresh execution fingerprint is unchanged', async () => {
  const submissions = []
  const contexts = []
  let revision = 0
  const instance = await setupList({ caseApi: {
    getApiTestCase: async id => {
      const context = { id, project: 7, execution_fingerprint: executionFingerprint(id, revision) }
      contexts.push(context)
      return { data: context }
    },
    executeApiTestCase: async (_id, payload) => { submissions.push(plain(payload)); throw new Error('response lost') }
  } })
  await instance.mount()
  const staleListRow = { id: 55, environment: 5, updated_at: '2026-11-27T12:00:00Z' }
  await instance.state.runCase(staleListRow)
  await instance.state.runCase(staleListRow)
  assert.equal(submissions[0].idempotency_key, submissions[1].idempotency_key)
  revision = 1
  await instance.state.runCase(staleListRow)
  assert.notEqual(submissions[1].idempotency_key, submissions[2].idempotency_key)
  assert.equal(submissions[2].execution_fingerprint, contexts[2].execution_fingerprint)
  assert.equal(contexts.length, 3)
})

test('case list retries an ambiguous case with its original key after another case succeeds', async () => {
  const submissions = []
  let firstA = true
  const instance = await setupList({ caseApi: {
    listApiTestCases: async () => ({ data: { count: 2, results: [] } }),
    executeApiTestCase: async (id, payload) => {
      submissions.push({ id, key: payload.idempotency_key })
      if (id === 41 && firstA) { firstA = false; throw new Error('response lost') }
      return { data: { id: 300 + submissions.length, status: 'PENDING' } }
    }
  } })
  await instance.mount()
  const caseA = { id: 41, project: 7, environment: 5, updated_at: '2026-11-27T12:00:00Z' }
  const caseB = { id: 42, project: 7, environment: 5, updated_at: '2026-11-27T12:00:00Z' }
  await instance.state.runCase(caseA)
  await instance.state.runCase(caseB)
  await instance.state.runCase(caseA)
  assert.deepEqual(submissions.map(item => item.id), [41, 42, 41])
  assert.equal(submissions[0].key, submissions[2].key)
  assert.notEqual(submissions[0].key, submissions[1].key)
})

test('a definitive failure for another case does not clear an ambiguous identity', async () => {
  const submissions = []
  let firstA = true
  const instance = await setupList({ caseApi: {
    executeApiTestCase: async (id, payload) => {
      submissions.push({ id, key: payload.idempotency_key })
      if (id === 51 && firstA) { firstA = false; throw new Error('response lost') }
      if (id === 52) throw Object.assign(new Error('rejected'), { response: { status: 400 } })
      throw new Error('response still lost')
    }
  } })
  await instance.mount()
  const caseA = { id: 51, project: 7, updated_at: '2026-11-27T12:00:00Z' }
  await instance.state.runCase(caseA)
  await instance.state.runCase({ id: 52, project: 7, updated_at: '2026-11-27T12:00:00Z' })
  await instance.state.runCase(caseA)
  assert.equal(submissions[0].key, submissions[2].key)
  assert.notEqual(submissions[0].key, submissions[1].key)
})

test('multiple ambiguous cases retain distinct identities and a known response clears only its own', async () => {
  const submissions = []
  const attempts = new Map()
  const instance = await setupList({ caseApi: {
    executeApiTestCase: async (id, payload) => {
      const attempt = (attempts.get(id) || 0) + 1
      attempts.set(id, attempt)
      submissions.push({ id, key: payload.idempotency_key })
      if (id === 61 && attempt === 2) return { data: { id: 601, status: 'PENDING' } }
      if (id === 61 && attempt === 3) return { data: { id: 603, status: 'PENDING' } }
      throw new Error('response lost')
    }
  } })
  await instance.mount()
  const caseA = { id: 61, project: 7, updated_at: '2026-11-27T12:00:00Z' }
  const caseB = { id: 62, project: 7, updated_at: '2026-11-27T12:00:00Z' }
  await instance.state.runCase(caseA)
  await instance.state.runCase(caseB)
  await instance.state.runCase(caseA)
  await instance.state.runCase(caseB)
  await instance.state.runCase(caseA)
  assert.equal(submissions[0].key, submissions[2].key)
  assert.equal(submissions[1].key, submissions[3].key)
  assert.notEqual(submissions[0].key, submissions[1].key)
  assert.notEqual(submissions[2].key, submissions[4].key)
})

test('pending identities are isolated by actor, project, and fresh server execution fingerprint', async () => {
  const submissions = []
  let project = 7
  let revision = 0
  const instance = await setupList({ caseApi: {
    getApiTestCase: async id => ({ data: {
      id, project, execution_fingerprint: executionFingerprint(id, revision)
    } }),
    executeApiTestCase: async (id, payload) => {
      submissions.push({ id, key: payload.idempotency_key })
      throw new Error('response lost')
    }
  } })
  await instance.mount()
  const original = { id: 71, project: 7, environment: 5, updated_at: '2026-11-27T12:00:00Z' }
  await instance.state.runCase(original)
  instance.userStore.user.id = 2
  await instance.state.runCase(original)
  instance.userStore.user.id = 1
  project = 8
  await instance.state.runCase({ ...original, project: 8 })
  project = 7
  revision = 1
  await instance.state.runCase({ ...original, environment: 6 })
  revision = 2
  await instance.state.runCase(original)
  await instance.state.runCase(original)
  revision = 0
  await instance.state.runCase(original)
  assert.equal(submissions[0].key, submissions[6].key)
  assert.equal(submissions[4].key, submissions[5].key)
  assert.equal(new Set(submissions.slice(0, 5).map(item => item.key)).size, 5)
})

test('list fetches authoritative context after a referenced request or environment edit', async () => {
  const submissions = []
  const reads = []
  const savedRequest = { id: 31, url: '/v1', headers: { Authorization: 'old-secret' }, params: { page: 1 }, body: { value: 'old' }, auth: { token: 'old' } }
  const savedEnvironment = { id: 5, variables: { host: 'old-host' }, default_headers: { 'X-Env': 'old' } }
  let fingerprintRevision = 0
  const instance = await setupList({ caseApi: {
    getApiTestCase: async id => {
      reads.push({ id, request: plain(savedRequest), environment: plain(savedEnvironment) })
      return { data: { id, project: 7, execution_fingerprint: executionFingerprint(id, fingerprintRevision) } }
    },
    executeApiTestCase: async (id, payload) => {
      submissions.push({ id, payload: plain(payload) })
      if (submissions.length === 1) throw new Error('response lost')
      return { data: { id: 390, status: 'PENDING' } }
    }
  } })
  await instance.mount()
  const staleListRow = { id: 39, project: 7, steps: [{ request: 31 }], environment: 5 }
  await instance.state.runCase(staleListRow)
  savedRequest.url = '/v2'
  savedRequest.headers.Authorization = 'new-secret'
  savedRequest.params.page = 2
  savedRequest.body.value = 'new'
  savedRequest.auth.token = 'new'
  savedEnvironment.variables.host = 'new-host'
  savedEnvironment.default_headers['X-Env'] = 'new'
  fingerprintRevision = 1
  await instance.state.runCase(staleListRow)
  assert.equal(reads.length, 2)
  assert.notEqual(submissions[0].payload.idempotency_key, submissions[1].payload.idempotency_key)
  assert.equal(submissions[0].payload.execution_fingerprint, executionFingerprint(39, 0))
  assert.equal(submissions[1].payload.execution_fingerprint, executionFingerprint(39, 1))
  assert.notEqual(reads[0].request.url, reads[1].request.url)
  assert.notEqual(reads[0].environment.variables.host, reads[1].environment.variables.host)
})

test('ambiguous identity survives component refresh and storage contains no case payload', async () => {
  const storage = createSessionStorage()
  const row = {
    id: 81, project: 7, environment: 5, updated_at: '2026-11-27T12:00:00Z',
    variables: { password: 'private-variable' },
    steps: [{ request: 31, request_overrides: { body: { data: { password: 'private-body' } } } }]
  }
  const first = await setupList({ storage, caseApi: { executeApiTestCase: async () => { throw new Error('response lost') } } })
  await first.mount()
  await first.state.runCase(row)
  const storedRecords = JSON.parse([...storage.values.values()][0])
  const firstKey = storedRecords[0].key
  assert.deepEqual(Object.keys(storedRecords[0]).sort(), ['actorId', 'caseId', 'fingerprint', 'key', 'projectId'])
  assert.doesNotMatch(JSON.stringify(storedRecords), /private-variable|private-body|password/)
  first.dispose()

  const second = await setupList({ storage, caseApi: { executeApiTestCase: async (_id, payload) => ({ data: { id: 881, status: 'PENDING', key: payload.idempotency_key } }) } })
  await second.mount()
  await second.state.runCase(row)
  assert.equal(second.state.currentExecution.value.key, firstKey)
})

test('blocked session storage falls back to in-memory retry identity', async () => {
  const submissions = []
  const storage = {
    getItem() { throw new Error('storage blocked') },
    setItem() { throw new Error('storage blocked') }
  }
  const instance = await setupList({ storage, caseApi: {
    executeApiTestCase: async (id, payload) => {
      submissions.push({ id, key: payload.idempotency_key })
      throw new Error('response lost')
    }
  } })
  await instance.mount()
  const row = { id: 91, project: 7, updated_at: '2026-11-27T12:00:00Z' }
  await instance.state.runCase(row)
  await instance.state.runCase(row)
  assert.equal(submissions[0].key, submissions[1].key)
})

test('late settlement for an older key cannot clear a newer pending identity', () => {
  const store = pendingIdentity.createApiCasePendingIdentityStore({ storage: createSessionStorage(), memory: new Map() })
  const identity = { actorId: '1', projectId: '7', caseId: '111', fingerprint: 'revision-a' }
  store.remember(identity, 'older-key')
  assert.equal(store.forget(identity, 'older-key'), true)
  store.remember(identity, 'newer-key')
  assert.equal(store.forget(identity, 'older-key'), false)
  assert.equal(store.get(identity), 'newer-key')
})

test('case list refuses execution without an authenticated actor scope', async () => {
  let executions = 0
  const instance = await setupList({ actorId: null, caseApi: { executeApiTestCase: async () => { executions += 1 } } })
  await instance.mount()
  await instance.state.runCase({ id: 101, project: 7 })
  assert.equal(executions, 0)
  assert.match(instance.state.pageError.value, /Cannot safely track/)
})

test('AssertionEditor emits only an array model update and normalizes legacy status_code.value', async () => {
  const instance = await loadSfcSetup('src/views/api-testing/components/AssertionEditor.vue', {}, {}, {
    modelValue: [{ type: 'status_code', value: 200 }, { type: 'header', header_name: 'Content-Type', expected_value: 'application/json' }]
  })
  instances.push(instance)
  assert.deepEqual(plain(instance.state.assertions.value), [
    { type: 'status_code', expected: 200 },
    { type: 'header', header_name: 'Content-Type', expected: 'application/json' }
  ])
  instance.state.setExpected(0, '401')
  assert.deepEqual(plain(instance.events.at(-1)), ['update:modelValue', [{ type: 'status_code', expected: 401 }, { type: 'header', header_name: 'Content-Type', expected: 'application/json' }]])
  instance.state.addAssertion()
  assert.deepEqual(instance.events.at(-1)[0], 'update:modelValue')
  assert.equal(Array.isArray(instance.events.at(-1)[1]), true)
})

test('JSONPath assertion editing preserves JSON value types and ambiguous strings', async () => {
  const instance = await loadSfcSetup('src/views/api-testing/components/AssertionEditor.vue', {}, {}, {
    modelValue: [{ type: 'json_path', json_path: '$.value', expected: '' }]
  })
  instances.push(instance)
  for (const [input, expected] of [
    ['false', false], ['null', null], ['42', 42], ['{"id":1}', { id: 1 }],
    ['["a"]', ['a']], ['plain text', 'plain text'], ['"false"', 'false']
  ]) {
    instance.state.setExpected(0, input)
    assert.deepEqual(plain(instance.events.at(-1)[1][0].expected), expected)
    assert.equal(instance.events.at(-1)[1][0].comparison, 'json')
  }
  assert.equal(instance.state.formatExpected(instance.state.assertions.value[0]), '"false"')
})

test('execution and history durations convert backend seconds to milliseconds', async () => {
  for (const component of ['ApiCaseRunPanel', 'ApiCaseExecutionHistory']) {
    const instance = await loadSfcSetup(`src/views/api-testing/components/${component}.vue`, {
      '@/api/api-test-cases': serviceDefaults()
    }, {}, { visible: false, execution: null, testCaseId: null })
    instances.push(instance)
    assert.equal(instance.state.formatDuration(1.234), '1234.0 ms')
    assert.equal(instance.state.formatDuration(null), '-')
  }
})

test('parent execution updates cannot reset the bounded polling budget', async () => {
  const props = reactive({ visible: true, execution: { id: 311, status: 'PENDING' }, pollIntervalMs: 1, pollLimit: 2 })
  let calls = 0
  const instance = await loadSfcSetup('src/views/api-testing/components/ApiCaseRunPanel.vue', {
    '@/api/api-test-cases': { getApiCaseExecution: async () => { calls += 1; return { data: { id: 311, status: 'RUNNING' } } } }
  }, {}, props)
  instances.push(instance)
  for (let index = 0; index < 2; index += 1) {
    instance.timers.at(-1)()
    await new Promise(resolve => setImmediate(resolve))
    props.execution = instance.events.filter(([name]) => name === 'update:execution').at(-1)[1]
    await nextTick()
  }
  assert.equal(calls, 2)
  assert.equal(instance.events.filter(([name]) => name === 'poll-limit').length, 1)
  assert.equal(instance.timers.length, 2)
})

test('execution panel polls only while open and emits terminal updates', async () => {
  const statuses = [
    { id: 301, status: 'RUNNING', results: [] },
    { id: 301, status: 'COMPLETED', results: [{ order: 0, name: 'Login', status: 'passed', assertions_results: [{ passed: true, expected: 200, actual: 200 }] }] }
  ]
  let calls = 0
  const instance = await loadSfcSetup('src/views/api-testing/components/ApiCaseRunPanel.vue', {
    '@/api/api-test-cases': { getApiCaseExecution: async () => ({ data: statuses[calls++] }) }
  }, {}, { visible: true, execution: { id: 301, status: 'PENDING' }, pollIntervalMs: 1, pollLimit: 4 })
  instances.push(instance)
  assert.equal(instance.timers.length, 1)
  instance.timers[0]()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(calls, 1)
  assert.equal(instance.timers.length, 2)
  instance.timers[1]()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(calls, 2)
  assert.equal(instance.events.some(([name, data]) => name === 'terminal' && data.status === 'COMPLETED'), true)
  assert.equal(instance.timers.length, 2)
})

test('execution polling ignores a pending detail response after unmount', async () => {
  const pending = deferred()
  let calls = 0
  const instance = await loadSfcSetup('src/views/api-testing/components/ApiCaseRunPanel.vue', {
    '@/api/api-test-cases': { getApiCaseExecution: async () => { calls += 1; return pending.promise } }
  }, {}, { visible: true, execution: { id: 302, status: 'RUNNING' }, pollIntervalMs: 1, pollLimit: 4 })
  instances.push(instance)
  instance.timers[0]()
  await Promise.resolve()
  instance.dispose()
  pending.resolve({ data: { id: 302, status: 'COMPLETED' } })
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(calls, 1)
  assert.equal(instance.events.some(([name]) => name === 'update:execution' || name === 'terminal'), false)
})

test('history component requests a bounded page and preserves backend-safe result records', async () => {
  const calls = []
  const instance = await loadSfcSetup('src/views/api-testing/components/ApiCaseExecutionHistory.vue', {
    '@/api/api-test-cases': { getApiTestCaseExecutions: async (id, params) => {
      calls.push({ id, params: plain(params) })
      return { data: { count: 11, results: [{ id: 71, status: 'FAILED', test_case_name: 'Login', results: [{ error: 'redacted' }] }] } }
    } }
  }, {}, { visible: true, testCaseId: 44, pageSize: 10 })
  instances.push(instance)
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(calls, [{ id: 44, params: { page: 1, page_size: 10 } }])
  assert.equal(instance.state.rows.value[0].id, 71)
  assert.equal(instance.state.total.value, 11)
})
