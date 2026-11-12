import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { reactive } from 'vue'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'

const instances = []
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))
const response = (results, count = results.length) => ({ data: { results, count } })
const job = (id, project = 4, extra = {}) => ({ id, project, status: 'pending', source_type: 'manual', final_attempt: null, attempts_count: 0, ...extra })
const spec = (id = 12, project = 4) => ({ id, project, name: `Contract ${id}`, version: '1', document_hash: `hash-${id}`, document: { paths: {} } })

const sessionStorageForTest = () => {
  const values = new Map()
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
    get length() { return values.size },
  }
}

async function setup(overrides = {}, runtime = {}) {
  const storage = runtime.storage || sessionStorageForTest()
  const userStore = runtime.userStore || reactive({ user: { id: 7 } })
  const noResult = async () => ({ data: {} })
  const contracts = Object.fromEntries([
    'createOpenApiContractSpec', 'deleteOpenApiContractSpec', 'diffOpenApiContractSpecs',
    'executeOpenApiContractSpec', 'getOpenApiContractCoverage', 'listGeneratedOpenApiContractCases',
    'materializeOpenApiContractCases', 'updateOpenApiContractSpec',
  ].map(name => [name, overrides[name] || noResult]))
  contracts.listOpenApiContractSpecs = overrides.listOpenApiContractSpecs || (async () => response([]))
  const executions = {
    listExecutionJobs: async () => response([]),
    listExecutionProjects: async () => response([]),
    createExecutionJob: async data => ({ data: job(22, data.project) }),
  }
  for (const name of Object.keys(executions)) if (overrides[name]) executions[name] = overrides[name]
  const instance = await loadSfcSetup('src/views/api-testing/OpenApiContracts.vue', {
    '@/api/contracts': contracts,
    '@/api/executions': executions,
    '@/stores/user': { useUserStore: () => userStore },
  }, { sessionStorage: storage })
  instances.push(instance)
  const state = instance.state
  state.selectedSpec.value = spec()
  state.activeOperation.value = 'execute'
  return { ...instance, state, storage, userStore }
}

test('a fresh contract can create, auto-select and execute a job entirely through the page', async () => {
  const created = []
  const executed = []
  const { state } = await setup({
    createExecutionJob: async data => { created.push(plain(data)); return { data: job(22, data.project) } },
    executeOpenApiContractSpec: async (id, data) => { executed.push({ id, data: plain(data) }); return { data: { job_id: 22, status: 'passed' } } },
  })
  assert.equal(state.jobs.value.length, 0)
  await state.createJob()
  assert.equal(state.executeForm.value.job_id, 22)
  assert.equal(created[0].project, 4)
  assert.equal(created[0].source_type, 'manual')
  assert.equal(created[0].source_id, 'openapi-contract:12')
  assert.ok(created[0].idempotency_key)
  state.executeForm.value.kinds = ['positive']
  await state.executeContract()
  assert.equal(executed.length, 1)
  assert.equal(executed[0].id, 12)
  assert.equal(executed[0].data.job_id, 22)
  assert.equal(state.operationResult.value.status, 'passed')
  assert.equal(state.executeForm.value.job_id, null)
  assert.equal(state.jobs.value.length, 0)
  await state.executeContract()
  assert.equal(executed.length, 1, 'the consumed job cannot be executed again')
})

test('double-clicking create makes one request and ambiguous network failure reuses its key', async () => {
  const waiting = deferred()
  const keys = []
  const { state } = await setup({ createExecutionJob: data => {
    keys.push(data.idempotency_key)
    return keys.length === 1 ? waiting.promise : Promise.resolve({ data: job(22) })
  } })
  const first = state.createJob()
  await state.createJob()
  assert.equal(keys.length, 1)
  waiting.reject(new Error('network response lost'))
  await first
  assert.equal(state.creatingJob.value, false)
  assert.match(state.jobsError.value, /network response lost/)
  await state.createJob()
  assert.equal(keys[1], keys[0])
  assert.equal(state.jobsError.value, '')
  await state.createJob()
  assert.notEqual(keys[2], keys[1], 'a new explicit creation uses a new idempotency key')
})

