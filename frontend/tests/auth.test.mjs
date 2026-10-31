import assert from 'node:assert/strict'
import { after, beforeEach, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { AxiosError } from 'axios'

// Compile the production modules, keeping the real Vue/Pinia/Axios runtimes.
// Only UI messages and analytics are stubbed; no browser/network is required.
const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const bundleDir = await mkdtemp(join(frontend, 'node_modules', '.auth-test-'))
await build({
  absWorkingDir: frontend,
  stdin: {
    contents: `export { useUserStore } from './src/stores/user.js';
      export { default as api } from './src/utils/api.js';
      export { createPinia, setActivePinia } from 'pinia';`,
    resolveDir: frontend,
  },
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  alias: { '@': join(frontend, 'src') },
  outfile: join(bundleDir, 'auth.mjs'),
  plugins: [{
    name: 'ui-stubs',
    setup(builder) {
      builder.onResolve({ filter: /^(element-plus|@\/utils\/tracker)$/ }, ({ path }) => ({ path, namespace: 'auth-stubs' }))
      builder.onLoad({ filter: /.*/, namespace: 'auth-stubs' }, () => ({
        contents: 'export const ElMessage = { error() {} }; export const track = () => {};',
        loader: 'js',
      }))
    },
  }],
})
const { useUserStore, api, createPinia, setActivePinia } = await import(pathToFileURL(join(bundleDir, 'auth.mjs')))
after(() => rm(bundleDir, { recursive: true, force: true }))

const fixtureUser = { id: 1, username: 'auth-fixture', is_admin: false }
const jwt = (expires, label = 'access') => `header.${Buffer.from(JSON.stringify({ exp: expires, label })).toString('base64url')}.signature`
const response = (config, data = {}, status = 200) => ({ config, data, status, statusText: '', headers: {} })
const unauthorized = (config) => {
  throw new AxiosError('Unauthorized fixture', 'ERR_BAD_REQUEST', config, {}, response(config, { detail: 'invalid token' }, 401))
}
const deferred = () => {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}
const turn = () => new Promise((resolve) => setImmediate(resolve))

let store
let expiredEvents
let storage
beforeEach((t) => {
  storage = new Map([['access_token', 'legacy'], ['refresh_token', 'legacy']])
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: { removeItem: (key) => storage.delete(key), getItem: (key) => storage.get(key) ?? null },
  })
  const fixtureWindow = new EventTarget()
  Object.defineProperty(fixtureWindow, 'location', {
    get() { throw new Error('Authentication must not navigate by reloading') },
    set() { throw new Error('Authentication must not navigate by reloading') },
  })
  globalThis.window = fixtureWindow
  expiredEvents = 0
  fixtureWindow.addEventListener('testhub:session-expired', () => expiredEvents++)
  setActivePinia(createPinia())
  store = useUserStore()
  t.after(() => store.stopAutoRefresh())
  api.defaults.adapter = () => { throw new Error('Unexpected fixture request') }
})

test('anonymous/invalid cookie startup settles once without logout or reload', { timeout: 2000 }, async () => {
  const requests = []
  api.defaults.adapter = async (config) => { requests.push(config.url); return unauthorized(config) }
  await Promise.all([store.initAuth(), store.initAuth(), store.initAuth()])
  await store.initAuth()
  assert.deepEqual(requests, ['/auth/token/refresh/'])
  assert.equal(store.authInitialized, true)
  assert.equal(store.isAuthenticated, false)
  assert.equal(store.user, null)
  assert.equal(expiredEvents, 0)
  assert.equal(storage.size, 0)
})

test('startup recovers cookie, profile and the actual JWT expiry', async () => {
  const exp = Math.floor(Date.now() / 1000) + 1800
  const token = jwt(exp)
  const requests = []
  api.defaults.adapter = async (config) => {
    requests.push(config.url)
    if (config.url === '/auth/token/refresh/') return response(config, { access: token })
    assert.equal(config.headers.Authorization, `Bearer ${token}`)
    return response(config, fixtureUser)
  }
  await Promise.all([store.initAuth(), store.initAuth()])
  assert.deepEqual(requests, ['/auth/token/refresh/', '/auth/profile/'])
  assert.equal(store.isAuthenticated, true)
  assert.equal(store.tokenExpiresAt, exp * 1000)
})

