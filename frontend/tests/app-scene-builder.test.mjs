import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { afterEach, test } from 'node:test'
import { effectScope, nextTick, reactive } from 'vue'
import { loadSfcSetup, plain } from './helpers/sfc-setup.mjs'

const instances = []
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

const stepDefinition = {
  schema: {
    properties: {
      selector: { type: 'string' },
      selector_type: { type: 'string', enum: ['text', 'resource_id'] },
      mode: { type: 'string', enum: ['fast', 'safe'] },
      retries: { type: 'integer', minimum: 0, maximum: 9, multipleOf: 1 },
      payload: { type: 'object', sensitive: true },
      steps: { type: 'array' },
    },
    required: ['selector'],
  },
  default_config: { selector_type: 'text', retries: 0 },
}

async function loadBuilder(detail = null, { userId = 7, devices = [], leaseSeconds = 90, releaseRecorderLease: releaseLease } = {}) {
  const writes = []
  const calls = { acquisitions: [], releases: [], heartbeats: [], heartbeatIntervals: [], interactions: [], confirmations: [], routeGuard: null, deviceOptions: [], elementQueries: [] }
  const session = { generation: 1 }
  const userStore = reactive({
    user: { id: userId, username: 'recorder-owner' },
    accessToken: 'fixture-session-token',
    getSessionGeneration: () => session.generation,
  })
  const liveDevices = devices.map(device => ({ ...device }))
  const findDevice = id => liveDevices.find(device => String(device.id) === String(id))
  const api = {
    getComponents: async () => ({ data: [] }),
    getCustomComponents: async () => ({ data: [] }),
    createCustomComponent: async payload => ({ data: { id: 2, ...payload } }),
    updateCustomComponent: async (id, payload) => ({ data: { id, ...payload } }),
    deleteCustomComponent: async () => ({}),
    importComponentPackage: async () => ({}),
    exportComponentPackage: async () => ({}),
    getTestCaseDetail: async () => ({ data: detail }),
    createTestCase: async payload => { writes.push(['create', payload]); return { data: { id: 51 } } },
    updateTestCase: async (id, payload) => { writes.push(['update', id, payload]); return { data: { id } } },
    getAppElementList: async params => { calls.elementQueries.push(params); return { data: { count: 0, results: [] } } },
    getAppImageCategories: async () => ({ data: [] }),
    getDeviceList: async () => ({ data: { results: liveDevices } }),
    getAppDevice: async id => ({ data: findDevice(id) }),
    acquireRecorderLease: async (id, payload) => {
      calls.acquisitions.push([id, payload])
      const current = findDevice(id)
      const locked = {
        ...current,
        status: 'locked',
        locked_by: payload.expected_user_id,
        locked_by_name: userStore.user.username,
        lease_requires_token: true,
        locked_at: '2026-09-23T12:00:00Z',
      }
      const index = liveDevices.findIndex(device => String(device.id) === String(id))
      liveDevices[index] = locked
      return { data: { device: locked, lease_token: `fixture-token-${id}`, lease_seconds: leaseSeconds } }
    },
    releaseRecorderLease: async (id, payload) => {
      calls.releases.push([id, payload])
      if (payload.lease_token !== `fixture-token-${id}`) return { data: { success: false } }
      const response = releaseLease ? await releaseLease(id, payload) : null
      if (response?.data?.success === false) return response
      const current = findDevice(id)
      const unlocked = { ...current, status: 'online', locked_by: null, locked_by_name: null, locked_at: null, lease_requires_token: false }
      const index = liveDevices.findIndex(device => String(device.id) === String(id))
      liveDevices[index] = unlocked
      return response || { data: { device: unlocked } }
    },
    heartbeatDevice: async (id, payload) => { calls.heartbeats.push([id, payload]); return { data: {} } },
    captureDeviceScreenshot: async () => ({ data: { success: true, data: { content: 'data:image/png;base64,mocked' } } }),
    recordDeviceInteraction: async (id, payload) => {
      calls.interactions.push([id, payload])
      return { data: { success: true } }
    },
    getAppProjects: async () => ({ data: [] }),
  }
  const icons = Object.fromEntries(['Upload', 'Download', 'FolderAdd', 'DocumentCopy', 'Check', 'Search', 'Link', 'Refresh', 'Camera'].map(name => [name, {}]))
  const instance = await loadSfcSetup('src/views/app-automation/test-cases/SceneBuilder.vue', {
    'vue-router': { useRoute: () => ({ query: {} }), onBeforeRouteLeave: guard => { calls.routeGuard = guard } },
    '@element-plus/icons-vue': icons,
    vuedraggable: { default: {} },
    '../elements/components/CaptureElementDialog.vue': { default: {} },
    '../components/DeviceRecorder.vue': { default: {} },
    '../components/StepDiagnostics.vue': { default: {} },
    './NestedFlowEditor.vue': { default: {} },
    '@/stores/user': { useUserStore: () => userStore },
    '@/api/app-automation': api,
    '@/api/app-device-options': { getAllAppDevices: async params => {
      calls.deviceOptions.push(params)
      return liveDevices
    } },
  }, { confirm: async (...args) => { calls.confirmations.push(args); return true }, setInterval: (callback, delay) => {
    calls.heartbeatIntervals.push({ callback, delay })
    return calls.heartbeatIntervals.length
  }, clearInterval() {} })
  instances.push(instance)
  await instance.mount()
  instance.state.componentDefinitions.value = { tap: stepDefinition, flow: { ...stepDefinition, default_config: {} } }
  return {
    ...instance, writes, calls, userStore, liveDevices,
    setSessionGeneration: generation => { session.generation = generation },
  }
}

