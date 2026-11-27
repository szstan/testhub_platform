import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'

const instances = []
afterEach(() => instances.splice(0).forEach(({ instance, state }) => {
  instance.component.beforeUnmount.call(state)
  instance.dispose()
}))

const ready = { configured: true, enabled: true, name: 'Configured model' }
const config = (autoReview = false) => ({
  writer_model: ready,
  writer_prompt: ready,
  reviewer_model: autoReview ? ready : { configured: false, enabled: false, required: false },
  reviewer_prompt: autoReview ? ready : { configured: false, enabled: false, required: false },
  generation_config: { configured: true, enable_auto_review: autoReview, default_output_mode: 'complete' },
})
const canonicalCase = {
  case_id: 'TC-001', title: '验证建议名称包含 | 字符', preconditions: '',
  steps: ['输入 a|b', '提交'], expected_result: '保留原始内容', priority: 'P1',
}

async function setup(api = {}, overrides = {}, globals = {}, xlsx = {}) {
  const intervals = []
  const streams = []
  const storage = new Map()
  class MockEventSource {
    constructor(url) { this.url = url; this.closed = false; streams.push(this) }
    close() { this.closed = true }
  }
  const instance = await loadSfcSetup('src/views/requirement-analysis/RequirementAnalysisView.vue', {
    '@/utils/api': { default: { get: async () => ({ data: { status: 'generating', progress: 30 } }), ...api } },
    '@/stores/user': { useUserStore: () => ({ user: { id: 42 } }) },
    '@/utils/safe-html.js': { renderSafeMarkdown: content => content },
    xlsx: { utils: {}, writeFile() {}, ...xlsx },
  }, {
    FormData, Date,
    window: { location: { origin: 'http://testhub.local' } },
    EventSource: MockEventSource,
    setInterval: fn => { const handle = { fn, cleared: false }; intervals.push(handle); return handle },
    clearInterval: handle => { handle.cleared = true },
    sessionStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    ...globals,
  })
  const state = {
    ...instance.component.data(),
    $t: key => key,
    $refs: {},
    $route: { query: {} },
    $router: { push() {} },
    configStatus: config(), checkingConfig: false, loadingProjects: false, showReviewStep: false,
    selectedProject: 7,
    manualInput: { title: '验证码登录', description: '验证码 5 分钟有效，连续输错 5 次后锁定 10 分钟。' },
    globalOutputMode: 'complete',
    ...overrides,
  }
  for (const [name, method] of Object.entries(instance.component.methods)) state[name] = method.bind(state)
  for (const [name, getter] of Object.entries(instance.component.computed)) Object.defineProperty(state, name, { get: () => getter.call(state), configurable: true })
  instances.push({ instance, state })
  return { instance, state, intervals, streams, storage }
}

function running(state, taskId = 'TASK_CURRENT') {
  state.currentTaskId = taskId
  state.isGenerating = true
  state.taskStatus = 'generating'
  state.runRevision = 1
}

test('disabled auto-review requires only writer setup and honors the default output mode', async () => {
  const { state } = await setup({ get: async () => ({ data: config(false) }) }, { checkingConfig: true, showReviewStep: true, globalOutputMode: 'stream' })
  await state.checkConfigStatus()
  assert.equal(state.configReady, true)
  assert.equal(state.showReviewStep, false)
  assert.equal(state.configItems.length, 2)
  assert.equal(state.globalOutputMode, 'complete')
  assert.equal(state.canSubmit, true)
})

test('enabled auto-review blocks submission when its model is missing', async () => {
  const body = config(true)
  body.reviewer_model = { configured: false, enabled: false }
  const { state } = await setup({ get: async () => ({ data: body }) })
  await state.checkConfigStatus()
  assert.equal(state.configReady, false)
  assert.equal(state.canSubmit, false)
})

test('refreshing setup preserves an explicitly chosen display mode', async () => {
  const { state } = await setup({ get: async () => ({ data: config(false) }) }, { outputModeTouched: true, globalOutputMode: 'stream' })
  await state.checkConfigStatus()
  assert.equal(state.globalOutputMode, 'stream')
})

