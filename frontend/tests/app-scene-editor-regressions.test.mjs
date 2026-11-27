import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import * as vue from 'vue'
import { effectScope, nextTick, reactive } from 'vue'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'

const instances = []
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

const apiNames = [
  'getComponents', 'getCustomComponents', 'createCustomComponent', 'updateCustomComponent',
  'deleteCustomComponent', 'importComponentPackage', 'exportComponentPackage', 'getTestCaseDetail',
  'createTestCase', 'updateTestCase', 'getAppElementList', 'getAppImageCategories', 'getDeviceList',
  'getAppDevice', 'acquireRecorderLease', 'releaseRecorderLease', 'heartbeatDevice',
  'captureDeviceScreenshot', 'recordDeviceInteraction', 'getAppProjects',
]

async function loadBuilder({ detail = null, userId = 7, createTestCase, updateTestCase, getTestCaseDetail: loadDetail,
  getComponents: loadBaseComponents, getCustomComponents: loadCustomComponents, getAllAppDevices: loadDevices,
  routeCaseId, autoMount = true } = {}) {
  const writes = []
  const session = { generation: 1 }
  const userStore = reactive({
    user: { id: userId },
    accessToken: 'fixture-token',
    getSessionGeneration: () => session.generation,
  })
  const api = Object.fromEntries(apiNames.map(name => [name, async () => ({ data: [] })]))
  api.getTestCaseDetail = async id => loadDetail ? loadDetail(id) : { data: detail }
  api.getComponents = loadBaseComponents || (async () => ({ data: [{
    type: 'tap', name: 'Tap', schema: tapDefinition.schema, default_config: tapDefinition.default_config,
  }] }))
  if (loadCustomComponents) api.getCustomComponents = loadCustomComponents
  api.createTestCase = async payload => {
    writes.push({ action: 'create', payload })
    return createTestCase ? createTestCase(payload) : { data: { id: 51 } }
  }
  api.updateTestCase = async (id, payload) => {
    writes.push({ action: 'update', id, payload })
    return updateTestCase ? updateTestCase(id, payload) : { data: { id } }
  }
  const loaded = await loadSfcSetup('src/views/app-automation/test-cases/SceneBuilder.vue', {
    'vue-router': { useRoute: () => ({ query: routeCaseId ? { case_id: routeCaseId } : {} }), onBeforeRouteLeave() {} },
    '@element-plus/icons-vue': Object.fromEntries(
      ['Upload', 'Download', 'FolderAdd', 'DocumentCopy', 'Check', 'Search', 'Link', 'Refresh', 'Camera']
        .map(name => [name, {}]),
    ),
    vuedraggable: { default: {} },
    '../elements/components/CaptureElementDialog.vue': { default: {} },
    '../components/DeviceRecorder.vue': { default: {} },
    '../components/StepDiagnostics.vue': { default: {} },
    '../components/SceneDataPanel.vue': { default: {} },
    './NestedFlowEditor.vue': { default: {} },
    '@/stores/user': { useUserStore: () => userStore },
    '@/api/app-automation': api,
    '@/api/app-device-options': { getAllAppDevices: loadDevices || (async () => []) },
  })
  if (autoMount) await loaded.mount()
  let disposed = false
  const instance = {
    ...loaded,
    writes,
    userStore,
    setSessionGeneration: generation => { session.generation = generation },
    dispose: () => {
      if (disposed) return
      disposed = true
      loaded.dispose()
    },
  }
  instances.push(instance)
  return instance
}

const tapDefinition = {
  schema: { properties: { selector: { type: 'string' } }, required: ['selector'] },
  default_config: {},
}

