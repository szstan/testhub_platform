import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import { test } from 'node:test'
import { build } from 'esbuild'
import { loadSfcSetup } from './helpers/sfc-setup.mjs'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const plain = value => JSON.parse(JSON.stringify(value))
const rosterVersion = 'a'.repeat(64)

async function loadDeviceOptions(getDeviceList) {
  const filename = resolve(frontend, 'src/api/app-device-options.js')
  const source = await readFile(filename, 'utf8')
  const bundle = await build({
    stdin: { contents: source, resolveDir: dirname(filename), sourcefile: filename, loader: 'js' },
    bundle: true, write: false, platform: 'node', format: 'cjs',
    plugins: [{
      name: 'device-options-api',
      setup(builder) {
        builder.onResolve({ filter: /^\.\/app-automation$/ }, () => ({ path: 'api', namespace: 'device-options-test' }))
        builder.onLoad({ filter: /.*/, namespace: 'device-options-test' }, () => ({
          contents: 'export const getDeviceList = __getDeviceList;', loader: 'js',
        }))
      },
    }],
  })
  const module = { exports: {} }
  runInNewContext(bundle.outputFiles[0].text, {
    module, exports: module.exports, __getDeviceList: getDeviceList,
  }, { filename })
  return module.exports
}

const devices = Array.from({ length: 101 }, (_, index) => ({
  id: index + 1, device_id: `synthetic-${index + 1}`, status: 'online',
  has_pending_action: index === 100, maintenance_mode: index === 100,
}))

function pagedApi(calls) {
  return async params => {
    calls.push(plain(params))
    return { data: {
      count: 101,
      roster_version: rosterVersion,
      next: params.page === 1 ? 'https://untrusted.invalid/not-followed?page=2' : null,
      results: params.page === 1 ? devices.slice(0, 100) : devices.slice(100),
    } }
  }
}

test('all device options use stable pages and carry their roster version past item 100', async () => {
  const calls = []
  const api = await loadDeviceOptions(pagedApi(calls))
  const rows = await api.getAllAppDevices({ status: 'online', page_size: 1000,
    ordering: '-updated_at', expected_roster: 'untrusted' })
  assert.equal(rows.length, 101)
  assert.equal(rows[100].id, 101)
  assert.deepEqual(calls, [
    { status: 'online', consistent: 1, ordering: 'id', page: 1, page_size: 100 },
    { status: 'online', consistent: 1, ordering: 'id', page: 2, page_size: 100, expected_roster: rosterVersion },
  ])
})

test('legacy unpaginated device responses remain supported', async () => {
  for (const response of [{ data: devices }, { data: { success: true, data: devices } }]) {
    const api = await loadDeviceOptions(async () => response)
    assert.equal((await api.getAllAppDevices()).length, 101)
  }
})

test('inconsistent, truncated and over-limit pages fail instead of silently dropping devices', async () => {
  for (const scenario of ['duplicate', 'changed-count', 'truncated', 'missing-next', 'missing-roster', 'limit', 'oversized-page']) {
    const api = await loadDeviceOptions(async params => {
      const reply = body => ({ data: { roster_version: rosterVersion, ...body } })
      if (scenario === 'limit') return reply({ count: 10001, next: null, results: [] })
      if (scenario === 'missing-next') return reply({ count: 1, results: [devices[0]] })
      if (scenario === 'missing-roster') return { data: { count: 1, next: null, results: [devices[0]] } }
      if (scenario === 'oversized-page') return reply({ count: 101, next: null, results: devices })
      if (params.page === 1) return reply({ count: 101, next: '/devices/?page=2', results: devices.slice(0, 100) })
      if (scenario === 'duplicate') return reply({ count: 101, next: null, results: [devices[0]] })
      if (scenario === 'changed-count') return reply({ count: 102, next: null, results: devices.slice(100) })
      return reply({ count: 101, next: null, results: [] })
    })
    await assert.rejects(api.getAllAppDevices(), undefined, scenario)
  }
})

test('balanced deletion and insertion cannot masquerade as a complete device list', async () => {
  const calls = []
  const api = await loadDeviceOptions(async params => {
    calls.push(plain(params))
    if (params.page === 1) return { data: {
      count: 101, next: '/devices/?page=2', results: devices.slice(0, 100), roster_version: rosterVersion,
    } }
    return { data: {
      count: 101, next: null, results: [{ id: 102 }], roster_version: 'b'.repeat(64),
    } }
  })
  await assert.rejects(api.getAllAppDevices(), /changed/)
  assert.equal(calls[1].expected_roster, rosterVersion)
})

test('device recovery UI receives and can manage the last device using the real paginator', async () => {
  const calls = []
  const api = await loadDeviceOptions(pagedApi(calls))
  const view = await loadSfcSetup('src/views/app-automation/devices/DeviceList.vue', {
    '@element-plus/icons-vue': { Refresh: {}, Plus: {}, CircleClose: {} },
    '@/api/app-device-options': api,
    '@/api/app-automation': {
      discoverDevices: async () => ({}), lockDevice: async () => ({}), unlockDevice: async () => ({}),
      connectDevice: async () => ({}), disconnectDevice: async () => ({}), deleteDevice: async () => ({}),
    },
    '@/api/app-device-recovery': { recoverPendingDeviceAction: async () => ({}), clearDeviceMaintenanceMode: async () => ({}) },
    '@/stores/user': { useUserStore: () => ({ isAdmin: true, user: { id: 7 }, getSessionGeneration: () => 1 }) },
    '@/utils/app-automation-helpers': { getDeviceStatusType: () => 'info', getDeviceStatusText: value => value, formatDateTime: value => value },
  }, { setInterval: () => 0, clearInterval() {} })
  try {
    await view.state.getDevices()
    assert.equal(view.state.devices.value.length, 101)
    assert.equal(view.state.canRecoverPendingAction(view.state.devices.value[100]), true)
    assert.equal(view.state.canLegacyUnlock({ status: 'locked', lease_requires_token: true }), false)
    assert.equal(view.state.canLegacyUnlock({ status: 'locked', has_pending_action: true }), false)
  } finally {
    view.dispose()
  }
})