test('generation requires project and accepts substantial requirements beyond the old 2000 limit', async () => {
  const { state } = await setup()
  state.selectedProject = ''
  assert.equal(state.canSubmit, false)
  assert.equal(state.submitHint, 'requirementAnalysis.selectProjectFirst')
  state.selectedProject = 7
  state.manualInput.description = '需求'.repeat(3000)
  assert.equal(state.canSubmit, true)
  state.manualInput.description = 'x'.repeat(50001)
  assert.equal(state.canSubmit, false)
  assert.equal(state.submitHint, 'requirementAnalysis.descriptionTooLong')
})

test('project choices load all pages and do not silently accept a partial list', async () => {
  const pages = []
  const { state } = await setup({ get: async (url, options) => {
    pages.push(options.params.page)
    return { data: options.params.page === 1 ? { results: [{ id: 1 }], next: 'page2' } : { results: [{ id: 2 }], next: null } }
  } })
  await state.loadProjects()
  assert.deepEqual(pages, [1, 2])
  assert.deepEqual(plain(state.projects).map(project => project.id), [1, 2])
  assert.equal(state.projectsError, '')
})

test('file validation rejects legacy DOC, empty and oversized files before uploading', async () => {
  const { state, instance } = await setup()
  for (const file of [{ name: 'legacy.doc', size: 10 }, { name: 'huge.pdf', size: 10 * 1024 * 1024 + 1 }, { name: 'empty.md', size: 0 }]) state.handleFileSelect({ target: { files: [file] } })
  assert.equal(state.selectedFile, null)
  assert.equal(instance.messages.filter(message => message.type === 'error').length, 3)
  const boundary = { name: 'valid.DOCX', size: 10 * 1024 * 1024 }
  state.handleFileSelect({ target: { files: [boundary] } })
  assert.equal(state.selectedFile, boundary)
  assert.doesNotThrow(() => state.removeFile())
  assert.equal(state.selectedFile, null)
})

test('upload and extraction share a submission lock, so a double click creates only one task', async () => {
  const uploading = deferred()
  const posts = []
  const { state } = await setup({
    post: async (url, data) => { posts.push({ url, data }); return url.includes('/documents/') ? uploading.promise : { data: { task_id: 'TASK_DOCUMENT' } } },
    get: async url => ({ data: url.includes('extract_text') ? { extracted_text: '文档的原始业务规则' } : { status: 'pending', progress: 0 } }),
  }, { inputSource: 'document', selectedFile: new File(['requirements'], 'requirements.txt') })
  const first = state.submitGeneration()
  await state.submitGeneration()
  assert.equal(posts.length, 1)
  assert.equal(state.isSubmitting, true)
  uploading.resolve({ data: { id: 9 } })
  await first
  assert.equal(posts.length, 2)
  assert.equal(posts[1].data.project, 7)
  assert.equal(posts[1].data.use_reviewer_model, false)
  assert.equal(state.currentTaskId, 'TASK_DOCUMENT')
})

test('retrying extraction reuses the uploaded document and never generates from empty text', async () => {
  let uploads = 0
  let generations = 0
  const { state } = await setup({
    post: async url => { if (url.includes('/documents/')) uploads++; else generations++; return { data: { id: 9 } } },
    get: async () => ({ data: { extracted_text: '' } }),
  }, { inputSource: 'document', selectedFile: new File(['x'], 'requirements.txt') })
  await state.submitGeneration()
  await state.submitGeneration()
  assert.equal(uploads, 1)
  assert.equal(generations, 0)
  assert.equal(state.isSubmitting, false)
  assert.equal(state.generationError, 'requirementAnalysis.extractionFailed')
})

test('serializer field errors remain visible instead of becoming a generic request error', async () => {
  const { state } = await setup({ post: async () => { throw { response: { status: 400, data: { project: ['项目不能为空'] } } } } })
  await state.submitGeneration()
  assert.equal(state.generationError, '项目不能为空')
  assert.equal(state.isGenerating, false)
})

