import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { nextTick, reactive } from 'vue'
import { loadSfcSetup } from './helpers/sfc-setup.mjs'

const instances = []
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

const failureDetail = overrides => ({
  id: 12,
  test_case: 44,
  case_revision: '2026-12-01T10:00:00Z',
  engine: 'appium',
  status: 'error',
  execution_state: 'device_recovery_pending',
  device_action_pending: true,
  stop_requested: false,
  flow_failure: {
    code: 'element_not_found',
    classification: 'locator',
    step_name: 'Tap Continue',
    step_path: 'ui_flow[2].if_steps[1]',
    step_index: 1,
    case_revision: '2026-11-30T10:00:00Z',
    current_case_revision: '2026-12-01T10:00:00Z',
    case_revision_changed: true,
    duration_ms: 125,
    match_count: 0,
    has_screenshot: true,
  },
  ...overrides,
})

async function setupExecutionDiagnostics({ detail, detailRequest } = {}) {
  const calls = { details: [], screenshots: [], routes: [], messages: [], revoked: [] }
  const store = reactive({
    user: { id: 7 },
    accessToken: 'first-token',
    generation: 3,
    getSessionGeneration() { return this.generation },
  })
  const route = reactive({ query: {} })
  const loaded = await loadSfcSetup('src/views/app-automation/executions/ExecutionList.vue', {
    'vue-router': {
      useRouter: () => ({ push: value => calls.routes.push(value) }),
      useRoute: () => route,
    },
    'element-plus': {
      ElMessage: Object.fromEntries(['error', 'warning', 'success'].map(type => [type, text => calls.messages.push([type, text])])),
      ElMessageBox: { confirm: async () => true },
    },
    '@element-plus/icons-vue': { Search: {}, Refresh: {} },
    '@/api/app-automation': {
      getExecutionList: async () => ({ data: { results: [] } }),
      getExecutionDetail: async id => {
        calls.details.push(id)
        return detailRequest ? detailRequest(id) : { data: detail || failureDetail({}) }
      },
      stopExecution: async () => ({ data: { success: true } }),
      getAppProjects: async () => ({ data: { results: [] } }),
    },
    '@/api/app-step-diagnostics': {
      getFlowFailureScreenshot: async id => {
        calls.screenshots.push(id)
        return { data: 'synthetic-png' }
      },
    },
    '@/utils/app-automation-report.mjs': { openAppExecutionReport: async () => ({ opened: true }) },
    '@/utils/app-automation-helpers': {
      getExecutionStatusType: () => 'info',
      getExecutionStatusText: value => value,
      getDisplayStatus: () => ({ type: 'info', text: '' }),
      formatDateTime: value => value,
    },
    '@/stores/user': { useUserStore: () => store },
  }, {
    URL: {
      createObjectURL: value => `blob:${value}`,
      revokeObjectURL: value => calls.revoked.push(value),
    },
    setTimeout: () => 0,
    clearTimeout() {},
    setInterval: () => 0,
    clearInterval() {},
  })
  instances.push(loaded)
  return { ...loaded, calls, store, route }
}

test('execution detail fetches protected evidence and routes changed revisions without a wrong step index', async () => {
  const view = await setupExecutionDiagnostics()
  await view.state.viewDiagnostics({ id: 12 })

  assert.deepEqual(view.calls.details, [12])
  assert.deepEqual(view.calls.screenshots, [12])
  assert.equal(view.state.diagnosticExecution.value.flow_failure.code, 'element_not_found')
  assert.equal(view.state.diagnosticScreenshotUrl.value, 'blob:synthetic-png')
  assert.doesNotMatch(JSON.stringify(view.state.diagnosticExecution.value), /screenshot_path|media_root|credential/i)

  view.state.openFailureStep()
  assert.deepEqual(JSON.parse(JSON.stringify(view.calls.routes)), [{
    name: 'AppSceneBuilder',
    query: {
      case_id: '44',
      execution_id: '12',
      step_path: 'ui_flow[2].if_steps[1]',
      case_revision: '2026-11-30T10:00:00Z',
    },
  }])

  view.state.clearDiagnostic()
  assert.deepEqual(view.calls.revoked, ['blob:synthetic-png'])
})

test('execution detail selects the recorded step only when its case revision still matches', async () => {
  const view = await setupExecutionDiagnostics({
    detail: failureDetail({
      flow_failure: {
        ...failureDetail({}).flow_failure,
        case_revision: 'same-revision',
        case_revision_changed: false,
      },
    }),
  })
  await view.state.viewDiagnostics({ id: 12 })
  view.state.openFailureStep()

  assert.equal(view.calls.routes[0].query.step_index, '1')
})

test('account replacement clears in-flight execution details before they can display', async () => {
  let resolveDetail
  const pending = new Promise(resolve => { resolveDetail = resolve })
  const view = await setupExecutionDiagnostics({ detailRequest: () => pending })
  const request = view.state.viewDiagnostics({ id: 12 })
  view.store.accessToken = 'second-token'
  view.store.generation = 4
  await nextTick()
  resolveDetail({ data: failureDetail({}) })
  await request

  assert.equal(view.state.diagnosticExecution.value, null)
  assert.equal(view.state.diagnosticScreenshotUrl.value, '')
  assert.deepEqual(view.calls.screenshots, [])
})
