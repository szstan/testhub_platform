import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import { build } from 'esbuild'
import { compileScript, parse } from '@vue/compiler-sfc'
import * as vue from 'vue'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const componentPath = 'src/views/app-automation/components/DeviceRecorder.vue'
const instances = []

async function loadRecorder({
  deviceId = 17,
  recordingEnabled = false,
  canStart = true,
  canViewScreenshot = false,
  getScreenshot,
  sendInteraction,
} = {}) {
  const filename = resolve(frontend, componentPath)
  const source = await readFile(filename, 'utf8')
  const { descriptor, errors } = parse(source, { filename })
  assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id: 'device-recorder-test' })
  const bundleDir = await mkdtemp(resolve(frontend, 'node_modules', '.app-recorder-test-'))
  const mocks = {
    vue,
    'vue-i18n': { useI18n: () => ({ t: key => key }) },
  }
  const bundle = await build({
    stdin: {
      contents: script.content,
      resolveDir: dirname(filename),
      sourcefile: filename,
      loader: 'js',
    },
    bundle: true,
    write: false,
    platform: 'node',
    format: 'cjs',
    plugins: [{
      name: 'recorder-sfc-imports',
      setup(builder) {
        builder.onResolve({ filter: /.*/ }, args => {
          if (!(args.path in mocks)) throw new Error(`Unexpected component import: ${args.path}`)
          return { path: args.path, namespace: 'recorder-test-mock' }
        })
        builder.onLoad({ filter: /.*/, namespace: 'recorder-test-mock' }, args => ({
          contents: Object.keys(mocks[args.path]).map(name =>
            `export const ${name} = __recorderMocks[${JSON.stringify(args.path)}][${JSON.stringify(name)}];`,
          ).join('\n'),
          loader: 'js',
        }))
      },
    }],
  })
  const module = { exports: {} }
  runInNewContext(bundle.outputFiles[0].text, {
    module,
    exports: module.exports,
    __recorderMocks: mocks,
    console,
  }, { filename })
  const emitted = []
  const props = vue.reactive({
    deviceId,
    recordingEnabled,
    canStart,
    starting: false,
    canViewScreenshot,
    getScreenshot: getScreenshot || (async () => ({ data: { success: true, data: { content: 'data:image/png;base64,fixture' } } })),
    sendInteraction: sendInteraction || (async () => ({ data: { success: true } })),
  })
  const scope = vue.effectScope()
  const state = scope.run(() => module.exports.default.setup(props, {
    emit: (name, ...args) => emitted.push([name, ...args]),
    expose() {},
  }))
  const instance = {
    component: module.exports.default,
    state,
    props,
    emitted,
    async dispose() {
      scope.stop()
      await rm(bundleDir, { recursive: true, force: true })
    },
  }
  instances.push(instance)
  return instance
}

const activateRecording = async recorder => {
  recorder.state.toggleRecording()
  recorder.props.recordingEnabled = true
  await vue.nextTick()
}

const imageFixture = () => ({
  naturalWidth: 200,
  naturalHeight: 400,
  getBoundingClientRect: () => ({ left: 10, top: 20, width: 100, height: 200 }),
})

const pointer = (pointerId, clientX, clientY) => ({
  pointerId,
  clientX,
  clientY,
  currentTarget: { setPointerCapture() {} },
  preventDefault() {},
})

afterEach(async () => {
  await Promise.all(instances.splice(0).map(instance => instance.dispose()))
})

test('refresh is explicit and never sends an interaction by itself', async () => {
  const screenshots = []
  const interactions = []
  const recorder = await loadRecorder({
    canViewScreenshot: true,
    getScreenshot: async id => {
      screenshots.push(id)
      return { data: { success: true, data: { content: 'data:image/png;base64,screen' } } }
    },
    sendInteraction: async (...args) => interactions.push(args),
  })

  assert.equal(recorder.state.screenshotUrl.value, '')
  assert.equal(recorder.state.recordingMode.value, false)
  assert.deepEqual(screenshots, [])
  assert.equal(await recorder.state.refreshScreenshot(), true)
  assert.deepEqual(screenshots, [17])
  assert.deepEqual(interactions, [])
  assert.equal(recorder.state.screenshotUrl.value, 'data:image/png;base64,screen')
})

test('screenshot capture is blocked until the parent confirms current lock ownership', async () => {
  const screenshots = []
  const recorder = await loadRecorder({ getScreenshot: async id => { screenshots.push(id); return 'data:image/png;base64,screen' } })

  assert.equal(await recorder.state.refreshScreenshot(), false)
  assert.deepEqual(screenshots, [])
  assert.equal(recorder.state.screenshotUrl.value, '')
})

