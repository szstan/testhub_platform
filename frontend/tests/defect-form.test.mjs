import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { nextTick, reactive } from 'vue'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'

const fixture = {
  id: 15,
  title: '登录状态未更新',
  description: '原描述',
  module: '登录',
  defect_type: 'functional',
  project: { id: 2 },
  version: { id: 7 },
  assignee: { id: 42 },
  due_at: '2026-10-01T12:00:00+08:00'
}
const instances = []
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

async function setup({ edit = true, getDefect = async () => ({ data: fixture }) } = {}) {
  const route = reactive({ params: edit ? { id: String(fixture.id) } : {} })
  const updates = []
  const creates = []
  const uploads = []
  const destinations = []
  const instance = await loadSfcSetup('src/views/defects/DefectForm.vue', {
    'vue-router': {
      useRoute: () => route,
      useRouter: () => ({ push(path) { destinations.push(path) } })
    },
    '@element-plus/icons-vue': { Back: {}, Upload: {} },
    '@/utils/api': { default: { get: async () => ({ data: [] }) } },
    '@/api/defects': {
      getDefect,
      async updateDefect(id, data) { updates.push([id, plain(data)]) },
      async createDefect(data) {
        creates.push(plain(data))
        return { data: { id: 90 } }
      },
      async uploadDefectAttachment(id, data) { uploads.push([id, data.get('name'), data.get('file').size]) }
    },
    './options': { defectTypeOptions: [], priorityOptions: [], severityOptions: [], sourceOptions: [] }
  }, { FormData })
  instances.push(instance)
  await instance.mount()
  instance.state.formRef.value = { validate: async () => true }
  return { ...instance, route, updates, creates, uploads, destinations }
}

test('editing the title omits the assignee and other values read when the form opened', async () => {
  const { state, updates, destinations } = await setup()
  state.form.title = '修改标题'
  await state.submitForm()
  assert.deepEqual(updates, [['15', { title: '修改标题' }]])
  assert.deepEqual(destinations, ['/defects/15'])
})

test('clearing existing fields sends explicit nulls and keeps a cleared text field empty', async () => {
  const { state, updates } = await setup()
  state.form.description = ''
  state.form.assignee_id = undefined
  state.form.version_id = ''
  state.form.due_at = null
  await state.submitForm()
  assert.deepEqual(updates, [['15', { description: '', assignee_id: null, version_id: null, due_at: null }]])
})

test('changing projects supplies the new version selection even when the original version was empty', async () => {
  const { state, updates } = await setup({ getDefect: async () => ({ data: { ...fixture, version: null } }) })
  state.form.project_id = 3
  await state.handleProjectChange()
  await state.submitForm()
  assert.deepEqual(updates, [['15', { project_id: 3, version_id: null }]])
})

test('saving only an attachment uploads it without patching the defect', async () => {
  const { state, updates, uploads, destinations } = await setup()
  state.handleFileChange({ uid: 1, raw: new File(['hello'], 'note.txt') })
  await state.submitForm()
  assert.deepEqual(updates, [])
  assert.deepEqual(uploads, [['15', 'note.txt', 5]])
  assert.deepEqual(destinations, ['/defects/15'])
})

test('a failed detail request blocks edits until the original values can be loaded', async () => {
  let fail = true
  const { state, updates } = await setup({
    getDefect: async () => {
      if (fail) throw new Error('Detail unavailable')
      return { data: fixture }
    }
  })
  state.form.title = '不应保存'
  await state.submitForm()
  assert.deepEqual(updates, [])

  fail = false
  await state.loadDefect()
  state.form.title = '重试后编辑'
  await state.submitForm()
  assert.deepEqual(updates, [['15', { title: '重试后编辑' }]])
})

test('reusing the form for another defect waits for its own detail before patching', async () => {
  const nextDetail = deferred()
  const { state, route, updates } = await setup({
    getDefect: async id => id === '15' ? { data: fixture } : nextDetail.promise
  })
  state.form.title = '上一个缺陷的编辑内容'
  route.params.id = '24'
  await state.submitForm()
  assert.deepEqual(updates, [])

  await nextTick()
  nextDetail.resolve({ data: { ...fixture, id: 24, title: '另一个缺陷', assignee: { id: 51 } } })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(state.form.title, '另一个缺陷')
  state.form.title = '只改新缺陷标题'
  await state.submitForm()
  assert.deepEqual(updates, [['24', { title: '只改新缺陷标题' }]])
})

test('creating a defect submits the full form with normalized optional fields', async () => {
  const { state, updates, creates, destinations } = await setup({ edit: false })
  Object.assign(state.form, { title: '新缺陷', project_id: 2, module: '登录', defect_type: 'functional' })
  await state.submitForm()
  assert.deepEqual(updates, [])
  assert.equal(creates.length, 1)
  assert.equal(creates[0].title, '新缺陷')
  assert.equal(creates[0].project_id, 2)
  assert.equal(creates[0].severity, 'major')
  assert.equal(creates[0].version_id, null)
  assert.equal(creates[0].assignee_id, null)
  assert.deepEqual(destinations, ['/defects/90'])
})
