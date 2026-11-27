import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import { build } from 'esbuild'
import { afterEach, test } from 'node:test'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'
import * as deviceExecution from '../src/utils/app-automation-device-execution.mjs'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const instances = []
const reportAccess = { openAppExecutionReport: async () => ({ opened: true }) }
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

const availableDevice = { id: 11, device_id: 'serial-11', name: 'Device 11', status: 'online', locked_by: null }
const manuallyLockedDevice = { ...availableDevice, status: 'locked', locked_by: 7 }
const executionDevice = { ...manuallyLockedDevice, last_used_at: 'now' }

const createUserStore = (id = 7) => ({
  user: { id },
  sessionGeneration: 0,
  getSessionGeneration() {
    return this.sessionGeneration
  },
})

async function setupCaseList({
  getAppDevice,
  executeTestCase,
  batchExecuteTestCases,
  stopExecution,
  userStore = createUserStore(),
  confirm,
  issueWebSocketTicket = async () => '',
} = {}) {
  const calls = []
  const api = {
    getTestCaseList: async () => ({ data: { results: [] } }),
    deleteTestCase: async () => ({}),
    executeTestCase: executeTestCase || (async (id, data) => {
      calls.push(['execute', id, data])
      return { data: { success: true, device: executionDevice } }
    }),
    batchExecuteTestCases: batchExecuteTestCases || (async data => {
      calls.push(['batch', data])
      return { data: { success: true, device: executionDevice, data: { execution_ids: [], test_case_count: data.case_ids.length } } }
    }),
    getExecutionList: async () => ({ data: { results: [] } }),
    getExecutionDetail: async () => ({ data: {} }),
    stopExecution: stopExecution || (async id => {
      calls.push(['stop', id])
      return { data: { success: true, stopping: false } }
    }),
    getPackageList: async () => ({ data: { results: [] } }),
    getAppProjects: async () => ({ data: { results: [] } }),
    getWsStatus: async () => ({ data: { websocket: false } }),
    getDeviceList: async () => ({ data: { results: [availableDevice] } }),
    getAppDevice: getAppDevice || (async id => {
      calls.push(['device', id])
      return { data: availableDevice }
    }),
  }
  const instance = await loadSfcSetup('src/views/app-automation/test-cases/TestCaseList.vue', {
    'vue-router': { useRouter: () => ({ push() {} }) },
    '@element-plus/icons-vue': { Refresh: {}, Search: {} },
    '@/api/app-automation': api,
    '@/utils/app-automation-helpers': {
      getExecutionStatusType: () => 'info',
      getExecutionStatusText: value => value,
      getDisplayStatus: () => ({ type: 'info', text: '' }),
      formatDateTime: value => value,
      getAppReportUrl: () => '',
    },
    '@/utils/websocket': { buildWebSocketUrl: () => '', issueWebSocketTicket },
    '@/utils/app-automation-report.mjs': reportAccess,
    '@/utils/app-automation-device-execution.mjs': deviceExecution,
    '@/stores/user': { useUserStore: () => userStore },
  }, { setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {}, confirm })
  instances.push(instance)
  return { ...instance, calls }
}

async function setupExecutionList({ stopExecution, confirm } = {}) {
  const calls = []
  const api = {
    getExecutionList: async () => ({ data: { results: [] } }),
    getExecutionDetail: async () => ({ data: {} }),
    stopExecution: stopExecution || (async id => {
      calls.push(['stop', id])
      return { data: { success: true, stopping: false } }
    }),
    getAppProjects: async () => ({ data: { results: [] } }),
  }
  const instance = await loadSfcSetup('src/views/app-automation/executions/ExecutionList.vue', {
    'vue-router': { useRouter: () => ({ push() {} }), useRoute: () => ({ query: {} }) },
    '@element-plus/icons-vue': { Search: {}, Refresh: {} },
    '@/api/app-automation': api,
    '@/api/app-step-diagnostics': { getFlowFailureScreenshot: async () => ({ data: new Blob() }) },
    '@/utils/app-automation-report.mjs': reportAccess,
    '@/utils/app-automation-helpers': {
      getExecutionStatusType: () => 'info',
      getExecutionStatusText: value => value,
      getDisplayStatus: () => ({ type: 'info', text: '' }),
      formatDateTime: value => value,
      getAppReportUrl: () => '',
    },
    '@/stores/user': { useUserStore: () => createUserStore() },
  }, { setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {}, confirm })
  instances.push(instance)
  return { ...instance, calls }
}

