import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { effectScope } from 'vue'
import { loadSfcSetup, plain } from './helpers/sfc-setup.mjs'

const instances = []
const apiNames = [
  'getComponents', 'getCustomComponents', 'createCustomComponent', 'updateCustomComponent',
  'deleteCustomComponent', 'importComponentPackage', 'exportComponentPackage', 'getTestCaseDetail',
  'createTestCase', 'updateTestCase', 'getAppElementList', 'getAppImageCategories', 'getDeviceList',
  'getAppDevice', 'acquireRecorderLease', 'releaseRecorderLease', 'heartbeatDevice',
  'captureDeviceScreenshot', 'recordDeviceInteraction', 'getAppProjects',
]

afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

async function loadBuilder(detail = null) {
  const writes = []
  const api = Object.fromEntries(apiNames.map(name => [name, async (...args) => {
    if (name === 'getTestCaseDetail') return { data: detail }
    if (name === 'createTestCase') {
      writes.push(args[0])
      return { data: { id: 51 } }
    }
    if (name === 'updateTestCase') {
      writes.push(args[1])
      return { data: { id: args[0] } }
    }
    return { data: [] }
  }]))
  const instance = await loadSfcSetup('src/views/app-automation/test-cases/SceneBuilder.vue', {
    'vue-router': { useRoute: () => ({ query: {} }), onBeforeRouteLeave() {} },
    '@element-plus/icons-vue': Object.fromEntries(
      ['Upload', 'Download', 'FolderAdd', 'DocumentCopy', 'Check', 'Search', 'Link', 'Refresh', 'Camera']
        .map(name => [name, {}]),
    ),
    vuedraggable: { default: {} },
    '../elements/components/CaptureElementDialog.vue': { default: {} },
    '../components/DeviceRecorder.vue': { default: {} },
    '../components/StepDiagnostics.vue': { default: {} },
    './NestedFlowEditor.vue': { default: {} },
    '@/stores/user': { useUserStore: () => ({ user: { id: 7, username: 'typed-parameter-user' } }) },
    '@/api/app-automation': api,
  })
  instances.push(instance)
  return { ...instance, writes }
}

const projectComponent = {
  id: 19,
  name: 'Typed parameter fixture',
  type: 'typed_parameter_fixture',
  version: '3',
  schema: {
    properties: {
      attempts: { type: 'integer', minimum: 0, maximum: 5 },
      enabled: { type: 'boolean' },
      labels: { type: 'array' },
      options: { type: 'object' },
      private_limit: { type: 'number', sensitive: true },
      literal_count: { type: 'number', minimum: 0, maximum: 10 },
      literal_enabled: { type: 'boolean' },
    },
    required: ['attempts'],
  },
  default_config: {
    attempts: 2,
    enabled: false,
    labels: ['smoke'],
    options: { retries: 1 },
    private_limit: 6,
    literal_count: 4.5,
    literal_enabled: true,
  },
  steps: [],
}

