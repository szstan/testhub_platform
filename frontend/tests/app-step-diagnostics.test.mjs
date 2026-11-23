import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { effectScope } from 'vue'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'
import * as deviceExecution from '../src/utils/app-automation-device-execution.mjs'

const instances = []
afterEach(() => instances.splice(0).forEach(item => item.dispose()))

async function setupDiagnostic({ props, getDevice, run, store } = {}) {
  const userStore = store || { user: { id: 7 }, generation: 1, getSessionGeneration() { return this.generation } }
  const calls = []
  const loaded = await loadSfcSetup('src/views/app-automation/components/StepDiagnostics.vue', {
    'vue-router': { useRouter: () => ({ push: value => calls.push(['navigate', value]) }) },
    '@/stores/user': { useUserStore: () => userStore },
    '@/api/app-automation': { getAppDevice: getDevice || (async () => ({ data: {
      id: 11, device_id: 'synthetic-device', status: 'online', locked_by: null,
    } })) },
    '@/api/app-step-diagnostics': { runSavedSceneStep: run || (async (id, body) => {
      calls.push(['run', id, body])
      return { data: { success: true, execution: { id: 52 } } }
    }) },
    '@/utils/app-automation-device-execution.mjs': deviceExecution,
  })
  const scope = effectScope()
  const effectiveProps = {
    caseId: 9, stepIndex: 1, stepName: 'Step', nested: false, dirty: false,
    recording: false, deviceId: 11, ...props,
  }
  const state = scope.run(() => loaded.component.setup(effectiveProps, { expose() {} }))
  instances.push({ dispose: () => { scope.stop(); loaded.dispose() } })
  return { state, calls, userStore, props: effectiveProps }
}

test('single-step diagnostics require an explicit click and submit only saved index', async () => {
  const { state, calls } = await setupDiagnostic()
  assert.equal(calls.length, 0)
  assert.equal(await state.runStep(), true)
  assert.deepEqual(plain(calls[0]), ['run', 9, {
    device_id: 'synthetic-device', auto_lock: true, expected_user_id: 7, step_index: 1,
  }])
  assert.equal(state.lastExecutionId.value, 52)
})

test('unsaved, nested and active-recording states block device requests', async () => {
  for (const props of [{ dirty: true }, { caseId: null }, { nested: true }, { recording: true }]) {
    const { state, calls } = await setupDiagnostic({ props })
    assert.equal(state.canRun.value, false)
    assert.equal(await state.runStep(), false)
    assert.equal(calls.length, 0)
  }
})

test('a user-session change during device lookup prevents step submission', async () => {
  const pending = deferred()
  const store = { user: { id: 7 }, generation: 1, getSessionGeneration() { return this.generation } }
  const { state, calls } = await setupDiagnostic({ getDevice: () => pending.promise, store })
  const request = state.runStep()
  store.generation += 1
  pending.resolve({ data: { id: 11, device_id: 'synthetic-device', status: 'online', locked_by: null } })
  assert.equal(await request, false)
  assert.equal(calls.length, 0)
})