async function loadNestedEditor({ modelValue = [], components = [], componentDefinitions = {} } = {}) {
  const loaded = await loadSfcSetup('src/views/app-automation/test-cases/NestedFlowEditor.vue', {
    vuedraggable: { default: {} },
    'element-plus': { ElMessage: { warning() {} } },
  })
  const emitted = []
  const props = { modelValue, components, componentDefinitions, fieldLabel: field => field, fieldOptions: () => [], depth: 0, maxDepth: 12 }
  const scope = effectScope()
  const state = scope.run(() => loaded.component.setup(props, {
    expose() {}, emit: (event, value) => emitted.push([event, value]),
  }))
  instances.push({ dispose: () => { scope.stop(); loaded.dispose() } })
  return { loaded, state, emitted }
}

test('mounted editor waits for definitions and detail, and never applies a response over an intervening draft', async () => {
  const detailRequest = deferred()
  const detailStarted = deferred()
  const builder = await loadBuilder({
    routeCaseId: 99,
    autoMount: false,
    getTestCaseDetail: id => {
      detailStarted.resolve(id)
      return detailRequest.promise
    },
  })
  const mounting = builder.mount()

  assert.equal(builder.state.sceneEditorReady.value, false)
  assert.equal(await detailStarted.promise, 99)
  assert.equal(builder.state.sceneEditorReady.value, false)
  await builder.state.saveScene()
  assert.equal(builder.writes.length, 0)

  builder.state.sceneForm.value.name = 'Intervening draft'
  builder.state.scenarioSteps.value = [{ id: 'draft-step', type: 'tap', config: { selector: 'draft' } }]
  detailRequest.resolve({ data: {
    id: 99,
    updated_at: '2026-11-24T11:00:00Z',
    name: 'Server case',
    project: null,
    variables: [],
    ui_flow: [{ id: 'server-step', type: 'tap', config: { selector: 'server' } }],
  } })
  await mounting

  assert.equal(builder.state.sceneEditorReady.value, false)
  assert.equal(builder.state.editorInitializationStatus.value, 'failed')
  assert.equal(builder.state.caseDetailStatus.value, 'failed')
  assert.equal(builder.state.sceneForm.value.name, 'Intervening draft')
  assert.equal(builder.state.scenarioSteps.value[0].config.selector, 'draft')
  await builder.state.saveScene()
  assert.equal(builder.writes.length, 0)
})

test('case loading enables editing and saves the exact revision while optional device paging is pending', async () => {
  const devicePaging = deferred()
  const detailRequest = deferred()
  const detailStarted = deferred()
  const builder = await loadBuilder({
    routeCaseId: 99,
    autoMount: false,
    getAllAppDevices: () => devicePaging.promise,
    getTestCaseDetail: id => {
      detailStarted.resolve(id)
      return detailRequest.promise
    },
    updateTestCase: async id => ({ data: { id, updated_at: 'revision-2' } }),
  })
  const mounting = builder.mount()
  assert.equal(await detailStarted.promise, 99)
  assert.equal(builder.state.sceneEditorReady.value, false)

  detailRequest.resolve({ data: {
    id: 99,
    updated_at: 'revision-1',
    name: 'Loaded case',
    project: null,
    variables: [],
    ui_flow: [{ id: 'loaded-step', type: 'tap', config: { selector: 'Continue' } }],
  } })
  await mounting

  assert.equal(builder.state.sceneEditorReady.value, true)
  assert.equal(builder.state.recorderDevicesLoading.value, true)
  assert.equal(builder.state.loadedCaseUpdatedAt.value, 'revision-1')
  builder.state.sceneForm.value.name = 'Updated case'
  await builder.state.saveScene()

  assert.equal(builder.writes.length, 1)
  assert.equal(builder.writes[0].id, 99)
  assert.equal(builder.state.loadedCaseUpdatedAt.value, 'revision-2')
  assert.equal(builder.state.caseDetailStatus.value, 'loaded')

  devicePaging.resolve([])
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(builder.state.recorderDevicesLoading.value, false)
})

