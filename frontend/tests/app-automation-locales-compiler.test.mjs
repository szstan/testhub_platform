import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { baseCompile } from '@intlify/message-compiler'
import { build } from 'esbuild'
import { createI18n } from 'vue-i18n'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const bundleDir = await mkdtemp(join(frontend, 'node_modules', '.app-automation-locales-test-'))
after(async () => rm(bundleDir, { recursive: true, force: true }))

await build({
  absWorkingDir: frontend,
  stdin: {
    contents: `import zhCn from './src/locales/lang/zh-cn/app-automation.js';
      import en from './src/locales/lang/en/app-automation.js';
      export { zhCn, en };`,
    resolveDir: frontend,
  },
  bundle: true,
  platform: 'node',
  format: 'esm',
  outfile: join(bundleDir, 'locales.mjs'),
})

const { zhCn, en } = await import(pathToFileURL(join(bundleDir, 'locales.mjs')))
const locales = [
  {
    code: 'zh-cn',
    messages: zhCn,
    headers: 'JSON 对象，如 {"Authorization": "Bearer ..."}',
    params: 'JSON 对象，如 {"page":1}',
    namedResult: '确定删除自定义组件 示例组件 吗？',
  },
  {
    code: 'en',
    messages: en,
    headers: 'JSON object, e.g. {"Authorization": "Bearer ..."}',
    params: 'JSON object, e.g. {"page":1}',
    namedResult: 'Are you sure to delete custom component Example component?',
  },
]

function collectMessages(value, path = '', messages = []) {
  for (const [key, child] of Object.entries(value)) {
    const childPath = path ? `${path}.${key}` : key
    if (typeof child === 'string') messages.push([childPath, child])
    else if (child && typeof child === 'object') collectMessages(child, childPath, messages)
  }
  return messages
}

test('every APP automation locale string compiles with the Vue i18n message compiler', () => {
  for (const locale of locales) {
    const messages = collectMessages(locale.messages)
    assert.ok(messages.length > 0, `${locale.code} locale should contain messages`)

    for (const [path, message] of messages) {
      const errors = []
      baseCompile(message, { onError: error => errors.push(error) })
      assert.deepEqual(errors, [], `${locale.code}.${path}: ${message}`)
    }
  }
})

test('scene builder JSON examples retain their exact displayed text', () => {
  for (const locale of locales) {
    const i18n = createI18n({
      legacy: false,
      locale: locale.code,
      fallbackLocale: false,
      messages: { [locale.code]: locale.messages },
    })

    assert.equal(i18n.global.t('sceneBuilder.fieldPlaceholder.headers'), locale.headers)
    assert.equal(i18n.global.t('sceneBuilder.fieldPlaceholder.params'), locale.params)
    assert.equal(
      i18n.global.t('sceneBuilder.messages.deleteCustomComponentConfirm', { name: locale.code === 'en' ? 'Example component' : '示例组件' }),
      locale.namedResult,
    )
  }
})
