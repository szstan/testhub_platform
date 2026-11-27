import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import { build } from 'esbuild'
import { loadSfcSetup, plain } from './helpers/sfc-setup.mjs'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const localeCache = new Map()
const instances = []
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

async function loadLocale(language) {
  if (!localeCache.has(language)) {
    const filename = resolve(frontend, `src/locales/lang/${language}/ui-automation.js`)
    localeCache.set(language, build({
      entryPoints: [filename],
      bundle: true,
      write: false,
      platform: 'node',
      format: 'cjs',
    }).then(result => {
      const module = { exports: {} }
      runInNewContext(result.outputFiles[0].text, { module, exports: module.exports }, { filename })
      return module.exports.default
    }))
  }
  return localeCache.get(language)
}

function translate(locale, key) {
  const path = key.replace(/^uiAutomation\./, '')
  const value = path.split('.').reduce((current, part) => current?.[part], locale)
  return typeof value === 'string' ? value : key
}

async function setup({ createError, updateError, language = 'zh-cn' } = {}) {
  const locale = await loadLocale(language)
  const calls = []
  const icons = Object.fromEntries(['Plus', 'Search', 'View', 'Edit', 'Delete'].map(name => [name, {}]))
  const instance = await loadSfcSetup('src/views/ui-automation/projects/ProjectList.vue', {
    'vue-i18n': { useI18n: () => ({ t: key => translate(locale, key) }) },
    'vue-router': { useRoute: () => ({ name: 'UiProjects', query: {} }), useRouter: () => ({}) },
    '@/utils/ui-project-context.mjs': { normalizeUiId: () => '' },
    '@element-plus/icons-vue': icons,
    '@/api/ui_automation': {
      getUiProjects: async () => ({ data: { results: [], count: 0 } }),
      createUiProject: async data => {
        if (createError) throw createError
        calls.push({ method: 'create', data: plain(data) })
      },
      updateUiProject: async (id, data) => {
        if (updateError) throw updateError
        calls.push({ method: 'update', id, data: plain(data) })
      },
      deleteUiProject: async () => {},
    },
    '@/stores/user': { useUserStore: () => ({ user: { id: 7 } }) },
  }, { URL })
  instances.push(instance)
  return { ...instance, calls, locale }
}

function validateUrl(rules, value) {
  return new Promise(resolve => {
    rules.base_url.find(rule => rule.validator).validator(null, value, error => resolve(error?.message || null))
  })
}

function formRef(rules, form) {
  return {
    async validate() {
      const error = await validateUrl(rules.value, form.base_url)
      if (error) throw new Error(error)
      return true
    },
  }
}

test('base URL form copy and validation are localized in the real Chinese and English catalogs', async () => {
  const source = await readFile(resolve(frontend, 'src/views/ui-automation/projects/ProjectList.vue'), 'utf8')
  assert.equal((source.match(/:label="\$t\('uiAutomation\.project\.baseUrl'\)" prop="base_url">/g) || []).length, 2)
  assert.equal((source.match(/:placeholder="\$t\('uiAutomation\.project\.baseUrlPlaceholder'\)"/g) || []).length, 2)
  assert.equal((source.match(/\$t\('uiAutomation\.project\.baseUrlHint'\)/g) || []).length, 2)
  assert.equal((source.match(/:placeholder="\$t\('uiAutomation\.project\.urlProtocol'\)"/g) || []).length, 2)

  for (const language of ['zh-cn', 'en']) {
    const { state, locale } = await setup({ language })
    const copy = {
      'zh-cn': {
        label: '基础URL',
        placeholder: '请输入地址（如：example.com/admin/）',
        hint: '提示：选择协议并输入地址，也可以粘贴包含协议的完整URL',
        protocol: '协议',
      },
      en: {
        label: 'Base URL',
        placeholder: 'Enter an address (e.g. example.com/admin/)',
        hint: 'Choose a protocol and enter an address, or paste a full URL including its protocol.',
        protocol: 'Protocol',
      },
    }[language]
    assert.equal(locale.project.baseUrl, copy.label)
    assert.equal(locale.project.baseUrlPlaceholder, copy.placeholder)
    assert.equal(locale.project.baseUrlHint, copy.hint)
    assert.equal(locale.project.urlProtocol, copy.protocol)
    assert.equal(state.createFormRules.value.base_url[0].message, locale.project.rules.baseUrlRequired)

    const invalidValues = [
      ['', 'baseUrlRequired'],
      ['http:/example.com', 'baseUrlMalformedProtocol'],
      ['ftp://example.com', 'baseUrlHttpOnly'],
      ['https://user:secret@example.com', 'baseUrlNoCredentials'],
      [`example.com/${'x'.repeat(190)}`, 'baseUrlLength'],
      ['https://[', 'baseUrlInvalid'],
    ]
    for (const rules of [state.createFormRules.value, state.editFormRules.value]) {
      for (const [value, key] of invalidValues) {
        assert.equal(
          await validateUrl(rules, value),
          locale.project.rules[key],
          `${language}: ${JSON.stringify(value)}`
        )
      }
    }
  }
})