test('save completion uses its submitted snapshot and leaves later edits dirty', async () => {
  const request = deferred()
  const { state, writes } = await loadBuilder({ createTestCase: () => request.promise })
  state.componentDefinitions.value = { tap: tapDefinition }
  state.componentDefinitionsStatus.value = 'loaded'
  state.sceneBaseline.value = state.getSceneSnapshot()
  state.sceneForm.value.name = 'Captured scene'
  state.scenarioSteps.value = [{ id: 'step-1', type: 'tap', config: { selector: 'before' } }]

  const save = state.saveScene()
  const submittedSnapshot = state.getSceneSnapshot()
  state.sceneForm.value.name = 'Edited during save'
  state.scenarioSteps.value[0].config.selector = 'after'
  request.resolve({ data: { id: 61 } })
  await save

  assert.equal(writes[0].payload.name, 'Captured scene')
  assert.equal(writes[0].payload.ui_flow[0].config.selector, 'before')
  assert.equal(state.sceneBaseline.value, submittedSnapshot)
  assert.equal(state.editingCaseId.value, 61)
  assert.equal(state.isSceneDirty.value, true)
})

test('save completion cannot replace a newer case context or its baseline', async () => {
  const request = deferred()
  const detail = {
    id: 99, name: 'Replacement case', project: null, variables: [],
    ui_flow: [{ id: 'replacement-step', type: 'tap', config: { selector: 'replacement' } }],
  }
  const { state } = await loadBuilder({ detail, createTestCase: () => request.promise })
  state.componentDefinitions.value = { tap: tapDefinition }
  state.componentDefinitionsStatus.value = 'loaded'
  state.sceneForm.value.name = 'Original draft'
  state.scenarioSteps.value = [{ id: 'original-step', type: 'tap', config: { selector: 'original' } }]
  const save = state.saveScene()

  await state.loadCaseDetail(99)
  state.editingCaseId.value = 99
  const replacementBaseline = state.sceneBaseline.value
  request.resolve({ data: { id: 61 } })
  await save

  assert.equal(state.sceneForm.value.name, 'Replacement case')
  assert.equal(state.editingCaseId.value, 99)
  assert.equal(state.sceneBaseline.value, replacementBaseline)
  assert.equal(state.isSceneDirty.value, false)
})

test('unmount invalidates a pending save without showing stale feedback', async () => {
  const request = deferred()
  const { state, messages, dispose } = await loadBuilder({ createTestCase: () => request.promise })
  state.componentDefinitions.value = { tap: tapDefinition }
  state.componentDefinitionsStatus.value = 'loaded'
  state.sceneForm.value.name = 'Pending save'
  state.scenarioSteps.value = [{ id: 'step-1', type: 'tap', config: { selector: 'Continue' } }]
  state.sceneBaseline.value = state.getSceneSnapshot()
  const baseline = state.sceneBaseline.value

  const save = state.saveScene()
  dispose()
  request.resolve({ data: { id: 61 } })
  await save

  assert.equal(state.editingCaseId.value, null)
  assert.equal(state.sceneBaseline.value, baseline)
  assert.equal(messages.length, 0)
})

test('unmount invalidates a pending case load and suppresses stale load errors', async () => {
  const request = deferred()
  const { state, messages, dispose } = await loadBuilder({ getTestCaseDetail: () => request.promise })
  state.sceneForm.value.name = 'Current draft'
  state.sceneBaseline.value = state.getSceneSnapshot()
  const baseline = state.sceneBaseline.value

  const load = state.loadCaseDetail(99)
  dispose()
  request.resolve({ data: {
    id: 99, name: 'Stale response', project: null, variables: [],
    ui_flow: [{ type: 'tap', config: { selector: 'Late' } }],
  } })
  await load

  assert.equal(state.sceneForm.value.name, 'Current draft')
  assert.equal(state.sceneBaseline.value, baseline)
  assert.equal(messages.length, 0)

  const rejectedRequest = deferred()
  const second = await loadBuilder({ getTestCaseDetail: () => rejectedRequest.promise })
  const failedLoad = second.state.loadCaseDetail(100)
  second.dispose()
  rejectedRequest.reject(new Error('late fixture failure'))
  await failedLoad
  assert.equal(second.messages.length, 0)
})

