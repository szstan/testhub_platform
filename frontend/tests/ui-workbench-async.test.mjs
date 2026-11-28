import assert from 'node:assert/strict'
import { test } from 'node:test'
import { reactive } from 'vue'
import { createUiCaseRunController } from '../src/utils/ui-case-run-controller.mjs'
import { newUiSubmissionId, uiPendingRunKey, readUiPendingRun, writeUiPendingRun } from '../src/utils/ui-execution-submission.mjs'
import { deferred } from './helpers/sfc-setup.mjs'

const missing = () => Object.assign(new Error('Not found'), { response: { status: 404 } })
const offline = () => new Error('Connection lost')
const config = { engine: 'playwright', browser: 'chrome', headless: true }
const flush = () => new Promise(resolve => setImmediate(resolve))
const storage = () => {
  const rows = new Map()
  return { rows, getItem: key => rows.get(key), setItem: (key, value) => rows.set(key, value), removeItem: key => rows.delete(key) }
}

function harness(overrides = {}, savedStorage = storage()) {
  const scope = { actor: 1, project: 2, hidden: false }
  const record = { id: 100, project: 2, test_case: 11, status: 'running', execution_logs: '' }
  const calls = { submit: [], lookup: [], detail: [], abort: [] }
  const results = []
  const timers = new Map()
  let timerId = 0
  const api = {
    submit: async () => ({ data: { execution_id: 100, accepted: true, status: 'pending' } }),
    lookup: async () => { throw missing() },
    detail: async id => ({ data: { ...record, id } }),
    abort: async () => { record.status = 'aborted'; return { data: { status: 'aborted' } } },
    ...overrides,
  }
  const controller = createUiCaseRunController({
    actorId: () => scope.actor, projectId: () => scope.project, hidden: () => scope.hidden,
    storage: () => savedStorage, reactive,
    schedule: callback => { timers.set(++timerId, callback); return timerId },
    clearTimer: id => timers.delete(id),
    ...Object.fromEntries(Object.keys(calls).map(name => [name, (...args) => { calls[name].push(args); return api[name](...args) }])),
    onResult: (project, caseId, result) => results.push({ project, caseId, result }),
  })
  return { scope, record, calls, results, timers, api, controller, storage: savedStorage,
    key: () => uiPendingRunKey('case', scope.actor, scope.project, 11),
    async tick() {
      const entry = timers.entries().next().value
      assert.ok(entry, 'an active run should have a scheduled poll')
      timers.delete(entry[0])
      await entry[1]()
    },
  }
}

for (const failureStage of ['dispatch', 'preflight']) {
  test(`an explicit 503 ${failureStage} refusal is terminal rather than an unknown submission`, async t => {
    const h = harness({ submit: async () => { throw Object.assign(new Error('Queue unavailable'), { response: { status: 503, data: { accepted: false, failure_stage: failureStage, message: 'Queue unavailable' } } }) } })
    t.after(() => h.controller.dispose())
    await h.controller.start(11, config)
    assert.equal(h.controller.currentRun(11), null)
    assert.equal(h.results[0].result.failure_stage, failureStage)
    assert.equal(h.results[0].result.success, false)
    assert.equal(h.calls.lookup.length, 0)
    assert.equal(readUiPendingRun(h.key(), h.storage), null)
  })
}

test('an ambiguous 503 still keeps the original identity for recovery', async t => {
  const h = harness({ submit: async () => { throw Object.assign(offline(), { response: { status: 503, data: { error: 'Upstream timeout' } } }) } })
  t.after(() => h.controller.dispose())
  await h.controller.start(11, config)
  assert.equal(h.controller.currentRun(11).phase, 'unknown')
  assert.equal(h.results.length, 0)
  assert.equal(h.calls.lookup.length, 1)
  assert.ok(readUiPendingRun(h.key(), h.storage))
})

test('an acceptance ID remains active until the authoritative detail reaches a terminal state', async t => {
  const h = harness()
  t.after(() => h.controller.dispose())
  await h.controller.start(11, config)
  assert.equal(h.controller.currentRun(11).status, 'running')
  assert.equal(h.results.length, 0)
  assert.equal(readUiPendingRun(h.key(), h.storage).execution_id, 100)
  h.record.status = 'passed'
  h.record.execution_logs = 'All steps completed'
  await h.tick()
  assert.equal(h.controller.currentRun(11), null)
  assert.equal(h.results[0].result.success, true)
  assert.equal(h.results[0].result.execution_id, 100)
  assert.equal(h.results[0].result.logs, 'All steps completed')
  assert.equal(readUiPendingRun(h.key(), h.storage), null)
})