test('versioned root component supports typed expressions and preserves typed literal round-trip', async () => {
  const { state, writes, messages } = await loadBuilder()
  state.setProjectComponents([projectComponent])
  state.insertProjectComponent(projectComponent)
  const step = state.scenarioSteps.value[0]
  state.sceneForm.value.name = 'Typed component case'
  const def = state.activeComponentDef.value

  assert.equal(state.isTypedParameterExpressionMode(step, 'attempts', def), false)
  state.setTypedParameterExpressionMode(step, 'attempts', def, true)
  assert.equal(state.getTypedParameterInput(step, 'attempts'), '2')
  assert.equal(step.config.attempts, 2, 'entering expression mode must not coerce or discard the literal')
  state.setTypedParameterExpressionMode(step, 'private_limit', def, true)
  const privateValue = 'synthetic-private-typed-parameter'
  state.updateTypedParameterInput(step, 'private_limit', def, privateValue)
  assert.equal(state.isSensitiveField(def, 'private_limit'), true)
  await state.saveScene()
  assert.equal(writes.length, 0)
  assert.ok(messages.some(message => message.text.includes('invalidTypedParameterAt')))
  assert.ok(messages.every(message => !message.text.includes(privateValue)))

  for (const [field, reference] of [
    ['attempts', '{{local.attempts}}'],
    ['enabled', '${global.enabled}'],
    ['labels', '{{local.labels}}'],
    ['options', '${global.options}'],
    ['private_limit', '{{local.private_limit}}'],
  ]) {
    state.setTypedParameterExpressionMode(step, field, def, true)
    state.updateTypedParameterInput(step, field, def, reference)
  }

  await state.saveScene()
  assert.equal(writes.length, 1)
  const savedReference = writes[0].ui_flow[0]
  assert.deepEqual(plain(savedReference.config), {
    attempts: '{{local.attempts}}',
    enabled: '${global.enabled}',
    labels: '{{local.labels}}',
    options: '${global.options}',
    private_limit: '{{local.private_limit}}',
    literal_count: 4.5,
    literal_enabled: true,
  })

  const reload = await loadBuilder({
    id: 51,
    name: 'Typed component case',
    variables: [],
    ui_flow: writes[0].ui_flow,
  })
  reload.state.setProjectComponents([projectComponent])
  await reload.state.loadCaseDetail(51)
  reload.state.selectedIndex.value = 0
  const loadedStep = reload.state.scenarioSteps.value[0]
  assert.deepEqual(plain(loadedStep.config), plain(savedReference.config))
  assert.equal(reload.state.isTypedParameterExpressionMode(loadedStep, 'attempts', reload.state.activeComponentDef.value), true)
  assert.equal(reload.state.getTypedParameterInput(loadedStep, 'options'), '${global.options}')
  assert.equal(reload.state.isTypedParameterExpressionMode(loadedStep, 'literal_enabled', reload.state.activeComponentDef.value), false)
})

test('nested editor uses expression input for versioned typed parameters without changing sibling literals', async () => {
  const loaded = await loadSfcSetup('src/views/app-automation/test-cases/NestedFlowEditor.vue', {
    vuedraggable: { default: {} },
  })
  instances.push(loaded)
  const step = {
    id: 'nested-typed-step',
    kind: 'custom',
    type: 'nested_typed_fixture',
    version: '2',
    config: { count: 3, enabled: false, tags: ['unit'] },
  }
  const scope = effectScope()
  const state = scope.run(() => loaded.component.setup({
    modelValue: [step],
    label: 'Then steps',
    components: [],
    componentDefinitions: {
      'nested_typed_fixture@2': { schema: { properties: {
        count: { type: 'number', minimum: 0 },
        enabled: { type: 'boolean' },
        tags: { type: 'array' },
      } } },
    },
    fieldLabel: field => field,
    fieldOptions: () => [],
    depth: 0,
    maxDepth: 12,
  }, { expose() {}, emit() {} }))

  assert.equal(state.isTypedParameterExpressionMode(step, 'count'), false)
  state.setTypedParameterExpressionMode(step, 'count', true)
  assert.equal(state.getTypedParameterInput(step, 'count'), '3')
  assert.equal(step.config.count, 3)
  state.updateTypedParameterInput(step, 'count', '{{local.count}}')
  state.setTypedParameterExpressionMode(step, 'enabled', true)
  state.updateTypedParameterInput(step, 'enabled', '${global.enabled}')

  assert.deepEqual(plain(step.config), {
    count: '{{local.count}}',
    enabled: '${global.enabled}',
    tags: ['unit'],
  })
  assert.equal(state.isTypedParameterExpressionMode(step, 'count'), true)
  scope.stop()
})

test('expression controls are available in both component parameter forms and mask sensitive fields', async () => {
  const sceneBuilder = await import('node:fs/promises').then(({ readFile }) => readFile(
    new URL('../src/views/app-automation/test-cases/SceneBuilder.vue', import.meta.url), 'utf8',
  ))
  const nestedEditor = await import('node:fs/promises').then(({ readFile }) => readFile(
    new URL('../src/views/app-automation/test-cases/NestedFlowEditor.vue', import.meta.url), 'utf8',
  ))

  assert.match(sceneBuilder, /isVersionedTypedParameter\(activeStep, field, activeComponentDef\)/)
  assert.match(sceneBuilder, /:type="isSensitiveField\(activeComponentDef, field\) \? 'password' : 'text'"/)
  assert.match(nestedEditor, /isVersionedTypedParameter\(element, field\)/)
  assert.match(nestedEditor, /:type="isSensitive\(element, field\) \? 'password' : 'text'"/)
})