async function setupSuiteList({ getAppDevice, runTestSuite, userStore = createUserStore(), confirm } = {}) {
  const calls = []
  const api = {
    getTestSuiteList: async () => ({ data: { results: [] } }),
    getTestSuiteDetail: async () => ({ data: {} }),
    createTestSuite: async () => ({ data: {} }),
    updateTestSuite: async () => ({ data: {} }),
    deleteTestSuite: async () => ({ data: {} }),
    addTestCasesToSuite: async () => ({ data: {} }),
    removeTestCaseFromSuite: async () => ({ data: {} }),
    updateSuiteTestCaseOrder: async () => ({ data: {} }),
    runTestSuite: runTestSuite || (async (id, data) => {
      calls.push(['run', id, data])
      return { data: { success: true, message: 'submitted', device: executionDevice } }
    }),
    getTestSuiteExecutions: async () => ({ data: { data: [] } }),
    getExecutionDetail: async () => ({ data: {} }),
    getTestCaseList: async () => ({ data: { results: [] } }),
    getDeviceList: async () => ({ data: { results: [availableDevice] } }),
    getAppDevice: getAppDevice || (async id => {
      calls.push(['device', id])
      return { data: availableDevice }
    }),
    getPackageList: async () => ({ data: { results: [] } }),
    getAppProjects: async () => ({ data: { results: [] } }),
  }
  const instance = await loadSfcSetup('src/views/app-automation/suites/SuiteList.vue', {
    '@element-plus/icons-vue': { Plus: {}, Refresh: {}, Search: {}, Check: {}, Close: {}, Rank: {} },
    'vuedraggable': { default: {} },
    '@/api/app-automation': api,
    '@/utils/app-automation-helpers': {
      getExecutionStatusType: () => 'info',
      getExecutionStatusText: value => value,
      getDisplayStatus: () => ({ type: 'info', text: '' }),
      formatDateTime: value => value,
      getAppReportUrl: () => '',
    },
    '@/utils/app-automation-report.mjs': reportAccess,
    '@/utils/app-automation-device-execution.mjs': deviceExecution,
    '@/stores/user': { useUserStore: () => userStore },
  }, { setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {}, confirm })
  instances.push(instance)
  return { ...instance, calls }
}

async function loadAppAutomationApi() {
  const filename = resolve(frontend, 'src/api/app-automation.js')
  const contents = await readFile(filename, 'utf8')
  const requestCalls = []
  const bundle = await build({
    stdin: { contents, resolveDir: dirname(filename), sourcefile: filename, loader: 'js' },
    bundle: true,
    write: false,
    platform: 'node',
    format: 'cjs',
    plugins: [{
      name: 'mock-api-request',
      setup(builder) {
        builder.onResolve({ filter: /^\.\.\/utils\/api$/ }, () => ({ path: 'request', namespace: 'api-test' }))
        builder.onLoad({ filter: /.*/, namespace: 'api-test' }, () => ({
          contents: 'export default globalThis.__request;',
          loader: 'js',
        }))
      },
    }],
  })
  const module = { exports: {} }
  runInNewContext(bundle.outputFiles[0].text, {
    module,
    exports: module.exports,
    __request: config => {
      requestCalls.push(config)
      return Promise.resolve({ data: {} })
    },
  }, { filename })
  return { api: module.exports, requestCalls }
}

