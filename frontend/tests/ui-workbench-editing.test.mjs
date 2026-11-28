import assert from 'node:assert/strict'
import { test } from 'node:test'
import { nextTick, reactive, ref } from 'vue'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'
import * as contextHelpers from '../src/utils/ui-project-context.mjs'
import * as caseHelpers from '../src/utils/ui-case-execution.mjs'
import { uiBrowserOptions, uiEngineOptions, uiReadinessError } from '../src/utils/ui-execution-options.mjs'

const flush = async () => { await new Promise(resolve => setImmediate(resolve)); await nextTick() }
const icons = (...names) => Object.fromEntries(names.map(name => [name, {}]))
const fixture = () => [
  { id: 11, project: 2, name: 'Case A', steps: [{ id: 1, step_number: 1, action_type: 'fill', element: 1, input_value: 'original', wait_time: 1000, description: 'First step' }] },
  { id: 12, project: 2, name: 'Case B', steps: [] },
]

async function editor(t, overrides = {}) {
  const records = fixture()
  const scope = { projects: ref([{ id: 2, name: 'Project' }, { id: 3, name: 'Other' }]), projectId: ref(2), userId: ref(1), projectsLoading: ref(false), projectsError: ref(false), invalidProject: ref(false), reloadProjects() {} }
  const route = reactive({ name: 'UiTestCases', query: { project: '2' } })
  const updates = []
  const result = await loadSfcSetup('src/views/ui-automation/test-cases/TestCaseManager.vue', {
    '@/composables/useUiProjectContext': { useUiProjectContext: () => scope },
    '@/utils/ui-project-context.mjs': contextHelpers,
    '@/utils/ui-case-execution.mjs': caseHelpers,
    'vue-router': { useRoute: () => route, useRouter: () => ({ replace: async () => {}, push: async () => {} }) },
    '@element-plus/icons-vue': icons('Search', 'Plus', 'Edit', 'Delete', 'Check', 'CaretRight', 'ArrowUp', 'ArrowDown', 'Rank', 'Picture', 'Warning', 'View', 'ZoomIn', 'WarningFilled', 'MagicStick'),
    'vuedraggable': { default: {} }, '@/components/DataFactorySelector.vue': { default: {} },
    '@/api/data-factory': { getVariableFunctions: async () => ({ data: [] }) },
    '@/api/ui_automation': {
      getTestCases: async () => ({ data: { count: records.length, next: null, results: records } }),
      getElements: async () => ({ data: { results: [] } }), getLocatorStrategies() {},
      updateTestCase: async (id, data) => { updates.push({ id, data: plain(data) }); return { data: { ...records.find(row => row.id === id), ...data } } },
      createTestCase: async data => ({ data: { ...data, id: 13, steps: [] } }),
      deleteTestCase() {}, copyTestCase() {}, runTestCase() {}, ...overrides,
    },
  })
  t.after(result.dispose)
  await flush()
  result.state.selectTestCase(result.state.testCases.value[0])
  return { ...result, scope, route, records, updates }
}

async function tryDiscardDuringSave(h) {
  const selection = h.state.selectTestCase(h.records[1])
  if (h.state.unsavedVisible.value) await h.state.chooseUnsaved('discard')
  return await selection
}

test('ordinary step save blocks leaving until its submitted snapshot settles', async t => {
  const response = deferred()
  const h = await editor(t, { updateTestCase: () => response.promise })
  h.state.currentSteps.value[0].input_value = 'submitted'
  const save = h.state.saveTestCase()
  assert.equal(await tryDiscardDuringSave(h), false)
  assert.equal(h.state.selectedTestCase.value.id, 11)
  assert.equal(h.state.unsavedVisible.value, false)
  assert.equal(h.routeLeaveGuards[0](), false)
  response.resolve({ data: { ...h.records[0], steps: [{ ...h.records[0].steps[0], input_value: 'submitted' }] } })
  assert.equal(await save, true)
  assert.equal(h.state.hasUnsavedChanges.value, false)
  assert.equal(await h.state.selectTestCase(h.records[1]), true)
  assert.ok(h.messages.some(row => row.type === 'warning' && String(row.text).includes('savePending')))
})