test('concurrent 401s share one refresh and all retry successfully', { timeout: 2000 }, async () => {
  const exp = Math.floor(Date.now() / 1000) + 3600
  const oldToken = jwt(exp, 'old')
  const nextToken = jwt(exp, 'new')
  store.applySession({ access: oldToken, user: fixtureUser })
  const rotation = deferred()
  let refreshes = 0
  const attempts = new Map()
  api.defaults.adapter = async (config) => {
    if (config.url === '/auth/token/refresh/') {
      refreshes++
      await rotation.promise
      return response(config, { access: nextToken })
    }
    attempts.set(config.url, (attempts.get(config.url) || 0) + 1)
    if (config.headers.Authorization === `Bearer ${oldToken}`) return unauthorized(config)
    assert.equal(config.headers.Authorization, `Bearer ${nextToken}`)
    return response(config, { ok: true })
  }
  const pending = ['/a/', '/b/', '/c/'].map((url) => api.get(url))
  await turn()
  assert.equal(refreshes, 1)
  rotation.resolve()
  const result = await Promise.all(pending)
  assert.equal(result.every((row) => row.data.ok), true)
  assert.deepEqual([...attempts.values()], [2, 2, 2])
  assert.equal(store.isAuthenticated, true)
  assert.equal(expiredEvents, 0)
})

test('a late old-token 401 reuses the completed refresh without another rotation', { timeout: 2000 }, async () => {
  const exp = Math.floor(Date.now() / 1000) + 3600
  const oldToken = jwt(exp, 'old')
  const newToken = jwt(exp, 'new')
  store.applySession({ access: oldToken, user: fixtureUser })
  const late = deferred()
  let refreshes = 0
  api.defaults.adapter = async (config) => {
    if (config.url === '/auth/token/refresh/') {
      refreshes++
      return response(config, { access: newToken })
    }
    if (config.headers.Authorization === `Bearer ${oldToken}`) {
      if (config.url === '/late/') await late.promise
      return unauthorized(config)
    }
    return response(config, { ok: true })
  }
  const first = api.get('/first/')
  const second = api.get('/late/')
  await first
  late.resolve()
  await second
  assert.equal(refreshes, 1)
  assert.equal(store.accessToken, newToken)
})

test('failed proactive refresh releases all callers and allows a later login', { timeout: 2000 }, async () => {
  const exp = Math.floor(Date.now() / 1000)
  store.applySession({ access: jwt(exp + 60), user: fixtureUser })
  const rotation = deferred()
  const requests = []
  api.defaults.adapter = async (config) => {
    requests.push(config.url)
    await rotation.promise
    return unauthorized(config)
  }
  const pending = [api.get('/a/'), api.get('/b/'), store.refreshAccessToken()]
  const settled = Promise.allSettled(pending)
  await turn()
  rotation.resolve()
  assert.equal((await settled).every((row) => row.status === 'rejected'), true)
  assert.deepEqual(requests, ['/auth/token/refresh/'])
  assert.equal(store.accessToken, '')
  assert.equal(store.user, null)
  assert.equal(expiredEvents, 1)
  api.defaults.adapter = async (config) => response(config, { access: jwt(exp + 3600), user: fixtureUser })
  await store.login({ username: 'fixture', password: 'fixture-only' })
  assert.equal(store.isAuthenticated, true)
})

test('401 after the one retry settles and clears the session', { timeout: 2000 }, async () => {
  const exp = Math.floor(Date.now() / 1000) + 3600
  store.applySession({ access: jwt(exp, 'old'), user: fixtureUser })
  let refreshes = 0
  api.defaults.adapter = async (config) => {
    if (config.url === '/auth/token/refresh/') {
      refreshes++
      return response(config, { access: jwt(exp, 'new') })
    }
    return unauthorized(config)
  }
  await assert.rejects(api.get('/still-unauthorized/'))
  assert.equal(refreshes, 1)
  assert.equal(store.isAuthenticated, false)
  assert.equal(expiredEvents, 1)
})

