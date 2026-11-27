import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import { build } from 'esbuild'
import { afterEach, test } from 'node:test'
import { loadSfcSetup, plain } from './helpers/sfc-setup.mjs'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const instances = []
const pendingDevice = {
  id: 31,
  device_id: 'mock-device-31',
  name: 'Mock device',
  has_pending_action: true,
  maintenance_mode: true,
}

afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

async function loadRecoveryApi() {
  const filename = resolve(frontend, 'src/api/app-device-recovery.js')
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

async function setupDeviceList({
  isAdmin = true,
  devices = [pendingDevice],
  confirm,
  onRecover,
  onClearMaintenance,
  recoveryError,
  maintenanceError,
  initialUserId = 7,
  initialSessionGeneration = 4,
} = {}) {
  const calls = []
  const confirmations = []
  const logs = []
  const session = {
    user: initialUserId === null ? null : { id: initialUserId },
    generation: initialSessionGeneration,
    accessToken: 'initial-token',
  }
  const userStore = {
    get user() { return session.user },
    get isAdmin() { return isAdmin && Boolean(session.user) },
    getSessionGeneration: () => session.generation,
  }
  const api = {
    getDeviceList: async params => {
      calls.push(['list', params])
      return { data: { results: devices } }
    },
    discoverDevices: async () => ({ data: { success: true, devices } }),
    lockDevice: async () => ({}),
    unlockDevice: async () => ({}),
    connectDevice: async () => ({}),
    disconnectDevice: async () => ({}),
    deleteDevice: async () => ({}),
  }
  const recoveryApi = {
    recoverPendingDeviceAction: async (id, expectedUserId) => {
      calls.push(['recover', id, expectedUserId])
      await onRecover?.(session)
      if (recoveryError) throw recoveryError
      return { data: { maintenance_mode: true } }
    },
    clearDeviceMaintenanceMode: async (id, expectedUserId) => {
      calls.push(['clear-maintenance', id, expectedUserId])
      await onClearMaintenance?.(session)
      if (maintenanceError) throw maintenanceError
      return { data: { maintenance_mode: false } }
    },
  }
  const instance = await loadSfcSetup('src/views/app-automation/devices/DeviceList.vue', {
    '@element-plus/icons-vue': { Refresh: {}, Plus: {}, CircleClose: {} },
    '@/api/app-automation': api,
    '@/api/app-device-recovery': recoveryApi,
    '@/stores/user': { useUserStore: () => userStore },
    '@/utils/app-automation-helpers': {
      getDeviceStatusType: () => 'info',
      getDeviceStatusText: value => value,
      formatDateTime: value => value,
    },
  }, {
    setInterval: () => 0,
    clearInterval() {},
    console: { error: (...args) => logs.push(args) },
    confirm: async (...args) => {
      confirmations.push({ type: 'confirm', args })
      if (confirm) return confirm(session, ...args)
      return true
    },
  })
  instances.push(instance)
  return { ...instance, calls, confirmations, logs, session, userStore }
}

test('recovery API posts the required operator attestation and maintenance PATCH is separate', async () => {
  const { api, requestCalls } = await loadRecoveryApi()

  await api.recoverPendingDeviceAction(31, 7)
  await api.clearDeviceMaintenanceMode(31, 7)

  assert.deepEqual(plain(requestCalls), [
    {
      url: '/app-automation/devices/31/recover-pending-action/',
      method: 'post',
      data: { confirm_no_active_worker: true, expected_user_id: 7 },
    },
    {
      url: '/app-automation/devices/31/',
      method: 'patch',
      data: { maintenance_mode: false, expected_user_id: 7 },
    },
  ])
})

test('admin recovery confirms manual worker/device verification and refreshes the list', async () => {
  const device = { ...pendingDevice }
  const { state, mount, calls, messages, confirmations } = await setupDeviceList({ devices: [device] })
  await mount()

  assert.equal(state.canRecoverPendingAction(device), true)
  assert.equal(state.canClearMaintenance(device), false)
  await state.recoverPendingAction(device)

  assert.deepEqual(plain(calls), [
    ['list', { ordering: 'id', page: 1, page_size: 100 }],
    ['recover', 31, 7],
    ['list', { ordering: 'id', page: 1, page_size: 100 }],
  ])
  assert.equal(confirmations[0].args[0], 'appAutomation.device.messages.recoverPendingActionConfirm')
  assert.equal(confirmations[0].args[2].type, 'warning')
  assert.equal(device.maintenance_mode, true)
  assert.equal(messages.some(message => message.type === 'success'), true)
})

test('cancelling either confirmation leaves pending markers and maintenance unchanged', async () => {
  const noPendingDevice = { ...pendingDevice, id: 32, has_pending_action: false }
  const cancel = async () => { throw 'cancel' }
  const { state, mount, calls, messages } = await setupDeviceList({
    devices: [pendingDevice, noPendingDevice],
    confirm: cancel,
  })
  await mount()

  await state.recoverPendingAction(pendingDevice)
  await state.clearMaintenanceMode(noPendingDevice)

  assert.deepEqual(plain(calls), [['list', { ordering: 'id', page: 1, page_size: 100 }]])
  assert.equal(messages.length, 0)
  assert.equal(pendingDevice.maintenance_mode, true)
})

test('maintenance clearing is admin-only, requires an explicit false pending marker, and refreshes', async () => {
  const { state, mount, calls, confirmations } = await setupDeviceList({
    devices: [{ ...pendingDevice, has_pending_action: false }],
  })
  await mount()

  assert.equal(state.canClearMaintenance({ ...pendingDevice, has_pending_action: true }), false)
  assert.equal(state.canClearMaintenance({ ...pendingDevice, has_pending_action: undefined }), false)
  assert.equal(state.canClearMaintenance({ ...pendingDevice, maintenance_mode: false, has_pending_action: false }), false)
  assert.equal(state.canClearMaintenance({ ...pendingDevice, has_pending_action: false }), true)

  await state.clearMaintenanceMode({ ...pendingDevice, has_pending_action: true })
  assert.equal(calls.some(call => call[0] === 'clear-maintenance'), false)
  await state.clearMaintenanceMode({ ...pendingDevice, has_pending_action: false })

  assert.deepEqual(plain(calls), [
    ['list', { ordering: 'id', page: 1, page_size: 100 }],
    ['clear-maintenance', 31, 7],
    ['list', { ordering: 'id', page: 1, page_size: 100 }],
  ])
  assert.equal(confirmations[0].args[0], 'appAutomation.device.messages.clearMaintenanceConfirm')
})

test('non-admins cannot invoke either administrative action', async () => {
  const { state, mount, calls, messages, confirmations } = await setupDeviceList({ isAdmin: false })
  await mount()

  assert.equal(state.canRecoverPendingAction(pendingDevice), false)
  assert.equal(state.canClearMaintenance({ ...pendingDevice, has_pending_action: false }), false)
  await state.recoverPendingAction(pendingDevice)
  await state.clearMaintenanceMode({ ...pendingDevice, has_pending_action: false })

  assert.equal(calls.some(call => call[0] === 'recover' || call[0] === 'clear-maintenance'), false)
  assert.equal(messages.length, 0)
  assert.equal(confirmations.length, 0)
})

test('session-bound confirmations reject logout, account switch, and same-account relogin', async () => {
  const maintenanceDevice = { ...pendingDevice, id: 32, has_pending_action: false }
  const operations = [
    { action: 'recoverPendingAction', device: pendingDevice, endpoint: 'recover' },
    { action: 'clearMaintenanceMode', device: maintenanceDevice, endpoint: 'clear-maintenance' },
  ]
  const changes = [
    ['logout', session => { session.user = null }],
    ['account switch', session => { session.user = { id: 8 } }],
    ['same-account relogin', session => { session.user = { id: 7 } }],
  ]

  for (const operation of operations) {
    for (const [changeName, changeSession] of changes) {
      const { state, mount, calls, messages, confirmations } = await setupDeviceList({
        devices: [operation.device],
        confirm: session => {
          changeSession(session)
          session.generation += 1
          return true
        },
      })
      await mount()
      await state[operation.action](operation.device)

      assert.equal(confirmations.length, 1, `${operation.action}: ${changeName} still confirms`)
      assert.equal(calls.some(call => call[0] === operation.endpoint), false, `${operation.action}: ${changeName} must not call the API`)
      assert.equal(messages.length, 0, `${operation.action}: ${changeName} should be silent`)
    }
  }
})

test('token refresh within the same session keeps both confirmed admin actions available', async () => {
  const maintenanceDevice = { ...pendingDevice, id: 32, has_pending_action: false }
  const { state, mount, calls, messages, session } = await setupDeviceList({
    devices: [pendingDevice, maintenanceDevice],
    confirm: currentSession => {
      currentSession.accessToken = 'refreshed-token'
      return true
    },
  })
  await mount()

  await state.recoverPendingAction(pendingDevice)
  await state.clearMaintenanceMode(maintenanceDevice)

  assert.deepEqual(calls.filter(call => call[0] === 'recover' || call[0] === 'clear-maintenance'), [
    ['recover', 31, 7],
    ['clear-maintenance', 32, 7],
  ])
  assert.equal(session.generation, 4)
  assert.equal(messages.filter(message => message.type === 'success').length, 2)
})

test('responses from an action that crosses a session boundary do not update success state', async () => {
  const { state, mount, calls, messages } = await setupDeviceList({
    onRecover: session => { session.generation += 1 },
  })
  await mount()

  await state.recoverPendingAction(pendingDevice)

  assert.deepEqual(calls.filter(call => call[0] === 'recover'), [['recover', 31, 7]])
  assert.equal(calls.filter(call => call[0] === 'list').length, 1)
  assert.equal(messages.length, 0)
})

test('recovery and maintenance failures log and display only generic safe messages', async () => {
  const token = 'Bearer synthetic-secret-token'
  const serial = 'SENSITIVE-SERIAL-9081'
  const axiosError = {
    message: `Request failed for ${serial}`,
    config: {
      url: `/app-automation/devices/${serial}/`,
      headers: { Authorization: token },
    },
    request: { responseURL: `/app-automation/devices/${serial}/` },
    response: { data: { detail: `Rejected ${serial} with ${token}` } },
  }
  const cases = [
    {
      action: 'recoverPendingAction',
      device: pendingDevice,
      failureOption: { recoveryError: axiosError },
      log: '恢复设备待处理动作失败',
      message: 'appAutomation.device.messages.recoverPendingActionFailed',
    },
    {
      action: 'clearMaintenanceMode',
      device: { ...pendingDevice, id: 32, has_pending_action: false },
      failureOption: { maintenanceError: axiosError },
      log: '解除设备维护模式失败',
      message: 'appAutomation.device.messages.clearMaintenanceFailed',
    },
  ]

  for (const failure of cases) {
    const { state, mount, logs, messages } = await setupDeviceList({
      devices: [failure.device],
      ...failure.failureOption,
    })
    await mount()
    await state[failure.action](failure.device)

    assert.deepEqual(plain(logs), [[failure.log]])
    assert.equal(messages.at(-1).text, failure.message)
    assert.doesNotMatch(JSON.stringify({ logs, messages }), /synthetic-secret-token|SENSITIVE-SERIAL-9081/)
  }
})

test('DeviceList renders each admin action only through its state predicate', async () => {
  const source = await readFile(resolve(frontend, 'src/views/app-automation/devices/DeviceList.vue'), 'utf8')

  assert.match(source, /v-if="canRecoverPendingAction\(row\)"/)
  assert.match(source, /v-if="canClearMaintenance\(row\)"/)
})
