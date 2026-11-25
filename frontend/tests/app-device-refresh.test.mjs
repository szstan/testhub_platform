import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'

const instances = []
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))
const flush = () => new Promise(resolve => setImmediate(resolve))

async function setupDevices(getAllAppDevices, discoverDevices = async () => ({ data: { success: true } })) {
  const session = { user: { id: 7 }, generation: 1 }
  const userStore = {
    get user() { return session.user },
    getSessionGeneration: () => session.generation,
  }
  const timers = []
  const loaded = await loadSfcSetup('src/views/app-automation/devices/DeviceList.vue', {
    '@element-plus/icons-vue': { Refresh: {}, Plus: {}, CircleClose: {} },
    '@/api/app-device-options': { getAllAppDevices },
    '@/api/app-automation': {
      discoverDevices, lockDevice: async () => ({}), unlockDevice: async () => ({}),
      connectDevice: async () => ({}), disconnectDevice: async () => ({}), deleteDevice: async () => ({}),
    },
    '@/api/app-device-recovery': { recoverPendingDeviceAction: async () => ({}), clearDeviceMaintenanceMode: async () => ({}) },
    '@/stores/user': { useUserStore: () => userStore },
    '@/utils/app-automation-helpers': { getDeviceStatusType: () => '', getDeviceStatusText: value => value, formatDateTime: value => value },
  }, { setInterval: callback => { timers.push(callback); return timers.length }, clearInterval() {}, console: { error() {} } })
  instances.push(loaded)
  return { ...loaded, session, tick: () => timers.forEach(callback => callback()) }
}

test('discovery queues a fresh list read and waits for it before reporting success', async () => {
  const oldRead = deferred()
  const freshRead = deferred()
  let reads = 0
  const view = await setupDevices(() => (++reads === 1 ? oldRead.promise : freshRead.promise))
  const initial = view.state.getDevices()
  await flush()
  let refreshed = false
  const refresh = view.state.refreshDevices().then(() => { refreshed = true })
  await flush()
  assert.equal(reads, 1)
  assert.equal(refreshed, false)
  assert.equal(view.messages.some(message => message.type === 'success'), false)

  oldRead.resolve([{ id: 'before-discovery' }])
  await flush()
  assert.equal(reads, 2)
  assert.equal(refreshed, false)
  assert.equal(view.state.refreshing.value, true)
  assert.equal(view.state.loading.value, true)
  assert.equal(view.messages.some(message => message.type === 'success'), false)

  freshRead.resolve([{ id: 'after-discovery' }])
  await Promise.all([initial, refresh])
  assert.deepEqual(plain(view.state.devices.value), [{ id: 'after-discovery' }])
  assert.equal(view.state.loading.value, false)
  assert.equal(view.state.refreshing.value, false)
  assert.equal(view.messages.filter(message => message.type === 'success').length, 1)
})

test('overlapping list requests coalesce into one follow-up read and share its result', async () => {
  const oldRead = deferred()
  const freshRead = deferred()
  let reads = 0
  const view = await setupDevices(() => (++reads === 1 ? oldRead.promise : freshRead.promise))
  const initial = view.state.getDevices()
  await flush()
  const queued = [view.state.getDevices(), view.state.getDevices(), view.state.getDevices()]
  assert.ok(queued.every(promise => promise === initial))
  oldRead.resolve([])
  await flush()
  assert.equal(reads, 2)
  freshRead.resolve([{ id: 2 }])
  assert.deepEqual(await Promise.all([initial, ...queued]), [true, true, true, true])
  assert.equal(reads, 2)
})

test('an old failed request still drains the successful discovery reload', async () => {
  const oldRead = deferred()
  let reads = 0
  const view = await setupDevices(() => (++reads === 1 ? oldRead.promise : Promise.resolve([{ id: 2 }])))
  const initial = view.state.getDevices()
  await flush()
  const refresh = view.state.refreshDevices()
  await flush()
  oldRead.reject(new Error('initial list failed'))
  await Promise.all([initial, refresh])
  assert.equal(reads, 2)
  assert.deepEqual(plain(view.state.devices.value), [{ id: 2 }])
  assert.equal(view.messages.filter(message => message.type === 'success').length, 1)
})

test('failed final reload never produces a discovery refreshed-success message', async () => {
  const oldRead = deferred()
  let reads = 0
  const view = await setupDevices(() => (++reads === 1 ? oldRead.promise : Promise.reject(new Error('fresh list failed'))))
  const initial = view.state.getDevices()
  await flush()
  const refresh = view.state.refreshDevices()
  await flush()
  oldRead.resolve([{ id: 'old' }])
  assert.equal(await initial, false)
  await refresh
  assert.equal(reads, 2)
  assert.equal(view.messages.some(message => message.type === 'success'), false)
  assert.ok(view.messages.some(message => message.type === 'error'))
  assert.equal(view.state.refreshing.value, false)
  assert.equal(view.state.loading.value, false)
})

test('failed discovery does not enqueue a list read or announce a refresh', async () => {
  const oldRead = deferred()
  let reads = 0
  const view = await setupDevices(() => { reads += 1; return oldRead.promise }, async () => ({ data: { success: false } }))
  const initial = view.state.getDevices()
  await flush()
  await view.state.refreshDevices()
  oldRead.resolve([])
  await initial
  assert.equal(reads, 1)
  assert.equal(view.messages.some(message => message.type === 'success'), false)
})

test('stale discovery and list responses cannot refresh a different account or login generation', async () => {
  for (const accountChanged of [false, true]) {
    const oldRead = deferred()
    const discovery = deferred()
    let reads = 0
    const view = await setupDevices(() => { reads += 1; return oldRead.promise }, () => discovery.promise)
    const initial = view.state.getDevices()
    await flush()
    const refresh = view.state.refreshDevices()
    view.session.user = { id: accountChanged ? 8 : 7 }
    view.session.generation += 1
    discovery.resolve({ data: { success: true } })
    oldRead.resolve([{ id: 'stale' }])
    await Promise.all([initial, refresh])
    assert.equal(reads, 1)
    assert.deepEqual(plain(view.state.devices.value), [])
    assert.deepEqual(view.messages, [])
  }
})

test('unmount discards a queued reload and suppresses late feedback', async () => {
  const oldRead = deferred()
  let reads = 0
  const view = await setupDevices(() => { reads += 1; return oldRead.promise })
  const initial = view.state.getDevices()
  await flush()
  const refresh = view.state.refreshDevices()
  await flush()
  view.dispose()
  oldRead.resolve([{ id: 'late' }])
  await Promise.all([initial, refresh])
  assert.equal(reads, 1)
  assert.deepEqual(plain(view.state.devices.value), [])
  assert.deepEqual(view.messages, [])
  assert.equal(await view.state.getDevices(), false)
})

test('periodic polling cannot keep a slow discovery refresh drain open indefinitely', async () => {
  const oldRead = deferred()
  const freshRead = deferred()
  let reads = 0
  const view = await setupDevices(() => (++reads === 1 ? oldRead.promise : freshRead.promise))
  await view.mount()
  await flush()
  const refresh = view.state.refreshDevices()
  await flush()
  oldRead.resolve([])
  await flush()
  assert.equal(reads, 2)
  view.tick()
  view.tick()
  freshRead.resolve([{ id: 2 }])
  await refresh
  assert.equal(reads, 2)
  assert.equal(view.state.loading.value, false)
  assert.equal(view.state.refreshing.value, false)
  view.tick()
  await flush()
  assert.equal(reads, 3)
})