test('cancel posts to the server, closes the stream, and clears polling', async () => {
  const posts = []
  const { state, streams, intervals } = await setup({ post: async url => { posts.push(url); return { data: {} } } })
  running(state)
  state.startStreamingProgressWithTicket('test-ticket')
  state.pollInterval = { cleared: false }
  const handle = state.pollInterval
  await state.cancelGeneration()
  assert.deepEqual(posts, ['/requirement-analysis/testcase-generation/TASK_CURRENT/cancel/'])
  assert.equal(streams[0].closed, true)
  assert.equal(handle.cleared, true)
  assert.equal(state.taskStatus, 'cancelled')
  assert.equal(state.isGenerating, false)
  assert.equal(intervals.length, 0)
})

test('cancellation failure never reports the running task as cancelled', async () => {
  const { state, instance } = await setup({ post: async () => { throw new Error('network unavailable') } })
  running(state)
  await state.cancelGeneration()
  assert.equal(state.isGenerating, true)
  assert.notEqual(state.taskStatus, 'cancelled')
  assert.equal(instance.messages.some(message => message.type === 'success'), false)
})

test('a pending progress response cannot complete a task after successful cancellation', async () => {
  const progress = deferred()
  const { state } = await setup({ post: async () => ({ data: {} }), get: () => progress.promise })
  running(state)
  const checking = state.pollTask(state.currentTaskId, state.runRevision)
  await state.cancelGeneration()
  progress.resolve({ data: { status: 'completed', final_test_cases: 'stale' } })
  await checking
  assert.equal(state.taskStatus, 'cancelled')
  assert.equal(state.showResults, false)
})

test('an old completed detail response cannot overwrite a newer task', async () => {
  const result = deferred()
  const { state } = await setup({ get: () => result.promise })
  running(state, 'TASK_OLD')
  const loading = state.fetchFinalResult()
  state.currentTaskId = 'TASK_NEW'
  state.runRevision++
  result.resolve({ data: { status: 'completed', final_test_cases: 'stale' } })
  await loading
  assert.equal(state.showResults, false)
  assert.equal(state.finalTestCases, '')
})

test('late SSE callbacks cannot append content to a newer task', async () => {
  const { state, streams } = await setup()
  running(state, 'TASK_OLD')
  state.startStreamingProgressWithTicket('test-ticket')
  const callback = streams[0].onmessage
  state.currentTaskId = 'TASK_NEW'
  state.runRevision++
  callback({ data: JSON.stringify({ type: 'content', content: 'stale draft' }) })
  assert.equal(state.streamedContent, '')
})

test('stream errors switch to one polling loop without replaying a ticket', async () => {
  const { state, streams, intervals } = await setup()
  running(state)
  state.startStreamingProgressWithTicket('test-ticket')
  const failure = streams[0].onerror
  failure()
  failure()
  assert.equal(streams[0].closed, true)
  assert.equal(intervals.length, 1)
  assert.equal(streams.length, 1)
})

test('polling failures persist the provider error and preserve the partial draft', async () => {
  const { state } = await setup({ get: async () => ({ data: { status: 'failed', error_message: 'review timed out', generated_test_cases: 'partial draft' } }) })
  running(state)
  await state.pollTask(state.currentTaskId, state.runRevision)
  assert.equal(state.generationError, 'review timed out')
  assert.equal(state.streamedContent, 'partial draft')
  assert.equal(state.showResults, false)
  assert.equal(state.isGenerating, false)
})

test('a done notification cannot turn a cancelled task into successful results', async () => {
  const { state } = await setup({ get: async () => ({ data: { status: 'cancelled' } }) })
  running(state)
  await state.fetchFinalResult()
  assert.equal(state.taskStatus, 'cancelled')
  assert.equal(state.showResults, false)
})

