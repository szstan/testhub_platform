import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import { build } from 'esbuild'
import { compileScript, parse } from '@vue/compiler-sfc'
import * as vue from 'vue'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const filename = resolve(frontend, 'src/views/app-automation/components/DeviceRecorder.vue')
const instances = []

async function loadRecorder({ sendInteraction, recordingEnabled = true } = {}) {
  const source = await readFile(filename, 'utf8')
  const { descriptor, errors } = parse(source, { filename })
  assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id: 'device-recorder-inflight-test' })
  const bundleDir = await mkdtemp(resolve(frontend, 'node_modules', '.app-recorder-inflight-test-'))
  const mocks = { vue, 'vue-i18n': { useI18n: () => ({ t: key => key }) } }
  const bundle = await build({
    stdin: { contents: script.content, resolveDir: dirname(filename), sourcefile: filename, loader: 'js' },
    bundle: true,
    write: false,
    platform: 'node',
    format: 'cjs',
    plugins: [{
      name: 'device-recorder-inflight-mocks',
      setup(builder) {
        builder.onResolve({ filter: /.*/ }, args => {
          if (!(args.path in mocks)) throw new Error(`Unexpected component import: ${args.path}`)
          return { path: args.path, namespace: 'device-recorder-inflight-mock' }
        })
        builder.onLoad({ filter: /.*/, namespace: 'device-recorder-inflight-mock' }, args => ({
          contents: Object.keys(mocks[args.path]).map(name =>
            `export const ${name} = __recorderMocks[${JSON.stringify(args.path)}][${JSON.stringify(name)}];`,
          ).join('\n'),
          loader: 'js',
        }))
      },
    }],
  })
  const module = { exports: {} }
  runInNewContext(bundle.outputFiles[0].text, {
    module, exports: module.exports, __recorderMocks: mocks, console,
  }, { filename })
  const props = vue.reactive({
    deviceId: 17,
    recordingEnabled,
    canStart: true,
    starting: false,
    canViewScreenshot: true,
    getScreenshot: async () => 'data:image/png;base64,fixture',
    sendInteraction,
  })
  const emitted = []
  const scope = vue.effectScope()
  const state = scope.run(() => module.exports.default.setup(props, {
    emit: (name, ...args) => emitted.push([name, ...args]),
    expose() {},
  }))
  const instance = {
    props,
    state,
    emitted,
    dispose: async () => {
      scope.stop()
      await rm(bundleDir, { recursive: true, force: true })
    },
  }
  instances.push(instance)
  return instance
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function completedResponse() {
  return { data: { success: true, action_outcome: 'completed', recovery_required: false } }
}

afterEach(async () => Promise.all(instances.splice(0).map(instance => instance.dispose())))

const origin = Object.freeze({
  caseId: 44,
  sceneGeneration: 3,
  projectId: 13,
  userId: 7,
  sessionGeneration: 9,
})

const beginKeyAction = recorder => {
  recorder.state.setMode('key')
  return recorder.state.recordKey('back')
}

test('active same-context acknowledgements carry origin context to the parent appender', async () => {
  const response = deferred()
  const sendInteraction = () => response.promise
  const draftOrigin = { ...origin, caseId: null, projectId: null }
  sendInteraction.getActionContext = () => draftOrigin
  const recorder = await loadRecorder({ sendInteraction })
  const request = beginKeyAction(recorder)
  response.resolve(completedResponse())

  assert.equal(await request, true)
  assert.deepEqual(JSON.parse(JSON.stringify(recorder.emitted.find(([name]) => name === 'step-staged'))), [
    'step-staged', {
      step: { action: 'key', key: 'back', index: 1 },
      context: { ...draftOrigin, deviceId: 17 },
    },
  ])
})

test('legacy successful responses remain compatible while staging the origin action', async () => {
  const sendInteraction = async () => ({ success: true })
  sendInteraction.getActionContext = () => origin
  const recorder = await loadRecorder({ sendInteraction })

  assert.equal(await beginKeyAction(recorder), true)
  assert.deepEqual(JSON.parse(JSON.stringify(recorder.emitted.find(([name]) => name === 'step-staged'))), [
    'step-staged', {
      step: { action: 'key', key: 'back', index: 1 },
      context: { ...origin, deviceId: 17 },
    },
  ])
})

