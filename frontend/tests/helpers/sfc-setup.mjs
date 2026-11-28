import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import { build } from 'esbuild'
import { compileScript, parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import * as elementLinks from '../../src/utils/app-automation-element-links.mjs'
import * as uiSubmissions from '../../src/utils/ui-execution-submission.mjs'
import * as uiOptions from '../../src/utils/ui-execution-options.mjs'
import * as uiCaseRunner from '../../src/utils/ui-case-run-controller.mjs'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '../..')

// Compile the real SFC script, replacing only imports that would perform IO or
// require a mounted application. Vue refs, computed values and watchers are real.
export async function loadSfcSetup(relativePath, imports = {}, globals = {}, setupProps = {}) {
  const filename = resolve(frontend, relativePath)
  const source = await readFile(filename, 'utf8')
  const { descriptor, errors } = parse(source, { filename })
  assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id: relativePath })
  const mounted = []
  const beforeUnmount = []
  const unmounted = []
  const routeLeaveGuards = []
  const routeUpdateGuards = []
  const timers = []
  const messages = []
  const mocks = {
    vue: {
      ...vue,
      onMounted: callback => mounted.push(callback),
      onBeforeUnmount: callback => beforeUnmount.push(callback),
      onUnmounted: callback => unmounted.push(callback),
    },
    'vue-i18n': { useI18n: () => ({ t: (key, params) => params?.message ? `${key}: ${params.message}` : key }) },
    'element-plus': {
      ElMessage: Object.fromEntries(['error', 'warning', 'success'].map(type => [type, text => messages.push({ type, text })])),
      ElMessageBox: { confirm: globals.confirm || (async () => true) },
    },
    '@/api/app-scene-data': {
      getSceneDatasets: async () => ({ data: [] }),
      getAllSceneDatasets: async () => [],
      uploadSceneDataset: async () => ({ data: {} }),
      getProjectComponents: async () => ({ data: [] }),
      getAllProjectComponents: async () => [],
      createProjectComponent: async () => ({ data: {} }),
      executeSceneDataset: async () => ({ data: { success: true, data: { execution_ids: [] } } }),
    },
    // Component tests mock IO at the aggregate device-list boundary. The real
    // pagination helper has its own multi-page and error regressions.
    '@/api/app-device-options': {
      getAllAppDevices: async (params = {}) => {
        const api = imports['@/api/app-automation'] || imports['@/api/app-automation.js']
        const response = await api?.getDeviceList?.({ ...params, ordering: 'id', page: 1, page_size: 100 })
        const body = response?.data?.data ?? response?.data ?? response
        return Array.isArray(body) ? body : body?.results || []
      },
    },
    '@/utils/app-automation-element-links.mjs': elementLinks,
    '@/utils/ui-execution-submission.mjs': uiSubmissions,
    '@/utils/ui-execution-options.mjs': uiOptions,
    '@/utils/ui-case-run-controller.mjs': uiCaseRunner,
    '../components/SceneDataPanel.vue': { default: {} },
    '../components/NativeElementInspector.vue': { default: {} },
    ...imports,
    'vue-router': {
      onBeforeRouteLeave: callback => routeLeaveGuards.push(callback),
      onBeforeRouteUpdate: callback => routeUpdateGuards.push(callback),
      ...imports['vue-router'],
    },
    '@/api/ui_automation': {
      getExecutionReadiness: async project => ({ data: {
        project_id: project, base_url_ready: true, recommended_headless: true,
        engines: ['playwright', 'selenium'].map(value => ({ value, available: true, browsers: ['chrome', 'firefox', 'edge'].map(browser => ({ value: browser, available: true })) })),
      } }),
      getTestCaseSubmission: async () => { throw Object.assign(new Error('Submission not found'), { response: { status: 404 } }) },
      getSuiteSubmission: async () => { throw Object.assign(new Error('Submission not found'), { response: { status: 404 } }) },
      getTestCaseExecutionDetail: async () => { throw new Error('Execution detail mock required') },
      abortTestCaseExecution: async () => ({ data: { status: 'aborted' } }),
      ...imports['@/api/ui_automation'],
    },
    '@/api/app-automation': {
      getPackageList: async () => ({ data: { results: [] } }),
      ...imports['@/api/app-automation'],
    },
  }
  const bundle = await build({
    stdin: {
      contents: script.content,
      resolveDir: dirname(filename),
      sourcefile: filename,
      loader: descriptor.scriptSetup?.lang === 'ts' || descriptor.script?.lang === 'ts' ? 'ts' : 'js',
    },
    bundle: true,
    write: false,
    platform: 'node',
    format: 'cjs',
    plugins: [{
      name: 'sfc-test-imports',
      setup(builder) {
        builder.onResolve({ filter: /.*/ }, args => {
          if (!(args.path in mocks)) throw new Error(`Unexpected SFC import: ${args.path}`)
          return { path: args.path, namespace: 'sfc-test-mock' }
        })
        builder.onLoad({ filter: /.*/, namespace: 'sfc-test-mock' }, args => ({
          contents: Object.keys(mocks[args.path]).map(name => name === 'default'
            ? `export default __sfcMocks[${JSON.stringify(args.path)}].default;`
            : `export const ${name} = __sfcMocks[${JSON.stringify(args.path)}][${JSON.stringify(name)}];`).join('\n'),
          loader: 'js',
        }))
      },
    }],
  })
  const module = { exports: {} }
  runInNewContext(bundle.outputFiles[0].text, {
    module, exports: module.exports, __sfcMocks: mocks, console, crypto: globalThis.crypto,
    setTimeout: callback => { timers.push(callback); return timers.length }, clearTimeout() {},
    ...globals,
  }, { filename })
  const scope = vue.effectScope()
  const component = module.exports.default
  const events = []
  const state = typeof component.setup === 'function'
    ? scope.run(() => component.setup(setupProps, { expose() {}, emit: (...args) => events.push(args) }))
    : null
  return {
    component,
    state,
    events,
    props: setupProps,
    messages,
    routeLeaveGuards,
    routeUpdateGuards,
    timers,
    mount: () => Promise.all(mounted.map(callback => callback())),
    dispose() {
      beforeUnmount.forEach(callback => callback())
      unmounted.forEach(callback => callback())
      scope.stop()
    },
  }
}

export function deferred() {
  let resolvePromise
  let rejectPromise
  const promise = new Promise((resolve, reject) => { resolvePromise = resolve; rejectPromise = reject })
  return { promise, resolve: resolvePromise, reject: rejectPromise }
}

export const plain = value => JSON.parse(JSON.stringify(value))