test('creation accepts a selected protocol with a trimmed private address', async () => {
  const { state, calls } = await setup()
  state.createForm.name = '商城web后台项目'
  state.urlProtocol.value = 'http://'
  state.createForm.base_url = '  192.168.1.111/admin/  '
  state.createFormRef.value = formRef(state.createFormRules, state.createForm)

  assert.equal(await validateUrl(state.createFormRules.value, state.createForm.base_url), null)
  await state.handleCreate()

  assert.equal(calls[0].method, 'create')
  assert.equal(calls[0].data.base_url, 'http://192.168.1.111/admin/')
  assert.equal(calls[0].data.owner, 7)
})

test('protocol-less hosts and IP addresses with ports receive the selected protocol', async () => {
  const { state, calls } = await setup()
  state.createForm.name = '商城web后台项目'
  state.urlProtocol.value = 'http://'
  state.createForm.base_url = 'localhost:5173/path'
  state.createFormRef.value = formRef(state.createFormRules, state.createForm)

  assert.equal(await validateUrl(state.createFormRules.value, state.createForm.base_url), null)
  assert.equal(await validateUrl(state.createFormRules.value, 'example.com:8443/path'), null)
  await state.handleCreate()
  assert.equal(calls[0].data.base_url, 'http://localhost:5173/path')

  state.editForm.name = '商城web后台项目'
  state.editForm.base_url = '127.0.0.1:8000/admin/'
  state.editUrlProtocol.value = 'https://'
  state.editFormRef.value = formRef(state.editFormRules, state.editForm)

  assert.equal(await validateUrl(state.editFormRules.value, state.editForm.base_url), null)
  await state.handleEdit()
  assert.equal(calls[1].data.base_url, 'https://127.0.0.1:8000/admin/')
})

test('malformed explicit schemes are rejected without sending a prefixed URL', async () => {
  const { state, calls } = await setup()
  state.createForm.name = '商城web后台项目'
  state.createForm.base_url = '  http:/example.com  '
  state.createFormRef.value = formRef(state.createFormRules, state.createForm)

  for (const rules of [state.createFormRules.value, state.editFormRules.value]) {
    for (const value of [
      'http:/example.com',
      'https:example.com',
      'ftp:/example.com',
      'http:80/example.com',
      'https:443/example.com',
      'ftp:21/example.com',
    ]) {
      assert.ok(await validateUrl(rules, value), `Expected rejection for ${JSON.stringify(value)}`)
    }
  }

  for (const value of ['http:/example.com', 'http:80/example.com', 'https:443/example.com', 'ftp:21/example.com']) {
    state.createForm.base_url = value
    await assert.rejects(state.handleCreate(), /URL必须包含完整协议/)
  }
  assert.deepEqual(calls, [])
})

