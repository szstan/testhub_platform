import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { afterEach, test } from 'node:test'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { nextTick } from 'vue'
import { loadSfcSetup, plain } from './helpers/sfc-setup.mjs'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const instances = []
const apiNames = [
  'getComponents', 'getCustomComponents', 'createCustomComponent', 'updateCustomComponent',
  'deleteCustomComponent', 'importComponentPackage', 'exportComponentPackage', 'getTestCaseDetail',
  'createTestCase', 'updateTestCase', 'getAppElementList', 'getAppImageCategories', 'getDeviceList',
  'getAppDevice', 'lockDevice', 'unlockDevice', 'acquireRecorderLease', 'releaseRecorderLease',
  'heartbeatDevice', 'captureDeviceScreenshot',
  'recordDeviceInteraction', 'getAppProjects',
]

afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

test('optional recorder and project data sections start collapsed without changing their wiring', async () => {
  const apiCalls = []
  const api = Object.fromEntries(apiNames.map(name => [name, async (...args) => {
    apiCalls.push([name, args])
    return { data: { results: [] } }
  }]))
  const instance = await loadSfcSetup('src/views/app-automation/test-cases/SceneBuilder.vue', {
    'vue-router': { useRoute: () => ({ query: {} }), onBeforeRouteLeave() {} },
    '@element-plus/icons-vue': Object.fromEntries(
      ['Upload', 'Download', 'FolderAdd', 'DocumentCopy', 'Check', 'Search', 'Link', 'Refresh', 'Camera']
        .map(name => [name, {}]),
    ),
    vuedraggable: { default: {} },
    '../elements/components/CaptureElementDialog.vue': { default: {} },
    '../components/DeviceRecorder.vue': { default: {} },
    '../components/StepDiagnostics.vue': { default: {} },
    './NestedFlowEditor.vue': { default: {} },
    '@/stores/user': { useUserStore: () => ({ user: { id: 7 } }) },
    '@/api/app-automation': api,
    '@/api/app-device-options': { getAllAppDevices: async () => [] },
  })
  instances.push(instance)

  assert.deepEqual(plain(instance.state.expandedOptionalPanels.value), [])
  assert.equal(instance.state.recorderEnabled.value, false)
  assert.equal(instance.state.recorderDeviceId.value, null)

  const source = await readFile(resolve(frontend, 'src/views/app-automation/test-cases/SceneBuilder.vue'), 'utf8')
  const template = source.slice(source.indexOf('<template>'), source.lastIndexOf('</template>', source.indexOf('<script setup')))
  const collapse = template.match(/<el-collapse\b[^>]*v-model="expandedOptionalPanels"[^>]*>/)?.[0]
  assert.ok(collapse, 'optional sections should use Element Plus collapse')
  assert.ok(collapse.includes('v-model="expandedOptionalPanels"'))
  assert.doesNotMatch(collapse, /@(?:click|change|update:model-value)=/)

  const recorderPanel = template.match(/<el-collapse-item\s+name="recorder"[^>]*>[\s\S]*?<\/el-collapse-item>/)?.[0]
  assert.ok(recorderPanel, 'recorder section should be collapsible')
  assert.ok(recorderPanel.includes(':title="$t(\'appAutomation.recorder.title\')"'))
  for (const binding of [
    ':device-id="recorderDeviceId"',
    ':recording-enabled="recorderEnabled"',
    ':can-start="canStartRecorder"',
    ':starting="recorderStarting"',
    ':can-view-screenshot="recorderHasOwnedLease"',
    ':get-screenshot="captureDeviceScreenshot"',
    ':send-interaction="sendRecorderInteraction"',
    '@recording-change="handleRecordingChange"',
    '@step-staged="appendRecorderStep"',
    '@error="handleRecorderError"',
  ]) assert.ok(recorderPanel.includes(binding), `recorder wiring should retain ${binding}`)
  assert.ok(recorderPanel.includes('<StepDiagnostics'))

  const dataPanel = template.match(/<el-collapse-item\s+name="scene-data"[^>]*>[\s\S]*?<\/el-collapse-item>/)?.[0]
  assert.ok(dataPanel, 'project data section should be collapsible')
  assert.ok(dataPanel.includes(':title="$t(\'appAutomation.sceneData.title\')"'))
  for (const binding of [
    ':project-id="sceneForm.project"',
    ':get-steps="getProjectComponentSteps"',
    '@components-loaded="setProjectComponents"',
    '@insert-component="insertProjectComponent"',
  ]) assert.ok(dataPanel.includes(binding), `project data wiring should retain ${binding}`)

  const collapseEnd = template.indexOf('</el-collapse>')
  const editorStart = template.indexOf('class="scene-layout"')
  assert.ok(collapseEnd >= 0 && editorStart > collapseEnd, 'core editor should follow the collapsed optional sections')

  instance.state.expandedOptionalPanels.value = ['recorder', 'scene-data']
  await nextTick()
  assert.deepEqual(apiCalls, [], 'expanding panels must not issue device input or lock API calls')
  assert.equal(instance.state.recorderEnabled.value, false)
})