test('element filters use the API search contract and reset pagination within the active project', async () => {
  const { state, calls } = await loadBuilder()
  state.sceneForm.value.project = 35
  state.elementCurrentPage.value = 4
  state.elementPageSize.value = 50
  state.elementFilterType.value = 'region'
  state.elementSearchKeyword.value = '  no-such-element  '
  await state.openElementSelector('selector')
  assert.deepEqual(plain(calls.elementQueries.at(-1)), {
    page: 1, page_size: 50, element_type: 'region', search: 'no-such-element', project: 35,
  })
  assert.deepEqual(plain(state.selectorElements.value), [])
  assert.equal(state.elementTotal.value, 0)
})

test('captured element binds to the initiating step and stays out of a changed project', async () => {
  const { state } = await loadBuilder()
  state.sceneForm.value.project = 35
  state.addPaletteComponent({ type: 'tap', name: 'Tap', default_config: {} })
  state.openCaptureElementDialog('selector')
  assert.equal(state.captureElementContext.value.projectId, 35)
  const element = { id: 23, name: 'Edge', project: 35, element_type: 'pos', config: { x: 0, y: 12 } }
  state.handleElementCreated(element)
  assert.equal(state.activeStep.value.config.element_id, 23)
  assert.equal(state.activeStep.value.config.selector, '0, 12')
  state.sceneForm.value.project = 46
  state.handleElementCreated({ ...element, id: 24 })
  assert.equal(state.activeStep.value.config.element_id, 23)
})

test('native inspector events bind the current matching Appium step without accepting stale or mismatched elements', async () => {
  const { state } = await loadBuilder()
  const source = await readFile(new URL('../src/views/app-automation/test-cases/SceneBuilder.vue', import.meta.url), 'utf8')
  assert.match(source, /@saved="bindInspectedElement"[\s\S]*@use-element="bindInspectedElement"/)
  state.projectList.value = [{ id: 35, appium_enabled: true }]
  state.appPackageList.value = [{ id: 61, package_name: 'org.example.qa' }]
  Object.assign(state.sceneForm.value, { engine: 'appium', project: 35, app_package: 61 })
  state.scenarioSteps.value = [
    { id: 'original', type: 'click', element_id: 99, config: {} },
    { id: 'selected-later', type: 'input', config: {} },
  ]
  state.selectStep(0)
  const element = {
    id: 21, project: 35, name: 'Submit', element_type: 'native',
    config: { package_name: 'org.example.qa', strategy: 'accessibility_id', value: 'Submit' },
  }
  const originalBinding = state.nativeBindingId.value
  assert.equal(state.bindInspectedElement({ element, bindingId: originalBinding }), true)
  assert.equal(state.activeStep.value.element_id, undefined)
  assert.deepEqual(plain(state.activeStep.value.config), {
    element_id: 21, selector_type: 'content_desc', selector: 'Submit', selector_match: 'exact',
  })
  state.selectStep(1)
  const secondBinding = state.nativeBindingId.value
  assert.equal(state.bindInspectedElement({ element, bindingId: originalBinding }), false)
  assert.equal(state.bindInspectedElement({ element: { ...element, project: 36 }, bindingId: secondBinding }), false)
  assert.equal(state.bindInspectedElement({
    element: { ...element, config: { ...element.config, package_name: 'org.other.fixture' } },
    bindingId: secondBinding,
  }), false)
  assert.equal(state.activeStep.value.config.element_id, undefined)
  // A copied scene can reuse a step ID while the first request still resolves.
  state.sceneContextGeneration.value += 1
  assert.equal(state.bindInspectedElement({ element, bindingId: secondBinding }), false)
  assert.equal(state.bindInspectedElement({ element, bindingId: state.nativeBindingId.value }), true)
})

test('manual locator editing detaches the saved reference when saving the scene', async () => {
  const { state, writes } = await loadBuilder()
  state.projectList.value = [{ id: 35, appium_enabled: true }]
  state.appPackageList.value = [{ id: 61, package_name: 'org.example.qa' }]
  Object.assign(state.sceneForm.value, { name: 'Checkout', engine: 'appium', project: 35, app_package: 61 })
  state.componentDefinitions.value = { click: stepDefinition }
  state.scenarioSteps.value = [
    { id: 'submit', type: 'click', element_id: 21, selector: 'original', config: { selector_type: 'resource_id', element_id: 21, selector: 'original' } },
    { id: 'other', type: 'click', config: { selector: 'control' } },
  ]
  state.selectStep(0)
  assert.equal(state.linkedElements.value.selector.id, 21)
  state.selectStep(1)
  state.selectStep(0)
  assert.equal(state.linkedElements.value.selector.id, 21)
  state.updateStepConfigField(state.activeStep.value, 'selector', 'original')
  assert.equal(state.linkedElements.value.selector.id, 21)
  state.updateStepConfigField(state.activeStep.value, 'timeout', 2)
  assert.equal(state.linkedElements.value.selector.id, 21)
  state.updateStepConfigField(state.activeStep.value, 'selector', 'org.example.qa:id/other')
  assert.equal(state.linkedElements.value.selector, null)
  await state.saveScene()
  assert.equal(writes.length, 1)
  const [step] = writes[0][1].ui_flow
  assert.equal(step.element_id, undefined)
  assert.equal(step.selector, undefined)
  assert.equal(step.config.element_id, undefined)
  assert.equal(step.config.selector, 'org.example.qa:id/other')
})