test('case loads cannot cross account or session replacement', async () => {
  for (const replacement of ['account', 'session']) {
    for (const rejected of [false, true]) {
      const request = deferred()
      const { state, userStore, setSessionGeneration, messages } = await loadBuilder({ getTestCaseDetail: () => request.promise })
      state.sceneForm.value.name = 'Current draft'
      state.sceneBaseline.value = state.getSceneSnapshot()
      const baseline = state.sceneBaseline.value
      const load = state.loadCaseDetail(99)
      if (replacement === 'account') userStore.user = { id: 8 }
      else setSessionGeneration(2)
      if (rejected) request.reject(new Error('old account response failed'))
      else request.resolve({ data: {
        id: 99, name: 'Old account case', project: 42,
        variables: [{ name: 'fixture', value: 'old account value' }],
        ui_flow: [{ type: 'wait', config: {} }],
      } })
      await load
      assert.equal(state.sceneForm.value.name, 'Current draft')
      assert.equal(state.sceneBaseline.value, baseline)
      assert.deepEqual(plain(state.sceneVariables.value), [])
      assert.equal(messages.length, 0)
    }
  }
})

test('custom params use one edited authority and preserve business arrays through clone, save and reload', async () => {
  const component = {
    id: 4,
    name: 'Data component',
    type: 'data_component',
    schema: { properties: {
      amount: { type: 'integer' },
      steps: { type: 'array' },
      then_steps: { type: 'array' },
      try_steps: { type: 'array' },
      finally_steps: { type: 'array' },
    }, required: ['amount'] },
    default_config: { amount: 2 },
    steps: [],
  }
  const loadComponents = async () => ({ data: [component] })
  const payloadArray = {
    steps: [{
      id: 'business-row', type: 'not-a-flow-component', _expanded: 'data-value',
      config: { _expanded: 'nested-data-value', retries: 'not-a-number', steps: ['ordinary-data'] },
    }],
    then_steps: [{ type: 'row', then_steps: ['business-branch'] }],
    try_steps: ['try-data'],
    finally_steps: ['finally-data'],
  }

  for (const parameterSource of ['params', 'config', 'both']) {
    const id = parameterSource === 'params' ? 71 : parameterSource === 'config' ? 72 : 73
    const source = { amount: 1, ...structuredClone(payloadArray) }
    const uiStep = { id: `custom-${id}`, kind: 'custom', type: component.type }
    if (parameterSource !== 'config') uiStep.params = source
    if (parameterSource !== 'params') uiStep.config = parameterSource === 'both'
      ? { amount: 99, steps: ['stale-config'] }
      : source
    const detail = {
      id, updated_at: `revision-${id}`, name: 'Data round trip', project: null,
      variables: [], ui_flow: [uiStep],
    }
    const builder = await loadBuilder({ detail, getCustomComponents: loadComponents })
    await builder.state.loadCaseDetail(id)
    assert.equal(builder.state.loadedCaseUpdatedAt.value, `revision-${id}`)
    assert.deepEqual(plain(builder.state.scenarioSteps.value[0].config), source)

    builder.state.duplicateStep(0)
    const duplicate = builder.state.scenarioSteps.value[1]
    duplicate.config.amount = 5
    builder.state.scenarioSteps.value[0].config.amount = 7
    assert.deepEqual(plain(duplicate.config.steps), payloadArray.steps)
    assert.deepEqual(plain(builder.state.scenarioSteps.value[0].config.steps), payloadArray.steps)

    await builder.state.saveScene()
    assert.equal(builder.writes.length, 1)
    const savedSteps = builder.writes[0].payload.ui_flow
    assert.equal(savedSteps[0].params?.amount ?? savedSteps[0].config.amount, 7)
    assert.equal(savedSteps[1].params?.amount ?? savedSteps[1].config.amount, 5)
    for (const saved of savedSteps) {
      const canonical = saved.params || saved.config
      assert.deepEqual(plain(canonical.steps), payloadArray.steps)
      assert.deepEqual(plain(canonical.then_steps), payloadArray.then_steps)
      assert.deepEqual(plain(canonical.try_steps), payloadArray.try_steps)
      assert.deepEqual(plain(canonical.finally_steps), payloadArray.finally_steps)
      if (parameterSource === 'both' || parameterSource === 'params') assert.equal(saved.config, undefined)
      if (parameterSource === 'config') assert.equal(saved.params, undefined)
    }

    const reloaded = await loadBuilder({
      detail: { ...detail, ui_flow: savedSteps }, getCustomComponents: loadComponents,
    })
    await reloaded.state.loadCaseDetail(id)
    assert.equal(reloaded.state.scenarioSteps.value[0].config.amount, 7)
    assert.deepEqual(plain(reloaded.state.scenarioSteps.value[0].config), plain(savedSteps[0].params || savedSteps[0].config))
  }
})

