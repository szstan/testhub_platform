import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { afterEach, test } from 'node:test'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import { build } from 'esbuild'
import { compileScript, parse } from '@vue/compiler-sfc'
import * as vue from 'vue'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const componentPath = 'src/views/app-automation/components/StepDiagnostics.vue'
const instances = []
const validProps = {
  caseId: 44,
  stepIndex: 2,
  stepName: 'Tap Continue',
  nested: false,
  dirty: false,
  recording: false,
  deviceId: 31,
}

async function loadDiagnostics({ props: propOverrides = {}, prepare } = {}) {
  const filename = resolve(frontend, componentPath)
  const source = await readFile(filename, 'utf8')
  const { descriptor, errors } = parse(source, { filename })
  assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id: 'step-diagnostics-test' })
  const calls = { devices: [], prepares: [], runs: [], messages: [], routes: [] }
  let sessionGeneration = 3
  const userStore = vue.reactive({
    user: { id: 7 },
    getSessionGeneration: () => sessionGeneration,
  })
  const mocks = {
    vue,
    'vue-i18n': { useI18n: () => ({ t: key => key }) },
    'vue-router': { useRouter: () => ({ push: route => calls.routes.push(route) }) },
    'element-plus': {
      ElMessage: {
        error: text => calls.messages.push(['error', text]),
        success: text => calls.messages.push(['success', text]),
      },
    },
    '@/stores/user': { useUserStore: () => userStore },
    '@/api/app-automation': {
      getAppDevice: async id => {
        calls.devices.push(id)
        return { data: { id, device_id: 'mock-device' } }
      },
    },
    '@/api/app-step-diagnostics': {
      runSavedSceneStep: async (caseId, data) => {
        calls.runs.push([caseId, data])
        return { data: { success: true, execution: { id: 91 } } }
      },
    },
    '@/utils/app-automation-device-execution.mjs': {
      getAppExecutionErrorMessage: (_error, fallback) => fallback,
      prepareAppDeviceForExecution: async options => {
        calls.prepares.push(options)
        return prepare ? prepare(options) : { device_id: 'mock-device' }
      },
    },
  }
  const bundle = await build({
    stdin: {
      contents: script.content,
      resolveDir: dirname(filename),
      sourcefile: filename,
      loader: 'js',
    },
    bundle: true,
    write: false,
    platform: 'node',
    format: 'cjs',
    plugins: [{
      name: 'step-diagnostics-sfc-mocks',
      setup(builder) {
        builder.onResolve({ filter: /.*/ }, args => {
          if (!(args.path in mocks)) throw new Error(`Unexpected component import: ${args.path}`)
          return { path: args.path, namespace: 'step-diagnostics-mock' }
        })
        builder.onLoad({ filter: /.*/, namespace: 'step-diagnostics-mock' }, args => ({
          contents: Object.keys(mocks[args.path]).map(name =>
            `export const ${name} = __stepDiagnosticsMocks[${JSON.stringify(args.path)}][${JSON.stringify(name)}];`,
          ).join('\n'),
          loader: 'js',
        }))
      },
    }],
  })
  const module = { exports: {} }
  runInNewContext(bundle.outputFiles[0].text, {
    module,
    exports: module.exports,
    __stepDiagnosticsMocks: mocks,
    console,
  }, { filename })

  const props = vue.reactive({ ...validProps, ...propOverrides })
  const scope = vue.effectScope()
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} }))
  const instance = {
    calls,
    props,
    state,
    userStore,
    setSessionGeneration: generation => { sessionGeneration = generation },
    dispose: () => scope.stop(),
  }
  instances.push(instance)
  return instance
}

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

function plain(value) {
  return JSON.parse(JSON.stringify(value))
}

afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