test('changing the drag start image category saves the new scope and detaches only the start element', async () => {
  const { state, writes } = await loadBuilder()
  const fields = ['start_selector_type', 'start_selector', 'end_selector_type', 'end_selector', 'image_scope']
  state.componentDefinitions.value = { drag: {
    schema: { required: fields.slice(0, 4), properties: Object.fromEntries(fields.map(field => [field, { type: 'string' }])) },
  } }
  state.sceneForm.value.name = 'Drag between screens'
  state.scenarioSteps.value = [{ id: 'drag', type: 'drag', config: {
    start_selector_type: 'image', start_selector: 'move.png',
    end_selector_type: 'image', end_selector: 'drop.png', image_scope: 'shared',
  } }]
  state.selectStep(0)
  const step = state.activeStep.value
  const fieldsByGroup = Object.fromEntries(state.getFieldGroups().map(group => [group.key, group.fields]))
  assert.ok(fieldsByGroup.start.includes('start_image_scope'))
  assert.ok(fieldsByGroup.end.includes('end_image_scope'))
  assert.ok(!Object.values(fieldsByGroup).flat().includes('image_scope'))
  assert.equal(state.getStepConfigField(step, 'start_image_scope'), 'shared')
  assert.equal(state.getStepConfigField(step, 'end_image_scope'), 'shared')
  assert.equal(step.config.start_image_scope, undefined)
  assert.equal(step.config.end_image_scope, undefined)

  assert.equal(state.linkElementToStep({
    id: 21, name: 'Start', element_type: 'image',
    config: { image_category: 'old_screen', image_path: 'old_screen/move.png' },
  }, step, 'start'), true)
  assert.equal(state.linkElementToStep({
    id: 22, name: 'End', element_type: 'image',
    config: { image_category: 'destination', image_path: 'destination/drop.png' },
  }, step, 'end'), true)
  assert.equal(state.getStepConfigField(step, 'start_image_scope'), 'old_screen')
  assert.ok(state.getFieldOptions('start_image_scope', state.activeComponentDef.value, step)
    .some(option => option.value === 'old_screen'))
  state.updateStepConfigField(step, 'start_image_scope', 'old_screen')
  assert.equal(state.linkedElements.value.start.id, 21)
  state.updateStepConfigField(step, 'start_image_scope', 'new_screen')
  assert.equal(state.linkedElements.value.start, null)
  assert.equal(state.linkedElements.value.end.id, 22)

  await state.saveScene()

  assert.equal(writes.length, 1)
  const [saved] = writes[0][1].ui_flow
  assert.equal(saved.config.start_image_scope, 'new_screen')
  assert.equal(saved.config.start_element_id, undefined)
  assert.equal(saved.config.end_image_scope, 'destination')
  assert.equal(saved.config.end_element_id, 22)
  assert.equal(saved.config.image_scope, 'shared')
})

test('palette groups and searches components, click-add clones config and selects the new step', async () => {
  const { state } = await loadBuilder()
  const source = {
    type: 'tap', name: 'Tap button', description: 'Submit action', category_name: 'Actions',
    default_config: { selector: 'submit', nested: { retry: 2 } },
  }
  state.componentPalette.value = [source, { ...source, type: 'wait', name: 'Wait', description: 'Pause briefly', category_name: 'Timing' }]
  state.paletteSearch.value = 'submit'

  assert.deepEqual(plain(state.filteredComponentGroups.value.map(group => group.label)), ['Actions'])
  assert.deepEqual(plain(state.filteredComponentGroups.value[0].items.map(item => item.type)), ['tap'])

  const added = state.addPaletteComponent(source)
  added.config.nested.retry = 7
  assert.notEqual(added.id, undefined)
  assert.equal(source.default_config.nested.retry, 2)
  assert.equal(state.selectedStepId.value, added.id)
  assert.equal(state.activeStep.value.id, added.id)

  const sourceText = await readFile(new URL('../src/views/app-automation/test-cases/SceneBuilder.vue', import.meta.url), 'utf8')
  assert.match(sourceText, /@click="addPaletteComponent\(element\)"/)
  assert.match(sourceText, /:clone="cloneComponent"/)
  assert.match(sourceText, /:group="\{ name: 'ui-components', pull: 'clone', put: false \}"/)
})

test('selection follows stable step ids when parent and nested steps reorder', async () => {
  const { state } = await loadBuilder()
  state.scenarioSteps.value = [
    { id: 'parent-a', type: 'tap', config: {}, steps: [{ id: 'child-a', type: 'tap', config: {} }] },
    { id: 'parent-b', type: 'tap', config: {}, steps: [{ id: 'child-b', type: 'tap', config: {} }] },
  ]
  state.selectSubStep(1, 0)
  state.scenarioSteps.value.reverse()

  assert.equal(state.selectedStepId.value, 'parent-b')
  assert.equal(state.selectedSubStepId.value, 'child-b')
  assert.equal(state.selectedIndex.value, 0)
  assert.equal(state.selectedSubIndex.value, 0)
  assert.equal(state.activeStep.value.id, 'child-b')
})

test('schema fields use declared enums and numeric/sensitive constraints', async () => {
  const { state } = await loadBuilder()
  const def = { schema: stepDefinition.schema }
  assert.deepEqual(plain(state.getFieldOptions('selector_type', def, {})), [
    { label: 'text', value: 'text' }, { label: 'resource_id', value: 'resource_id' },
  ])
  assert.deepEqual(plain(state.getFieldOptions('mode', def, {})), [
    { label: 'fast', value: 'fast' }, { label: 'safe', value: 'safe' },
  ])
  assert.equal(state.getFieldTypeForDef(def, 'retries'), 'integer')
  assert.deepEqual(plain(state.getFieldNumberRuleForDef(def, 'retries')), { min: 0, max: 9, step: 1 })
  assert.equal(state.isSensitiveField(def, 'payload'), true)
  assert.deepEqual(plain(state.normalizeSceneRuntime({ retry_times: 12, retry_interval: 11 })), {
    base_url: 'http://127.0.0.1:8000', retry_times: 10, retry_interval: 10,
  })
})