test('acknowledged in-flight action is staged after recording stops in the same scene', async () => {
  const response = deferred()
  const sendInteraction = () => response.promise
  sendInteraction.getActionContext = () => origin
  const recorder = await loadRecorder({ sendInteraction })
  const request = beginKeyAction(recorder)
  assert.equal(recorder.state.interactionBusy.value, true)
  assert.deepEqual(JSON.parse(JSON.stringify(recorder.emitted.find(([name]) => name === 'interaction-state'))), [
    'interaction-state', { active: true, context: { ...origin, deviceId: 17 }, action: 'key' },
  ])

  recorder.props.recordingEnabled = false
  await vue.nextTick()
  response.resolve(completedResponse())

  assert.equal(await request, true)
  assert.deepEqual(JSON.parse(JSON.stringify(recorder.emitted.find(([name]) => name === 'step-staged'))), [
    'step-staged', {
      step: { action: 'key', key: 'back', index: 1 },
      context: { ...origin, deviceId: 17 },
    },
  ])
  assert.equal(recorder.state.visibleUnresolvedActions.value.length, 0)
  assert.deepEqual(JSON.parse(JSON.stringify(recorder.emitted.find(([name, value]) => name === 'interaction-state' && value.active === false))), [
    'interaction-state', { active: false, context: { ...origin, deviceId: 17 }, action: 'key' },
  ])
})

test('an acknowledged no-project draft action is staged after recording stops', async () => {
  const response = deferred()
  const draftOrigin = { ...origin, caseId: null, projectId: null }
  const sendInteraction = () => response.promise
  sendInteraction.getActionContext = () => draftOrigin
  const recorder = await loadRecorder({ sendInteraction })
  const request = beginKeyAction(recorder)

  recorder.props.recordingEnabled = false
  await vue.nextTick()
  response.resolve(completedResponse())

  assert.equal(await request, true)
  assert.deepEqual(JSON.parse(JSON.stringify(recorder.emitted.find(([name]) => name === 'step-staged'))), [
    'step-staged', {
      step: { action: 'key', key: 'back', index: 1 },
      context: { ...draftOrigin, deviceId: 17 },
    },
  ])
  assert.equal(recorder.state.visibleUnresolvedActions.value.length, 0)
})

test('an acknowledged action stays with its draft after the draft is first saved', async () => {
  const response = deferred()
  const context = vue.reactive({ ...origin, caseId: null })
  const sendInteraction = () => response.promise
  sendInteraction.getActionContext = () => context
  const recorder = await loadRecorder({ sendInteraction })
  const request = beginKeyAction(recorder)
  context.caseId = 44
  response.resolve(completedResponse())

  assert.equal(await request, true)
  assert.equal(recorder.emitted.some(([name]) => name === 'step-staged'), true)
  assert.equal(recorder.state.visibleUnresolvedActions.value.length, 0)
})

test('a lost response preserves an explicitly uncertain action and warns the parent', async () => {
  const response = deferred()
  const sendInteraction = () => response.promise
  sendInteraction.getActionContext = () => origin
  const recorder = await loadRecorder({ sendInteraction })
  const request = beginKeyAction(recorder)
  response.reject(new Error('synthetic network failure'))

  assert.equal(await request, false)
  assert.equal(recorder.emitted.some(([name]) => name === 'step-staged'), false)
  const unresolved = recorder.emitted.find(([name]) => name === 'action-unresolved')[1]
  assert.equal(unresolved.uncertain, true)
  assert.equal(recorder.state.visibleUnresolvedActions.value.length, 1)
  assert.deepEqual(JSON.parse(JSON.stringify(recorder.emitted.find(([name]) => name === 'error'))), [
    'error', { operation: 'interaction', uncertain: true },
  ])
})

