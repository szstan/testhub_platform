import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import { build } from 'esbuild'
import { compileScript, parse } from '@vue/compiler-sfc'
import * as vue from 'vue'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '../..')

// Compile the real SFC setup, replacing only imports that would perform IO or
// require a mounted application. Vue refs, computed values and watchers are real.
export async function loadSfcSetup(relativePath, imports = {}, globals = {}) {
  const filename = resolve(frontend, relativePath)
  const source = await readFile(filename, 'utf8')
  const { descriptor, errors } = parse(source, { filename })
  assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id: relativePath })
  const mounted = []
  const beforeUnmount = []
  const messages = []
  const mocks = {
    vue: {
      ...vue,
      onMounted: callback => mounted.push(callback),
      onBeforeUnmount: callback => beforeUnmount.push(callback),
    },
    'vue-i18n': { useI18n: () => ({ t: key => key }) },
    'element-plus': {
      ElMessage: Object.fromEntries(['error', 'warning', 'success'].map(type => [type, text => messages.push({ type, text })])),
      ElMessageBox: { confirm: async () => true },
    },
    ...imports,
  }
  const bundle = await build({
    stdin: { contents: script.content, resolveDir: dirname(filename), sourcefile: filename, loader: 'js' },
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
    module, exports: module.exports, __sfcMocks: mocks, console, crypto: globalThis.crypto, ...globals,
  }, { filename })
  const scope = vue.effectScope()
  const state = scope.run(() => module.exports.default.setup({}, { expose() {} }))
  return {
    state,
    messages,
    mount: () => Promise.all(mounted.map(callback => callback())),
    dispose() {
      beforeUnmount.forEach(callback => callback())
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