test('expiry and auto-refresh advance with the clock without a state mutation', async (t) => {
  let now = 1900000000000
  t.mock.method(Date, 'now', () => now)
  let tick
  t.mock.method(globalThis, 'setInterval', (fn) => { tick = fn; return 123 })
  t.mock.method(globalThis, 'clearInterval', () => {})
  store.applySession({ access: jwt(now / 1000 + 1800), user: fixtureUser })
  assert.equal(store.isTokenExpiringSoon(), false)
  assert.equal(store.isTokenExpired(), false)
  now += 26 * 60 * 1000
  assert.equal(store.isTokenExpiringSoon(), true)
  let refreshes = 0
  api.defaults.adapter = async (config) => {
    refreshes++
    return response(config, { access: jwt(now / 1000 + 1800) })
  }
  tick()
  await turn()
  assert.equal(refreshes, 1)
  now += 31 * 60 * 1000
  assert.equal(store.isTokenExpired(), true)
})

test('logout waits for an earlier rotation and cannot restore the cleared session', { timeout: 2000 }, async () => {
  const exp = Math.floor(Date.now() / 1000) + 3600
  store.applySession({ access: jwt(exp, 'old'), user: fixtureUser })
  const rotation = deferred()
  const requests = []
  api.defaults.adapter = async (config) => {
    requests.push(config.url)
    if (config.url === '/auth/token/refresh/') {
      await rotation.promise
      return response(config, { access: jwt(exp, 'old-rotation') })
    }
    return response(config)
  }
  const refreshing = store.refreshAccessToken()
  const refreshingResult = assert.rejects(refreshing, /Session changed/)
  await turn()
  const loggingOut = store.logout()
  assert.equal(store.isAuthenticated, false)
  assert.deepEqual(requests, ['/auth/token/refresh/'])
  rotation.resolve()
  await Promise.all([refreshingResult, loggingOut])
  assert.deepEqual(requests, ['/auth/token/refresh/', '/auth/logout/'])
  assert.equal(store.accessToken, '')
  assert.equal(expiredEvents, 0)
})

test('late refresh failure cannot clear a newly logged-in session', { timeout: 2000 }, async () => {
  const exp = Math.floor(Date.now() / 1000) + 3600
  store.applySession({ access: jwt(exp, 'old'), user: fixtureUser })
  const rotation = deferred()
  api.defaults.adapter = async (config) => {
    await rotation.promise
    return unauthorized(config)
  }
  const refreshing = store.refreshAccessToken()
  const result = assert.rejects(refreshing)
  await turn()
  const newToken = jwt(exp, 'new-login')
  store.applySession({ access: newToken, user: fixtureUser })
  rotation.resolve()
  await result
  assert.equal(store.isAuthenticated, true)
  assert.equal(store.accessToken, newToken)
  assert.equal(expiredEvents, 0)
})

test('startup failure cannot erase a login completed while refresh was pending', { timeout: 2000 }, async () => {
  const exp = Math.floor(Date.now() / 1000) + 3600
  const rotation = deferred()
  api.defaults.adapter = async (config) => {
    await rotation.promise
    return unauthorized(config)
  }
  const initializing = store.initAuth()
  await turn()
  const newToken = jwt(exp, 'new-login')
  store.applySession({ access: newToken, user: fixtureUser })
  rotation.resolve()
  await initializing
  assert.equal(store.accessToken, newToken)
  assert.equal(store.isAuthenticated, true)
})

test('an old request 401 cannot log out a newer account', { timeout: 2000 }, async () => {
  const exp = Math.floor(Date.now() / 1000) + 3600
  store.applySession({ access: jwt(exp, 'old-user'), user: fixtureUser })
  const delayed = deferred()
  let refreshes = 0
  api.defaults.adapter = async (config) => {
    if (config.url === '/auth/token/refresh/') refreshes++
    await delayed.promise
    return unauthorized(config)
  }
  const pending = assert.rejects(api.get('/old-user-data/'))
  await turn()
  const token = jwt(exp, 'new-user')
  store.applySession({ access: token, user: { ...fixtureUser, id: 2 } })
  delayed.resolve()
  await pending
  assert.equal(store.accessToken, token)
  assert.equal(store.user.id, 2)
  assert.equal(refreshes, 0)
  assert.equal(expiredEvents, 0)
})
