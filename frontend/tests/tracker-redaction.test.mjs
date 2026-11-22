import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import { build } from 'esbuild'
import { test } from 'node:test'

async function loadTracker() {
  const sourcePath = resolve(dirname(fileURLToPath(import.meta.url)), '../src/utils/tracker.js')
  const source = await readFile(sourcePath, 'utf8')
  const events = []
  const apiClient = {
    post: async (_url, payload) => {
      events.push(payload)
      return { data: {} }
    },
  }
  const bundle = await build({
    stdin: { contents: source, resolveDir: dirname(sourcePath), sourcefile: sourcePath, loader: 'js' },
    bundle: true,
    write: false,
    platform: 'node',
    format: 'cjs',
    define: { 'import.meta.env.VITE_ANALYTICS_ENABLED': 'true' },
    plugins: [{
      name: 'tracker-api-mock',
      setup(builder) {
        builder.onResolve({ filter: /^@\/utils\/api$/ }, () => ({ path: 'api', namespace: 'tracker-test' }))
        builder.onLoad({ filter: /.*/, namespace: 'tracker-test' }, () => ({
          contents: 'export default __trackerApi',
          loader: 'js',
        }))
      },
    }],
  })
  const module = { exports: {} }
  runInNewContext(bundle.outputFiles[0].text, {
    module,
    exports: module.exports,
    __trackerApi: apiClient,
    URL,
    crypto: globalThis.crypto,
    window: {
      location: { pathname: '/sso/callback' },
      sessionStorage: { getItem: () => null, setItem() {} },
      crypto: { randomUUID: () => 'test-session' },
    },
  }, { filename: sourcePath })
  return { tracker: module.exports, events }
}

test('SSO authorization codes are excluded from analytics route and query metadata', async () => {
  const secret = 'sensitive-oauth-code'
  const { tracker, events } = await loadTracker()

  await tracker.trackPageView({
    path: '/sso/callback',
    fullPath: `/sso/callback?code=${secret}&state=kept#code=${secret}&state=fragment-state`,
    name: 'sso-callback',
    params: { code: secret, tenant: 'test' },
    query: { code: secret, authorization_code: `${secret}-second`, continue: '/dashboard' },
  }, {
    fullPath: `/login?code=${secret}#authorization_code=${secret}`,
  })

  const payload = events[0]
  assert.equal(payload.page_path, '/sso/callback?state=kept')
  assert.equal(payload.referrer_path, '/login')
  assert.deepEqual(JSON.parse(payload.metadata.params), { tenant: 'test' })
  assert.deepEqual(JSON.parse(payload.metadata.query), { continue: '/dashboard' })
  assert.equal(JSON.stringify(payload).includes(secret), false)
})

test('generic analytics paths redact authorization-code query parameters too', async () => {
  const secret = 'custom-event-oauth-code'
  const { tracker, events } = await loadTracker()

  await tracker.track('oauth_redirect', {
    page_path: `/start?code=${secret}&keep=1#oauth_code=${secret}`,
    referrer_path: `/authorize?authorization_code=${secret}#code=${secret}`,
    target_path: `/callback?oauth_code=${secret}#auth-code=${secret}`,
  })

  assert.equal(events[0].page_path, '/start?keep=1')
  assert.equal(events[0].referrer_path, '/authorize')
  assert.equal(events[0].target_path, '/callback')
  assert.equal(JSON.stringify(events[0]).includes(secret), false)
})
