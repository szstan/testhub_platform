import assert from 'node:assert/strict'
import { test } from 'node:test'
import { nextTick, reactive } from 'vue'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'

const inspector = 'src/views/app-automation/components/NativeElementInspector.vue'

async function setupInspector({ handlers = {}, props = {}, globals = {} } = {}) {
  const requests = []
  const api = Object.fromEntries([
    'acquireRecorderLease', 'closeAppiumSession', 'createAppElement', 'getAppElementList',
    'getAppiumOperation', 'heartbeatDevice', 'releaseRecorderLease', 'runAppiumSessionOperation',
    'startAppiumSession', 'updateAppElement',
  ].map(name => [name, async (...args) => {
    requests.push(name)
    return handlers[name] ? handlers[name](...args) : { data: {} }
  }]))
  const instance = await loadSfcSetup(inspector, {
    '@/stores/user': { useUserStore: () => ({ user: { id: 7 }, accessToken: 'fixture', getSessionGeneration: () => 1 }) },
    '@/api/app-automation': api,
  }, globals, props)
  return { ...instance, requests }
}

test('native drafts only offer verification after the Appium session is active', async () => {
  const view = await setupInspector()
  try {
    view.state.form.value.value = 'org.example.qa:id/submit'
    assert.equal(view.state.canVerifyLocator.value, false)
    assert.equal(await view.state.verifyLocator(), false)
    assert.deepEqual(view.requests, [])
    view.state.ownedSession.value = { sessionId: 'fake-session', status: 'active' }
    assert.equal(view.state.canVerifyLocator.value, true)
    view.state.ownedSession.value = null
  } finally {
    view.dispose()
  }
})

const projectProps = () => reactive({
  projectId: 35, appPackageId: 61, projectEnabled: true, bindingId: 'original', allowStepBinding: true,
})

const selectFixturePackage = state => {
  state.packageOptions.value = [{ id: 61, package_name: 'org.example.qa' }]
}

const selectFixtureDevice = state => {
  state.devices.value = [{ id: 10, status: 'available' }]
  state.selectedDeviceId.value = 10
}

const sessionLease = async () => ({ data: {
  lease_token: 'fixture-lease', lease_seconds: 90,
  device: { locked_by: 7, status: 'locked', lease_requires_token: true },
} })

test('a late native save carries the original binding even if the selected step changed', async () => {
  const saved = deferred()
  const props = projectProps()
  const view = await setupInspector({ props, handlers: { createAppElement: () => saved.promise } })
  try {
    selectFixturePackage(view.state)
    Object.assign(view.state.form.value, { name: 'Submit', value: 'org.example.qa:id/submit' })
    const promise = view.state.saveElement()
    await nextTick()
    assert.ok(view.requests.includes('createAppElement'))
    props.bindingId = 'new-step'
    saved.resolve({ data: { id: 21, name: 'Submit', element_type: 'native' } })
    assert.equal(await promise, true)
    assert.equal(view.events[0][0], 'saved')
    assert.equal(view.events[0][1].bindingId, 'original')
  } finally {
    view.dispose()
  }
})

test('a confirmed Appium startup failure releases the lease without requesting device recovery', async () => {
  const view = await setupInspector({ props: projectProps(), handlers: {
    acquireRecorderLease: sessionLease,
    startAppiumSession: async () => ({ data: { session_id: 'session', operation_id: 'start' } }),
    getAppiumOperation: async () => ({ data: { status: 'failed', recovery_required: false } }),
  } })
  try {
    selectFixturePackage(view.state)
    selectFixtureDevice(view.state)
    await nextTick()
    assert.equal(await view.state.startSession(), false)
    assert.equal(view.state.ownedSession.value, null)
    assert.ok(view.requests.includes('releaseRecorderLease'))
    assert.ok(!view.requests.includes('closeAppiumSession'))
    assert.equal(view.state.statusKey.value, 'appAutomation.nativeInspector.status.error')
  } finally {
    view.dispose()
  }
})

test('a started inspector session can fetch a snapshot and close using its reactive lease', async () => {
  const view = await setupInspector({ props: projectProps(), handlers: {
    acquireRecorderLease: sessionLease,
    startAppiumSession: async () => ({ data: { session_id: 'session', operation_id: 'start' } }),
    runAppiumSessionOperation: async () => ({ data: { operation_id: 'tree' } }),
    closeAppiumSession: async () => ({ data: { operation_id: 'close' } }),
    getAppiumOperation: async id => ({ data: { status: 'succeeded', result: {
      success: true, status: id === 'close' ? 'closed' : 'active',
      ...(id === 'tree' ? { screenshot: 'data:image/png;base64,ZmFrZQ==', nodes: [] } : {}),
    } } }),
  }, globals: { setInterval: () => 1, clearInterval: () => {} } })
  try {
    selectFixturePackage(view.state)
    selectFixtureDevice(view.state)
    await nextTick()
    assert.equal(await view.state.startSession(), true)
    assert.equal(view.state.hasActiveSession.value, true)
    assert.ok(view.requests.includes('runAppiumSessionOperation'))
    assert.equal(await view.state.closeSession(), true)
    assert.equal(view.state.ownedSession.value, null)
    assert.ok(view.requests.includes('releaseRecorderLease'))
  } finally {
    view.dispose()
  }
})