test('numeric and boolean variables reject invalid literals and retain null and empty values', async () => {
  const { state, writes, messages } = await loadBuilder()
  state.sceneForm.value.name = 'Typed variables'
  state.scenarioSteps.value = [{ id: 'tap-step', type: 'tap', config: { selector: 'Continue' } }]
  state.sceneVariables.value = [
    { name: 'number_value', type: 'number', value: 'not-a-number' },
    { name: 'boolean_value', type: 'boolean', value: 'not-a-boolean' },
  ]

  await state.saveScene()
  assert.equal(writes.length, 0)
  assert.ok(messages.some(message => message.text.includes('invalidVariableNumber')))

  state.sceneVariables.value[0].value = '3.5'
  await state.saveScene()
  assert.equal(writes.length, 0)
  assert.ok(messages.some(message => message.text.includes('invalidVariableBoolean')))

  state.sceneVariables.value = [
    { name: 'null_number', type: 'number', value: null },
    { name: 'empty_number', type: 'number', value: '' },
    { name: 'zero', type: 'number', value: '0' },
    { name: 'false_value', type: 'boolean', value: false },
    { name: 'empty_boolean', type: 'boolean', value: '' },
  ]
  await state.saveScene()
  assert.equal(writes.length, 1)
  assert.deepEqual(plain(writes[0].payload.variables.map(({ name, value }) => ({ name, value }))), [
    { name: 'null_number', value: null },
    { name: 'empty_number', value: '' },
    { name: 'zero', value: 0 },
    { name: 'false_value', value: false },
    { name: 'empty_boolean', value: '' },
  ])
})

test('save accepts legacy inline custom steps when their global definition was removed', async () => {
  const { state, writes, messages } = await loadBuilder()
  state.componentDefinitions.value = { wait: { schema: { properties: {}, required: [] }, default_config: {} } }
  state.componentDefinitionsStatus.value = 'loaded'
  state.customComponentDefinitionsStatus.value = 'loaded'
  state.sceneForm.value.name = 'Legacy inline case'
  state.scenarioSteps.value = [{
    kind: 'custom', type: 'removed_inline_component', steps: [{ type: 'wait', config: {} }],
  }]

  await state.saveScene()

  assert.equal(writes.length, 1)
  assert.equal(writes[0].payload.ui_flow[0].type, 'removed_inline_component')
  assert.deepEqual(plain(writes[0].payload.ui_flow[0].steps), [{ type: 'wait', config: {} }])
  assert.equal(messages.some(message => message.type === 'error'), false)
})