test('an HTTP error with explicit completion stages the action and requires recovery', async () => {
  const response = deferred()
  const sendInteraction = () => response.promise
  sendInteraction.getActionContext = () => origin
  const recorder = await loadRecorder({ sendInteraction })
  const request = beginKeyAction(recorder)
  response.reject(Object.assign(new Error('synthetic HTTP failure'), {
    response: {
      status: 503,
      data: {
        action_outcome: 'completed',
        recovery_required: true,
        detail: 'localized text is not completion proof',
      },
    },
  }))

  assert.equal(await request, false)
  assert.deepEqual(JSON.parse(JSON.stringify(recorder.emitted.find(([name]) => name === 'step-staged'))), [
    'step-staged', {
      step: { action: 'key', key: 'back', index: 1 },
      context: { ...origin, deviceId: 17 },
    },
  ])
  assert.equal(recorder.state.visibleUnresolvedActions.value.length, 0)
  assert.deepEqual(JSON.parse(JSON.stringify(recorder.emitted.find(([name]) => name === 'error'))), [
    'error', {
      operation: 'interaction',
      status: 503,
      actionOutcome: 'completed',
      recoveryRequired: true,
    },
  ])
  assert.equal(JSON.stringify(recorder.emitted).includes('localized text'), false)
})

test('an explicit pre-dispatch failure is not staged or marked uncertain', async () => {
  const response = deferred()
  const sendInteraction = () => response.promise
  sendInteraction.getActionContext = () => origin
  const recorder = await loadRecorder({ sendInteraction })
  const request = beginKeyAction(recorder)
  response.reject(Object.assign(new Error('synthetic HTTP failure'), {
    response: {
      status: 503,
      data: {
        action_outcome: 'not_dispatched',
        recovery_required: true,
        detail: 'localized text is not outcome evidence',
      },
    },
  }))

  assert.equal(await request, false)
  assert.equal(recorder.emitted.some(([name]) => name === 'step-staged'), false)
  assert.equal(recorder.emitted.some(([name]) => name === 'action-unresolved'), false)
  assert.deepEqual(JSON.parse(JSON.stringify(recorder.emitted.find(([name]) => name === 'error'))), [
    'error', {
      operation: 'interaction',
      status: 503,
      actionOutcome: 'not_dispatched',
      recoveryRequired: true,
    },
  ])
})

test('an explicit unknown result remains unresolved and requires recovery', async () => {
  const response = deferred()
  const sendInteraction = () => response.promise
  sendInteraction.getActionContext = () => origin
  const recorder = await loadRecorder({ sendInteraction })
  const request = beginKeyAction(recorder)
  response.reject(Object.assign(new Error('synthetic HTTP timeout'), {
    response: {
      status: 504,
      data: { action_outcome: 'unknown', recovery_required: true },
    },
  }))

  assert.equal(await request, false)
  assert.equal(recorder.emitted.some(([name]) => name === 'step-staged'), false)
  assert.equal(recorder.emitted.find(([name]) => name === 'action-unresolved')[1].uncertain, true)
  assert.deepEqual(JSON.parse(JSON.stringify(recorder.emitted.find(([name]) => name === 'error'))), [
    'error', {
      operation: 'interaction',
      status: 504,
      actionOutcome: 'unknown',
      uncertain: true,
      recoveryRequired: true,
    },
  ])
})

test('unresolved draft actions stay visible across project, scene and device changes in one session', async () => {
  const changes = [
    { name: 'account', patch: { userId: 8 }, expectedVisible: false },
    { name: 'session', patch: { sessionGeneration: 10 }, expectedVisible: false },
    { name: 'project', patch: { projectId: 14 }, expectedVisible: true },
    { name: 'scene', patch: { sceneGeneration: 4 }, expectedVisible: true },
    { name: 'device', deviceId: 18, expectedVisible: true },
  ]

  for (const change of changes) {
    const response = deferred()
    const draftOrigin = { ...origin, caseId: null, projectId: null }
    const context = vue.reactive({ ...draftOrigin })
    const sendInteraction = () => response.promise
    sendInteraction.getActionContext = () => context
    const recorder = await loadRecorder({ sendInteraction })
    const request = beginKeyAction(recorder)

    if (change.deviceId !== undefined) recorder.props.deviceId = change.deviceId
    else Object.assign(context, change.patch)
    await vue.nextTick()
    response.resolve(completedResponse())

    assert.equal(await request, false, change.name)
    assert.equal(recorder.emitted.some(([name]) => name === 'step-staged'), false, change.name)
    assert.equal(recorder.state.visibleUnresolvedActions.value.length, change.expectedVisible ? 1 : 0, change.name)
    if (!change.expectedVisible) {
      assert.equal(recorder.emitted.some(([name]) => name === 'action-unresolved'), false, change.name)
    } else {
      assert.deepEqual(JSON.parse(JSON.stringify(recorder.emitted.find(([name]) => name === 'action-unresolved'))), [
        'action-unresolved', {
          id: 1,
          step: { action: 'key', key: 'back', index: 1 },
          context: { ...draftOrigin, deviceId: 17 },
        },
      ], change.name)
    }
  }
})