test('API exposes exact-device lookup and one ordered batch execution endpoint', async () => {
  const { api, requestCalls } = await loadAppAutomationApi()
  const payload = { case_ids: [31, 32], device_id: 'serial-11', auto_lock: true, expected_user_id: 7 }

  await api.getAppDevice(11)
  await api.batchExecuteTestCases(payload)

  assert.deepEqual(plain(requestCalls), [
    { url: '/app-automation/devices/11/', method: 'get' },
    { url: '/app-automation/test-cases/batch-execute/', method: 'post', data: payload },
  ])
})

test('execution refresh resolves an exact device outside the first list page', async () => {
  const firstPage = [{ ...availableDevice, id: 1 }]
  const offPageDevice = { ...availableDevice, id: 204, device_id: 'serial-204' }
  const requestedIds = []

  const selected = await deviceExecution.prepareAppDeviceForExecution({
    selectedId: 204,
    userId: 7,
    getDevice: async id => {
      requestedIds.push(id)
      return offPageDevice
    },
  })

  assert.equal(deviceExecution.findAppDevice(firstPage, 204), undefined)
  assert.deepEqual(requestedIds, [204])
  assert.equal(selected.device_id, 'serial-204')
})

test('manual user-owned lock remains selectable but token-backed lease is blocked with guidance', async () => {
  assert.equal(deviceExecution.canSelectAppExecutionDevice(manuallyLockedDevice, 7), true)
  const tokenDevice = { ...manuallyLockedDevice, lease_requires_token: true }
  assert.equal(deviceExecution.canSelectAppExecutionDevice(tokenDevice, 7), false)

  await assert.rejects(deviceExecution.prepareAppDeviceForExecution({
    selectedId: 11,
    userId: 7,
    getDevice: async () => tokenDevice,
    t: key => key,
  }), error => /API 令牌/.test(error.message) && /释放|授权/.test(error.message))
})

test('single case refreshes by database ID, sends auto_lock and updates from success response', async () => {
  const { state, calls } = await setupCaseList()
  state.form.value.deviceId = 11
  state.appPackages.value = [{ id: 3, package_name: 'com.example.app' }]
  state.form.value.packageId = 3

  await state.runCase({ id: 23 })

  assert.deepEqual(plain(calls), [
    ['device', 11],
    ['execute', 23, { device_id: 'serial-11', auto_lock: true, expected_user_id: 7, package_name: 'com.example.app' }],
  ])
  assert.equal(state.availableDevices.value[0].status, 'locked')
  assert.equal(state.availableDevices.value[0].last_used_at, 'now')
})

test('single case freezes case, device and package values while exact-device lookup is pending', async () => {
  const deviceResponse = deferred()
  let deviceStarted
  const didStartDevice = new Promise(resolve => { deviceStarted = resolve })
  const { state, calls } = await setupCaseList({
    getAppDevice: id => {
      calls.push(['device', id])
      deviceStarted()
      return deviceResponse.promise
    },
  })
  const testCase = { id: 23 }
  state.form.value.deviceId = 11
  state.form.value.packageId = 3
  state.appPackages.value = [
    { id: 3, package_name: 'com.original.app' },
    { id: 4, package_name: 'com.changed.app' },
  ]

  const running = state.runCase(testCase)
  await didStartDevice
  testCase.id = 99
  state.form.value.deviceId = 99
  state.form.value.packageId = 4
  deviceResponse.resolve({ data: availableDevice })
  await running

  assert.deepEqual(plain(calls), [
    ['device', 11],
    ['execute', 23, { device_id: 'serial-11', auto_lock: true, expected_user_id: 7, package_name: 'com.original.app' }],
  ])
})