test('save accepts supported legacy atomic aliases absent from the palette', async () => {
  const { state, writes, messages } = await loadBuilder()
  state.componentDefinitions.value = { wait: { schema: { properties: {}, required: [] }, default_config: {} } }
  state.componentDefinitionsStatus.value = 'loaded'
  state.customComponentDefinitionsStatus.value = 'loaded'
  state.sceneForm.value.name = 'Legacy action aliases'
  state.scenarioSteps.value = [
    { type: 'touch', config: {} },
    { type: 'text', config: { text: 'fixture text' } },
    { type: 'sleep', config: {} },
  ]

  await state.saveScene()

  assert.equal(writes.length, 1)
  assert.deepEqual(plain(writes[0].payload.ui_flow.map(step => step.type)), ['touch', 'text', 'sleep'])
  assert.equal(messages.some(message => message.type === 'error'), false)
})

test('save completion cannot bind a created case to a different project', async () => {
  const request = deferred()
  const { state, writes } = await loadBuilder({ createTestCase: () => request.promise })
  state.componentDefinitions.value = { tap: tapDefinition }
  state.componentDefinitionsStatus.value = 'loaded'
  state.sceneForm.value = { name: '', description: '', project: 8 }
  state.sceneBaseline.value = state.getSceneSnapshot()
  state.sceneForm.value.name = 'Project 8 draft'
  state.scenarioSteps.value = [{ id: 'step-1', type: 'tap', config: { selector: 'before' } }]
  const priorBaseline = state.sceneBaseline.value

  const save = state.saveScene()
  assert.equal(writes[0].payload.project, 8)
  state.sceneForm.value.project = 9
  state.sceneForm.value.name = 'Edited after project switch'
  request.resolve({ data: { id: 61 } })
  await save

  assert.equal(state.editingCaseId.value, null)
  assert.equal(state.sceneBaseline.value, priorBaseline)
  assert.equal(state.isSceneDirty.value, true)
})

test('missing and unavailable project component definitions block versioned-reference saves', async () => {
  const { state, writes, messages } = await loadBuilder()
  state.sceneForm.value = { name: 'Versioned case', description: '', project: 8 }
  state.scenarioSteps.value = [{
    id: 'project-step', kind: 'custom', type: 'login_flow', version: '3', config: {},
  }]

  state.setProjectComponentLoadState({ projectId: 8, userId: 7, sessionGeneration: 1, status: 'loading' })
  await state.saveScene()
  assert.equal(writes.length, 0)
  assert.ok(messages.some(message => message.text.includes('projectComponentDefinitionsLoading')))

  state.setProjectComponentLoadState({ projectId: 8, userId: 7, sessionGeneration: 1, status: 'failed' })
  await state.saveScene()
  assert.equal(writes.length, 0)
  assert.ok(messages.some(message => message.text.includes('projectComponentDefinitionsUnavailable')))

  state.setProjectComponents([{
    type: 'login_flow', version: '3', name: 'Login', schema: { properties: {}, required: [] }, default_config: {},
  }])
  state.userStore.user = { id: 12 }
  state.setProjectComponentLoadState({ projectId: 8, userId: 7, sessionGeneration: 1, status: 'loaded' })
  await state.saveScene()
  assert.equal(writes.length, 0, 'definitions from the prior account must not authorize saving')
})

test('unknown base steps fail clearly after the palette has loaded', async () => {
  const { state, writes, messages } = await loadBuilder()
  state.componentDefinitions.value = {}
  state.componentDefinitionsStatus.value = 'loaded'
  state.sceneForm.value.name = 'Unknown component case'
  state.scenarioSteps.value = [{ id: 'unknown-step', type: 'missing_component', config: {} }]

  await state.saveScene()
  assert.equal(writes.length, 0)
  assert.ok(messages.some(message => message.text.includes('componentDefinitionsUnavailable')))

  state.componentDefinitionsStatus.value = 'failed'
  await state.saveScene()
  assert.equal(writes.length, 0)
  assert.ok(messages.some(message => message.text.includes('editorLoadFailed')))
})