test('pending save protects reverted local content, route/project changes, creation and beforeunload', async t => {
  const response = deferred()
  const h = await editor(t, { updateTestCase: () => response.promise })
  h.state.currentSteps.value[0].input_value = 'submitted'
  const save = h.state.saveTestCase()
  h.state.currentSteps.value[0].input_value = 'original'
  assert.equal(h.state.hasUnsavedChanges.value, false)
  assert.equal(await tryDiscardDuringSave(h), false)
  assert.equal(h.routeUpdateGuards[0]({ name: 'UiTestCases', query: { project: '3' } }, h.route), false)
  assert.equal(await h.state.openCreateCase(), false)
  let prevented = false
  h.state.beforeUnload({ preventDefault() { prevented = true } })
  assert.equal(prevented, true)
  response.resolve({ data: { ...h.records[0], steps: [{ ...h.records[0].steps[0], input_value: 'submitted' }] } })
  await save
  assert.equal(h.state.hasUnsavedSteps.value, true)
})

test('later local edits stay dirty after the pending save and discard only those edits', async t => {
  const response = deferred()
  const h = await editor(t, { updateTestCase: () => response.promise })
  h.state.currentSteps.value[0].input_value = 'submitted'
  const save = h.state.saveTestCase()
  h.state.currentSteps.value[0].input_value = 'later local edit'
  assert.equal(await tryDiscardDuringSave(h), false)
  response.resolve({ data: { ...h.records[0], steps: [{ ...h.records[0].steps[0], input_value: 'submitted' }] } })
  await save
  assert.equal(h.state.currentSteps.value[0].input_value, 'later local edit')
  assert.equal(h.state.hasUnsavedSteps.value, true)
  const leave = h.state.selectTestCase(h.records[1])
  await h.state.chooseUnsaved('discard')
  assert.equal(await leave, true)
  h.state.selectTestCase(h.state.testCases.value.find(row => row.id === 11))
  assert.equal(h.state.currentSteps.value[0].input_value, 'submitted')
})

test('failed ordinary save releases the busy guard while preserving edits for an explicit discard', async t => {
  const response = deferred()
  const h = await editor(t, { updateTestCase: () => response.promise })
  h.state.currentSteps.value[0].input_value = 'not saved'
  const save = h.state.saveTestCase()
  assert.equal(await tryDiscardDuringSave(h), false)
  response.reject(new Error('save refused'))
  assert.equal(await save, false)
  assert.equal(h.state.savingSteps.value, false)
  assert.equal(h.state.currentSteps.value[0].input_value, 'not saved')
  const leave = h.state.selectTestCase(h.records[1])
  assert.equal(h.state.unsavedVisible.value, true)
  await h.state.chooseUnsaved('discard')
  assert.equal(await leave, true)
})

test('an already open unsaved prompt cannot discard a subsequently started save', async t => {
  const response = deferred()
  const h = await editor(t, { updateTestCase: () => response.promise })
  h.state.currentSteps.value[0].input_value = 'submitted'
  const leave = h.state.selectTestCase(h.records[1])
  const save = h.state.saveTestCase()
  await h.state.chooseUnsaved('discard')
  assert.equal(h.state.selectedTestCase.value.id, 11)
  assert.equal(h.state.unsavedVisible.value, true)
  assert.equal(h.state.currentSteps.value[0].input_value, 'submitted')
  await h.state.chooseUnsaved('cancel')
  assert.equal(await leave, false)
  response.resolve({ data: { ...h.records[0], steps: [{ ...h.records[0].steps[0], input_value: 'submitted' }] } })
  await save
})

test('ordinary form save blocks navigation and closing even if its fields are locally reverted', async t => {
  const response = deferred()
  const h = await editor(t, { updateTestCase: () => response.promise })
  h.state.editTestCase(h.records[0])
  h.state.testCaseForm.name = 'submitted name'
  const save = h.state.saveTestCaseForm()
  h.state.testCaseForm.name = h.records[0].name
  assert.equal(h.state.hasUnsavedForm.value, false)
  assert.equal(h.routeLeaveGuards[0](), false)
  assert.equal(await h.state.requestCloseCaseForm(), false)
  response.resolve({ data: { ...h.records[0], name: 'submitted name' } })
  assert.equal(await save, false)
  assert.equal(h.state.showCreateDialog.value, true)
  assert.equal(h.state.hasUnsavedForm.value, true)
})

