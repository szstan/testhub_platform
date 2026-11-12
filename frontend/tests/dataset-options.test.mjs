import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'
import { build } from 'esbuild'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const componentPath = join(frontend, 'src/views/executions/DatasetsPlugins.vue')
const source = await readFile(componentPath, 'utf8')
const { descriptor, errors: parseErrors } = parse(source, { filename: componentPath })
assert.deepEqual(parseErrors, [], 'dataset page SFC parses')
const compiledScript = compileScript(descriptor, { id: 'dataset-options-regression' })
const compiledTemplate = compileTemplate({
  source: descriptor.template.content,
  filename: componentPath,
  id: 'dataset-options-regression',
})
assert.deepEqual(compiledTemplate.errors, [], 'dataset page template compiles')

const bundled = await build({
  absWorkingDir: frontend,
  stdin: {
    contents: compiledScript.content,
    resolveDir: frontend,
    sourcefile: 'src/views/executions/DatasetsPlugins.vue',
  },
  bundle: true,
  format: 'esm',
  platform: 'node',
  write: false,
  plugins: [{
    name: 'dataset-options-test-stubs',
    setup(builder) {
      builder.onResolve({ filter: /^@\/api\/executions$/ }, () => ({ path: 'executions-api', namespace: 'dataset-options-test' }))
      builder.onLoad({ filter: /^executions-api$/, namespace: 'dataset-options-test' }, () => ({
        contents: `
          const api = () => globalThis.__datasetOptionsTestApi;
          export const createExecutionDataMatrix = (...args) => api().createExecutionDataMatrix(...args);
          export const createExecutionDataset = (...args) => api().createExecutionDataset(...args);
          export const deleteExecutionDataMatrix = (...args) => api().deleteExecutionDataMatrix(...args);
          export const listExecutionDataMatrices = (...args) => api().listExecutionDataMatrices(...args);
          export const listExecutionDatasets = (...args) => api().listExecutionDatasets(...args);
          export const listExecutionPlugins = (...args) => api().listExecutionPlugins(...args);
          export const updateExecutionDataMatrix = (...args) => api().updateExecutionDataMatrix(...args);
        `,
        loader: 'js',
      }))
      builder.onResolve({ filter: /^element-plus$/ }, () => ({ path: 'element-plus-stub', namespace: 'dataset-options-test' }))
      builder.onLoad({ filter: /^element-plus-stub$/, namespace: 'dataset-options-test' }, () => ({
        contents: 'export const ElMessage = { error() {}, warning() {}, success() {} }; export const ElMessageBox = { confirm: async () => true };',
        loader: 'js',
      }))
      builder.onResolve({ filter: /^vue-i18n$/ }, () => ({ path: 'vue-i18n-stub', namespace: 'dataset-options-test' }))
      builder.onLoad({ filter: /^vue-i18n-stub$/, namespace: 'dataset-options-test' }, () => ({
        contents: 'export const useI18n = () => ({ t: key => key });',
        loader: 'js',
      }))
    },
  }],
})
const componentModule = await import(`data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].text).toString('base64')}`)
const component = componentModule.default

const emptyMatrices = async () => ({ data: { count: 0, results: [] } })
const setApi = overrides => {
  globalThis.__datasetOptionsTestApi = {
    listExecutionDatasets: async () => ({ data: { count: 0, results: [] } }),
    listExecutionDataMatrices: emptyMatrices,
    listExecutionPlugins: async () => ({ data: { count: 0, results: [] } }),
    createExecutionDataMatrix: async () => ({}),
    createExecutionDataset: async () => ({}),
    deleteExecutionDataMatrix: async () => ({}),
    updateExecutionDataMatrix: async () => ({}),
    ...overrides,
  }
}

const setup = () => {
  const originalWarn = console.warn
  console.warn = (...args) => {
    if (!String(args[0]).includes('onMounted')) originalWarn(...args)
  }
  try {
    return component.setup({}, { expose() {} })
  } finally {
    console.warn = originalWarn
  }
}

const pageResponse = (items, count, next = null) => ({
  data: { count, next, results: items },
})
const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