test('confirmed recorder steps survive lease loss only in their captured account, project and scene', async () => {
  const { state, userStore, setSessionGeneration } = await loadBuilder()
  state.editingCaseId.value = 14
  state.sceneForm.value.project = 8
  state.recorderDeviceId.value = 31
  const context = state.sendRecorderInteraction.getActionContext()
  const staged = { action: 'tap', x_ratio: 0.4, y_ratio: 0.6 }
  state.recorderEnabled.value = false
  state.recorderLease.value = null

  const appended = state.appendRecorderStep({ step: staged, context })
  assert.equal(appended.type, 'click')
  assert.equal(state.scenarioSteps.value.length, 1)

  state.sceneForm.value.project = 9
  assert.equal(state.appendRecorderStep({ step: staged, context }), null)
  assert.equal(state.scenarioSteps.value.length, 1)

  state.sceneForm.value.project = 8
  state.recorderDeviceId.value = 32
  assert.equal(state.appendRecorderStep({ step: staged, context }), null)
  state.recorderDeviceId.value = 31

  setSessionGeneration(2)
  assert.equal(state.appendRecorderStep({ step: staged, context }), null)
  setSessionGeneration(1)

  userStore.user = { id: 12 }
  assert.equal(state.appendRecorderStep({ step: staged, context }), null)
  userStore.user = { id: 7 }

  state.sceneContextGeneration.value += 1
  assert.equal(state.appendRecorderStep({ step: staged, context }), null)
  assert.equal(state.scenarioSteps.value.length, 1)
  assert.equal(state.recorderErrorKey.value, 'appAutomation.recorder.errors.actionContextChanged')
})

test('root and nested insertions merge schema defaults with configured defaults', async () => {
  const { state } = await loadBuilder()
  const projectComponent = {
    type: 'login_flow', name: 'Login', kind: 'custom', version: '4',
    schema: { properties: {
      retries: { type: 'integer', default: 2 },
      options: { type: 'object', default: { remember: true } },
      mode: { type: 'string', default: 'schema-mode' },
    } },
    default_config: { retries: 5, mode: 'configured-mode' },
  }
  state.sceneForm.value.project = 8
  state.setProjectComponents([projectComponent])
  state.insertProjectComponent(projectComponent)

  assert.equal(state.scenarioSteps.value[0].version, '4')
  assert.deepEqual(plain(state.scenarioSteps.value[0].config), {
    retries: 5, options: { remember: true }, mode: 'configured-mode',
  })
  state.scenarioSteps.value[0].config.options.remember = false
  assert.equal(projectComponent.schema.properties.options.default.remember, true)

  const nestedComponent = {
    type: 'nested_fixture', name: 'Nested fixture', kind: 'custom',
    schema: { properties: { retries: { type: 'integer', default: 3 }, mode: { type: 'string', default: 'schema-mode' } } },
    default_config: { mode: 'nested-config' }, steps: [],
  }
  const nested = await loadNestedEditor({
    components: [nestedComponent],
    componentDefinitions: { nested_fixture: nestedComponent },
  })
  nested.state.newStepType.value = 'nested_fixture'
  nested.state.addStep()
  assert.deepEqual(plain(nested.emitted.at(-1)[1][0].config), {
    retries: 3, mode: 'nested-config',
  })
})

