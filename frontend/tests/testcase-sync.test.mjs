import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'

const instances = []
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))
const flush = () => new Promise(resolve => setImmediate(resolve))

async function setup(methods = {}, globals = {}) {
  const instance = await loadSfcSetup('src/views/testcases/TestCaseSync.vue', {
    '@/api/testcase-sync': {
      createTestCaseSyncProfile: async () => ({ data: {} }),
      deleteTestCaseSyncProfile: async () => ({}),
      updateTestCaseSyncProfile: async () => ({ data: {} }),
      listTestCaseSyncProfiles: async () => ({ data: { count: 0, results: [] } }),
      syncTestCaseSyncProfile: async () => ({ data: { status: 'queued', task_id: 'claim-1' } }),
      getTestCaseSyncProfile: async () => ({ data: { id: 7, sync_status: 'running', sync_task_id: 'claim-1' } }),
      ...methods,
    },
  }, { AbortController, ...globals })
  instances.push(instance)
  instance.state.profiles.value = [{ id: 7, name: 'Repository' }]
  return instance
}

test('202 submission shows progress and renders only the completed summary', async () => {
  let calls = 0
  const summary = { commit: 'a'.repeat(40), files: 2, created: 1, updated: 1, skipped: 0, conflicts: 0, warnings: [] }
  const instance = await setup({
    getTestCaseSyncProfile: async () => ({ data: {
      id: 7, sync_task_id: 'claim-1',
      sync_status: ++calls === 1 ? 'running' : 'completed',
      sync_result: calls === 1 ? {} : summary,
    } }),
  })
  await instance.state.sync({ id: 7 })
  assert.equal(instance.state.syncing.value, 7)
  assert.equal(instance.state.resultVisible.value, true)
  assert.equal(instance.state.result.value, null)
  assert.equal(instance.state.syncStatus.value, 'running')
  instance.timers.shift()()
  await flush()
  assert.equal(calls, 2)
  assert.equal(instance.state.syncing.value, null)
  assert.equal(instance.state.syncStatus.value, 'completed')
  assert.deepEqual(plain(instance.state.result.value), summary)
  assert.equal(instance.messages.filter(message => message.type === 'success').length, 1)
})

test('backend failure is displayed without inventing an empty successful summary', async () => {
  const instance = await setup({
    getTestCaseSyncProfile: async () => ({ data: {
      id: 7, sync_task_id: 'claim-1', sync_status: 'failed', sync_error: '同步总数据量超过限制',
    } }),
  })
  await instance.state.sync({ id: 7 })
  assert.equal(instance.state.syncing.value, null)
  assert.equal(instance.state.result.value, null)
  assert.equal(instance.state.syncFeedback.value, '同步总数据量超过限制')
  assert.equal(instance.state.syncFeedbackType.value, 'error')
  assert.equal(instance.timers.length, 0)
})

test('a replaced claim does not display the result from the newer submission', async () => {
  const instance = await setup({
    getTestCaseSyncProfile: async () => ({ data: {
      id: 7, sync_task_id: 'new-claim', sync_status: 'completed', sync_result: { files: 99 },
    } }),
  })
  await instance.state.sync({ id: 7 })
  assert.equal(instance.state.result.value, null)
  assert.equal(instance.state.syncStatus.value, 'changed')
  assert.equal(instance.state.syncFeedbackType.value, 'warning')
  assert.equal(instance.state.syncing.value, null)
})

test('polling remains bounded when a queue never claims the job', async () => {
  let clock = 0
  let requests = 0
  const instance = await setup({
    getTestCaseSyncProfile: async () => {
      requests += 1
      return { data: { id: 7, sync_status: 'queued', sync_task_id: 'claim-1' } }
    },
  }, { Date: { now: () => clock } })
  await instance.state.sync({ id: 7 })
  clock = 5 * 60 * 1000
  instance.timers.shift()()
  await flush()
  assert.equal(requests, 1)
  assert.equal(instance.state.syncStatus.value, 'pending')
  assert.equal(instance.state.syncing.value, null)
  assert.equal(instance.state.result.value, null)
})

test('unmount aborts an in-flight poll and ignores its late response', async () => {
  const detail = deferred()
  let signal
  const instance = await setup({
    getTestCaseSyncProfile: (_id, config) => { signal = config.signal; return detail.promise },
  })
  const syncing = instance.state.sync({ id: 7 })
  await flush()
  assert.equal(signal.aborted, false)
  instance.dispose()
  assert.equal(signal.aborted, true)
  detail.resolve({ data: { id: 7, sync_status: 'completed', sync_task_id: 'claim-1', sync_result: { files: 9 } } })
  await syncing
  assert.equal(instance.state.result.value, null)
  assert.equal(instance.state.syncing.value, null)
  assert.equal(instance.messages.length, 0)
  assert.equal(instance.timers.length, 0)
})

test('completion refreshes the current page and duplicate clicks do not submit twice', async () => {
  const detail = deferred()
  const calls = []
  const pages = []
  const instance = await setup({
    syncTestCaseSyncProfile: async id => { calls.push(id); return { data: { status: 'queued', task_id: 'claim-1' } } },
    getTestCaseSyncProfile: () => detail.promise,
    listTestCaseSyncProfiles: async params => {
      pages.push(plain(params))
      return { data: { count: 150, results: [{ id: 80, name: 'Current page row' }] } }
    },
  })
  instance.state.currentPage.value = 3
  instance.state.pageSize.value = 50
  const syncing = instance.state.sync({ id: 7 })
  await flush()
  await instance.state.sync({ id: 8 })
  detail.resolve({ data: { id: 7, sync_status: 'completed', sync_task_id: 'claim-1', sync_result: { files: 1 } } })
  await syncing
  assert.deepEqual(calls, [7])
  assert.deepEqual(pages, [{ page: 3, page_size: 50 }])
  assert.equal(instance.state.currentPage.value, 3)
  assert.deepEqual(plain(instance.state.profiles.value), [{ id: 80, name: 'Current page row' }])
})

test('queued, running and locally submitted profiles cannot be deleted', async () => {
  const deleted = []
  const instance = await setup({ deleteTestCaseSyncProfile: async id => { deleted.push(id) } })
  for (const status of ['queued', 'running']) {
    await instance.state.remove({ id: 7, name: 'Active source', sync_status: status })
  }
  instance.state.syncing.value = 7
  await instance.state.remove({ id: 7, name: 'Submitting source', sync_status: 'idle' })
  assert.deepEqual(deleted, [])
  assert.equal(instance.messages.filter(message => message.type === 'warning').length, 3)
})