test('an unchanged successful new form save still selects the created case', async t => {
  const h = await editor(t)
  h.state.openCreateCase()
  h.state.testCaseForm.name = 'created normally'
  assert.equal(await h.state.saveTestCaseForm(), true)
  assert.equal(h.state.showCreateDialog.value, false)
  assert.equal(h.state.selectedTestCase.value.id, 13)
  assert.equal(h.state.hasUnsavedChanges.value, false)
})

test('form save keeps later edits dirty and does not release a pending leave guard', async t => {
  const save = deferred()
  const h = await editor(t, { updateTestCase: () => save.promise })
  h.state.editTestCase(h.records[0])
  h.state.testCaseForm.name = 'submitted name'
  const leave = h.routeLeaveGuards[0]()
  const saving = h.state.chooseUnsaved('save')
  h.state.testCaseForm.name = 'later edit'
  save.resolve({ data: { ...h.records[0], name: 'submitted name' } })
  await saving
  assert.equal(h.state.showCreateDialog.value, true)
  assert.equal(h.state.testCaseForm.name, 'later edit')
  assert.equal(h.state.hasUnsavedForm.value, true)
  assert.equal(h.state.unsavedVisible.value, true)
  assert.equal(h.state.selectedTestCase.value.name, 'submitted name')
  await h.state.chooseUnsaved('cancel')
  assert.equal(await leave, false)
})

test('creating with edits during submission keeps the draft bound to the created case for the next save', async t => {
  const save = deferred()
  let creates = 0
  const updates = []
  const h = await editor(t, {
    createTestCase: () => { creates += 1; return save.promise },
    updateTestCase: async (id, data) => { updates.push({ id, data: plain(data) }); return { data: { ...data, id, steps: [] } } },
  })
  h.state.openCreateCase()
  h.state.testCaseForm.name = 'submitted name'
  const saving = h.state.saveTestCaseForm()
  h.state.testCaseForm.name = 'later name'
  save.resolve({ data: { id: 13, project: 2, name: 'submitted name', steps: [] } })
  assert.equal(await saving, false)
  assert.equal(h.state.showCreateDialog.value, true)
  assert.equal(h.state.hasUnsavedForm.value, true)
  assert.equal(h.state.testCaseForm.name, 'later name')
  assert.equal(h.state.editingTestCase.value.id, 13)
  assert.equal(await h.state.saveTestCaseForm(), true)
  assert.equal(creates, 1)
  assert.equal(updates[0].id, 13)
  assert.equal(updates[0].data.name, 'later name')
})

test('case switching offers keep editing or discard without silently replacing dirty steps', async t => {
  const h = await editor(t)
  h.state.currentSteps.value[0].input_value = 'unsaved value'
  const cancelled = h.state.selectTestCase(h.records[1])
  assert.equal(h.state.unsavedVisible.value, true)
  assert.equal(h.state.selectedTestCase.value.id, 11)
  await h.state.chooseUnsaved('cancel')
  assert.equal(await cancelled, false)
  assert.equal(h.state.currentSteps.value[0].input_value, 'unsaved value')
  const discarded = h.state.selectTestCase(h.records[1])
  await h.state.chooseUnsaved('discard')
  assert.equal(await discarded, true)
  assert.equal(h.state.selectedTestCase.value.id, 12)
  h.state.selectTestCase(h.records[0])
  assert.equal(h.state.currentSteps.value[0].input_value, 'original')
  assert.equal(h.updates.length, 0)
})

test('save and continue persists dirty steps before selecting another case', async t => {
  const h = await editor(t)
  h.state.currentSteps.value[0].input_value = 'saved value'
  h.state.currentSteps.value[0].element_id = 101
  const selection = h.state.selectTestCase(h.records[1])
  await h.state.chooseUnsaved('save')
  assert.equal(await selection, true)
  assert.equal(h.updates[0].id, 11)
  assert.equal(h.updates[0].data.steps[0].input_value, 'saved value')
  assert.equal(h.updates[0].data.steps[0].element_id, 101)
  assert.equal(h.state.selectedTestCase.value.id, 12)
  assert.equal(h.state.hasUnsavedChanges.value, false)
})

