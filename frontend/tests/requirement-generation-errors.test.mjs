import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { deferred, loadSfcSetup } from './helpers/sfc-setup.mjs'

const instances = []
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

async function setup(api, clearedIntervals = []) {
  const instance = await loadSfcSetup('src/views/requirement-analysis/RequirementAnalysisView.vue', {
    '@/utils/api': { default: api },
    xlsx: { utils: {}, writeFile() {} },
    '@/stores/user': { useUserStore: () => ({}) },
  }, {
    clearInterval: handle => clearedIntervals.push(handle),
    console: { error() {}, log() {}, warn() {} },
  })
  instances.push(instance)
  return instance
}

const view = (taskId = 'TASK_6762AAEE') => ({
  currentTaskId: taskId,
  generationError: '',
  isGenerating: true,
  eventSource: { closed: false, close() { this.closed = true } },
  pollInterval: { id: 1 },
  $t: key => key,
})

test('SSE failure loads and persistently displays the task error details', async () => {
  const requests = []
  const cleared = []
  const instance = await setup({
    get: async url => {
      requests.push(url)
      return { data: { status: 'failed', error_message: 'Writer API request timed out' } }
    },
  }, cleared)
  const state = view()

  await instance.component.methods.handleGenerationError.call(state)

  assert.deepEqual(requests, ['/requirement-analysis/testcase-generation/TASK_6762AAEE/progress/'])
  assert.equal(state.generationError, 'Writer API request timed out')
  assert.equal(state.isGenerating, false)
  assert.equal(state.eventSource, null)
  assert.equal(state.pollInterval, null)
  assert.equal(cleared.length, 1)
})

test('failed progress lookup falls back to the generic generation error', async () => {
  const instance = await setup({ get: async () => { throw new Error('progress unavailable') } })
  const state = view()

  await instance.component.methods.handleGenerationError.call(state)

  assert.equal(state.generationError, 'requirementAnalysis.unknownError')
})

test('a late failure lookup cannot overwrite a newer generation', async () => {
  const waiting = deferred()
  const instance = await setup({ get: () => waiting.promise })
  const state = view()
  const failedTask = instance.component.methods.handleGenerationError.call(state)
  state.currentTaskId = 'TASK_NEWER'
  waiting.resolve({ data: { error_message: 'stale provider error' } })

  await failedTask

  assert.equal(state.generationError, '')
})
