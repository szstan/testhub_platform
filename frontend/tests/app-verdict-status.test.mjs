import assert from 'node:assert/strict'
import { resolve } from 'node:path'
import { runInNewContext } from 'node:vm'
import { test } from 'node:test'
import { build } from 'esbuild'

const frontend = resolve('frontend')

test('an unverified run is not displayed as a passed business result', async () => {
  const bundle = await build({
    stdin: {
      contents: "export { getDisplayStatus } from './src/utils/app-automation-helpers.js'",
      resolveDir: frontend,
      loader: 'js',
    },
    bundle: true,
    write: false,
    platform: 'node',
    format: 'cjs',
    plugins: [{
      name: 'mock-i18n',
      setup(builder) {
        builder.onResolve({ filter: /^@\/locales$/ }, () => ({ path: 'locales', namespace: 'fixture' }))
        builder.onLoad({ filter: /.*/, namespace: 'fixture' }, () => ({
          contents: 'export default { global: { t: key => key } }', loader: 'js',
        }))
      },
    }],
  })
  const module = { exports: {} }
  runInNewContext(bundle.outputFiles[0].text, { module, exports: module.exports })
  const actual = module.exports.getDisplayStatus('completed', 'unverified')
  assert.equal(actual.type, 'warning')
  assert.equal(actual.text, 'appAutomation.status.unverified')
  assert.equal(module.exports.getDisplayStatus('completed', 'passed').type, 'success')
  assert.equal(module.exports.getDisplayStatus('completed', 'failed').type, 'danger')
})