test('tap is sent and staged only after explicit recording and a pointer interaction', async () => {
  const interactions = []
  const recorder = await loadRecorder({
    sendInteraction: async (...args) => {
      interactions.push(args)
      return { data: { success: true } }
    },
  })
  recorder.state.imageRef.value = imageFixture()
  recorder.state.handleImageLoad()

  await recorder.state.handlePointerDown(pointer(1, 60, 120))
  await recorder.state.handlePointerUp(pointer(1, 60, 120))
  assert.deepEqual(interactions, [])

  recorder.state.toggleRecording()
  assert.equal(recorder.state.recordingMode.value, false)
  assert.deepEqual(interactions, [])
  recorder.props.recordingEnabled = true
  await vue.nextTick()
  await recorder.state.handlePointerDown(pointer(2, 60, 120))
  await recorder.state.handlePointerUp(pointer(2, 60, 120))

  assert.deepEqual(JSON.parse(JSON.stringify(interactions)), [[17, { action: 'tap', x: 100, y: 200 }]])
  const staged = recorder.emitted.filter(([name]) => name === 'step-staged').at(-1)[1]
  assert.deepEqual(JSON.parse(JSON.stringify(staged)), {
    action: 'tap',
    x_ratio: 0.5,
    y_ratio: 0.5,
    coordinate_space: 'image_ratio',
    image_width: 200,
    image_height: 400,
    index: 1,
  })
  assert.equal(recorder.emitted.some(([name]) => name === 'update:steps'), true)
})

test('swipe sends integer image pixels and stages image-relative endpoints', async () => {
  const interactions = []
  const recorder = await loadRecorder({
    sendInteraction: async (...args) => {
      interactions.push(args)
      return { success: true }
    },
  })
  recorder.state.imageRef.value = imageFixture()
  recorder.state.handleImageLoad()
  await activateRecording(recorder)
  recorder.state.setMode('swipe')

  await recorder.state.handlePointerDown(pointer(3, 20, 30))
  recorder.state.handlePointerMove(pointer(3, 90, 180))
  await recorder.state.handlePointerUp(pointer(3, 90, 180))

  assert.deepEqual(JSON.parse(JSON.stringify(interactions)), [[17, {
    action: 'swipe', x1: 20, y1: 20, x2: 160, y2: 320, duration_ms: 300,
  }]])
  const staged = recorder.emitted.filter(([name]) => name === 'step-staged').at(-1)[1]
  assert.deepEqual(JSON.parse(JSON.stringify(staged)), {
    action: 'swipe',
    start: { x_ratio: 0.1, y_ratio: 0.05 },
    end: { x_ratio: 0.8, y_ratio: 0.8 },
    duration_ms: 300,
    coordinate_space: 'image_ratio',
    image_width: 200,
    image_height: 400,
    index: 1,
  })
})

test('only back, home and enter key actions are sent in key mode', async () => {
  const interactions = []
  const recorder = await loadRecorder({ canStart: false, sendInteraction: async (...args) => {
    interactions.push(args)
    return { success: true }
  } })
  recorder.state.toggleRecording()
  assert.equal(recorder.state.recordingMode.value, false)
  assert.equal(recorder.emitted.some(([name]) => name === 'recording-change'), false)
  recorder.props.canStart = true
  await activateRecording(recorder)
  recorder.state.setMode('key')
  await recorder.state.recordKey('back')
  await recorder.state.recordKey('home')
  await recorder.state.recordKey('enter')
  await recorder.state.recordKey('power')

  assert.deepEqual(JSON.parse(JSON.stringify(interactions)), [
    [17, { action: 'key', key: 'back' }],
    [17, { action: 'key', key: 'home' }],
    [17, { action: 'key', key: 'enter' }],
  ])
  assert.equal(recorder.emitted.filter(([name]) => name === 'step-staged').length, 3)
})

test('failed interactions are not staged and emit only a safe operation code', async () => {
  const recorder = await loadRecorder({
    sendInteraction: async () => { throw new Error('private fixture token and content') },
  })
  await activateRecording(recorder)
  recorder.state.setMode('key')
  await recorder.state.recordKey('back')

  assert.equal(recorder.state.steps.value.length, 0)
  assert.deepEqual(JSON.parse(JSON.stringify(recorder.emitted.filter(([name]) => name === 'error'))), [
    ['error', { operation: 'interaction' }],
  ])
  assert.equal(JSON.stringify(recorder.emitted).includes('private fixture'), false)
})

test('component declares the independent props, staged-step events and accessible controls', async () => {
  const source = await readFile(resolve(frontend, componentPath), 'utf8')
  const recorder = await loadRecorder()

  assert.deepEqual(Object.keys(recorder.component.props).sort(), [
    'canStart', 'canViewScreenshot', 'deviceId', 'getScreenshot', 'recordingEnabled', 'sendInteraction', 'starting',
  ])
  assert.match(source, /aria-pressed/)
  assert.match(source, /aria-live="polite"/)
  assert.match(source, /@pointerdown="handlePointerDown"/)
  assert.match(source, /@pointerup="handlePointerUp"/)
  assert.doesNotMatch(source, /type="password"|type="text"|input\s+type=/i)
})
