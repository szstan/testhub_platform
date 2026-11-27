import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import { build } from 'esbuild'
import { effectScope, nextTick, watch } from 'vue'
import * as vue from 'vue'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'
import * as deviceExecution from '../src/utils/app-automation-device-execution.mjs'

const instances = []
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

class FakeFormData {
  constructor() { this.entries = [] }
  append(key, value) { this.entries.push([key, value]) }
}

async function loadSceneDataApi(request) {
  const filename = resolve(dirname(fileURLToPath(import.meta.url)), '../src/api/app-scene-data.js')
  const source = await readFile(filename, 'utf8')
  const bundle = await build({
    stdin: { contents: source, resolveDir: dirname(filename), sourcefile: filename, loader: 'js' },
    bundle: true, write: false, platform: 'node', format: 'cjs',
    plugins: [{
      name: 'scene-data-api-request',
      setup(builder) {
        builder.onResolve({ filter: /^\.\.\/utils\/api$/ }, () => ({ path: 'api', namespace: 'scene-data-api' }))
        builder.onLoad({ filter: /.*/, namespace: 'scene-data-api' }, () => ({
          contents: 'export default __sceneDataRequest', loader: 'js',
        }))
      },
    }],
  })
  const module = { exports: {} }
  runInNewContext(bundle.outputFiles[0].text, {
    module, exports: module.exports, __sceneDataRequest: request, console,
  }, { filename })
  return module.exports
}

async function setupPanel(api, { projectId = 8, userStore = vue.reactive({
  user: { id: 7 }, generation: 1, getSessionGeneration() { return this.generation },
}) } = {}) {
  const loaded = await loadSfcSetup('src/views/app-automation/components/SceneDataPanel.vue', {
    vue: {
      ...vue,
      watch: (source, callback, options) => {
        const value = source()
        return value == null || (Array.isArray(value) && value[0] == null)
          ? () => {}
          : watch(source, callback, options)
      },
      onBeforeUnmount: () => {},
    },
    '@/api/app-scene-data': {
      createProjectComponent: async () => ({ data: {} }),
      uploadSceneDataset: async () => ({ data: {} }),
      ...api,
    },
    '@/stores/user': { useUserStore: () => userStore },
  }, { FormData: FakeFormData })
  const emitted = []
  const props = vue.reactive({ projectId, getSteps: () => [{ type: 'wait', config: { timeout: 1 } }] })
  const scope = effectScope()
  const state = scope.run(() => loaded.component.setup(props, { expose() {}, emit: (...args) => emitted.push(args) }))
  instances.push({ dispose: () => { scope.stop(); loaded.dispose() } })
  await nextTick()
  await new Promise(resolve => setImmediate(resolve))
  return { state, emitted, props, userStore, loaded, messages: loaded.messages }
}

test('project panel uploads a bounded file and emits project versions without resolving secrets', async () => {
  const uploads = []
  const created = []
  const component = { id: 4, project: 8, name: 'Login', type: 'login_flow', version: '1', schema: { properties: {} } }
  const panel = await setupPanel({
    getAllSceneDatasets: async () => [{ id: 2, name: 'Accounts', version: 1, row_count: 2 }],
    getAllProjectComponents: async () => [component],
    uploadSceneDataset: async body => { uploads.push(body); return { data: {} } },
    createProjectComponent: async body => { created.push(body); return { data: {} } },
  })
  assert.equal(panel.state.datasets.value[0].row_count, 2)
  assert.equal(panel.state.components.value[0].type, 'login_flow')
  assert.ok(panel.emitted.some(([name, value]) => name === 'components-loaded' && value.length === 1))
  panel.state.datasetName.value = 'Accounts'
  panel.state.selectedFile.value = { name: 'accounts.csv', size: 25 }
  panel.state.sensitiveColumns.value = 'password,api_token'
  await panel.state.uploadDataset()
  assert.deepEqual(uploads[0].entries.map(([key]) => key), ['project', 'name', 'version', 'file', 'sensitive_columns'])
  assert.equal(uploads[0].entries.at(-1)[1], '["password","api_token"]')
  panel.state.componentName.value = 'Reusable wait'
  panel.state.componentType.value = 'wait_fixture'
  panel.state.parameterProperties.value = '{"label":{"type":"string"}}'
  panel.state.requiredParameters.value = 'label'
  await panel.state.saveComponent()
  assert.equal(created[0].project, 8)
  assert.deepEqual(plain(created[0].schema.required), ['label'])
  assert.equal(created[0].steps[0].type, 'wait')
})

