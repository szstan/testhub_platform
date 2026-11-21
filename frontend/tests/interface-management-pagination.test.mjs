import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { deferred, loadSfcSetup } from './helpers/sfc-setup.mjs'

const instances = []
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

const request = (id, collection, name = `Request ${id}`) => ({ id, collection, name })

async function setup(get) {
  const icons = Object.fromEntries(
    ['Plus', 'Folder', 'Document', 'MagicStick', 'Search', 'Close', 'CopyDocument', 'Delete']
      .map(name => [name, {}])
  )
  const instance = await loadSfcSetup('src/views/api-testing/InterfaceManagement.vue', {
    '@/utils/api': { default: { get } },
    '@/utils/safe-html': { escapeHtml: value => value },
    './components/KeyValueEditor.vue': { default: {} },
    '@/components/DataFactorySelector.vue': { default: {} },
    '@/utils/requestModel': { RequestModelParser: class {} },
    '@/api/data-factory': { getVariableFunctions: async () => [] },
    '@/utils/codeGenerator': { CodeGenerator: class {} },
    'lodash-es': { debounce: callback => callback },
    '@element-plus/icons-vue': icons,
  }, {
    URL,
    document: { removeEventListener() {} },
    console: { error() {}, log() {}, warn() {} },
  })
  instances.push(instance)
  instance.state.selectedProject.value = 7
  instance.state.collections.value = [
    { id: 4, name: 'Frontend', type: 'collection', children: [] },
    { id: 5, name: 'Backend', type: 'collection', children: [] },
  ]
  return instance
}

test('loads every project page, deduplicates requests and preserves collection grouping', async () => {
  const calls = []
  const instance = await setup(async (url, config) => {
    calls.push({ url, params: { ...config.params } })
    if (config.params.page === 1) {
      return { data: {
        count: 4,
        next: 'https://testhub.example/api/api-testing/requests/?page=2&project=7',
        results: [request(11, 4), request(21, 5), request(31, null)],
      } }
    }
    return { data: {
      count: 4,
      next: null,
      results: [request(31, null), request(22, 5)],
    } }
  })

  await instance.state.loadRequests()

  assert.equal(calls.length, 2)
  assert.equal(calls[0].url, '/api-testing/requests/')
  assert.deepEqual(calls[0].params, { project: 7, page: 1 })
  assert.equal(calls[1].url, '/api-testing/requests/')
  assert.equal(calls[1].params.project, '7')
  assert.equal(calls[1].params.page, '2')

  const collections = instance.state.collections.value
  assert.deepEqual(collections.find(item => item.id === 4).children.map(item => item.id), [11])
  assert.deepEqual(collections.find(item => item.id === 5).children.map(item => item.id), [21, 22])
  assert.deepEqual(collections.filter(item => item.type === 'request').map(item => item.id), [31])
})

test('an empty project result clears old requests without removing collections', async () => {
  const instance = await setup(async () => ({ data: { count: 0, next: null, results: [] } }))
  instance.state.collections.value[0].children = [request(99, 4)]

  await instance.state.loadRequests()

  assert.equal(instance.state.collections.value.length, 2)
  assert.deepEqual(instance.state.collections.value[0].children, [])
  assert.equal(instance.messages.length, 0)
})

test('a later-page failure leaves the previously rendered tree intact', async () => {
  const instance = await setup(async (_url, config) => {
    if (config.params.page === 1) {
      return { data: {
        count: 2,
        next: 'https://testhub.example/api/api-testing/requests/?page=2&project=7',
        results: [request(1, 4)],
      } }
    }
    throw new Error('page two unavailable')
  })
  instance.state.collections.value[0].children = [request(99, 4)]

  await instance.state.loadRequests()

  assert.deepEqual(instance.state.collections.value[0].children.map(item => item.id), [99])
  assert.deepEqual(instance.messages.map(message => message.type), ['error'])
})

test('a late response from the previous project cannot replace the current tree', async () => {
  const oldPage = deferred()
  const instance = await setup((_url, config) => config.params.project === 7
    ? oldPage.promise
    : Promise.resolve({ data: { count: 1, next: null, results: [request(80, 8)] } }))

  const oldLoad = instance.state.loadRequests()
  instance.state.selectedProject.value = 8
  instance.state.collections.value = [{ id: 8, name: 'Current', type: 'collection', children: [] }]
  await instance.state.loadRequests()
  oldPage.resolve({ data: { count: 1, next: null, results: [request(70, 4)] } })
  await oldLoad

  assert.deepEqual(instance.state.collections.value.map(item => item.id), [8])
  assert.deepEqual(instance.state.collections.value[0].children.map(item => item.id), [80])
})