test('a lost POST response recovers its existing execution without posting again', async t => {
  const h = harness({ submit: async () => { throw offline() }, lookup: async () => ({ data: { execution_id: 100 } }) })
  t.after(() => h.controller.dispose())
  await h.controller.start(11, config)
  assert.equal(h.calls.submit.length, 1)
  assert.equal(h.calls.lookup.length, 1)
  assert.equal(h.calls.lookup[0][1].client_request_id, h.calls.submit[0][1].client_request_id)
  assert.equal(h.controller.currentRun(11).executionId, 100)
  assert.equal(h.results.length, 0)
})

test('an unknown submission stays recoverable and explicit retry uses the same UUID', async t => {
  const h = harness({ submit: async () => { throw offline() } })
  t.after(() => h.controller.dispose())
  await h.controller.start(11, config)
  const id = h.controller.currentRun(11).requestId
  assert.equal(h.controller.currentRun(11).phase, 'unknown')
  assert.equal(h.results.length, 0)
  await h.controller.start(11, config)
  assert.equal(h.calls.submit.length, 1)
  h.api.submit = async () => ({ data: { execution_id: 100, accepted: true } })
  await h.controller.retry(11)
  assert.equal(h.calls.submit.length, 2)
  assert.equal(h.calls.submit[1][1].client_request_id, id)
  assert.equal(h.controller.currentRun(11).executionId, 100)
})

test('refresh restores the pending identity and only queries the known execution', async t => {
  const savedStorage = storage()
  const first = harness({}, savedStorage)
  await first.controller.start(11, config)
  const id = first.controller.currentRun(11).requestId
  first.controller.dispose()
  const second = harness({}, savedStorage)
  t.after(() => second.controller.dispose())
  await second.controller.restore(11)
  assert.equal(second.controller.currentRun(11).requestId, id)
  assert.deepEqual(second.calls.detail, [[100]])
  assert.equal(second.calls.submit.length, 0)
  assert.equal(second.calls.lookup.length, 0)
})

test('a late acceptance after disposal enriches only its stored submission and cannot update the old view', async t => {
  const accepted = deferred()
  const h = harness({ submit: () => accepted.promise })
  const running = h.controller.start(11, config)
  const key = h.key()
  h.controller.dispose()
  accepted.resolve({ data: { execution_id: 100, accepted: true } })
  await running
  assert.equal(h.results.length, 0)
  assert.equal(h.calls.detail.length, 0)
  assert.equal(readUiPendingRun(key, h.storage).execution_id, 100)
  const restored = harness({}, h.storage)
  t.after(() => restored.controller.dispose())
  await restored.controller.restore(11)
  assert.equal(restored.controller.currentRun(11).executionId, 100)
  assert.equal(restored.calls.submit.length, 0)
})

test('switching actor fences pending results and keeps submission storage actor scoped', async t => {
  const detail = deferred()
  const h = harness({ detail: () => detail.promise })
  t.after(() => h.controller.dispose())
  const running = h.controller.start(11, config)
  await flush()
  const oldKey = h.key()
  h.scope.actor = 2
  h.controller.resetScope()
  detail.resolve({ data: { ...h.record, status: 'passed' } })
  await running
  assert.equal(h.controller.currentRun(11), null)
  assert.equal(h.results.length, 0)
  assert.notEqual(oldKey, h.key())
  assert.ok(readUiPendingRun(oldKey, h.storage))
  assert.equal(readUiPendingRun(h.key(), h.storage), null)
  await h.controller.restore(11)
  assert.equal(h.calls.submit.length, 1)
  assert.equal(h.controller.currentRun(11), null)
})

test('unavailable browser storage still preserves the in-memory identity across project switches', async t => {
  const brokenStorage = { getItem() { throw offline() }, setItem() { throw offline() }, removeItem() { throw offline() } }
  const h = harness({}, brokenStorage)
  t.after(() => h.controller.dispose())
  await h.controller.start(11, config)
  const id = h.controller.currentRun(11).requestId
  h.scope.project = 3
  h.controller.resetScope()
  h.scope.project = 2
  h.controller.resetScope()
  await h.controller.restore(11)
  assert.equal(h.controller.currentRun(11).requestId, id)
  assert.equal(h.controller.currentRun(11).executionId, 100)
  assert.equal(h.calls.submit.length, 1)
})