test('invalid JSON drafts block save and required fields are validated without runtime errors', async () => {
  const { state, writes, messages } = await loadBuilder()
  state.sceneForm.value.name = 'Draft case'
  state.scenarioSteps.value = [{
    id: 'invalid-step', type: 'tap', name: 'Tap',
    config: { selector: 'button', payload_input: '{invalid' },
  }]
  await state.saveScene()
  assert.equal(writes.length, 0)
  assert.ok(messages.some(message => message.type === 'error' && message.text.includes('invalidJsonAt')))

  state.scenarioSteps.value = [{
    id: 'nested-root', type: 'flow', name: 'Flow',
    config: { selector: 'root', steps: [{ id: 'nested-invalid', type: 'tap', config: { selector: 'button', payload_input: '{invalid' } }] },
  }]
  await state.saveScene()
  assert.equal(writes.length, 0)
  assert.equal(messages.filter(message => message.type === 'error' && message.text.includes('invalidJsonAt')).length, 2)

  state.scenarioSteps.value = [{ id: 'missing-required', type: 'tap', config: { selector: '' } }]
  await state.saveScene()
  assert.equal(writes.length, 0)
  assert.ok(messages.some(message => message.type === 'error' && message.text.includes('requiredFieldAt')))
})

test('legacy dict ui_flow loads into the editor and saves with runtime_config', async () => {
  const detail = {
    id: 44,
    name: 'Legacy case',
    description: 'retained wrapper',
    variables: [],
    ui_flow: {
      format_version: 1,
      runtime_config: { retry_times: 1, retry_interval: 0.25 },
      steps: [{ id: 'legacy-step', type: 'tap', name: 'Tap', selector: 'Continue', selector_type: 'text' }],
    },
    runtime_config: { base_url: 'https://api.example.test', retry_times: 3, ocr_languages: ['en'] },
  }
  const { state, writes } = await loadBuilder(detail)
  await state.loadCaseDetail(44)
  state.editingCaseId.value = 44

  assert.equal(state.sceneRuntime.value.base_url, 'https://api.example.test')
  assert.equal(state.sceneRuntime.value.retry_times, 3)
  assert.equal(state.sceneRuntime.value.retry_interval, 0.25)
  assert.deepEqual(plain(state.scenarioSteps.value[0].config), {
    selector: 'Continue', selector_type: 'text', retries: 0,
  })

  await state.saveScene()
  assert.equal(writes.length, 1)
  const payload = writes[0][2]
  assert.deepEqual(payload.ui_flow.format_version, 1)
  assert.equal(payload.ui_flow.steps[0].config.selector, 'Continue')
  assert.deepEqual(plain(payload.runtime_config), {
    base_url: 'https://api.example.test', retry_times: 3,
    retry_interval: 0.25, ocr_languages: ['en'],
  })
})

test('recorder acquires a short token-fenced lease only on explicit activation', async () => {
  const idleDevice = {
    id: 31, device_id: 'fixture-serial', status: 'online', locked_by: null,
    maintenance_mode: false, lease_requires_token: false,
  }
  const { state, calls, liveDevices } = await loadBuilder(null, { devices: [idleDevice] })
  state.recorderDevices.value = [idleDevice]
  state.recorderDeviceId.value = idleDevice.id

  assert.equal(state.canStartRecorder.value, true)
  assert.equal(await state.startRecorder(), true)
  assert.deepEqual(plain(calls.acquisitions), [[31, { expected_user_id: 7 }]])
  assert.deepEqual(calls.interactions, [])
  assert.equal(state.recorderLease.value.expectedUserId, 7)
  assert.equal(state.recorderLease.value.leaseToken, 'fixture-token-31')
  assert.deepEqual(calls.heartbeatIntervals.map(({ delay }) => delay), [30000])

  await state.sendRecorderInteraction(31, { action: 'tap', x: 20, y: 40 })
  assert.deepEqual(plain(calls.interactions), [[31, {
    action: 'tap', x: 20, y: 40, lease_token: 'fixture-token-31', expected_user_id: 7,
  }]])

  await state.heartbeatRecorderLease(state.recorderLease.value)
  assert.deepEqual(plain(calls.heartbeats), [[31, {
    lease_token: 'fixture-token-31', lease_seconds: 90,
  }]])

  await state.stopRecorder()
  assert.deepEqual(plain(calls.releases), [[31, {
    expected_user_id: 7, lease_token: 'fixture-token-31',
  }]])
  assert.equal(liveDevices[0].locked_by, null)
})

test('recorder schedules a 30-second server lease heartbeat at one third and renews the same duration', async () => {
  const idleDevice = {
    id: 41, device_id: 'short-lease-serial', status: 'online', locked_by: null,
    maintenance_mode: false, lease_requires_token: false,
  }
  const { state, calls } = await loadBuilder(null, { devices: [idleDevice], leaseSeconds: 30 })
  state.recorderDevices.value = [idleDevice]
  state.recorderDeviceId.value = idleDevice.id

  assert.equal(await state.startRecorder(), true)
  assert.equal(state.recorderLease.value.leaseSeconds, 30)
  assert.deepEqual(calls.heartbeatIntervals.map(({ delay }) => delay), [10000])

  await state.heartbeatRecorderLease(state.recorderLease.value)
  assert.deepEqual(plain(calls.heartbeats), [[41, {
    lease_token: 'fixture-token-41', lease_seconds: 30,
  }]])
  await state.stopRecorder()
})

test('recorder rejects server lease durations outside the bounded integer range', async () => {
  for (const leaseSeconds of [29, 91, '30']) {
    const idleDevice = {
      id: 42, device_id: `invalid-lease-${leaseSeconds}`, status: 'online', locked_by: null,
      maintenance_mode: false, lease_requires_token: false,
    }
    const { state, calls } = await loadBuilder(null, { devices: [idleDevice], leaseSeconds })
    state.recorderDevices.value = [idleDevice]
    state.recorderDeviceId.value = idleDevice.id

    assert.equal(await state.startRecorder(), false)
    assert.equal(state.recorderEnabled.value, false)
    assert.deepEqual(calls.heartbeatIntervals, [])
    assert.deepEqual(calls.heartbeats, [])
    assert.deepEqual(plain(calls.releases), [[42, {
      expected_user_id: 7, lease_token: 'fixture-token-42',
    }]])
  }
})