test('completed results use canonical cases and select the final tab', async () => {
  const { state } = await setup({ get: async () => ({ data: { status: 'completed', canonical_cases: [canonicalCase], final_test_cases: 'final content' } }) })
  running(state)
  await state.fetchFinalResult()
  assert.equal(state.showResults, true)
  assert.equal(state.contentTab, 'final')
  assert.equal(state.canonicalCases.length, 1)
  assert.equal(state.progress, 100)
})

test('Excel export preserves empty columns, pipes and text containing recommendation keywords', async () => {
  let sheetRows
  let exported = false
  const { state } = await setup({}, { generationResult: { canonical_cases: [canonicalCase] } }, {}, {
    utils: { book_new: () => ({}), aoa_to_sheet: rows => { sheetRows = rows; return {} }, book_append_sheet() {} },
    writeFile() { exported = true },
  })
  state.downloadTestCases()
  assert.equal(exported, true)
  assert.deepEqual(plain(sheetRows[1]), ['TC-001', canonicalCase.title, '', '输入 a|b\n提交', canonicalCase.expected_result, 'P1'])
})

test('double import clicks submit once and imported results disable later imports', async () => {
  const saving = deferred()
  let requests = 0
  const { state } = await setup({ post: async () => { requests++; return saving.promise } }, { showResults: true, generationResult: { canonical_cases: [canonicalCase] } })
  state.currentTaskId = 'TASK_COMPLETE'
  const first = state.saveToTestCaseRecords()
  await state.saveToTestCaseRecords()
  assert.equal(requests, 1)
  saving.resolve({ data: { imported_count: 1, adopted_case_ids: [canonicalCase.case_id], remaining_count: 0 } })
  await first
  await state.saveToTestCaseRecords()
  assert.equal(requests, 1)
  assert.equal(state.isSaved, true)
})

test('partially adopted results can import remaining cases even when the saved flag is already true', async () => {
  const secondCase = { ...canonicalCase, case_id: 'TC-002', title: 'Second case' }
  const requests = []
  const { state } = await setup({ post: async url => {
    requests.push(url)
    return { data: { imported_count: 1, adopted_case_ids: ['TC-001', 'TC-002'], remaining_count: 0 } }
  } }, {
    currentTaskId: 'TASK_PARTIAL', showResults: true,
    generationResult: { canonical_cases: [canonicalCase, secondCase], adopted_case_ids: ['TC-001'], is_saved_to_records: true },
  })
  assert.equal(state.isSaved, false)
  assert.deepEqual(plain(state.remainingCases).map(testCase => testCase.case_id), ['TC-002'])
  await state.saveToTestCaseRecords()
  assert.deepEqual(requests, ['/requirement-analysis/testcase-generation/TASK_PARTIAL/save_to_records/'])
  assert.equal(state.remainingCases.length, 0)
  assert.equal(state.isSaved, true)
  await state.saveToTestCaseRecords()
  assert.equal(requests.length, 1)
})

test('adopted IDs outside the current result cannot hide cases that remain to import', async () => {
  const { state } = await setup({}, {
    generationResult: { canonical_cases: [canonicalCase, { ...canonicalCase, case_id: 'TC-002' }], adopted_case_ids: ['UNKNOWN', 'TC-001', 'TC-001'], is_saved_to_records: true },
  })
  assert.equal(state.remainingCases.length, 1)
  assert.equal(state.isSaved, false)
})

test('legacy saved results without IDs reconcile through the idempotent server import', async () => {
  let requests = 0
  const { state } = await setup({ post: async () => {
    requests++
    return { data: { already_saved: true, imported_count: 0, adopted_case_ids: ['TC-001'], remaining_count: 0 } }
  } }, { currentTaskId: 'TASK_LEGACY', showResults: true, generationResult: { canonical_cases: [canonicalCase], is_saved_to_records: true } })
  assert.equal(state.isSaved, false)
  await state.saveToTestCaseRecords()
  assert.equal(requests, 1)
  assert.equal(state.isSaved, true)
})