test('eligible job options include later pages and reject running, attempted and cross-project rows', async () => {
  const pages = []
  const { state } = await setup({ listExecutionJobs: async params => {
    pages.push(plain(params))
    return params.page === 1
      ? response(Array.from({ length: 100 }, (_, index) => job(index + 1)), 105)
      : response([job(101), job(102, 4, { status: 'running' }), job(103, 4, { attempts_count: 1 }), job(104, 4, { final_attempt: 9 }), job(105, 7)], 105)
  } })
  await state.loadJobs()
  assert.deepEqual(pages.map(page => page.page), [1, 2])
  assert.ok(pages.every(page => page.contract_eligible === true && page.status === 'pending' && page.project === 4))
  assert.equal(state.jobs.value.length, 101)
  assert.equal(state.jobs.value.at(-1).id, 101)
})

test('job pagination failure is visible and a retry replaces incomplete options', async () => {
  let failing = true
  const { state } = await setup({ listExecutionJobs: async params => {
    if (params.page === 2 && failing) throw new Error('second job page unavailable')
    return params.page === 1 ? response(Array.from({ length: 100 }, (_, index) => job(index + 1)), 101) : response([job(101)], 101)
  } })
  await state.loadJobs()
  assert.match(state.jobsError.value, /second job page unavailable/)
  assert.equal(state.jobsLoading.value, false)
  assert.equal(state.jobs.value.length, 0)
  failing = false
  await state.loadJobs()
  assert.equal(state.jobsError.value, '')
  assert.equal(state.jobs.value.length, 101)
})

test('late job responses from another project cannot overwrite options or the new loading state', async () => {
  const first = deferred()
  const second = deferred()
  const { state } = await setup({ listExecutionJobs: params => params.project === 4 ? first.promise : second.promise })
  const oldLoad = state.loadJobs()
  state.selectedSpec.value = spec(13, 7)
  const currentLoad = state.loadJobs()
  first.resolve(response([job(22, 4)]))
  await oldLoad
  assert.equal(state.jobsLoading.value, true)
  assert.equal(state.jobs.value.length, 0)
  second.resolve(response([job(33, 7)]))
  await currentLoad
  assert.equal(state.jobs.value[0].id, 33)
  assert.equal(state.jobsLoading.value, false)
})

test('late creation for the previous contract cannot auto-select a job in the current contract', async () => {
  const first = deferred()
  const second = deferred()
  const { state } = await setup({ createExecutionJob: data => data.project === 4 ? first.promise : second.promise })
  const oldCreation = state.createJob()
  state.selectedSpec.value = spec(13, 7)
  const currentCreation = state.createJob()
  first.resolve({ data: job(22, 4) })
  await oldCreation
  assert.equal(state.creatingJob.value, true)
  assert.equal(state.executeForm.value.job_id, null)
  second.resolve({ data: job(33, 7) })
  await currentCreation
  assert.equal(state.executeForm.value.job_id, 33)
  assert.equal(state.creatingJob.value, false)
})

test('an old contract result cannot replace the newly selected contract result', async () => {
  const previous = deferred()
  const { state } = await setup()
  const oldRequest = state.runOperation(() => previous.promise)
  state.selectedSpec.value = spec(13, 7)
  await state.runOperation(async () => ({ data: { spec_id: 13 } }))
  previous.resolve({ data: { spec_id: 12 } })
  await oldRequest
  assert.equal(state.operationResult.value.spec_id, 13)
  assert.equal(state.operationLoading.value, false)
})

test('an old permission error cannot disable the new operation or clear its loading state', async () => {
  const previous = deferred()
  const current = deferred()
  const { state } = await setup()
  const oldRequest = state.runOperation(() => previous.promise)
  state.selectedSpec.value = spec(13, 7)
  const currentRequest = state.runOperation(() => current.promise)
  previous.reject({ response: { status: 403, data: { detail: 'old contract denied' } } })
  await oldRequest
  assert.equal(state.operationError.value, '')
  assert.equal(state.operationPermissionDenied.value, false)
  assert.equal(state.operationLoading.value, true)
  current.resolve({ data: { spec_id: 13 } })
  await currentRequest
  assert.equal(state.operationLoading.value, false)
})

test('switching operation tabs, closing, and unmounting invalidate outstanding responses', async () => {
  for (const change of ['tab', 'close', 'unmount']) {
    const previous = deferred()
    const instance = await setup()
    const state = instance.state
    const request = state.runOperation(() => previous.promise)
    if (change === 'tab') state.activeOperation.value = 'diff'
    else if (change === 'close') state.selectedSpec.value = null
    else instance.dispose()
    previous.resolve({ data: { spec_id: 12, stale: true } })
    await request
    assert.equal(state.operationResult.value, null, change)
    assert.equal(state.operationLoading.value, false, change)
  }
})