test('failed save preserves edits and leaves navigation pending until the user keeps editing', async t => {
  const h = await editor(t, { updateTestCase: async () => { throw new Error('Cannot save') } })
  h.state.currentSteps.value[0].input_value = 'must survive'
  const selection = h.state.selectTestCase(h.records[1])
  await h.state.chooseUnsaved('save')
  assert.equal(h.state.selectedTestCase.value.id, 11)
  assert.equal(h.state.currentSteps.value[0].input_value, 'must survive')
  assert.equal(h.state.unsavedVisible.value, true)
  assert.equal(h.state.hasUnsavedChanges.value, true)
  await h.state.chooseUnsaved('cancel')
  assert.equal(await selection, false)
})

test('edits made while a save is in flight remain dirty and prevent navigation', async t => {
  const save = deferred()
  const h = await editor(t, { updateTestCase: () => save.promise })
  h.state.currentSteps.value[0].input_value = 'first change'
  const selection = h.state.selectTestCase(h.records[1])
  const saving = h.state.chooseUnsaved('save')
  h.state.currentSteps.value[0].input_value = 'second change'
  save.resolve({ data: {} })
  await saving
  assert.equal(h.state.hasUnsavedSteps.value, true)
  assert.equal(h.state.selectedTestCase.value.id, 11)
  assert.equal(h.state.currentSteps.value[0].input_value, 'second change')
  assert.equal(h.state.unsavedVisible.value, true)
  await h.state.chooseUnsaved('cancel')
  assert.equal(await selection, false)
})

test('route departure, case/project query changes and beforeunload protect the same edits', async t => {
  const h = await editor(t)
  h.state.currentSteps.value[0].description = 'unsaved description'
  const leave = h.routeLeaveGuards[0]()
  await h.state.chooseUnsaved('cancel')
  assert.equal(await leave, false)
  const update = h.routeUpdateGuards[0]({ name: 'UiTestCases', query: { project: '3' } }, h.route)
  await h.state.chooseUnsaved('cancel')
  assert.equal(await update, false)
  assert.equal(h.scope.projectId.value, 2)
  let prevented = false
  const event = { preventDefault() { prevented = true } }
  h.state.beforeUnload(event)
  assert.equal(prevented, true)
  assert.equal(event.returnValue, '')
  assert.equal(h.routeUpdateGuards[0]({ name: 'UiTestCases', query: { project: '2', stepNumber: '1' } }, h.route), true)
})

test('closing a dirty case form uses save/discard/keep and preserves underlying step edits', async t => {
  const h = await editor(t)
  h.state.currentSteps.value[0].input_value = 'underlying edit'
  h.state.editTestCase(h.records[0])
  h.state.testCaseForm.name = 'draft name'
  let closed = false
  const close = h.state.requestCloseCaseForm(() => { closed = true })
  await h.state.chooseUnsaved('cancel')
  assert.equal(await close, false)
  assert.equal(closed, false)
  assert.equal(h.state.showCreateDialog.value, true)
  assert.equal(h.state.testCaseForm.name, 'draft name')
  const discard = h.state.requestCloseCaseForm(() => { closed = true })
  await h.state.chooseUnsaved('discard')
  assert.equal(await discard, true)
  assert.equal(closed, true)
  assert.equal(h.state.currentSteps.value[0].input_value, 'underlying edit')
  assert.equal(h.state.hasUnsavedSteps.value, true)
})

test('save on form close persists the form without discarding or implicitly saving dirty steps', async t => {
  const h = await editor(t)
  h.state.currentSteps.value[0].input_value = 'underlying edit'
  h.state.editTestCase(h.records[0])
  h.state.testCaseForm.name = 'New name'
  const close = h.state.requestCloseCaseForm()
  await h.state.chooseUnsaved('save')
  assert.equal(await close, true)
  assert.equal(h.updates.length, 1)
  assert.equal(h.updates[0].data.name, 'New name')
  assert.equal(h.updates[0].data.steps, undefined)
  assert.equal(h.state.currentSteps.value[0].input_value, 'underlying edit')
  assert.equal(h.state.hasUnsavedSteps.value, true)
})

test('new case form changes also prevent route departure and remain on failed creation', async t => {
  const h = await editor(t, { createTestCase: async () => { throw new Error('Create failed') } })
  h.state.openCreateCase()
  h.state.testCaseForm.name = 'New unsaved case'
  const leave = h.routeLeaveGuards[0]()
  await h.state.chooseUnsaved('save')
  assert.equal(h.state.showCreateDialog.value, true)
  assert.equal(h.state.testCaseForm.name, 'New unsaved case')
  assert.equal(h.state.unsavedVisible.value, true)
  await h.state.chooseUnsaved('cancel')
  assert.equal(await leave, false)
})