test('a restored 60000-character document retries from saved text without re-uploading or adding a title', async () => {
  const body = 'x'.repeat(60000)
  const dto = { task_id: 'TASK_LONG', title: 'Long requirement', project: 7, requirement_text: body, status: 'failed', error_message: 'provider timeout', output_mode: 'complete' }
  const posts = []
  const { state } = await setup({
    get: async url => ({ data: url.includes('/config/check/') ? config(false) : url.endsWith('/progress/') ? { status: 'pending', progress: 0 } : dto }),
    post: async (url, data) => { posts.push({ url, data }); return { data: { task_id: 'TASK_RETRY_LONG' } } },
  }, { inputSource: 'document', selectedFile: new File(['old content'], 'old.txt') })
  await state.restoreActiveTask(dto.task_id)
  state.resetGeneration()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(state.inputSource, 'manual')
  assert.equal(state.selectedFile, null)
  assert.equal(state.maxInputLength, 100000)
  assert.equal(state.canSubmit, true)
  assert.equal(state.submitHint, 'requirementAnalysis.readyHint')
  await state.submitGeneration()
  assert.equal(posts.length, 1)
  assert.equal(posts[0].url, '/requirement-analysis/testcase-generation/generate/')
  assert.equal(posts[0].data.requirement_text, body)
})

test('restoring a titled request unwraps exactly one envelope and uses the edited title once', async () => {
  const body = 'business rule\n'.repeat(4500).trim()
  const dto = { task_id: 'TASK_WRAPPED', title: 'Original title', project: 7, requirement_text: `Requirement Title: Original title\n\n${body}`, status: 'failed', error_message: 'timeout', output_mode: 'complete' }
  let submitted
  const { state } = await setup({
    get: async url => ({ data: url.includes('/config/check/') ? config(false) : url.endsWith('/progress/') ? { status: 'pending', progress: 0 } : dto }),
    post: async (url, data) => { submitted = data; return { data: { task_id: 'TASK_EDITED' } } },
  })
  await state.restoreActiveTask(dto.task_id)
  state.resetGeneration()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(state.manualInput.description, body)
  state.manualInput.title = 'Edited title'
  assert.equal(state.canSubmit, true)
  await state.submitGeneration()
  assert.equal(submitted.requirement_text, `Requirement Title: Edited title\n\n${body}`)
  assert.equal(submitted.requirement_text.includes('Original title'), false)
})

test('plain saved requests can retry at 100000 characters and clearly reject one character over budget', async () => {
  const body = 'x'.repeat(100000)
  const dto = { task_id: 'TASK_LIMIT', title: 'At limit', project: 7, requirement_text: body, status: 'failed', error_message: 'timeout', output_mode: 'complete' }
  const submissions = []
  const { state } = await setup({
    get: async url => ({ data: url.includes('/config/check/') ? config(false) : url.endsWith('/progress/') ? { status: 'pending', progress: 0 } : dto }),
    post: async (url, data) => { submissions.push(data); return { data: { task_id: 'TASK_LIMIT_RETRY' } } },
  })
  await state.restoreActiveTask(dto.task_id)
  state.resetGeneration()
  await new Promise(resolve => setImmediate(resolve))
  state.manualInput.description += 'x'
  assert.equal(state.canSubmit, false)
  assert.equal(state.submitHint, 'requirementAnalysis.descriptionTooLong')
  await state.submitGeneration()
  assert.equal(submissions.length, 0)
  state.manualInput.description = body
  assert.equal(state.canSubmit, true)
  await state.submitGeneration()
  assert.equal(submissions[0].requirement_text, body)
})

test('restored titled input reserves exactly the title envelope and recalculates the budget after title edits', async () => {
  const prefix = '需求标题: 边界需求\n\n'
  const body = 'x'.repeat(100000 - prefix.length)
  const dto = { task_id: 'TASK_TITLED_LIMIT', title: '边界需求', project: 7, requirement_text: prefix + body, status: 'failed', error_message: 'timeout', output_mode: 'complete' }
  const { state } = await setup({ get: async url => ({ data: url.includes('/config/check/') ? config(false) : dto }) })
  await state.restoreActiveTask(dto.task_id)
  state.resetGeneration()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(state.maxInputLength, body.length)
  assert.equal(state.canSubmit, true)
  assert.equal(state.buildRequirementText(state.manualInput.title, state.manualInput.description), dto.requirement_text)
  state.manualInput.title += '更长'
  assert.equal(state.canSubmit, false)
  assert.equal(state.submitHint, 'requirementAnalysis.descriptionTooLong')
  state.manualInput.description = body.slice(0, -2)
  assert.equal(state.canSubmit, true)
  assert.equal(state.buildRequirementText(state.manualInput.title, state.manualInput.description).length, 100000)
})