test('newer requests for the same operation win and late requests do not clear loading', async () => {
  const first = deferred()
  const second = deferred()
  const { state } = await setup()
  const firstRequest = state.runOperation(() => first.promise)
  const secondRequest = state.runOperation(() => second.promise)
  first.resolve({ data: { version: 'old' } })
  await firstRequest
  assert.equal(state.operationResult.value, null)
  assert.equal(state.operationLoading.value, true)
  second.resolve({ data: { version: 'new' } })
  await secondRequest
  assert.equal(state.operationResult.value.version, 'new')
})

test('editing leaves project ownership unchanged while creating includes the chosen project', async () => {
  const updates = []
  const creations = []
  const { state } = await setup({
    updateOpenApiContractSpec: async (id, data) => { updates.push({ id, data: plain(data) }); return { data: {} } },
    createOpenApiContractSpec: async data => { creations.push(plain(data)); return { data: {} } },
  })
  state.openEdit(spec())
  state.form.value.name = 'Updated contract'
  await state.saveSpec()
  assert.equal(updates[0].id, 12)
  assert.equal('project' in updates[0].data, false)
  state.openCreate()
  Object.assign(state.form.value, { project: 7, name: 'New contract', version: '2' })
  await state.saveSpec()
  assert.equal(creations[0].project, 7)
})

test('a stale successful creation cannot discard the key needed by a current ambiguous retry', async () => {
  const first = deferred()
  const second = deferred()
  const keys = []
  const { state } = await setup({ createExecutionJob: data => {
    keys.push(data.idempotency_key)
    if (keys.length === 1) return first.promise
    if (keys.length === 2) return second.promise
    return Promise.resolve({ data: job(22) })
  } })
  const oldCreation = state.createJob()
  state.activeOperation.value = 'coverage'
  state.activeOperation.value = 'execute'
  const currentCreation = state.createJob()
  first.resolve({ data: job(22) })
  await oldCreation
  second.reject(new Error('current response lost'))
  await currentCreation
  await state.createJob()
  assert.equal(keys.length, 3)
  assert.equal(new Set(keys).size, 1)
  assert.equal(state.executeForm.value.job_id, 22)
})

test('create refuses a reused terminal job and can start a fresh explicit request afterwards', async () => {
  const keys = []
  const { state } = await setup({ createExecutionJob: async data => {
    keys.push(data.idempotency_key)
    return { data: keys.length === 1 ? job(22, 4, { status: 'passed', attempts_count: 1, final_attempt: 3 }) : job(23) }
  } })
  await state.createJob()
  assert.equal(state.executeForm.value.job_id, null)
  assert.ok(state.jobsError.value)
  await state.createJob()
  assert.notEqual(keys[0], keys[1])
  assert.equal(state.executeForm.value.job_id, 23)
})

test('an attempted job cannot be submitted even when injected into the selector state', async () => {
  let executed = 0
  const { state } = await setup({ executeOpenApiContractSpec: async () => { executed += 1; return { data: {} } } })
  state.jobs.value = [job(22, 4, { attempts_count: 1 })]
  state.executeForm.value.job_id = 22
  await state.executeContract()
  assert.equal(executed, 0)
  assert.ok(state.jobsError.value)
})

test('a committed create with a lost reply reuses its key after route unmount or reload', async () => {
  const storage = sessionStorageForTest()
  const serverJobs = new Map()
  const keys = []
  let loseReply = true
  const createExecutionJob = async data => {
    keys.push(data.idempotency_key)
    if (!serverJobs.has(data.idempotency_key)) serverJobs.set(data.idempotency_key, job(22))
    if (loseReply) { loseReply = false; throw new Error('create committed, reply lost') }
    return { data: serverJobs.get(data.idempotency_key) }
  }
  const firstVisit = await setup({ createExecutionJob }, { storage })
  await firstVisit.state.createJob()
  assert.equal(storage.length, 1)
  firstVisit.dispose()
  const nextVisit = await setup({ createExecutionJob }, { storage })
  await nextVisit.state.createJob()
  assert.equal(serverJobs.size, 1)
  assert.equal(keys[0], keys[1])
  assert.equal(nextVisit.state.executeForm.value.job_id, 22)
  assert.equal(storage.length, 0, 'the retry record clears after acknowledging the job')
})