test('nested editor creates a version-pinned project reference without copying its body', async () => {
  const loaded = await loadSfcSetup('src/views/app-automation/test-cases/NestedFlowEditor.vue', {
    vuedraggable: { default: {} },
    'element-plus': { ElMessage: { warning() {} } },
  })
  const emitted = []
  const scope = effectScope()
  const state = scope.run(() => loaded.component.setup({
    modelValue: [], label: 'Then', depth: 0, maxDepth: 12,
    components: [{ kind: 'custom', type: 'login_flow', version: '2', optionKey: 'login_flow@2',
                   name: 'Login v2', default_config: { user: 'fixture' } }],
    componentDefinitions: { 'login_flow@2': { schema: { properties: {} } } },
    fieldLabel: field => field, fieldOptions: () => [],
  }, { expose() {}, emit: (...args) => emitted.push(args) }))
  instances.push({ dispose: () => { scope.stop(); loaded.dispose() } })
  state.newStepType.value = 'login_flow@2'
  state.addStep()
  const step = emitted.at(-1)[1][0]
  assert.equal(step.kind, 'custom')
  assert.equal(step.version, '2')
  assert.equal(step.type, 'login_flow')
  assert.equal(step.steps, undefined)
})

async function setupCaseList(sceneApi, userStore) {
  const device = { id: 11, device_id: 'synthetic-serial', name: 'Device', status: 'online', locked_by: null }
  const api = {
    getTestCaseList: async () => ({ data: { results: [] } }), deleteTestCase: async () => ({}),
    executeTestCase: async () => ({}), batchExecuteTestCases: async () => ({}),
    getExecutionList: async () => ({ data: { results: [] } }), getExecutionDetail: async () => ({ data: {} }),
    stopExecution: async () => ({}), getPackageList: async () => ({ data: { results: [] } }),
    getAppProjects: async () => ({ data: { results: [] } }), getWsStatus: async () => ({ data: { websocket: false } }),
    getDeviceList: async () => ({ data: { results: [device] } }), getAppDevice: async () => ({ data: device }),
  }
  const instance = await loadSfcSetup('src/views/app-automation/test-cases/TestCaseList.vue', {
    'vue-router': { useRouter: () => ({ push() {} }) },
    '@element-plus/icons-vue': { Refresh: {}, Search: {} },
    '@/api/app-automation': api, '@/api/app-scene-data': sceneApi,
    '@/utils/app-automation-helpers': { getExecutionStatusType: () => '', getExecutionStatusText: () => '',
      getDisplayStatus: () => ({}), formatDateTime: value => value },
    '@/utils/websocket': { buildWebSocketUrl: () => '', issueWebSocketTicket: async () => '' },
    '@/utils/app-automation-report.mjs': { openAppExecutionReport: async () => ({ opened: true }) },
    '@/utils/app-automation-device-execution.mjs': deviceExecution,
    '@/stores/user': { useUserStore: () => userStore },
  }, { setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {} })
  instances.push(instance)
  instance.state.form.value.deviceId = 11
  return instance
}