test('batch freezes selection before confirmation and submits one ordered request', async () => {
  const confirmation = deferred()
  const deviceResponse = deferred()
  let confirmStarted
  let deviceStarted
  const didStartConfirm = new Promise(resolve => { confirmStarted = resolve })
  const didStartDevice = new Promise(resolve => { deviceStarted = resolve })
  const { state, calls } = await setupCaseList({
    confirm: () => {
      confirmStarted()
      return confirmation.promise
    },
    getAppDevice: id => {
      calls.push(['device', id])
      deviceStarted()
      return deviceResponse.promise
    },
  })
  state.form.value.deviceId = 11
  state.appPackages.value = [
    { id: 3, package_name: 'com.original.app' },
    { id: 4, package_name: 'com.changed.app' },
  ]
  state.form.value.packageId = 3
  state.selectedCases.value = [{ id: 31 }, { id: 32 }, { id: 33 }]

  const running = state.batchRun()
  await didStartConfirm
  state.selectedCases.value = [{ id: 99 }]
  confirmation.resolve()
  await didStartDevice
  state.form.value.deviceId = 99
  state.form.value.packageId = 4
  deviceResponse.resolve({ data: availableDevice })
  await running

  assert.deepEqual(plain(calls), [
    ['device', 11],
    ['batch', { case_ids: [31, 32, 33], device_id: 'serial-11', auto_lock: true, expected_user_id: 7, package_name: 'com.original.app' }],
  ])
  assert.deepEqual(plain(state.selectedCases.value), [{ id: 99 }])
})

test('batch preserves backend lease errors and never fans out to individual case requests', async () => {
  const { state, messages, calls } = await setupCaseList({
    batchExecuteTestCases: async data => {
      calls.push(['batch', data])
      throw { response: { status: 403, data: { message: 'Device lease token is invalid' } } }
    },
  })
  state.form.value.deviceId = 11
  state.selectedCases.value = [{ id: 31 }, { id: 32 }]

  await state.batchRun()

  assert.deepEqual(plain(calls), [
    ['device', 11],
    ['batch', { case_ids: [31, 32], device_id: 'serial-11', auto_lock: true, expected_user_id: 7 }],
  ])
  assert.ok(messages.some(message => message.type === 'error' && message.text.includes('Device lease token is invalid')))
  assert.equal(deviceExecution.getAppExecutionErrorMessage(
    { response: { status: 403, data: { message: 'Device lease token is invalid' } } },
    'fallback'
  ), 'Device lease token is invalid')
})

test('batch submission does not open a websocket for every execution', async () => {
  let ticketRequests = 0
  const { state } = await setupCaseList({
    issueWebSocketTicket: async () => { ticketRequests++; return '' },
    batchExecuteTestCases: async data => ({
      data: {
        success: true,
        data: { execution_ids: Array.from({ length: 500 }, (_, index) => index + 1), test_case_count: data.case_ids.length },
      },
    }),
  })
  state.form.value.deviceId = 11
  state.selectedCases.value = [{ id: 31 }, { id: 32 }]

  await state.batchRun()

  assert.equal(ticketRequests, 0)
})

test('suite stores database ID and submits serial with server-side auto-lock', async () => {
  const { state, calls } = await setupSuiteList()
  state.runConfig.value.deviceId = 11
  state.runConfig.value.packageName = 'com.example.app'

  await state.runSuite({ id: 5, name: 'Smoke', test_case_count: 2 })

  assert.deepEqual(plain(calls), [
    ['device', 11],
    ['run', 5, { device_id: 'serial-11', auto_lock: true, expected_user_id: 7, package_name: 'com.example.app' }],
  ])
  assert.equal(state.availableDevices.value[0].status, 'locked')
})

test('suite freezes suite, device and package values while exact-device lookup is pending', async () => {
  const deviceResponse = deferred()
  let deviceStarted
  const didStartDevice = new Promise(resolve => { deviceStarted = resolve })
  const { state, calls } = await setupSuiteList({
    getAppDevice: id => {
      calls.push(['device', id])
      deviceStarted()
      return deviceResponse.promise
    },
  })
  const suite = { id: 5, name: 'Smoke', test_case_count: 2 }
  state.runConfig.value.deviceId = 11
  state.runConfig.value.packageName = 'com.original.app'

  const running = state.runSuite(suite)
  await didStartDevice
  suite.id = 55
  suite.name = 'Changed'
  suite.test_case_count = 8
  state.runConfig.value.deviceId = 99
  state.runConfig.value.packageName = 'com.changed.app'
  deviceResponse.resolve({ data: availableDevice })
  await running

  assert.deepEqual(plain(calls), [
    ['device', 11],
    ['run', 5, { device_id: 'serial-11', auto_lock: true, expected_user_id: 7, package_name: 'com.original.app' }],
  ])
})