test('restricted-network and save fallback errors are localized without exposing internals', async () => {
  const cases = [
    {
      language: 'zh-cn',
      error: { response: { data: { base_url: ['目标地址属于受限网络'] } } },
    },
    {
      language: 'en',
      error: { response: { data: { detail: 'Target belongs to a restricted network' } } },
    },
  ]

  for (const { language, error } of cases) {
    const network = await setup({ createError: error, language })
    network.state.createForm.name = 'Web project'
    network.state.createForm.base_url = 'https://example.com'
    network.state.createFormRef.value = formRef(network.state.createFormRules, network.state.createForm)
    await network.state.handleCreate()
    assert.equal(network.messages.at(-1).text, network.locale.project.messages.baseUrlRestrictedNetwork)
    assert.doesNotMatch(network.messages.at(-1).text, /UI_ALLOW_PRIVATE_TARGETS|UnsafeOutboundURL|DNS/)

    const updateNetwork = await setup({ updateError: error, language })
    updateNetwork.state.editForm.name = 'Web project'
    updateNetwork.state.editForm.base_url = 'https://example.com'
    updateNetwork.state.editFormRef.value = formRef(updateNetwork.state.editFormRules, updateNetwork.state.editForm)
    await updateNetwork.state.handleEdit()
    assert.equal(updateNetwork.messages.at(-1).text, updateNetwork.locale.project.messages.baseUrlRestrictedNetwork)
    assert.doesNotMatch(updateNetwork.messages.at(-1).text, /UI_ALLOW_PRIVATE_TARGETS|UnsafeOutboundURL|DNS/)

    const createFallback = await setup({ createError: new Error('network unavailable'), language })
    createFallback.state.createForm.name = 'Web project'
    createFallback.state.createForm.base_url = 'https://example.com'
    createFallback.state.createFormRef.value = formRef(createFallback.state.createFormRules, createFallback.state.createForm)
    await createFallback.state.handleCreate()
    assert.equal(createFallback.messages.at(-1).text, createFallback.locale.project.messages.createFailed)

    const updateFallback = await setup({ updateError: new Error('network unavailable'), language })
    updateFallback.state.editForm.name = 'Web project'
    updateFallback.state.editForm.base_url = 'https://example.com'
    updateFallback.state.editFormRef.value = formRef(updateFallback.state.editFormRules, updateFallback.state.editForm)
    await updateFallback.state.handleEdit()
    assert.equal(updateFallback.messages.at(-1).text, updateFallback.locale.project.messages.updateFailed)
  }
})

test('project details keep date-only values on their calendar day and timestamps retain time', async () => {
  const { state } = await setup()
  const dateOptions = { year: 'numeric', month: '2-digit', day: '2-digit' }
  const timestampOptions = {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }
  const date = '2026-11-23'
  const timestamp = '2026-11-23T02:30:00Z'

  assert.equal(state.formatDate(null, null, date), new Date(2026, 10, 23).toLocaleDateString('zh-CN', dateOptions))
  assert.equal(state.formatDate(null, null, timestamp), new Date(timestamp).toLocaleString('zh-CN', timestampOptions))
})

test('pasting a complete URL updates the protocol and saves only one prefix', async () => {
  const { state, calls } = await setup()
  state.createForm.name = '商城web后台项目'
  const pasted = '  http://192.168.1.111/admin/  '
  state.createForm.base_url = pasted
  state.handleCreateUrlInput(pasted)
  state.createFormRef.value = formRef(state.createFormRules, state.createForm)

  assert.equal(state.urlProtocol.value, 'http://')
  assert.equal(state.createForm.base_url, '192.168.1.111/admin/  ')
  await state.handleCreate()

  assert.equal(calls[0].data.base_url, 'http://192.168.1.111/admin/')
})

test('editing reflects the stored protocol and saves a newly selected protocol', async () => {
  const { state, calls } = await setup()
  state.editProject({
    id: 3,
    name: '商城web后台项目',
    description: '',
    status: 'IN_PROGRESS',
    base_url: 'http://192.168.1.111/admin/',
  })
  state.editFormRef.value = formRef(state.editFormRules, state.editForm)
  assert.equal(state.editUrlProtocol.value, 'http://')
  assert.equal(state.editForm.base_url, '192.168.1.111/admin/')

  state.editUrlProtocol.value = 'https://'
  await state.handleEdit()

  assert.equal(calls[0].method, 'update')
  assert.equal(calls[0].id, 3)
  assert.equal(calls[0].data.base_url, 'https://192.168.1.111/admin/')
})