test('dataset run uses captured account, selected row and the standard device lease', async () => {
  const calls = []
  const store = { user: { id: 7 }, generation: 1, getSessionGeneration() { return this.generation } }
  const instance = await setupCaseList({
    getAllSceneDatasets: async project => {
      assert.equal(project, 8)
      return [
        { id: 3, name: 'Rows', version: 1, row_count: 4 },
        { id: 4, name: 'Rows', version: 2, row_count: 3 },
      ]
    },
    executeSceneDataset: async (caseId, data) => { calls.push([caseId, data]); return { data: { success: true, data: { execution_ids: [] } } } },
  }, store)
  await instance.state.openDatasetDialog({ id: 5, project: 8 })
  assert.equal(instance.state.availableDatasets.value.length, 2)
  instance.state.selectedDatasetId.value = 3
  instance.state.datasetRowNumber.value = 2
  await instance.state.runDatasetCase()
  assert.equal(calls.length, 1)
  assert.deepEqual(plain(calls[0][1]), { dataset_id: 3, device_id: 'synthetic-serial', auto_lock: true,
    expected_user_id: 7, mode: 'single', row_index: 1 })
  await instance.state.openDatasetDialog({ id: 5, project: 8 })
  instance.state.selectedDatasetId.value = 3
  store.generation += 1
  await instance.state.runDatasetCase()
  assert.equal(calls.length, 1)
})

test('complete scene-data helpers request each numeric page and ignore server next URLs', async () => {
  const calls = []
  const api = await loadSceneDataApi(async config => {
    calls.push(config)
    const page = config.params.page
    if (config.url.endsWith('/scene-datasets/')) {
      return { data: page === 1
        ? { count: 3, next: 'https://untrusted.invalid/?page=999', results: [{ id: 1 }, { id: 2 }] }
        : { count: 3, next: null, results: [{ id: 3 }] } }
    }
    return { data: page === 1
      ? { count: 3, next: '/untrusted?page=999', results: [{ id: 11 }, { id: 12 }] }
      : { count: 3, next: null, results: [{ id: 13 }] } }
  })

  const [datasets, components] = await Promise.all([
    api.getAllSceneDatasets(8), api.getAllProjectComponents(8),
  ])

  assert.deepEqual(plain(datasets), [{ id: 1 }, { id: 2 }, { id: 3 }])
  assert.deepEqual(plain(components), [{ id: 11 }, { id: 12 }, { id: 13 }])
  for (const url of ['/app-automation/scene-datasets/', '/app-automation/project-components/']) {
    const pages = calls.filter(call => call.url === url)
    assert.deepEqual(pages.map(call => call.params.page), [1, 2])
    assert.ok(pages.every(call => call.params.project === 8 && call.params.page_size === 100))
  }
  assert.ok(calls.every(call => call.url.startsWith('/app-automation/')))
})

test('complete scene-data helper rejects page caps, incomplete counts, and failed next pages', async () => {
  const cappedCalls = []
  const cappedApi = await loadSceneDataApi(async config => {
    cappedCalls.push(config)
    return { data: { count: 1000, next: '/ignored', results: Array.from({ length: 100 }, (_, id) => ({ id })) } }
  })
  await assert.rejects(cappedApi.getAllSceneDatasets(8), /page limit/)
  assert.equal(cappedCalls.length, 10)
  assert.deepEqual(cappedCalls.map(call => call.params.page), Array.from({ length: 10 }, (_, index) => index + 1))

  const incompleteApi = await loadSceneDataApi(async () => ({
    data: { count: 2, next: null, results: [{ id: 1 }] },
  }))
  await assert.rejects(incompleteApi.getAllProjectComponents(8), /Incomplete/)

  const missingMetadataApi = await loadSceneDataApi(async () => ({ data: { results: [{ id: 1 }] } }))
  await assert.rejects(missingMetadataApi.getAllSceneDatasets(8), /pagination metadata/)

  const changingCountApi = await loadSceneDataApi(async config => ({
    data: config.params.page === 1
      ? { count: 2, next: '/ignored', results: [{ id: 1 }] }
      : { count: 3, next: null, results: [{ id: 2 }, { id: 3 }] },
  }))
  await assert.rejects(changingCountApi.getAllSceneDatasets(8), /count changed/)

  const failedPages = []
  const failedApi = await loadSceneDataApi(async config => {
    failedPages.push(config.params.page)
    if (config.params.page === 1) return { data: { count: 2, next: '/ignored', results: [{ id: 1 }] } }
    throw new Error('second page failed')
  })
  await assert.rejects(failedApi.getAllSceneDatasets(8), /second page failed/)
  assert.deepEqual(failedPages, [1, 2])
})