test('mounting the scene builder does not acquire a lease or send device actions', async () => {
  const idleDevice = {
    id: 38, device_id: 'mount-fixture-serial', status: 'online', locked_by: null,
    maintenance_mode: false, lease_requires_token: false,
  }
  const { calls } = await loadBuilder(null, { devices: [idleDevice] })

  assert.deepEqual(calls.deviceOptions, [undefined])
  assert.deepEqual(calls.acquisitions, [])
  assert.deepEqual(calls.releases, [])
  assert.deepEqual(calls.heartbeats, [])
  assert.deepEqual(calls.interactions, [])
})

test('existing owner leases are visible but are never taken or auto-unlocked', async () => {
  const existingLease = {
    id: 32, device_id: 'owned-serial', status: 'locked', locked_by: 7,
    locked_by_name: 'recorder-owner', locked_at: '2026-09-23T11:00:00Z',
    lease_requires_token: true, maintenance_mode: false,
  }
  const { state, calls } = await loadBuilder(null, { devices: [existingLease] })
  state.recorderDevices.value = [existingLease]
  state.recorderDeviceId.value = existingLease.id

  assert.equal(state.canStartRecorder.value, false)
  assert.equal(await state.startRecorder(), false)
  assert.deepEqual(calls.acquisitions, [])
  assert.deepEqual(calls.releases, [])
  assert.equal(state.recorderEnabled.value, false)
})

test('account changes disable recording and fence all later interactions', async () => {
  const idleDevice = {
    id: 33, device_id: 'account-fence-serial', status: 'available', locked_by: null,
    maintenance_mode: false, lease_requires_token: false,
  }
  const { state, calls, userStore } = await loadBuilder(null, { devices: [idleDevice] })
  state.recorderDevices.value = [idleDevice]
  state.recorderDeviceId.value = idleDevice.id
  assert.equal(await state.startRecorder(), true, state.recorderErrorKey.value)

  userStore.user = { id: 8, username: 'different-user' }
  await nextTick()
  assert.equal(state.recorderEnabled.value, false)
  assert.equal(state.recorderErrorKey.value, 'appAutomation.recorder.errors.accountChanged')
  await assert.rejects(state.sendRecorderInteraction(33, { action: 'tap', x: 1, y: 2 }))
  assert.deepEqual(calls.interactions, [])
  assert.deepEqual(calls.releases, [])
})

test('same-generation access-token refresh keeps recorder active', async () => {
  const idleDevice = {
    id: 39, device_id: 'session-refresh-serial', status: 'online', locked_by: null,
    maintenance_mode: false, lease_requires_token: false,
  }
  const { state, calls, userStore } = await loadBuilder(null, { devices: [idleDevice] })
  state.recorderDevices.value = [idleDevice]
  state.recorderDeviceId.value = idleDevice.id
  assert.equal(await state.startRecorder(), true)

  const originalUser = userStore.user
  userStore.accessToken = 'refreshed-access-token'
  await nextTick()

  assert.equal(userStore.user, originalUser)
  assert.equal(userStore.getSessionGeneration(), 1)
  assert.equal(state.recorderEnabled.value, true)
  assert.equal(state.recorderLease.value.sessionGeneration, 1)
  await state.sendRecorderInteraction(39, { action: 'tap', x: 1, y: 2 })
  assert.equal(calls.interactions.length, 1)
  assert.deepEqual(calls.releases, [])
  await state.stopRecorder({ release: false })
})

test('new generation fences recording even when the account stays the same', async () => {
  const idleDevice = {
    id: 40, device_id: 'session-generation-serial', status: 'online', locked_by: null,
    maintenance_mode: false, lease_requires_token: false,
  }
  const { state, calls, userStore, setSessionGeneration } = await loadBuilder(null, { devices: [idleDevice] })
  state.recorderDevices.value = [idleDevice]
  state.recorderDeviceId.value = idleDevice.id
  assert.equal(await state.startRecorder(), true)

  const originalUser = userStore.user
  setSessionGeneration(2)
  userStore.accessToken = 'new-session-token'
  await nextTick()

  assert.equal(userStore.user, originalUser)
  assert.equal(state.recorderEnabled.value, false)
  assert.equal(state.recorderErrorKey.value, 'appAutomation.recorder.errors.accountChanged')
  assert.deepEqual(calls.releases, [])
  await assert.rejects(state.sendRecorderInteraction(40, { action: 'tap', x: 1, y: 2 }))
  assert.deepEqual(calls.interactions, [])
})

test('device changes stop recording and release only the recorder-owned lease token', async () => {
  const devices = [
    { id: 34, device_id: 'first-serial', status: 'online', locked_by: null, maintenance_mode: false, lease_requires_token: false },
    { id: 35, device_id: 'second-serial', status: 'online', locked_by: null, maintenance_mode: false, lease_requires_token: false },
  ]
  const { state, calls } = await loadBuilder(null, { devices })
  state.recorderDevices.value = devices
  state.recorderDeviceId.value = 34
  assert.equal(await state.startRecorder(), true)

  state.recorderDeviceId.value = 35
  await nextTick()
  await new Promise(resolve => setImmediate(resolve))

  assert.equal(state.recorderEnabled.value, false)
  assert.deepEqual(plain(calls.releases), [[34, {
    expected_user_id: 7, lease_token: 'fixture-token-34',
  }]])
})