test('saving the removal of the last step clears the case and prevents running an empty editor', async t => {
  const h = await editor(t)
  h.state.removeStep(0)
  assert.equal(h.state.hasUnsavedSteps.value, true)
  assert.equal(await h.state.saveTestCase(), true)
  assert.deepEqual(h.updates[0].data.steps, [])
  assert.deepEqual(plain(h.state.selectedTestCase.value.steps), [])
  assert.equal(h.state.hasUnsavedSteps.value, false)
  assert.equal(h.state.canRunSelectedCase.value, false)
})

test('element selection loads every page rather than only the first twenty records', async t => {
  const calls = []
  const h = await editor(t, { getElements: async params => {
    calls.push(plain(params))
    return { data: { count: 101, next: params.page === 1 ? '/untrusted-next-is-not-followed' : null,
      results: params.page === 1 ? Array.from({ length: 100 }, (_, index) => ({ id: index + 1, name: `Element ${index + 1}` })) : [{ id: 101, name: 'Last element' }] } }
  } })
  assert.equal(h.state.availableElements.value.length, 101)
  assert.equal(h.state.availableElements.value.at(-1).id, 101)
  assert.deepEqual(calls, [{ project: 2, page: 1, page_size: 100, options_only: true }, { project: 2, page: 2, page_size: 100, options_only: true }])
})

test('element pagination retries changing counts and never returns a partial inventory', async () => {
  const calls = []
  const rows = await contextHelpers.loadUiElementOptions(async params => {
    calls.push(params.page)
    const changed = calls.length === 1
    return { data: { count: changed ? 2 : 3, next: params.page === 1 ? '/next' : null, results: params.page === 1 ? [{ id: 1 }] : [{ id: 2 }, { id: 3 }] } }
  }, 2)
  assert.deepEqual(calls, [1, 2, 1, 2])
  assert.deepEqual(rows.map(row => row.id), [1, 2, 3])
  await assert.rejects(() => contextHelpers.loadUiElementOptions(async ({ page }) => ({ data: { count: 2, next: page === 1 ? '/next' : null, results: [{ id: 1 }] } }), 2), /changed during pagination/)
})

test('readiness blocks unavailable engines/browsers and keeps the server explanation', () => {
  const data = { base_url_ready: true, engines: [
    { value: 'playwright', available: true, browsers: [{ value: 'chrome', available: true }, { value: 'safari', available: false, message: 'WebKit network isolation unavailable' }] },
    { value: 'selenium', available: false, message: 'Compatibility mode disabled', browsers: [{ value: 'chrome', available: true }] },
  ] }
  assert.equal(uiReadinessError(data, 'playwright', 'chrome'), '')
  assert.equal(uiReadinessError(data, 'playwright', 'safari'), 'WebKit network isolation unavailable')
  assert.equal(uiReadinessError(data, 'selenium', 'chrome'), 'Compatibility mode disabled')
  assert.equal(uiEngineOptions(data)[1].disabled, true)
  assert.equal(uiBrowserOptions(data, 'selenium')[0].disabled, true)
  assert.equal(uiReadinessError({ ...data, base_url_ready: false, base_url_message: 'Project URL required' }, 'playwright', 'chrome'), 'Project URL required')
})

test('repair links survive login only with validated project/case/step/origin metadata', () => {
  assert.deepEqual(contextHelpers.uiWorkbenchReturnLocation('UiTestCases', { project: '2', caseId: '11', stepNumber: '3', fromExecutionId: '100', fromKind: 'suite', arbitrary: 'ignored' }), {
    name: 'UiTestCases', query: { project: '2', caseId: '11', stepNumber: '3', fromExecutionId: '100', fromKind: 'suite' },
  })
  assert.deepEqual(contextHelpers.uiWorkbenchReturnLocation('UiTestCases', { project: '2', caseId: '11', fromExecutionId: '../secret', fromKind: 'suite', stepNumber: '-1' }), {
    name: 'UiTestCases', query: { project: '2', caseId: '11' },
  })
})