test('a confirmed remote close retries only lease release after a request failure', async () => {
  let releaseAttempts = 0
  const view = await setupInspector({ props: projectProps(), handlers: {
    closeAppiumSession: async () => ({ data: { operation_id: 'close' } }),
    getAppiumOperation: async () => ({ data: { status: 'succeeded', result: { success: true, status: 'closed' } } }),
    releaseRecorderLease: async () => {
      if (++releaseAttempts === 1) throw new Error('Synthetic release request failure')
      return { data: {} }
    },
  }, globals: { clearInterval: () => {} } })
  try {
    selectFixtureDevice(view.state)
    await nextTick()
    view.state.ownedSession.value = {
      identity: view.state.captureIdentity(), status: 'active', sessionId: 'fixture-session',
      lease: { leaseToken: 'fixture-lease' },
    }
    assert.equal(await view.state.closeSession(), false)
    assert.equal(view.state.ownedSession.value.status, 'closed')
    assert.equal(view.state.statusKey.value, 'appAutomation.nativeInspector.status.releasePending')
    assert.equal(await view.state.closeSession(), true)
    assert.equal(view.state.ownedSession.value, null)
    assert.equal(releaseAttempts, 2)
    assert.equal(view.requests.filter(name => name === 'closeAppiumSession').length, 1)
  } finally {
    view.dispose()
  }
})

test('closing after a timeout checks the original operation and releases its lease once', async () => {
  let now = 0
  let closed = false
  const polled = []
  const heartbeat = deferred()
  const view = await setupInspector({ props: projectProps(), handlers: {
    heartbeatDevice: () => heartbeat.promise,
    closeAppiumSession: async () => ({ data: { operation_id: 'close' } }),
    getAppiumOperation: async id => {
      polled.push(id)
      return closed
        ? { data: { status: 'succeeded', result: { success: true, status: 'closed' } } }
        : { data: { status: 'queued' } }
    },
  }, globals: { Date: { now: () => now }, setTimeout: resolve => { now += 61_000; resolve() } } })
  try {
    selectFixtureDevice(view.state)
    await nextTick()
    view.state.ownedSession.value = {
      identity: view.state.captureIdentity(), status: 'active', sessionId: 'fixture-session',
      lease: { leaseToken: 'fixture-lease' },
    }
    const renewal = view.state.heartbeat(view.state.ownedSession.value)
    assert.equal(await view.state.closeSession(), false)
    assert.equal(view.state.ownedSession.value.status, 'closing')
    assert.equal(view.state.statusKey.value, 'appAutomation.nativeInspector.status.closePending')
    assert.ok(!view.requests.includes('releaseRecorderLease'))
    heartbeat.reject(new Error('Synthetic late heartbeat failure'))
    await renewal
    assert.equal(view.state.ownedSession.value.status, 'closing')

    closed = true
    assert.equal(await view.state.closeSession(), true)
    assert.equal(view.state.ownedSession.value, null)
    assert.deepEqual(polled, ['close', 'close'])
    assert.equal(view.requests.filter(name => name === 'closeAppiumSession').length, 1)
    assert.equal(view.requests.filter(name => name === 'releaseRecorderLease').length, 1)
  } finally {
    view.dispose()
  }
})

test('retrying a lost close response reuses its key and accepts a lease already returned', async () => {
  const keys = []
  const view = await setupInspector({ props: projectProps(), handlers: {
    closeAppiumSession: async (_sessionId, payload) => {
      keys.push(payload.idempotency_key)
      if (keys.length === 1) throw new Error('Synthetic response lost')
      return { data: { operation_id: 'close' } }
    },
    getAppiumOperation: async () => ({ data: { status: 'succeeded', result: {
      success: true, status: 'closed', lease_ended: true,
    } } }),
  } })
  try {
    selectFixtureDevice(view.state)
    await nextTick()
    view.state.ownedSession.value = {
      identity: view.state.captureIdentity(), status: 'active', sessionId: 'fixture-session',
      lease: { leaseToken: 'fixture-lease' },
    }
    assert.equal(await view.state.closeSession(), false)
    assert.equal(view.state.ownedSession.value.status, 'closing')
    assert.equal(await view.state.closeSession(), true)
    assert.equal(view.state.ownedSession.value, null)
    assert.equal(keys.length, 2)
    assert.equal(keys[0], keys[1])
    assert.ok(!view.requests.includes('releaseRecorderLease'))
  } finally {
    view.dispose()
  }
})