test('SceneBuilder supplies the selected saved root step, recorder state, and device', async () => {
  const source = await readFile(resolve(frontend, 'src/views/app-automation/test-cases/SceneBuilder.vue'), 'utf8')
  assert.match(source, /import StepDiagnostics from '\.\.\/components\/StepDiagnostics\.vue'/)
  const mount = source.match(/<StepDiagnostics[\s\S]*?\/>/)?.[0]
  assert.ok(mount, 'StepDiagnostics should be mounted in the recorder panel')
  for (const binding of [
    ':case-id="editingCaseId"',
    ':step-index="selectedIndex"',
    ':step-name="activeParentStep?.name"',
    ':nested="selectedSubIndex !== null"',
    ':dirty="isSceneDirty"',
    ':recording="recorderEnabled"',
    ':device-id="recorderDeviceId"',
  ]) assert.ok(mount.includes(binding), `missing ${binding}`)

  const keys = [
    'title', 'run', 'saveFirst', 'stopRecording', 'selectRootStep', 'selectDevice',
    'ready', 'viewResult', 'accountChanged', 'submitted', 'failed',
  ]
  for (const locale of ['zh-cn', 'en']) {
    const messages = await readFile(resolve(frontend, `src/locales/lang/${locale}/app-automation.js`), 'utf8')
    const groupStart = messages.indexOf('stepDiagnostics: {')
    const groupEnd = messages.indexOf('// sceneBuilder', groupStart)
    assert.ok(groupStart >= 0 && groupEnd > groupStart, `${locale} step diagnostics messages are missing`)
    const group = messages.slice(groupStart, groupEnd)
    for (const key of keys) assert.match(group, new RegExp(`^\\s*${key}:`, 'm'), `${locale} missing ${key}`)
  }
})

test('diagnostics setup does not touch a device or submit until explicitly invoked', async () => {
  const diagnostics = await loadDiagnostics()
  assert.equal(diagnostics.state.canRun.value, true)
  assert.deepEqual(diagnostics.calls.devices, [])
  assert.deepEqual(diagnostics.calls.prepares, [])
  assert.deepEqual(diagnostics.calls.runs, [])

  const source = await readFile(resolve(frontend, componentPath), 'utf8')
  assert.match(source, /@click="runStep"/)
  assert.match(source, /:disabled="!canRun"/)

  assert.equal(await diagnostics.state.runStep(), true)
  assert.equal(diagnostics.calls.prepares.length, 1)
  assert.equal(diagnostics.calls.devices.length, 0)
  assert.deepEqual(plain(diagnostics.calls.runs), [[44, {
    device_id: 'mock-device',
    auto_lock: true,
    expected_user_id: 7,
    step_index: 2,
  }]])
})

test('unsaved, dirty, nested, recording, unselected, and device-less contexts are blocked', async () => {
  const cases = [
    [{ caseId: null }, 'saveFirst'],
    [{ dirty: true }, 'saveFirst'],
    [{ nested: true }, 'selectRootStep'],
    [{ stepIndex: null }, 'selectRootStep'],
    [{ recording: true }, 'stopRecording'],
    [{ deviceId: null }, 'selectDevice'],
  ]
  for (const [props, reason] of cases) {
    const diagnostics = await loadDiagnostics({ props })
    assert.equal(diagnostics.state.reasonKey.value, `appAutomation.stepDiagnostics.${reason}`)
    assert.equal(diagnostics.state.canRun.value, false)
    assert.equal(await diagnostics.state.runStep(), false)
    assert.deepEqual(diagnostics.calls.devices, [])
    assert.deepEqual(diagnostics.calls.prepares, [])
    assert.deepEqual(diagnostics.calls.runs, [])
  }
})

test('account and session changes during device preparation fence the run request', async () => {
  for (const change of [
    diagnostics => { diagnostics.userStore.user = { id: 8 } },
    diagnostics => diagnostics.setSessionGeneration(4),
  ]) {
    const preparation = deferred()
    const diagnostics = await loadDiagnostics({ prepare: () => preparation.promise })
    const running = diagnostics.state.runStep()
    assert.equal(diagnostics.calls.prepares.length, 1)

    change(diagnostics)
    preparation.resolve({ device_id: 'mock-device' })

    assert.equal(await running, false)
    assert.deepEqual(diagnostics.calls.runs, [])
    assert.ok(diagnostics.calls.messages.some(([type, message]) =>
      type === 'error' && message === 'appAutomation.stepDiagnostics.accountChanged'))
  }
})