test('an acknowledged action remains recoverable across scene changes and when returning to its origin', async () => {
  const response = deferred()
  const context = vue.reactive({ ...origin })
  const sendInteraction = () => response.promise
  sendInteraction.getActionContext = () => context
  const recorder = await loadRecorder({ sendInteraction })
  const request = beginKeyAction(recorder)
  Object.assign(context, { projectId: 14, sceneGeneration: 4 })
  await vue.nextTick()
  response.resolve(completedResponse())

  assert.equal(await request, false)
  assert.equal(recorder.emitted.some(([name]) => name === 'step-staged'), false)
  assert.deepEqual(JSON.parse(JSON.stringify(recorder.emitted.find(([name]) => name === 'action-unresolved'))), [
    'action-unresolved', {
      id: 1,
      step: { action: 'key', key: 'back', index: 1 },
      context: { ...origin, deviceId: 17 },
    },
  ])
  assert.equal(recorder.state.visibleUnresolvedActions.value.length, 1)
  assert.deepEqual(JSON.parse(JSON.stringify(recorder.state.visibleUnresolvedActions.value[0].context)), {
    ...origin,
    deviceId: 17,
  })

  Object.assign(context, origin)
  await vue.nextTick()
  assert.equal(recorder.state.visibleUnresolvedActions.value.length, 1)
  assert.deepEqual(JSON.parse(JSON.stringify(recorder.state.visibleUnresolvedActions.value[0].context)), {
    ...origin,
    deviceId: 17,
  })
  assert.equal(recorder.emitted.some(([name]) => name === 'step-staged'), false)
})

test('deferred acknowledgements are not surfaced after an account or session change', async () => {
  for (const change of [
    { name: 'account', patch: { userId: 8 } },
    { name: 'session', patch: { sessionGeneration: 10 } },
  ]) {
    const response = deferred()
    const context = vue.reactive({ ...origin })
    const sendInteraction = () => response.promise
    sendInteraction.getActionContext = () => context
    const recorder = await loadRecorder({ sendInteraction })
    const request = beginKeyAction(recorder)
    Object.assign(context, change.patch)
    await vue.nextTick()
    response.resolve(completedResponse())

    assert.equal(await request, false, change.name)
    assert.equal(recorder.emitted.some(([name]) => name === 'step-staged'), false, change.name)
    assert.equal(recorder.emitted.some(([name]) => name === 'action-unresolved'), false, change.name)
    assert.equal(recorder.state.visibleUnresolvedActions.value.length, 0, change.name)
  }
})

test('existing recovery actions are cleared on account or session changes', async () => {
  for (const change of [
    { name: 'account', patch: { userId: 8 } },
    { name: 'session', patch: { sessionGeneration: 10 } },
  ]) {
    const response = deferred()
    const context = vue.reactive({ ...origin, caseId: null, projectId: null })
    const sendInteraction = () => response.promise
    sendInteraction.getActionContext = () => context
    const recorder = await loadRecorder({ sendInteraction })
    const request = beginKeyAction(recorder)
    recorder.props.recordingEnabled = false
    context.sceneGeneration += 1
    await vue.nextTick()
    response.resolve(completedResponse())
    await request
    assert.equal(recorder.state.visibleUnresolvedActions.value.length, 1, change.name)

    Object.assign(context, change.patch)
    await vue.nextTick()
    assert.equal(recorder.state.visibleUnresolvedActions.value.length, 0, change.name)
  }
})
