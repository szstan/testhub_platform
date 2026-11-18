import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
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

async function setupGeneratedTestCaseList(openedUrls = []) {
  const instance = await loadSfcSetup('src/views/requirement-analysis/GeneratedTestCaseList.vue', {
    '@/utils/api': { default: {} },
  }, {
    window: { open: url => openedUrls.push(url) },
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

test('failed generation task detail opens and displays the stored error', async () => {
  const openedUrls = []
  const instance = await setupGeneratedTestCaseList(openedUrls)
  const task = {
    task_id: 'TASK_FAILED',
    title: 'Failed generation',
    status: 'failed',
    error_message: 'Writer API request timed out',
  }
  const state = { selectedTaskDetail: null }

  instance.component.methods.viewTaskDetail.call(state, task)

  assert.equal(state.selectedTaskDetail, task)
  assert.deepEqual(openedUrls, [])
  const source = await readFile(new URL('../src/views/requirement-analysis/GeneratedTestCaseList.vue', import.meta.url), 'utf8')
  assert.match(source, /selectedTaskDetail\.error_message/)
})

test('completed generation task detail keeps its existing route', async () => {
  const openedUrls = []
  const instance = await setupGeneratedTestCaseList(openedUrls)
  const state = {
    $router: {
      resolve: ({ name, params }) => {
        assert.equal(name, 'TaskDetail')
        return { href: `/generated-testcases/${params.taskId}` }
      },
    },
  }

  instance.component.methods.viewTaskDetail.call(state, {
    task_id: 'TASK_COMPLETE',
    status: 'completed',
  })

  assert.deepEqual(openedUrls, ['/generated-testcases/TASK_COMPLETE'])
})
