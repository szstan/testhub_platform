import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { afterEach, test } from 'node:test'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'

const instances = []
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

const request = (id, collection, name = `Request ${id}`) => ({ id, collection, name })

async function setup(get, apiMethods = {}) {
  const icons = Object.fromEntries(
    ['Plus', 'Folder', 'Document', 'MagicStick', 'Search', 'Close', 'CopyDocument', 'Delete']
      .map(name => [name, {}])
  )
  const instance = await loadSfcSetup('src/views/api-testing/InterfaceManagement.vue', {
    '@/utils/api': { default: { get, ...apiMethods } },
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

test('loads every page of projects, collections, and environments', async () => {
  const projects = Array.from({ length: 21 }, (_, index) => ({ id: index + 1, name: `Project ${index + 1}` }))
  const collections = Array.from({ length: 21 }, (_, index) => ({ id: index + 1, project: 1, name: `Collection ${index + 1}` }))
  const environments = Array.from({ length: 20 }, (_, index) => ({ id: index + 1, scope: 'LOCAL', project: 1 }))
  environments.push({ id: 21, scope: 'LOCAL', project: 1 })
  const calls = new Map()
  const respondWithPages = (items, page, filterParams = '') => ({
    data: {
      count: items.length,
      next: page === 1 ? `https://testhub.example/api/list/?page=2${filterParams}` : null,
      results: page === 1 ? items.slice(0, 20) : items.slice(20),
    },
  })
  const instance = await setup(async (url, config) => {
    const page = Number(config.params?.page || 1)
    calls.set(url, (calls.get(url) || 0) + 1)
    if (url.endsWith('/projects/')) return respondWithPages(projects, page)
    if (url.endsWith('/collections/')) return respondWithPages(collections, page, '&project=1')
    if (url.endsWith('/environments/')) return respondWithPages(environments, page)
    if (url.endsWith('/requests/')) return { data: { count: 0, next: null, results: [] } }
    throw new Error(`Unexpected endpoint ${url}`)
  })

  await instance.state.loadProjects()

  assert.equal(instance.state.projects.value.length, 21)
  assert.equal(instance.state.flatCollections.value.length, 21)
  assert.equal(instance.state.environments.value.length, 21)
  assert.equal(instance.state.environments.value.some(environment => environment.id === 21), true)
  assert.equal(calls.get('/api-testing/projects/'), 2)
  assert.equal(calls.get('/api-testing/collections/'), 2)
  assert.equal(calls.get('/api-testing/environments/'), 2)
})

test('a project switch fences late collection and environment responses', async () => {
  const oldCollections = deferred()
  const oldEnvironments = deferred()
  const instance = await setup(async (url, config) => {
    if (url.endsWith('/collections/')) {
      if (config.params.project === 7) return oldCollections.promise
      return { data: { count: 1, next: null, results: [{ id: 8, name: 'Current', project: 8 }] } }
    }
    if (url.endsWith('/environments/')) {
      if (config.params.project === undefined && instance.state.selectedProject.value === 7) return oldEnvironments.promise
      return { data: [{ id: 82, scope: 'LOCAL', project: 8 }] }
    }
    if (url.endsWith('/requests/')) return { data: { count: 1, next: null, results: [request(80, 8)] } }
    throw new Error(`Unexpected endpoint ${url}`)
  })

  const oldLoad = instance.state.onProjectChange(7)
  instance.state.selectedProject.value = 8
  await instance.state.onProjectChange(8)
  oldCollections.resolve({ data: { count: 1, next: null, results: [{ id: 7, name: 'Stale', project: 7 }] } })
  oldEnvironments.resolve({ data: [{ id: 72, scope: 'LOCAL', project: 7 }] })
  await oldLoad

  assert.deepEqual(plain(instance.state.collections.value.map(item => item.id)), [8])
  assert.deepEqual(plain(instance.state.collections.value[0].children.map(item => item.id)), [80])
  assert.deepEqual(plain(instance.state.environments.value.map(item => item.id)), [82])
})

test('loading request details ignores late responses after a newer selection', async () => {
  const oldDetails = deferred()
  const instance = await setup(async url => {
    if (url.endsWith('/requests/1/')) return oldDetails.promise
    return { data: { id: 2, name: 'Current', params: {}, headers: {}, body: { type: 'raw', data: 'current' } } }
  })

  const oldLoad = instance.state.onNodeClick({ id: 1, type: 'request' })
  await instance.state.onNodeClick({ id: 2, type: 'request' })
  oldDetails.resolve({ data: { id: 1, name: 'Stale', params: {}, headers: {}, body: { type: 'raw', data: 'stale' } } })
  await oldLoad

  assert.equal(instance.state.selectedRequest.value.id, 2)
  assert.equal(instance.state.rawBody.value, 'current')
})

test('new drafts reset request body refs and invalidate loaded detail', async () => {
  const pendingDetails = deferred()
  const instance = await setup(() => pendingDetails.promise)
  instance.state.bodyType.value = 'raw'
  instance.state.rawType.value = 'json'
  instance.state.formData.value = [{ key: 'old', value: 'value' }]
  instance.state.formUrlEncoded.value = [{ key: 'old', value: 'value' }]
  instance.state.rawBody.value = '{"old":true}'
  instance.state.response.value = { status_code: 200 }

  const oldLoad = instance.state.onNodeClick({ id: 1, type: 'request' })
  instance.state.createEmptyRequest()
  pendingDetails.resolve({ data: { id: 1, params: {}, headers: {}, body: { type: 'raw', data: 'late' } } })
  await oldLoad

  assert.equal(instance.state.selectedRequest.value.id, null)
  assert.deepEqual(plain(instance.state.selectedRequest.value.body), { type: 'none', data: null })
  assert.equal(instance.state.bodyType.value, 'none')
  assert.equal(instance.state.rawType.value, 'text')
  assert.equal(instance.state.rawBody.value, '')
  assert.deepEqual(plain(instance.state.formData.value), [])
  assert.deepEqual(plain(instance.state.formUrlEncoded.value), [])
  assert.equal(instance.state.response.value, null)
})

test('request details preserve falsy JSON body values', async () => {
  const values = [false, 0, '', null]
  const instance = await setup(async url => {
    const id = Number(url.match(/requests\/(\d+)\//)[1])
    return { data: { id, params: {}, headers: {}, body: { type: 'json', data: values[id] } } }
  })

  for (let id = 0; id < values.length; id += 1) {
    await instance.state.onNodeClick({ id, type: 'request' })
    assert.equal(instance.state.bodyType.value, 'raw')
    assert.equal(instance.state.rawType.value, 'json')
    assert.equal(instance.state.rawBody.value, JSON.stringify(values[id], null, 2))
  }
})

test('sending an unsaved request is blocked before calling the execute endpoint', async () => {
  let postCalls = 0
  const instance = await setup(async () => ({ data: [] }), {
    post: async () => { postCalls += 1; return { data: {} } },
  })
  instance.state.selectedRequest.value = { id: null, url: 'https://example.test/api', method: 'POST', params: {}, headers: {} }

  await instance.state.sendRequest()

  assert.equal(postCalls, 0)
  assert.deepEqual(instance.messages.map(message => message.type), ['warning'])
})

test('sending falsy raw JSON bodies preserves their parsed values', async () => {
  const submitted = []
  const values = [false, 0, '', null]
  const instance = await setup(async () => ({ data: [] }), {
    post: async (_url, data) => { submitted.push(data.body.data); return { data: { status_code: 200 } } },
  })
  instance.state.selectedRequest.value = { id: 3, url: 'https://example.test/api', method: 'POST', params: {}, headers: {} }
  instance.state.bodyType.value = 'raw'
  instance.state.rawType.value = 'json'

  for (const value of values) {
    instance.state.rawBody.value = JSON.stringify(value)
    await instance.state.sendRequest()
  }

  assert.deepEqual(submitted, values)
})

test('form-data body editor does not expose file upload without a multipart execute contract', async () => {
  const editorSource = await readFile(new URL('../src/views/api-testing/components/KeyValueEditor.vue', import.meta.url), 'utf8')
  const interfaceSource = await readFile(new URL('../src/views/api-testing/InterfaceManagement.vue', import.meta.url), 'utf8')

  assert.doesNotMatch(editorSource, /<el-upload\b/)
  assert.doesNotMatch(editorSource, /value="file"/)
  assert.doesNotMatch(interfaceSource, /:show-file="true"/)
})