test('recorder lease release waits until its active interaction settles', async () => {
  const device = {
    id: 43, device_id: 'pending-interaction-serial', status: 'online', locked_by: null,
    maintenance_mode: false, lease_requires_token: false,
  }
  const { state, calls } = await loadBuilder(null, { devices: [device] })
  state.recorderDevices.value = [device]
  state.recorderDeviceId.value = device.id
  assert.equal(await state.startRecorder(), true)

  const context = state.sendRecorderInteraction.getActionContext()
  state.handleRecorderInteractionState({ active: true, context })
  await state.stopRecorder()

  assert.equal(state.recorderEnabled.value, false)
  assert.equal(state.canStartRecorder.value, false)
  assert.equal(state.recorderDiagnosticBusy.value, true)
  assert.deepEqual(calls.releases, [])

  state.handleRecorderInteractionState({
    active: false,
    context: { ...context, sceneGeneration: context.sceneGeneration + 1 },
  })
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(calls.releases, [])
  assert.ok(state.deferredRecorderRelease.value)

  state.handleRecorderInteractionState({ active: false, context })
  await new Promise(resolve => setImmediate(resolve))

  assert.deepEqual(plain(calls.releases), [[43, {
    expected_user_id: 7, lease_token: 'fixture-token-43',
  }]])
  assert.equal(state.deferredRecorderRelease.value, null)
  assert.equal(state.recorderDiagnosticBusy.value, false)
})

test('diagnostics stay blocked until the stop request and asynchronous lease release settle', async () => {
  const releaseRequest = deferred()
  const device = {
    id: 46, device_id: 'diagnostic-release-serial', status: 'online', locked_by: null,
    maintenance_mode: false, lease_requires_token: false,
  }
  const { state, calls } = await loadBuilder(null, {
    devices: [device], releaseRecorderLease: () => releaseRequest.promise,
  })
  state.recorderDevices.value = [device]
  state.recorderDeviceId.value = device.id
  assert.equal(await state.startRecorder(), true)

  const stopping = state.stopRecorder()
  assert.equal(state.recorderEnabled.value, false)
  assert.equal(state.recorderDiagnosticBusy.value, true)
  assert.equal(calls.releases.length, 1)
  releaseRequest.resolve({ data: { success: true } })
  await stopping

  assert.equal(state.recorderDiagnosticBusy.value, false)
})

test('recorded gestures append runnable relative-coordinate ui_flow steps', async () => {
  const idleDevice = {
    id: 36, device_id: 'staging-serial', status: 'online', locked_by: null,
    maintenance_mode: false, lease_requires_token: false,
  }
  const { state, writes } = await loadBuilder(null, { devices: [idleDevice] })
  state.componentDefinitions.value = {
    ...state.componentDefinitions.value,
    click: { schema: { required: ['selector_type', 'selector'], properties: {
      selector_type: { type: 'string' }, selector: { type: 'string' },
    } } },
    swipe: { schema: { required: ['direction'], properties: {
      direction: { type: 'string', enum: ['up', 'down', 'left', 'right'] },
      selector_type: { type: 'string' }, start: { type: 'string' }, end: { type: 'string' },
    } } },
    key_event: { schema: { required: ['key'], properties: {
      key: { type: 'string', enum: ['BACK', 'HOME', 'ENTER'] },
    } } },
  }
  state.recorderDevices.value = [idleDevice]
  state.recorderDeviceId.value = idleDevice.id
  assert.equal(await state.startRecorder(), true)
  assert.ok(state.getFieldOptions('selector_type', state.componentDefinitions.value.click, {
    type: 'click', config: {},
  }).some(option => option.value === 'relative'))

  state.appendRecorderStep({ action: 'tap', x_ratio: 0.125, y_ratio: 0.75 })
  state.appendRecorderStep({
    action: 'swipe',
    start: { x_ratio: 0.1, y_ratio: 0.2 },
    end: { x_ratio: 0.9, y_ratio: 0.95 },
    duration_ms: 450,
  })
  state.appendRecorderStep({ action: 'key', key: 'enter' })
  state.sceneForm.value.name = 'Recorded gestures'
  await state.saveScene()

  const steps = writes[0][1].ui_flow
  assert.deepEqual(plain(steps.map(step => ({ type: step.type, config: step.config }))), [
    { type: 'click', config: { selector_type: 'relative', selector: '0.125,0.75' } },
    { type: 'swipe', config: {
      coordinate_space: 'image_ratio', selector_type: 'relative',
      start: '0.1,0.2', end: '0.9,0.95', direction: 'right', duration: 0.45,
    } },
    { type: 'key_event', config: { key: 'ENTER' } },
  ])
  await state.stopRecorder()
})

test('maintenance errors are surfaced and recording is disabled', async () => {
  const idleDevice = {
    id: 37, device_id: 'maintenance-serial', status: 'online', locked_by: null,
    maintenance_mode: false, lease_requires_token: false,
  }
  const { state } = await loadBuilder(null, { devices: [idleDevice] })
  state.recorderDevices.value = [idleDevice]
  state.recorderDeviceId.value = idleDevice.id
  assert.equal(await state.startRecorder(), true)

  state.handleRecorderError({ operation: 'interaction', status: 504 })
  assert.equal(state.recorderEnabled.value, false)
  assert.equal(state.recorderErrorKey.value, 'appAutomation.recorder.errors.maintenance')
})

