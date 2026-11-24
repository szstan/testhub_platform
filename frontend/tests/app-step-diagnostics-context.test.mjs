import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import { build } from 'esbuild'
import { compileScript, parse } from '@vue/compiler-sfc'
import * as vue from 'vue'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const filename = resolve(frontend, 'src/views/app-automation/components/StepDiagnostics.vue')
const instances = []

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

async function loadDiagnostics({ run } = {}) {
  const source = await (await import('node:fs/promises')).readFile(filename, 'utf8')
  const { descriptor, errors } = parse(source, { filename })
  assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id: 'step-diagnostics-context-test' })
  const calls = { messages: [], runs: [], routes: [] }
  const store = vue.reactive({ user: { id: 7 }, generation: 3 })
  const mocks = {
    vue,
    'vue-i18n': { useI18n: () => ({ t: key => key }) },
    'vue-router': { useRouter: () => ({ push: value => calls.routes.push(value) }) },
    'element-plus': {
      ElMessage: {
        error: text => calls.messages.push(['error', text]),
        success: text => calls.messages.push(['success', text]),
      },
    },
    '@/stores/user': { useUserStore: () => ({
      get user() { return store.user },
      getSessionGeneration: () => store.generation,
    }) },
    '@/api/app-automation': { getAppDevice: async id => ({ data: { id, device_id: 'mock-device' } }) },
    '@/api/app-step-diagnostics': {
      runSavedSceneStep: async (caseId, body) => {
        calls.runs.push([caseId, body])
        return run ? run(caseId, body) : { data: { success: true, execution: { id: 91 } } }
      },
    },
    '@/utils/app-automation-device-execution.mjs': {
      getAppExecutionErrorMessage: (_error, fallback) => fallback,
      prepareAppDeviceForExecution: async () => ({ device_id: 'mock-device' }),
    },
  }
  const bundle = await build({
    stdin: { contents: script.content, resolveDir: dirname(filename), sourcefile: filename, loader: 'js' },
    bundle: true,
    write: false,
    platform: 'node',
    format: 'cjs',
    plugins: [{
      name: 'step-diagnostics-context-mocks',
      setup(builder) {
        builder.onResolve({ filter: /.*/ }, args => {
          if (!(args.path in mocks)) throw new Error(`Unexpected component import: ${args.path}`)
          return { path: args.path, namespace: 'step-diagnostics-context-mock' }
        })
        builder.onLoad({ filter: /.*/, namespace: 'step-diagnostics-context-mock' }, args => ({
          contents: Object.keys(mocks[args.path]).map(name =>
            `export const ${name} = __diagnosticMocks[${JSON.stringify(args.path)}][${JSON.stringify(name)}];`,
          ).join('\n'),
          loader: 'js',
        }))
      },
    }],
  })
  const module = { exports: {} }
  runInNewContext(bundle.outputFiles[0].text, {
    module, exports: module.exports, __diagnosticMocks: mocks, console,
  }, { filename })
  const props = vue.reactive({
    caseId: 44,
    stepIndex: 2,
    stepName: 'Tap Continue',
    nested: false,
    dirty: false,
    recording: false,
    deviceId: 31,
  })
  const scope = vue.effectScope()
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} }))
  const instance = {
    calls,
    props,
    store,
    state,
    dispose: () => scope.stop(),
  }
  instances.push(instance)
  return instance
}

afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

test('accepted diagnostic retains its execution ID after selection and editor state change', async () => {
  const accepted = deferred()
  const submitted = deferred()
  const diagnostics = await loadDiagnostics({
    run: (...args) => {
      submitted.resolve(args)
      return accepted.promise
    },
  })
  const request = diagnostics.state.runStep()
  await submitted.promise
  diagnostics.props.stepIndex = 4
  diagnostics.props.dirty = true
  accepted.resolve({ data: { success: true, execution: { id: 731 } } })

  assert.equal(await request, true)
  assert.equal(diagnostics.state.lastExecutionId.value, 731)
  assert.deepEqual(diagnostics.calls.messages, [[
    'success', 'appAutomation.stepDiagnostics.submittedAfterContextChange',
  ]])
})

test('diagnostic execution ID is not exposed after account or session replacement', async () => {
  const accepted = deferred()
  const submitted = deferred()
  const diagnostics = await loadDiagnostics({
    run: (...args) => {
      submitted.resolve(args)
      return accepted.promise
    },
  })
  const request = diagnostics.state.runStep()
  await submitted.promise
  diagnostics.store.user = { id: 8 }
  diagnostics.store.generation = 4
  accepted.resolve({ data: { success: true, execution: { id: 842 } } })

  assert.equal(await request, false)
  assert.equal(diagnostics.state.lastExecutionId.value, null)
  assert.ok(diagnostics.calls.messages.some(([type, text]) =>
    type === 'error' && text === 'appAutomation.stepDiagnostics.accountChanged'))
  assert.equal(JSON.stringify(diagnostics.calls.messages).includes('842'), false)
})

test('previously accepted execution ID is hidden immediately when the session changes', async () => {
  const diagnostics = await loadDiagnostics()
  assert.equal(await diagnostics.state.runStep(), true)
  assert.equal(diagnostics.state.lastExecutionId.value, 91)

  diagnostics.store.user = { id: 8 }
  await vue.nextTick()

  assert.equal(diagnostics.state.lastExecutionId.value, null)
})