test('temporary detail failure pauses tracking, and retry queries the same run', async t => {
  const h = harness()
  t.after(() => h.controller.dispose())
  await h.controller.start(11, config)
  h.api.detail = async () => { throw offline() }
  await h.tick()
  assert.equal(h.controller.currentRun(11).phase, 'paused')
  assert.equal(h.results.length, 0)
  h.api.detail = async () => ({ data: { ...h.record, status: 'failed', error_message: 'Step 2 failed' } })
  await h.controller.retry(11)
  assert.equal(h.calls.submit.length, 1)
  assert.equal(h.results[0].result.status, 'failed')
  assert.equal(h.results[0].result.errors[0].message, 'Step 2 failed')
})

test('visibility pauses polling and reconnects without launching business actions', async t => {
  const h = harness()
  t.after(() => h.controller.dispose())
  await h.controller.start(11, config)
  h.scope.hidden = true
  h.controller.visible()
  assert.equal(h.timers.size, 0)
  assert.equal(h.controller.currentRun(11).phase, 'paused')
  h.scope.hidden = false
  h.controller.visible()
  await flush()
  assert.equal(h.calls.submit.length, 1)
  assert.equal(h.calls.detail.length, 2)
  assert.equal(h.controller.currentRun(11).phase, 'polling')
})

test('cancel waits for authoritative aborted state and invalidates an already queued poll', async t => {
  const h = harness()
  t.after(() => h.controller.dispose())
  await h.controller.start(11, config)
  const stalePoll = [...h.timers.values()][0]
  await h.controller.cancel(11)
  assert.deepEqual(h.calls.abort, [[100]])
  assert.equal(h.results[0].result.status, 'aborted')
  assert.equal(h.results[0].result.success, false)
  assert.equal(h.controller.currentRun(11), null)
  const queries = h.calls.detail.length
  await stalePoll()
  assert.equal(h.calls.detail.length, queries)
  assert.equal(h.results.length, 1)
})

test('mismatched project data is rejected instead of displaying a foreign execution', async t => {
  const h = harness({ detail: async () => ({ data: { id: 100, project: 3, test_case: 11, status: 'passed' } }) })
  t.after(() => h.controller.dispose())
  await h.controller.start(11, config)
  assert.equal(h.controller.currentRun(11), null)
  assert.equal(h.results[0].result.success, false)
  assert.equal(h.results[0].result.failure_stage, 'access')
  assert.equal(h.results[0].result.execution_id, undefined)
})

test('only the canonical record may confirm execution success, and 403 ends polling', async t => {
  const h = harness({ detail: async () => { throw Object.assign(new Error('Forbidden'), { response: { status: 403 } }) } })
  t.after(() => h.controller.dispose())
  await h.controller.start(11, config)
  assert.equal(h.results[0].result.failure_stage, 'access')
  assert.equal(h.results[0].result.success, false)
  assert.equal(h.timers.size, 0)
})

test('pending metadata retains only safe identity/configuration fields and rejects corrupt identities', () => {
  const savedStorage = storage()
  const key = uiPendingRunKey('case', 1, 2, 11)
  const id = newUiSubmissionId()
  writeUiPendingRun(key, { ...config, client_request_id: id, execution_id: '100', rerun_of: 9, rerun_kind: 'case', password: 'never store this', logs: 'private details', snapshot: { value: 'private' } }, savedStorage)
  assert.deepEqual(readUiPendingRun(key, savedStorage), { ...config, client_request_id: id, execution_id: 100, rerun_of: 9, rerun_kind: 'case' })
  assert.equal(savedStorage.getItem(key).includes('private'), false)
  savedStorage.setItem(key, '{broken')
  assert.equal(readUiPendingRun(key, savedStorage), null)
  assert.equal(uiPendingRunKey('case', '', 2, 11), '')
  assert.equal(uiPendingRunKey('case', 1, [2, 3], 11), '')
})

test('submission UUID generation works on LAN HTTP where randomUUID is unavailable', () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'crypto')
  const getRandomValues = globalThis.crypto.getRandomValues.bind(globalThis.crypto)
  Object.defineProperty(globalThis, 'crypto', { configurable: true, value: { getRandomValues } })
  try {
    const ids = Array.from({ length: 5 }, newUiSubmissionId)
    assert.equal(new Set(ids).size, 5)
    for (const id of ids) assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  } finally { Object.defineProperty(globalThis, 'crypto', descriptor) }
})