test('a lost submission response recovers the committed task by the persisted request ID', async () => {
  let requestId
  const { state, storage } = await setup({
    post: async (url, body) => { requestId = body.client_request_id; throw new Error('response lost') },
    get: async (url, options) => {
      if (url.endsWith('/submission/')) {
        assert.equal(options.params.client_request_id, requestId)
        return { data: { task_id: 'TASK_COMMITTED' } }
      }
      return { data: { task_id: 'TASK_COMMITTED', title: 'Restored task', project: 7, status: 'generating', progress: 30, reviewer_model_config: null, output_mode: 'complete' } }
    },
  }, { storageKey: 'request-owner' })
  await state.submitGeneration()
  assert.match(requestId, /^[0-9a-f-]{36}$/i)
  assert.equal(state.currentTaskId, 'TASK_COMMITTED')
  assert.equal(state.isGenerating, true)
  assert.equal(state.isSubmitting, false)
  assert.equal(state.pendingSubmissionId, '')
  assert.equal(storage.get('request-owner'), 'TASK_COMMITTED')
  assert.equal(storage.has('request-owner:pending'), false)
})

test('an uncertain submission retries with the same request ID instead of creating another identity', async () => {
  const submittedIds = []
  const { state, storage } = await setup({
    post: async (url, body) => {
      submittedIds.push(body.client_request_id)
      if (submittedIds.length === 1) throw new Error('network unavailable')
      return { data: { task_id: 'TASK_RETRY', task: { reviewer_model_config: null, output_mode: 'complete' } } }
    },
    get: async url => {
      if (url.endsWith('/submission/')) throw { response: { status: 404 } }
      return { data: { status: 'pending', progress: 0 } }
    },
  }, { storageKey: 'request-retry' })
  await state.submitGeneration()
  assert.equal(state.pendingSubmissionId, submittedIds[0])
  assert.equal(storage.get('request-retry:pending'), submittedIds[0])
  assert.equal(state.monitoringError, 'requirementAnalysis.submissionUnconfirmed')
  assert.equal(state.generationError, '')
  await state.submitGeneration()
  assert.equal(submittedIds.length, 2)
  assert.equal(submittedIds[0], submittedIds[1])
  assert.equal(state.currentTaskId, 'TASK_RETRY')
  assert.equal(storage.has('request-retry:pending'), false)
})

test('leaving during submission retains its identity for recovery in a new component', async () => {
  const response = deferred()
  const first = await setup({ post: () => response.promise }, { storageKey: 'request-leave' })
  const submitting = first.state.submitGeneration()
  const requestId = first.storage.get('request-leave:pending')
  assert.ok(requestId)
  first.instance.component.beforeUnmount.call(first.state)
  response.resolve({ data: { task_id: 'TASK_LEFT_PAGE' } })
  await submitting
  assert.equal(first.storage.get('request-leave:pending'), requestId)
  const sharedStorage = {
    getItem: key => first.storage.get(key),
    setItem: (key, value) => first.storage.set(key, value),
    removeItem: key => first.storage.delete(key),
  }
  const second = await setup({ get: async url => ({ data: url.endsWith('/submission/')
    ? { task_id: 'TASK_LEFT_PAGE' }
    : { task_id: 'TASK_LEFT_PAGE', title: 'Recovered', project: 7, status: 'generating', progress: 30, output_mode: 'complete' } }) }, {
    storageKey: 'request-leave', pendingSubmissionId: requestId,
  }, { sessionStorage: sharedStorage })
  await second.state.restorePendingSubmission()
  assert.equal(second.state.currentTaskId, 'TASK_LEFT_PAGE')
  assert.equal(second.state.isGenerating, true)
  assert.equal(first.storage.has('request-leave:pending'), false)
})