test('nested editor recursively exposes unversioned inline steps but not versioned bodies', async () => {
  const inlineChild = {
    id: 'inline-child', kind: 'custom', type: 'inline_child', config: { label: 'before' },
    steps: [{ id: 'inline-leaf', type: 'tap', config: { selector: 'Continue' } }],
  }
  const parent = { id: 'inline-parent', kind: 'custom', type: 'inline_parent', config: {}, steps: [inlineChild] }
  const { loaded, state } = await loadNestedEditor({
    modelValue: [parent],
    components: [{ type: 'tap', name: 'Tap', default_config: { selector: 'schema default' } }],
    componentDefinitions: {
      inline_parent: { schema: { properties: {} } },
      inline_child: { schema: { properties: { label: { type: 'string' } } } },
      tap: tapDefinition,
      'inline_child@2': { schema: { properties: { label: { type: 'string' } } } },
    },
  })
  assert.equal(state.hasInlineComponentSteps(parent), true)
  assert.equal(state.hasInlineComponentSteps(inlineChild), true)
  assert.equal(state.hasInlineComponentSteps({ ...inlineChild, version: '2' }), false)
  assert.deepEqual(plain(state.getSchemaFields(inlineChild)), ['label'])

  const childEditor = await loadNestedEditor({
    modelValue: inlineChild.steps,
    components: [{ type: 'tap', name: 'Tap', default_config: { selector: 'schema default' } }],
    componentDefinitions: { tap: tapDefinition },
  })
  childEditor.state.newStepType.value = 'tap'
  childEditor.state.addStep()
  inlineChild.steps = childEditor.emitted.at(-1)[1]
  assert.equal(inlineChild.steps.length, 2)
  assert.equal(inlineChild.steps[1].config.selector, 'schema default')

  const source = await (await import('node:fs/promises')).readFile(
    new URL('../src/views/app-automation/test-cases/NestedFlowEditor.vue', import.meta.url), 'utf8',
  )
  assert.match(source, /v-if="hasInlineComponentSteps\(element\)"[\s\S]*?:model-value="element\.steps"[\s\S]*?@update:model-value="element\.steps = \$event"/)
  assert.ok(loaded.component)
})

test('project definition load states are tagged and stale project responses are ignored', async () => {
  const oldDatasets = deferred()
  const oldComponents = deferred()
  const userStore = reactive({ user: { id: 7 }, generation: 1, getSessionGeneration() { return this.generation } })
  const loaded = await loadSfcSetup('src/views/app-automation/components/SceneDataPanel.vue', {
    vue: {
      ...vue,
      onBeforeUnmount: () => {},
      watch: (source, callback, options) => {
        const value = source()
        return value == null || (Array.isArray(value) && value[0] == null)
          ? () => {}
          : vue.watch(source, callback, options)
      },
    },
    '@/api/app-scene-data': {
      createProjectComponent: async () => ({ data: {} }),
      uploadSceneDataset: async () => ({ data: {} }),
      getAllSceneDatasets: async project => project === 8 ? oldDatasets.promise : [{ id: `dataset-${project}` }],
      getAllProjectComponents: async project => project === 8 ? oldComponents.promise : [{ id: `component-${project}`, type: 'login_flow' }],
    },
    '@/stores/user': { useUserStore: () => userStore },
  })
  const emitted = []
  const props = reactive({ projectId: 8, getSteps: () => [] })
  const scope = effectScope()
  const state = scope.run(() => loaded.component.setup(props, {
    expose() {}, emit: (...args) => emitted.push(args),
  }))
  instances.push({ dispose: () => { scope.stop(); loaded.dispose() } })
  assert.ok(emitted.some(([name, value]) => name === 'components-load-state'
    && value.status === 'loading' && value.projectId === 8 && value.userId === 7))

  props.projectId = 9
  await nextTick()
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(plain(state.components.value), [{ id: 'component-9', type: 'login_flow' }])
  assert.ok(emitted.some(([name, value]) => name === 'components-load-state'
    && value.status === 'loaded' && value.projectId === 9))

  oldDatasets.resolve([{ id: 'stale-dataset-8' }])
  oldComponents.resolve([{ id: 'stale-component-8', type: 'old_login' }])
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(plain(state.components.value), [{ id: 'component-9', type: 'login_flow' }])
  assert.equal(emitted.some(([name, value]) => name === 'components-load-state'
    && value.status === 'loaded' && value.projectId === 8), false)
})