test('pending execution can stop its dispatch and reports cooperative stops accurately', async () => {
  const { state, calls, messages } = await setupCaseList({
    stopExecution: async id => {
      calls.push(['stop', id])
      return { data: { success: true, stopping: true } }
    },
  })

  await state.stopTest({ id: 81, status: 'pending', task_id: 'batch-dispatch' })
  await state.stopTest({ id: 82, status: 'completed', task_id: 'batch-dispatch' })

  assert.deepEqual(plain(calls), [['stop', 81]])
  assert.ok(messages.some(message => message.type === 'success' && message.text === 'appAutomation.testCase.stoppingExecution'))
})

test('execution history can stop pending dispatch records with the batch-aware response message', async () => {
  const { state, calls, messages } = await setupExecutionList({
    stopExecution: async id => {
      calls.push(['stop', id])
      return { data: { success: true, stopping: false } }
    },
  })

  await state.stopExecution({ id: 91, status: 'pending', task_id: 'suite-dispatch' })

  assert.deepEqual(plain(calls), [['stop', 91]])
  assert.ok(messages.some(message => message.type === 'success' && message.text === 'appAutomation.execution.executionStopped'))
})

test('A to B to A during exact-device refresh prevents submission and stale local update', async () => {
  const userStore = createUserStore()
  const deviceResponse = deferred()
  let requestStarted
  const didStartRequest = new Promise(resolve => { requestStarted = resolve })
  let submissions = 0
  const { state, messages } = await setupCaseList({
    userStore,
    getAppDevice: id => {
      requestStarted()
      return deviceResponse.promise
    },
    executeTestCase: async () => {
      submissions++
      return { data: { success: true, device: executionDevice } }
    },
  })
  state.form.value.deviceId = 11

  const running = state.runCase({ id: 23 })
  await didStartRequest
  userStore.user = { id: 8 }
  userStore.user = { id: 7 }
  userStore.sessionGeneration++
  deviceResponse.resolve({ data: availableDevice })
  await running

  assert.equal(submissions, 0)
  assert.equal(state.availableDevices.value.length, 0)
  assert.ok(messages.some(message => message.type === 'error' && message.text.includes('executionUserChanged')))
})

test('A to B to A during submission ignores success device state', async () => {
  const userStore = createUserStore()
  const submission = deferred()
  let submissionStarted
  const didStartSubmission = new Promise(resolve => { submissionStarted = resolve })
  const { state, messages, calls } = await setupCaseList({
    userStore,
    executeTestCase: (id, data) => {
      calls.push(['execute', id, data])
      submissionStarted()
      return submission.promise
    },
  })
  state.form.value.deviceId = 11

  const running = state.runCase({ id: 23 })
  await didStartSubmission
  userStore.user = { id: 8 }
  userStore.user = { id: 7 }
  userStore.sessionGeneration++
  submission.resolve({ data: { success: true, device: executionDevice } })
  await running

  assert.equal(state.availableDevices.value[0].status, 'online')
  assert.ok(messages.some(message => message.type === 'error' && message.text.includes('executionUserChanged')))
  assert.ok(!messages.some(message => message.type === 'success'))
})

test('duplicate single-case submission is guarded while the server request is pending', async () => {
  const submission = deferred()
  let submissionStarted
  const didStartSubmission = new Promise(resolve => { submissionStarted = resolve })
  const calls = []
  const { state } = await setupCaseList({
    executeTestCase: (id, data) => {
      calls.push(['execute', id, data])
      submissionStarted()
      return submission.promise
    },
  })
  state.form.value.deviceId = 11

  const first = state.runCase({ id: 23 })
  await didStartSubmission
  await state.runCase({ id: 23 })
  assert.equal(calls.filter(([kind]) => kind === 'execute').length, 1)

  submission.resolve({ data: { success: true, device: executionDevice } })
  await first
})