test('nested editor can visually add a schema-defined component and creates fresh nested ids', async () => {
  const loaded = await loadSfcSetup('src/views/app-automation/test-cases/NestedFlowEditor.vue', {
    'vuedraggable': { default: {} },
    'element-plus': { ElMessage: { warning() {} } },
  })
  instances.push(loaded)
  const emitted = []
  const scope = effectScope()
  const state = scope.run(() => loaded.component.setup({
    modelValue: [],
    label: 'Then steps',
    components: [{
      type: 'flow', name: 'Nested flow', kind: 'custom', default_config: {},
      steps: [{ id: 'source-child', type: 'tap', config: { selector: 'x' } }],
    }],
    componentDefinitions: { flow: stepDefinition },
    fieldLabel: field => field,
    fieldOptions: () => [],
    depth: 0,
    maxDepth: 12,
  }, { expose() {}, emit: (event, value) => emitted.push([event, value]) }))

  state.newStepType.value = 'flow'
  state.addStep()
  const added = emitted.at(-1)[1][0]
  assert.equal(added.type, 'flow')
  assert.notEqual(added.steps[0].id, 'source-child')
  assert.equal(added.steps[0].config.selector, 'x')
  scope.stop()
})

test('nested custom parameter arrays named like flow fields remain structured data', async () => {
  const parameterDefaults = {
    steps: [{ id: 'payload-row', type: 'business-value', config: { _expanded: 'keep' } }],
    then_steps: ['then-data'],
    try_steps: ['try-data'],
  }
  const component = {
    type: 'versioned_data', name: 'Versioned data', kind: 'custom', version: '2', optionKey: 'versioned_data@2',
    schema: { properties: {
      steps: { type: 'array' }, then_steps: { type: 'array' }, try_steps: { type: 'array' },
    } },
    default_config: parameterDefaults,
  }
  const loaded = await loadSfcSetup('src/views/app-automation/test-cases/NestedFlowEditor.vue', {
    vuedraggable: { default: {} },
    'element-plus': { ElMessage: { warning() {} } },
  })
  instances.push(loaded)
  const emitted = []
  const scope = effectScope()
  const state = scope.run(() => loaded.component.setup({
    modelValue: [], label: 'Parameters', components: [component],
    componentDefinitions: { 'versioned_data@2': component },
    fieldLabel: field => field, fieldOptions: () => [], depth: 0, maxDepth: 12,
  }, { expose() {}, emit: (event, value) => emitted.push([event, value]) }))
  const existing = { id: 'existing', ...component, config: structuredClone(parameterDefaults) }
  assert.equal(state.isStepArrayField(existing, 'steps', component.schema.properties.steps), false)
  assert.equal(state.isStructuredField(existing, 'steps'), true)

  state.newStepType.value = 'versioned_data@2'
  state.addStep()
  const added = emitted.at(-1)[1][0]
  assert.deepEqual(plain(added.config), parameterDefaults)
  assert.deepEqual(plain(added.config.steps[0]), parameterDefaults.steps[0])
  state.updateStructuredField(added, 'steps', '["edited-data"]')
  assert.deepEqual(plain(added.config.steps), ['edited-data'])
  scope.stop()
})

test('versioned project component references expose schema fields and save pinned identity', async () => {
  const { state, writes } = await loadBuilder()
  const definition = {
    id: 19,
    name: 'Synthetic login',
    type: 'project_login',
    version: '4',
    schema: {
      properties: {
        account: { type: 'string' },
        retry_count: { type: 'integer', minimum: 0, maximum: 3 },
      },
      required: ['account'],
    },
    default_config: { account: 'synthetic-account' },
    steps: [{ type: 'input', config: { value: '{{params.account}}' } }],
  }
  state.setProjectComponents([definition])
  state.insertProjectComponent(definition)
  state.sceneForm.value.name = 'Synthetic versioned component case'

  assert.ok(state.schemaFields.value.includes('account'))
  assert.ok(state.schemaFields.value.includes('retry_count'))
  assert.equal(state.activeStep.value.type, 'project_login')
  assert.equal(state.activeStep.value.version, '4')
  state.activeStep.value.config.account = 'synthetic-edited-account'

  await state.saveScene()

  assert.equal(writes.length, 1)
  const savedReference = writes[0][1].ui_flow[0]
  assert.equal(savedReference.type, 'project_login')
  assert.equal(savedReference.version, '4')
  assert.deepEqual(plain(savedReference.config), { account: 'synthetic-edited-account' })
  assert.equal(savedReference.steps, undefined)

  const source = await readFile(new URL('../src/views/app-automation/test-cases/SceneBuilder.vue', import.meta.url), 'utf8')
  assert.match(source, /activeParentStep\.kind === 'custom' && activeParentStep\.version === undefined && selectedSubIndex === null/)
})

test('custom-component JSON editors are wired to draft and validation handlers', async () => {
  const source = await readFile(new URL('../src/views/app-automation/test-cases/SceneBuilder.vue', import.meta.url), 'utf8')
  assert.match(source, /@update:model-value="updateJsonField\(editingActiveStep\.config, field, \$event, editingActiveStep, editingActiveDef\)"/)
  assert.match(source, /@update:model-value="updateExpectedList\(editingActiveStep\.config, \$event\)"/)
  assert.match(source, /:required="editingSchemaRequired\.includes\(field\)"/)
  assert.match(source, /runtime_config: normalizeSceneRuntime\(sceneRuntime\.value\)/)
  assert.doesNotMatch(source, /runtime:\s*normalizeSceneRuntime\(sceneRuntime\.value\)/)
})