test('panel discards responses from prior projects and accounts', async () => {
  const initialDatasets = deferred()
  const initialComponents = deferred()
  const userStore = vue.reactive({
    user: { id: 7 }, generation: 1, getSessionGeneration() { return this.generation },
  })
  const calls = []
  const panel = await setupPanel({
    getAllSceneDatasets: async project => {
      calls.push(['dataset', project, userStore.user.id])
      if (project === 8 && userStore.user.id === 7) return initialDatasets.promise
      return [{ id: `${userStore.generation}-${project}` }]
    },
    getAllProjectComponents: async project => {
      calls.push(['component', project, userStore.user.id])
      if (project === 8 && userStore.user.id === 7) return initialComponents.promise
      return [{ id: `${userStore.generation}-${project}` }]
    },
  }, { userStore })

  panel.props.projectId = 9
  await nextTick()
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(plain(panel.state.datasets.value), [{ id: '1-9' }])
  assert.deepEqual(plain(panel.state.components.value), [{ id: '1-9' }])

  userStore.user = { id: 7 }
  userStore.generation += 1
  await nextTick()
  await new Promise(resolve => setImmediate(resolve))
  initialDatasets.resolve([{ id: 'stale-project-8' }])
  initialComponents.resolve([{ id: 'stale-project-8' }])
  await new Promise(resolve => setImmediate(resolve))

  assert.deepEqual(plain(panel.state.datasets.value), [{ id: '2-9' }])
  assert.deepEqual(plain(panel.state.components.value), [{ id: '2-9' }])
  assert.equal(calls.length, 6)
  assert.ok(calls.every(([, project]) => project === 8 || project === 9))
})

test('panel clears list and reports pagination failures instead of showing partial data', async () => {
  const panel = await setupPanel({
    getAllSceneDatasets: async () => { throw new Error('page limit') },
    getAllProjectComponents: async () => [{ id: 1 }],
  })
  assert.deepEqual(plain(panel.state.datasets.value), [])
  assert.deepEqual(plain(panel.state.components.value), [])
  assert.ok(panel.messages.some(message => message.type === 'error' && message.text === 'appAutomation.sceneData.loadFailed'))
})

test('dataset selector ignores older project and account responses', async () => {
  const oldProject = deferred()
  const accountResponse = deferred()
  const store = { user: { id: 7 }, generation: 1, getSessionGeneration() { return this.generation } }
  const projects = []
  let holdAccountResponse = false
  const instance = await setupCaseList({
    getAllSceneDatasets: project => {
      projects.push(project)
      if (project === 8) return oldProject.promise
      if (holdAccountResponse) return accountResponse.promise
      return Promise.resolve([{ id: 90, project }])
    },
    executeSceneDataset: async () => ({}),
  }, store)

  const oldLoad = instance.state.openDatasetDialog({ id: 5, project: 8 })
  await instance.state.openDatasetDialog({ id: 6, project: 9 })
  oldProject.resolve([{ id: 80, project: 8 }])
  await oldLoad
  assert.deepEqual(plain(instance.state.availableDatasets.value), [{ id: 90, project: 9 }])

  holdAccountResponse = true
  const oldAccountLoad = instance.state.openDatasetDialog({ id: 6, project: 9 })
  store.generation += 1
  store.user = { id: 10 }
  accountResponse.resolve([{ id: 910, project: 9 }])
  await oldAccountLoad
  assert.deepEqual(plain(instance.state.availableDatasets.value), [])
  assert.equal(instance.state.datasetLoading.value, false)
  assert.deepEqual(projects, [8, 9, 9])
})