test('a payload conflict resumes its original task rather than treating the conflict as generation failure', async () => {
  const { state } = await setup({
    post: async () => { throw { response: { status: 409, data: { task_id: 'TASK_ORIGINAL' } } } },
    get: async () => ({ data: { task_id: 'TASK_ORIGINAL', title: 'Original', project: 7, status: 'generating', progress: 30, output_mode: 'complete' } }),
  })
  await state.submitGeneration()
  assert.equal(state.currentTaskId, 'TASK_ORIGINAL')
  assert.equal(state.activeTitle, 'Original')
  assert.equal(state.generationError, '')
  assert.equal(state.isSubmitting, false)
})

test('the generation response controls the review steps even when the setup changed after loading', async () => {
  const { state } = await setup({ post: async () => ({ data: { task_id: 'TASK_POLICY', task: { reviewer_model_config: null, output_mode: 'complete' } } }) }, {
    configStatus: config(true), showReviewStep: true,
  })
  await state.submitGeneration()
  assert.equal(state.showReviewStep, false)
  assert.equal(state.contentTabs.some(tab => tab.key === 'review'), false)
})

test('LAN HTTP deployments can create a UUID without crypto.randomUUID', async () => {
  let requestId
  const { state } = await setup({ post: async (url, body) => {
    requestId = body.client_request_id
    return { data: { task_id: 'TASK_LAN' } }
  } }, {}, { crypto: { getRandomValues: bytes => bytes.fill(7) } })
  await state.submitGeneration()
  assert.match(requestId, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)
})

test('refresh restores a running task using snapshots and replaces any old timer', async () => {
  const { state, intervals } = await setup({ get: async () => ({ data: { title: 'Restored', requirement_text: 'source', project: 7, reviewer_model_config: null, status: 'revising', progress: 75, generated_test_cases: 'snapshot' } }) }, { storageKey: 'task-storage' })
  state.pollInterval = { cleared: false }
  const oldTimer = state.pollInterval
  await state.restoreActiveTask('TASK_RESTORE')
  assert.equal(oldTimer.cleared, true)
  assert.equal(state.isRestoring, false)
  assert.equal(state.currentTaskId, 'TASK_RESTORE')
  assert.equal(state.manualInput.title, 'Restored')
  assert.equal(state.streamedContent, 'snapshot')
  assert.equal(state.isGenerating, true)
  assert.equal(intervals.length, 1)
})

test('an inaccessible remembered task is cleared instead of being shown to another user', async () => {
  const { state, storage } = await setup({ get: async () => { throw { response: { status: 403 } } } }, { storageKey: 'user-specific-task' })
  storage.set('user-specific-task', 'TASK_PRIVATE')
  await state.restoreActiveTask()
  assert.equal(state.currentTaskId, null)
  assert.equal(storage.has('user-specific-task'), false)
  assert.equal(state.showResults, false)
})

test('returning to input clears a history task link so refresh cannot restore the old task', async () => {
  const routes = []
  const { state } = await setup({ get: async () => ({ data: config(false) }) }, {
    currentTaskId: 'TASK_OLD',
    $route: { query: { task: 'TASK_OLD', filter: 'mine' } },
    $router: { replace: route => routes.push(route) },
  })
  state.resetGeneration()
  assert.equal(routes.length, 1)
  assert.deepEqual(plain(routes[0].query), { filter: 'mine' })
})

test('starting another generation retains requirements without reloading the page', async () => {
  const { state } = await setup({ get: async () => ({ data: config(false) }) }, { currentTaskId: 'TASK_FINISHED', showResults: true })
  const original = plain(state.manualInput)
  state.resetGeneration()
  assert.deepEqual(plain(state.manualInput), original)
  assert.equal(state.currentTaskId, null)
  assert.equal(state.showResults, false)
})