test('sensitive scene variables round-trip as aliases and block plaintext saves', async () => {
  const detail = {
    id: 48,
    name: 'Synthetic sensitive variables',
    description: '',
    project: 3,
    variables: [
      { name: 'api_token', scope: 'global', type: 'string', value: 'synthetic-plaintext-token', description: 'API token', sensitive: true },
      { name: 'empty_token', scope: 'local', type: 'string', value: '', description: '', sensitive: true },
      { name: 'display_name', scope: 'local', type: 'string', value: 'Synthetic', description: '', sensitive: false },
    ],
    ui_flow: [{ id: 'variable-save-step', type: 'tap', name: 'Tap continue', config: { selector: 'Continue' } }],
  }
  const { state, writes, messages } = await loadBuilder(detail)
  await state.loadCaseDetail(48)
  state.editingCaseId.value = 48

  assert.equal(state.sceneVariables.value[0].sensitive, true)
  assert.equal(state.sceneVariables.value[1].sensitive, true)
  assert.equal(state.sceneVariables.value[2].sensitive, false)

  await state.saveScene()
  assert.equal(writes.length, 0)
  assert.ok(messages.some(message => message.type === 'error' && message.text === 'appAutomation.sceneData.secretHelp'))

  state.sceneVariables.value[0].value = ' secret://synthetic_api_token'
  await state.saveScene()
  assert.equal(writes.length, 0)

  state.sceneVariables.value[0].value = 'secret://synthetic_api_token'
  await state.saveScene()

  assert.equal(writes.length, 1)
  assert.deepEqual(plain(writes[0][2].variables), [
    { name: 'api_token', scope: 'global', type: 'string', value: 'secret://synthetic_api_token', description: 'API token', sensitive: true },
    { name: 'empty_token', scope: 'local', type: 'string', value: '', description: '', sensitive: true },
    { name: 'display_name', scope: 'local', type: 'string', value: 'Synthetic', description: '', sensitive: false },
  ])

  const source = await readFile(new URL('../src/views/app-automation/test-cases/SceneBuilder.vue', import.meta.url), 'utf8')
  assert.match(source, /v-model="item\.sensitive">\{\{ \$t\('appAutomation\.sceneData\.sensitiveVariable'\) \}\}<\/el-checkbox>/)
  assert.match(source, /appAutomation\.sceneData\.secretHelp/)
})

test('in-flight recorder action blocks navigation and reset, and fences a changed project', async () => {
  const device = {
    id: 43, device_id: 'fixture-device', status: 'online', locked_by: null,
    maintenance_mode: false, lease_requires_token: false,
  }
  const { state, calls } = await loadBuilder(null, { devices: [device] })
  state.sceneForm.value.name = 'Original scene'
  state.recorderDevices.value = [device]
  state.recorderDeviceId.value = device.id
  assert.equal(await state.startRecorder(), true)
  const context = state.sendRecorderInteraction.getActionContext()
  state.handleRecorderInteractionState({ active: true, context, action: 'key' })

  let prevented = false
  state.beforeUnloadHandler({ preventDefault: () => { prevented = true }, returnValue: undefined })
  assert.equal(prevented, true)
  assert.equal(await calls.routeGuard(), false)
  await state.resetScene()
  assert.equal(state.sceneForm.value.name, 'Original scene')

  state.sceneForm.value.project = 99
  await assert.rejects(state.sendRecorderInteraction(device.id, { action: 'key', key: 'back' }))
  assert.deepEqual(calls.interactions, [])
  state.sceneForm.value.project = null
  state.handleRecorderInteractionState({ active: false, context, action: 'key' })
  await state.stopRecorder()
})

test('unresolved recorder actions trigger an explicit leave warning', async () => {
  const { state, calls } = await loadBuilder()
  state.handleRecorderUnresolvedChange(1)
  assert.equal(state.recorderDiagnosticBusy.value, true)
  let prevented = false
  state.beforeUnloadHandler({ preventDefault: () => { prevented = true }, returnValue: undefined })
  assert.equal(prevented, true)
  assert.equal(await calls.routeGuard(), true)
  assert.equal(calls.confirmations.at(-1)[0], 'appAutomation.recorder.unresolvedLeave')
  state.handleRecorderUnresolvedChange(0)
  assert.equal(state.recorderDiagnosticBusy.value, false)
})

test('an uncertain recorder result disables recording without releasing its lease', async () => {
  const device = {
    id: 44, device_id: 'fixture-device', status: 'online', locked_by: null,
    maintenance_mode: false, lease_requires_token: false,
  }
  const { state, calls } = await loadBuilder(null, { devices: [device] })
  state.recorderDevices.value = [device]
  state.recorderDeviceId.value = device.id
  assert.equal(await state.startRecorder(), true)
  const context = state.sendRecorderInteraction.getActionContext()
  state.handleRecorderInteractionState({ active: true, context, action: 'tap' })
  state.handleRecorderError({ operation: 'interaction', uncertain: true })
  await nextTick()

  assert.equal(state.recorderEnabled.value, false)
  assert.equal(state.recorderErrorKey.value, 'appAutomation.recorder.errors.interactionUncertain')
  assert.deepEqual(calls.releases, [])
  state.handleRecorderInteractionState({ active: false, context, action: 'tap' })
})

test('a completed action needing device recovery cancels a deferred lease release', async () => {
  const device = {
    id: 45, device_id: 'fixture-device', status: 'online', locked_by: null,
    maintenance_mode: false, lease_requires_token: false,
  }
  const { state, calls } = await loadBuilder(null, { devices: [device] })
  state.recorderDevices.value = [device]
  state.recorderDeviceId.value = device.id
  assert.equal(await state.startRecorder(), true)
  const context = state.sendRecorderInteraction.getActionContext()
  state.handleRecorderInteractionState({ active: true, context, action: 'tap' })
  await state.stopRecorder()
  assert.ok(state.deferredRecorderRelease.value)
  assert.ok(state.appendRecorderStep({ step: { action: 'key', key: 'back' }, context }))
  state.handleRecorderError({ operation: 'interaction', actionOutcome: 'completed', recoveryRequired: true, status: 503 })
  state.handleRecorderInteractionState({ active: false, context, action: 'tap' })
  await new Promise(resolve => setImmediate(resolve))

  assert.equal(state.recorderEnabled.value, false)
  assert.equal(state.deferredRecorderRelease.value, null)
  assert.equal(state.scenarioSteps.value.length, 1)
  assert.equal(state.recorderErrorKey.value, 'appAutomation.recorder.errors.completedNeedsRecovery')
  assert.deepEqual(calls.releases, [])
})