test('dataset options traverse every API page and keep edit selections available', async () => {
  const datasets = Array.from({ length: 437 }, (_, index) => ({ id: index + 1, name: `dataset-${index + 1}` }))
  const calls = []
  let activeRequests = 0
  let maxConcurrentRequests = 0
  setApi({
    listExecutionDatasets: async params => {
      calls.push(params)
      activeRequests++
      maxConcurrentRequests = Math.max(maxConcurrentRequests, activeRequests)
      try {
        await new Promise(resolve => setImmediate(resolve))
        const start = (params.page - 1) * params.page_size
        const items = datasets.slice(start, start + params.page_size)
        const next = start + params.page_size < datasets.length ? `/api/executions/datasets/?page=${params.page + 1}` : null
        return pageResponse(items, datasets.length, next)
      } finally {
        activeRequests--
      }
    },
  })
  const state = setup()

  assert.equal(await state.loadDatasetOptions(), true)
  assert.deepEqual(calls.map(call => call.page).sort((a, b) => a - b), [1, 2, 3, 4, 5])
  assert.ok(calls.every(call => call.page_size === 100))
  assert.ok(maxConcurrentRequests > 1 && maxConcurrentRequests <= 4)
  assert.equal(state.datasetOptions.value.length, 437)
  assert.equal(state.datasetOptions.value.find(item => item.id === 101)?.name, 'dataset-101')

  state.openMatrixEdit({ id: 4, dataset: 101, name: 'matrix-101', columns: [], rows: [], enabled: true })
  assert.equal(state.matrixForm.value.dataset, 101)
  assert.ok(state.datasetOptions.value.some(item => item.id === 101))

  state.openMatrixEdit({ id: 5, dataset: 9001, name: 'matrix-legacy', columns: [], rows: [], enabled: true })
  assert.equal(state.matrixForm.value.dataset, 9001)
  assert.ok(state.datasetOptions.value.some(item => item.id === 9001))
})

test('an empty dataset response settles as empty without a load error', async () => {
  setApi({ listExecutionDatasets: async () => pageResponse([], 0) })
  const state = setup()

  assert.equal(await state.loadDatasetOptions(), true)
  assert.deepEqual(state.datasetOptions.value, [])
  assert.equal(state.datasetOptionsLoading.value, false)
  assert.equal(state.datasetOptionsError.value, '')
  assert.equal(state.datasetOptionsPermissionDenied.value, false)
})

test('a later-page failure is atomic and the option retry recovers', async () => {
  const calls = []
  let failSecondPage = true
  const datasets = Array.from({ length: 100 }, (_, index) => ({ id: index + 1, name: `dataset-${index + 1}` }))
  setApi({
    listExecutionDatasets: async params => {
      calls.push(params)
      if (failSecondPage && params.page === 2) {
        failSecondPage = false
        throw { response: { status: 503, data: { detail: 'Page 2 unavailable' } } }
      }
      if (failSecondPage) return pageResponse(datasets, 101, '/api/executions/datasets/?page=2')
      return pageResponse([{ id: 101, name: 'dataset-101' }], 1)
    },
  })
  const state = setup()

  assert.equal(await state.loadDatasetOptions(), false)
  assert.deepEqual(state.datasetOptions.value, [])
  assert.equal(state.datasetOptionsError.value, 'Page 2 unavailable')
  assert.equal(state.datasetOptionsLoading.value, false)

  state.tab.value = 'matrices'
  assert.equal(await state.retryDatasetOptions(), true)
  assert.deepEqual(state.datasetOptions.value, [{ id: 101, name: 'dataset-101' }])
  assert.equal(state.datasetOptionsError.value, '')
  assert.equal(state.datasetOptionsLoading.value, false)
  assert.equal(state.matrixLoading.value, false)
  assert.ok(calls.some(call => call.page === 2))
})

test('stale option requests cannot overwrite newer options or clear their loading and error state', async () => {
  const oldResult = deferred()
  const currentResult = deferred()
  const staleFailure = deferred()
  const lastResult = deferred()
  let request = 0
  setApi({
    listExecutionDatasets: () => [oldResult, currentResult, staleFailure, lastResult][request++].promise,
  })
  const state = setup()

  const oldLoad = state.loadDatasetOptions()
  const currentLoad = state.loadDatasetOptions()
  oldResult.resolve(pageResponse([{ id: 1, name: 'old' }], 1))
  assert.equal(await oldLoad, false)
  assert.equal(state.datasetOptionsLoading.value, true)
  assert.deepEqual(state.datasetOptions.value, [])
  assert.equal(state.datasetOptionsError.value, '')
  currentResult.resolve(pageResponse([{ id: 2, name: 'current' }], 1))
  assert.equal(await currentLoad, true)
  assert.deepEqual(state.datasetOptions.value, [{ id: 2, name: 'current' }])
  assert.equal(state.datasetOptionsLoading.value, false)

  const staleLoad = state.loadDatasetOptions()
  const latestLoad = state.loadDatasetOptions()
  lastResult.resolve(pageResponse([{ id: 4, name: 'latest' }], 1))
  assert.equal(await latestLoad, true)
  staleFailure.reject(new Error('late stale failure'))
  assert.equal(await staleLoad, false)
  assert.deepEqual(state.datasetOptions.value, [{ id: 4, name: 'latest' }])
  assert.equal(state.datasetOptionsError.value, '')
  assert.equal(state.datasetOptionsLoading.value, false)
})