test('confirming an undelivered close follows the expiry close receipt into recovery', async () => {
  const keys = []
  const polled = []
  const view = await setupInspector({ props: projectProps(), handlers: {
    closeAppiumSession: async (_sessionId, payload) => {
      keys.push(payload.idempotency_key)
      if (keys.length === 1) throw new Error('Synthetic request was not delivered')
      return { data: { operation_id: 'expiry-close', status: 'unknown' } }
    },
    getAppiumOperation: async id => {
      polled.push(id)
      return { data: { status: 'unknown', recovery_required: true } }
    },
  } })
  try {
    selectFixtureDevice(view.state)
    await nextTick()
    view.state.ownedSession.value = {
      identity: view.state.captureIdentity(), status: 'active', sessionId: 'fixture-session',
      lease: { leaseToken: 'fixture-lease' },
    }
    assert.equal(await view.state.closeSession(), false)
    assert.equal(view.state.statusKey.value, 'appAutomation.nativeInspector.status.closePending')
    assert.equal(await view.state.closeSession(), false)
    assert.equal(view.state.statusKey.value, 'appAutomation.nativeInspector.status.cleanupRequired')
    assert.equal(view.state.ownedSession.value.status, 'unknown')
    assert.equal(view.state.ownedSession.value.recoveryRequired, true)
    assert.deepEqual(keys, [keys[0], keys[0]])
    assert.deepEqual(polled, ['expiry-close'])
    assert.ok(!view.requests.includes('releaseRecorderLease'))
  } finally {
    view.dispose()
  }
})

test('release checks the close receipt if the lease was reclaimed after confirmation', async () => {
  let checks = 0
  const view = await setupInspector({ props: projectProps(), handlers: {
    closeAppiumSession: async () => ({ data: { operation_id: 'close' } }),
    getAppiumOperation: async () => ({ data: { status: 'succeeded', result: {
      success: true, status: 'closed', lease_ended: ++checks > 1,
    } } }),
    releaseRecorderLease: async () => {
      throw Object.assign(new Error('Synthetic lease replaced'), { response: { status: 403 } })
    },
  } })
  try {
    selectFixtureDevice(view.state)
    await nextTick()
    view.state.ownedSession.value = {
      identity: view.state.captureIdentity(), status: 'active', sessionId: 'fixture-session',
      lease: { leaseToken: 'fixture-lease' },
    }
    assert.equal(await view.state.closeSession(), true)
    assert.equal(view.state.ownedSession.value, null)
    assert.equal(checks, 2)
    assert.equal(view.requests.filter(name => name === 'releaseRecorderLease').length, 1)
  } finally {
    view.dispose()
  }
})

test('a server-confirmed unknown close keeps the device bound for recovery', async () => {
  const view = await setupInspector({ props: projectProps(), handlers: {
    closeAppiumSession: async () => ({ data: { operation_id: 'close' } }),
    getAppiumOperation: async () => ({ data: { status: 'unknown', recovery_required: true } }),
  } })
  try {
    selectFixtureDevice(view.state)
    await nextTick()
    view.state.ownedSession.value = {
      identity: view.state.captureIdentity(), status: 'active', sessionId: 'fixture-session',
      lease: { leaseToken: 'fixture-lease' },
    }
    assert.equal(await view.state.closeSession(), false)
    assert.equal(view.state.ownedSession.value.status, 'unknown')
    assert.equal(view.state.ownedSession.value.recoveryRequired, true)
    assert.equal(await view.state.closeSession(), false)
    assert.equal(view.requests.filter(name => name === 'closeAppiumSession').length, 1)
    assert.ok(!view.requests.includes('releaseRecorderLease'))
  } finally {
    view.dispose()
  }
})

test('native verification distinguishes missing, unique, and ambiguous controls', async () => {
  const view = await setupInspector()
  try {
    const { state } = view
    const unique = state.normalizeVerification({ count: 1, displayed: true, enabled: true })
    assert.deepEqual(plain(unique), { count: 1, displayed: true, enabled: true, bounds: null, ambiguous_container: false })
    for (const [count, expected] of [[0, 'verification-missing'], [1, 'verification-unique'], [2, 'verification-ambiguous']]) {
      state.verification.value = state.normalizeVerification({ count })
      assert.equal(state.verificationClass.value, expected)
    }
  } finally {
    view.dispose()
  }
})