test('persisted create requests are isolated by account and project', async () => {
  const storage = sessionStorageForTest()
  const keys = []
  const createExecutionJob = async data => { keys.push(data.idempotency_key); throw new Error('reply lost') }
  const first = await setup({ createExecutionJob }, { storage })
  await first.state.createJob()
  first.dispose()
  const otherAccount = await setup({ createExecutionJob }, { storage, userStore: reactive({ user: { id: 8 } }) })
  await otherAccount.state.createJob()
  otherAccount.dispose()
  const otherProject = await setup({ createExecutionJob }, { storage })
  otherProject.state.selectedSpec.value = spec(12, 9)
  await otherProject.state.createJob()
  assert.equal(new Set(keys).size, 3)
  const originalAccount = await setup({ createExecutionJob }, { storage })
  await originalAccount.state.createJob()
  assert.equal(keys[3], keys[0])
})

test('blocked session storage prevents an unrecoverable create request', async () => {
  let created = 0
  const storage = { getItem() { throw new Error('storage blocked') } }
  const { state } = await setup({ createExecutionJob: async () => { created += 1 } }, { storage })
  await state.createJob()
  assert.equal(created, 0)
  assert.equal(state.jobsError.value, 'execution.contracts.storageUnavailable')
  assert.equal(state.creatingJob.value, false)
})

test('changing accounts invalidates responses from the previous authenticated user', async () => {
  const oldResult = deferred()
  const { state, userStore } = await setup()
  const pending = state.runOperation(() => oldResult.promise)
  userStore.user = { id: 8 }
  oldResult.resolve({ data: { stale: true } })
  await pending
  assert.equal(state.operationResult.value, null)
  assert.equal(state.operationLoading.value, false)
})

test('a lost execution reply reconciles eligibility without replaying the external request', async () => {
  let executions = 0
  const reconciliations = []
  const { state } = await setup({
    executeOpenApiContractSpec: async () => { executions += 1; throw new Error('execution committed, reply lost') },
    listExecutionJobs: async params => { reconciliations.push(plain(params)); return response([]) },
  })
  await state.createJob()
  await state.executeContract()
  assert.equal(executions, 1)
  assert.equal(reconciliations.length, 1)
  assert.equal(reconciliations[0].contract_eligible, true)
  assert.equal(state.executeForm.value.job_id, null)
  assert.equal(state.jobs.value.length, 0)
  await state.executeContract()
  assert.equal(executions, 1)
})

test('a validation failure preserves the job only after the server confirms it is still eligible', async () => {
  let executions = 0
  const { state } = await setup({
    executeOpenApiContractSpec: async () => { executions += 1; throw { response: { status: 400, data: { detail: 'base URL required' } } } },
    listExecutionJobs: async () => response([job(22)]),
  })
  await state.createJob()
  await state.executeContract()
  assert.equal(executions, 1)
  assert.equal(state.executeForm.value.job_id, 22)
  assert.match(state.operationError.value, /base URL required/)
})

test('failed eligibility reconciliation removes the stale selection and remains retryable', async () => {
  let failing = true
  const { state } = await setup({
    executeOpenApiContractSpec: async () => { throw new Error('execution reply lost') },
    listExecutionJobs: async () => { if (failing) throw new Error('reconciliation unavailable'); return response([]) },
  })
  await state.createJob()
  await state.executeContract()
  assert.equal(state.executeForm.value.job_id, null)
  assert.equal(state.jobs.value.length, 0)
  assert.match(state.jobsError.value, /reconciliation unavailable/)
  failing = false
  await state.loadJobs()
  assert.equal(state.jobsError.value, '')
})

test('a reconciliation response cannot restore a job after changing the contract', async () => {
  const started = deferred()
  const reconciliation = deferred()
  const { state } = await setup({
    executeOpenApiContractSpec: async () => { throw new Error('reply lost') },
    listExecutionJobs: () => { started.resolve(); return reconciliation.promise },
  })
  await state.createJob()
  const execution = state.executeContract()
  await started.promise
  state.selectedSpec.value = spec(13, 7)
  reconciliation.resolve(response([job(22)]))
  await execution
  assert.equal(state.executeForm.value.job_id, null)
  assert.equal(state.jobs.value.length, 0)
  assert.equal(state.jobsLoading.value, false)
})
