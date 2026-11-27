import assert from 'node:assert/strict'
import { after, afterEach, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { AxiosError } from 'axios'
import { loadSfcSetup } from './helpers/sfc-setup.mjs'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const bundleDir = await mkdtemp(join(frontend, 'node_modules', '.app-screenshot-test-'))
const interceptorMessages = []
let refreshes = 0
const userStore = {
  accessToken: '',
  isTokenExpiringSoon: () => false,
  getSessionGeneration: () => 0,
  async refreshAccessToken() {
    refreshes++
    userStore.accessToken = 'fresh-token'
  },
  clearSession() {},
}
globalThis.__appScreenshotFixture = { interceptorMessages, userStore }

await build({
  absWorkingDir: frontend,
  stdin: {
    contents: `export { default as api } from './src/utils/api.js';
      export { captureDeviceScreenshot } from './src/api/app-automation.js';`,
    resolveDir: frontend,
  },
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  alias: { '@': join(frontend, 'src') },
  outfile: join(bundleDir, 'app-screenshot.mjs'),
  plugins: [{
    name: 'screenshot-ui-and-store-stubs',
    setup(builder) {
      builder.onResolve({ filter: /^(element-plus|@\/stores\/user)$/ }, ({ path }) => ({
        path,
        namespace: 'screenshot-test-stubs',
      }))
      builder.onLoad({ filter: /.*/, namespace: 'screenshot-test-stubs' }, ({ path }) => ({
        contents: path === 'element-plus'
          ? 'export const ElMessage = { error: text => globalThis.__appScreenshotFixture.interceptorMessages.push(text) };'
          : 'export const useUserStore = () => globalThis.__appScreenshotFixture.userStore;',
        loader: 'js',
      }))
    },
  }],
})
const { api, captureDeviceScreenshot } = await import(pathToFileURL(join(bundleDir, 'app-screenshot.mjs')))
after(async () => {
  delete globalThis.__appScreenshotFixture
  await rm(bundleDir, { recursive: true, force: true })
})

afterEach(() => {
  interceptorMessages.length = 0
  refreshes = 0
  userStore.accessToken = ''
  api.defaults.adapter = async () => { throw new Error('Unexpected fixture request') }
})

const responseError = (config, status, data) => new AxiosError(
  `HTTP ${status} fixture`,
  status === 401 ? 'ERR_BAD_REQUEST' : 'ERR_BAD_RESPONSE',
  config,
  {},
  { config, data, status, statusText: '', headers: {} },
)

test('screenshot API suppresses only its generic 5xx toast', async () => {
  api.defaults.adapter = async (config) => {
    assert.equal(config.url, '/app-automation/devices/17/screenshot/')
    assert.equal(config.suppressServerErrorToast, true)
    throw responseError(config, 503, { detail: 'Device screenshot is unavailable' })
  }

  await assert.rejects(captureDeviceScreenshot(17))
  assert.deepEqual(interceptorMessages, [])
})

test('other 5xx requests retain the shared generic toast', async () => {
  api.defaults.adapter = async (config) => {
    throw responseError(config, 500, { detail: 'Internal fixture detail' })
  }

  await assert.rejects(api.get('/ordinary-endpoint/'))
  assert.deepEqual(interceptorMessages, ['服务器错误，请稍后重试'])
})

test('screenshot 401 still refreshes, retries and preserves its success response', async () => {
  userStore.accessToken = 'old-token'
  let attempts = 0
  api.defaults.adapter = async (config) => {
    attempts++
    if (attempts === 1) throw responseError(config, 401, { detail: 'expired' })
    assert.equal(config.headers.Authorization, 'Bearer fresh-token')
    return {
      config,
      data: { success: true, data: { content: 'image-data' } },
      status: 200,
      statusText: 'OK',
      headers: {},
    }
  }

  const response = await captureDeviceScreenshot(23)

  assert.equal(attempts, 2)
  assert.equal(refreshes, 1)
  assert.deepEqual(response.data, { success: true, data: { content: 'image-data' } })
  assert.deepEqual(interceptorMessages, [])
})

const dialogInstances = []
afterEach(() => dialogInstances.splice(0).forEach(instance => instance.dispose()))

async function setupDialog() {
  let captureRequest = async () => { throw new Error('Unexpected screenshot fixture') }
  const loggedErrors = []
  const instance = await loadSfcSetup('src/views/app-automation/elements/components/CaptureElementDialog.vue', {
    '@element-plus/icons-vue': { FolderOpened: {}, Plus: {}, Delete: {} },
    '@/stores/user': { useUserStore: () => ({ user: { id: 7 } }) },
    '@/api/app-automation': {
      getDeviceList: async () => ({ data: { results: [] } }),
      lockDevice: async () => ({ data: { success: true } }),
      captureDeviceScreenshot: (...args) => captureRequest(...args),
      uploadAppElementImage: async () => ({}),
      createAppElement: async () => ({}),
      getAppImageCategories: async () => ({}),
      createAppImageCategory: async () => ({}),
      deleteAppImageCategory: async () => ({}),
    },
  }, {
    console: { error: (...args) => loggedErrors.push(args) },
  })
  dialogInstances.push(instance)
  instance.state.devices.value = [{ id: 42, status: 'locked', locked_by: 7 }]
  return {
    instance,
    loggedErrors,
    setCaptureRequest(handler) { captureRequest = handler },
  }
}

test('capture allows zero coordinates while distinguishing unset or empty selections', async () => {
  const { instance: { state } } = await setupDialog()
  state.formData.name = 'Screen edge'
  state.formData.element_type = 'pos'
  assert.equal(state.canSave.value, false)
  Object.assign(state.formData.config, { x: 0, y: 0 })
  assert.equal(state.canSave.value, true)
  assert.equal(state.posValue.value, '0,0')

  state.formData.element_type = 'region'
  Object.assign(state.formData.config, { x1: 0, y1: 0, x2: 60, y2: 20 })
  assert.equal(state.canSave.value, true)
  assert.equal(state.regionValue.value, '0,0,60,20')
  state.formData.config.x2 = 0
  assert.equal(state.canSave.value, false)
})

test('capture requires an explicit full image selection and shows its preview', async () => {
  const { instance: { state } } = await setupDialog()
  state.formData.name = 'Screenshot'
  state.templateFileName.value = 'screenshot.png'
  state.capturedImage.value = 'data:image/png;base64,ZmFrZQ=='
  state.imageSize.value = { width: 1200, height: 800 }
  assert.equal(state.canSave.value, false)
  state.saveFullImage.value = true
  assert.equal(state.imagePreview.value, state.capturedImage.value)
  assert.equal(state.canSave.value, true)
})

test('device capture displays readiness without requesting screenshots for pending actions', async () => {
  const { instance: { state }, setCaptureRequest } = await setupDialog()
  let screenshotCalls = 0
  setCaptureRequest(() => { screenshotCalls++ })
  state.selectedDevice.value = 42
  state.devices.value[0].has_pending_action = true
  assert.equal(state.deviceCaptureState.value, 'recovery')
  await state.captureScreen()
  assert.equal(screenshotCalls, 0)
})

test('screenshot dialog displays trimmed string details from each supported backend field', async () => {
  const { instance, loggedErrors, setCaptureRequest } = await setupDialog()
  const details = [
    ['msg', 'Device message'],
    ['message', 'Device message'],
    ['detail', 'Device message'],
  ]

  for (const [field, message] of details) {
    setCaptureRequest(async (deviceId) => {
      assert.equal(deviceId, 42)
      throw {
        response: { status: 503, data: { [field]: `  ${message}  ` } },
        config: { headers: { Authorization: 'must-not-be-logged' } },
      }
    })
    instance.state.selectedDevice.value = 42
    await instance.state.captureScreen()
    assert.equal(instance.messages.at(-1).text, message)
  }

  assert.equal(instance.messages.filter(message => message.type === 'error').length, details.length)
  assert.deepEqual(loggedErrors, [])
})

test('screenshot dialog shows a classified msg from a success=false response', async () => {
  const { instance, setCaptureRequest } = await setupDialog()
  setCaptureRequest(async () => ({ data: { success: false, msg: 'Device is not connected' } }))
  instance.state.selectedDevice.value = 42

  await instance.state.captureScreen()

  assert.equal(instance.messages.at(-1).text, 'Device is not connected')
  assert.equal(instance.state.capturing.value, false)
})

test('screenshot dialog keeps the specific empty-screenshot message', async () => {
  const { instance, setCaptureRequest } = await setupDialog()
  setCaptureRequest(async () => ({ data: { success: true, data: { content: '' } } }))
  instance.state.selectedDevice.value = 42

  await instance.state.captureScreen()

  assert.equal(instance.messages.at(-1).text, 'appAutomation.element.capture.messages.emptyScreenshot')
  assert.equal(instance.state.capturing.value, false)
})

test('screenshot dialog uses its generic fallback for non-string or blank backend details', async () => {
  const { instance, loggedErrors, setCaptureRequest } = await setupDialog()
  setCaptureRequest(async () => {
    throw {
      response: { status: 500, data: { msg: { internal: 'not safe to render' }, message: '  ', detail: 503 } },
      config: { headers: { Authorization: 'must-not-be-logged' } },
    }
  })
  instance.state.selectedDevice.value = 42

  await instance.state.captureScreen()

  assert.equal(instance.messages.at(-1).text, 'appAutomation.element.capture.messages.captureFailed')

  setCaptureRequest(async () => { throw new Error('network failure detail should not be exposed') })
  await instance.state.captureScreen()

  assert.equal(instance.messages.at(-1).text, 'appAutomation.element.capture.messages.captureFailed')
  assert.deepEqual(loggedErrors, [])
})
