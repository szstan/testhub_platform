import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { readFile } from 'node:fs/promises'
import { ref } from 'vue'
import { loadSfcSetup } from './helpers/sfc-setup.mjs'

const instances = []
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

async function loadElementList({ deleteAppElement, bulkArchive } = {}) {
  const calls = { requests: [], deletes: [], confirmations: [] }
  const translations = {
    'appAutomation.element.messages.archiveTitle': '归档确认',
    'appAutomation.element.messages.archiveConfirm': '确认归档元素“{name}”吗？',
    'appAutomation.element.messages.archiveBlocked': '该元素仍被引用，无法归档。',
    'appAutomation.element.messages.archiveFailed': '归档失败',
  }
  const translate = (key, params = {}) => Object.entries(params).reduce(
    (message, [name, value]) => message.replace(`{${name}}`, value),
    translations[key] || key,
  )
  const api = {
    getAppElementList: async () => ({ data: { results: [], count: 0 } }),
    createAppElement: async () => ({ data: {} }),
    deleteAppElement: async id => {
      calls.deletes.push(id)
      return deleteAppElement ? deleteAppElement(id) : { data: {} }
    },
    getAppProjects: async () => ({ data: [] }),
  }
  const instance = await loadSfcSetup('src/views/app-automation/elements/ElementList.vue', {
    'vue-i18n': { useI18n: () => ({ t: translate, locale: ref('zh-cn') }) },
    '@/api/app-automation': api,
    '@/utils/api': { default: async config => {
      calls.requests.push(config)
      return bulkArchive ? bulkArchive(config) : { data: {} }
    } },
    '@/utils/app-automation-helpers': { formatDateTime: value => value },
    '@element-plus/icons-vue': Object.fromEntries(['Search', 'Plus', 'Camera', 'InfoFilled'].map(name => [name, {}])),
    './components/CaptureElementDialog.vue': { default: {} },
    './components/ManualElementDialog.vue': { default: {} },
  }, {
    confirm: async (...args) => { calls.confirmations.push(args); return true },
    console: { error() {} },
  })
  instances.push(instance)
  return { ...instance, calls }
}

test('from-device capture button opens the existing screenshot dialog', async () => {
  const { state } = await loadElementList()
  const file = new URL('../src/views/app-automation/elements/ElementList.vue', import.meta.url)
  const source = await readFile(file, 'utf8')
  assert.match(source, /@click="showCaptureDialog"/)
  assert.match(source, /<CaptureElementDialog\s+v-model="captureDialogVisible"/)
  assert.equal(state.captureDialogVisible.value, false)
  state.showCaptureDialog()
  assert.equal(state.captureDialogVisible.value, true)
})

test('batch archive uses one atomic request and clears selection after success', async () => {
  const { state, calls, messages } = await loadElementList()
  state.selectedElements.value = [{ id: 12 }, { id: 18 }]

  await state.handleBatchArchive()

  assert.equal(calls.requests.length, 1)
  assert.equal(calls.requests[0].url, '/app-automation/elements/bulk-archive/')
  assert.equal(calls.requests[0].method, 'post')
  assert.deepEqual(Array.from(calls.requests[0].data.ids), [12, 18])
  assert.equal(state.selectedElements.value.length, 0)
  assert.ok(messages.some(message => message.type === 'success'))
})

test('batch conflict keeps all selected rows and shows the reference warning', async () => {
  const conflict = Object.assign(new Error('Conflict'), { response: { status: 409, data: {} } })
  const { state, messages } = await loadElementList({ bulkArchive: async () => { throw conflict } })
  state.selectedElements.value = [{ id: 12 }, { id: 18 }]

  await state.handleBatchArchive()

  assert.equal(state.selectedElements.value.length, 2)
  assert.equal(messages.at(-1).type, 'error')
  assert.match(messages.at(-1).text, /引用/)
})

test('reference count shows a lower-bound marker for unresolved dynamic selectors', async () => {
  const { state } = await loadElementList()

  assert.equal(state.formatReferenceCount({ reference_count: 3, reference_count_unknown: true }), '3+')
  assert.equal(state.formatReferenceCount({ reference_count: 3, reference_count_unknown: false }), 3)
})

test('single archive surfaces a localized reference conflict', async () => {
  const conflict = Object.assign(new Error('Conflict'), { response: { status: 409, data: { msg: 'backend detail' } } })
  const { state, calls, messages } = await loadElementList({ deleteAppElement: async () => { throw conflict } })

  await state.handleArchive({ id: 12, name: 'login button' })

  assert.deepEqual(calls.deletes, [12])
  assert.equal(messages.at(-1).type, 'error')
  assert.match(messages.at(-1).text, /引用/)
  assert.equal(calls.confirmations[0][0], '确认归档元素“login button”吗？')
})