test('create picker dates and edit API dates preserve calendar days in submitted payloads', async () => {
  const { state, calls } = await setup()
  const startDate = new Date(2026, 10, 23)
  const endDate = new Date(2026, 10, 30)

  state.createForm.name = '商城web后台项目'
  state.createForm.base_url = 'https://example.com'
  state.createForm.start_date = startDate
  state.createForm.end_date = endDate
  state.createFormRef.value = formRef(state.createFormRules, state.createForm)
  await state.handleCreate()

  assert.equal(calls[0].data.start_date, '2026-11-23')
  assert.equal(calls[0].data.end_date, '2026-11-30')

  state.editProject({
    id: 3,
    name: '商城web后台项目',
    description: '',
    status: 'IN_PROGRESS',
    base_url: 'https://example.com',
    start_date: '2026-11-23',
    end_date: '2026-11-30',
  })
  assert.deepEqual([
    state.editForm.start_date.getFullYear(),
    state.editForm.start_date.getMonth() + 1,
    state.editForm.start_date.getDate(),
  ], [2026, 11, 23])
  assert.deepEqual([
    state.editForm.end_date.getFullYear(),
    state.editForm.end_date.getMonth() + 1,
    state.editForm.end_date.getDate(),
  ], [2026, 11, 30])
  state.editFormRef.value = formRef(state.editFormRules, state.editForm)
  await state.handleEdit()

  assert.equal(calls[1].method, 'update')
  assert.equal(calls[1].data.start_date, '2026-11-23')
  assert.equal(calls[1].data.end_date, '2026-11-30')
})

test('empty create dates remain null in the submitted payload', async () => {
  const { state, calls } = await setup()
  state.createForm.name = '商城web后台项目'
  state.createForm.base_url = 'https://example.com'
  state.createFormRef.value = formRef(state.createFormRules, state.createForm)

  await state.handleCreate()

  assert.equal(calls[0].data.start_date, null)
  assert.equal(calls[0].data.end_date, null)
})

test('form rules stay stable while URL validation reads the current protocol', async () => {
  const { state } = await setup()
  const createRules = state.createFormRules.value
  const editRules = state.editFormRules.value

  state.urlProtocol.value = 'http://'
  state.editUrlProtocol.value = 'http://'
  assert.strictEqual(state.createFormRules.value, createRules)
  assert.strictEqual(state.editFormRules.value, editRules)
  assert.equal(await validateUrl(createRules, 'example.com'), null)
  assert.equal(await validateUrl(editRules, 'example.com'), null)

  state.urlProtocol.value = 'ftp://'
  state.editUrlProtocol.value = 'ftp://'
  assert.strictEqual(state.createFormRules.value, createRules)
  assert.strictEqual(state.editFormRules.value, editRules)
  assert.equal(await validateUrl(createRules, 'example.com'), 'URL必须以 http:// 或 https:// 开头')
  assert.equal(await validateUrl(editRules, 'example.com'), 'URL必须以 http:// 或 https:// 开头')
})

test('both forms enforce the 200-character limit on the complete URL', async () => {
  const { state } = await setup()
  const prefix = 'http://example.com/'
  const atLimit = 'example.com/' + 'a'.repeat(200 - prefix.length)
  const aboveLimit = atLimit + 'a'

  state.urlProtocol.value = 'http://'
  state.editUrlProtocol.value = 'http://'
  for (const rules of [state.createFormRules.value, state.editFormRules.value]) {
    assert.equal(await validateUrl(rules, atLimit), null)
    assert.equal(await validateUrl(rules, aboveLimit), '基础URL不能超过200个字符')
    assert.equal(await validateUrl(rules, 'http://' + atLimit), null)
    assert.equal(await validateUrl(rules, 'http://' + aboveLimit), '基础URL不能超过200个字符')
  }
})

test('both forms reject empty, malformed, credentialed and unsupported URL inputs', async () => {
  const { state } = await setup()
  for (const rules of [state.createFormRules.value, state.editFormRules.value]) {
    for (const value of ['', '   ', 'ftp://example.com', 'http://', 'http://user:pass@example.com', 'https://@example.com']) {
      assert.ok(await validateUrl(rules, value), `Expected rejection for ${JSON.stringify(value)}`)
    }
    assert.equal(await validateUrl(rules, '  http://192.168.1.111/admin/  '), null)
    assert.equal(await validateUrl(rules, 'https://example.com:8443/path'), null)
  }
})
