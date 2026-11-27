<template>
  <section class="native-inspector" :aria-label="t('appAutomation.nativeInspector.title')">
    <div class="inspector-context">
      <label v-if="!fixedProject" class="context-field">
        <span>{{ t('appAutomation.nativeInspector.project') }}</span>
        <select v-model="selectedProjectId" :disabled="busy || Boolean(ownedSession)">
          <option :value="null">{{ t('appAutomation.nativeInspector.selectProject') }}</option>
          <option
            v-for="project in projectOptions"
            :key="project.id"
            :value="project.id"
            :disabled="project.appium_enabled !== true"
          >{{ project.name }}{{ project.appium_enabled === true ? '' : ` · ${t('appAutomation.nativeInspector.projectDisabled')}` }}</option>
        </select>
      </label>
      <div v-else class="context-value">
        <span>{{ t('appAutomation.nativeInspector.project') }}</span>
        <strong>{{ projectName || selectedProjectId || t('appAutomation.nativeInspector.selectProject') }}</strong>
      </div>

      <label class="context-field">
        <span>{{ t('appAutomation.nativeInspector.application') }}</span>
        <select v-model="selectedPackageId" :disabled="busy || Boolean(ownedSession) || appPackageLocked">
          <option :value="null">{{ t('appAutomation.nativeInspector.selectApplication') }}</option>
          <option v-for="item in packageOptions" :key="item.id" :value="item.id">
            {{ item.name || item.package_name }} ({{ item.package_name }})
          </option>
        </select>
      </label>

      <label class="context-field">
        <span>{{ t('appAutomation.nativeInspector.device') }}</span>
        <select v-model="selectedDeviceId" :disabled="busy || Boolean(ownedSession)">
          <option :value="null">{{ t('appAutomation.nativeInspector.selectDevice') }}</option>
          <option
            v-for="device in devices"
            :key="device.id"
            :value="device.id"
            :disabled="!isDeviceUsable(device)"
          >{{ device.name || device.device_id }} · {{ deviceStateLabel(device) }}</option>
        </select>
      </label>

      <div class="device-state" role="status">
        <span>{{ t('appAutomation.nativeInspector.deviceState') }}</span>
        <strong>{{ selectedDevice ? deviceStateLabel(selectedDevice) : t('appAutomation.nativeInspector.noDevice') }}</strong>
      </div>
    </div>

    <div class="inspector-actions">
      <button type="button" :disabled="!canStart || busy || Boolean(ownedSession)" @click="startSession">
        {{ t('appAutomation.nativeInspector.startSession') }}
      </button>
      <button type="button" :disabled="!hasActiveSession || busy" @click="refreshSnapshot">
        {{ t('appAutomation.nativeInspector.refresh') }}
      </button>
      <button type="button" class="danger-action" :disabled="!ownedSession || busy" @click="closeSession">
        {{ t(ownedSession?.status === 'closing' ? 'appAutomation.nativeInspector.confirmClose' : 'appAutomation.nativeInspector.closeSession') }}
      </button>
      <span class="session-status" role="status" aria-live="polite">{{ t(statusKey) }}</span>
    </div>

    <p v-if="errorKey" class="inspector-error" role="alert">{{ t(errorKey, errorParams) }}</p>
    <p v-if="projectId && !projectEnabled" class="inspector-error" role="alert">
      {{ t('appAutomation.nativeInspector.projectDisabled') }}
    </p>
    <p v-if="ownedSession && !hasActiveSession && !['closed', 'failed', 'closing'].includes(ownedSession.status)" class="inspector-warning" role="alert">
      {{ t('appAutomation.nativeInspector.cleanupRequired') }}
    </p>

    <div v-if="hasActiveSession" class="inspector-workspace">
      <div class="snapshot-layout">
        <div class="screen-panel">
          <h4>{{ t('appAutomation.nativeInspector.screen') }}</h4>
          <div v-if="screenshotUrl" ref="screenWrap" class="screen-wrap" @click="selectNodeFromScreen">
            <img ref="screenImage" :src="screenshotUrl" alt="" @load="updateImageSize" />
            <span
              v-if="selectedBoundsStyle"
              class="selected-bounds"
              :style="selectedBoundsStyle"
              aria-hidden="true"
            />
          </div>
          <p v-else class="empty-screen">{{ t('appAutomation.nativeInspector.noScreenshot') }}</p>
          <p v-if="selectedNode" class="selected-node-label">
            {{ t('appAutomation.nativeInspector.selectedNode') }}: {{ nodeSummary(selectedNode) }}
          </p>
        </div>

        <div class="tree-panel">
          <h4>{{ t('appAutomation.nativeInspector.controlTree') }}</h4>
          <p v-if="nodes.length === 0" class="empty-tree">{{ t('appAutomation.nativeInspector.noNodes') }}</p>
          <div v-else class="node-list" role="list">
            <button
              v-for="node in nodes"
              :key="node.node_id"
              type="button"
              class="tree-node"
              :class="{ selected: selectedNodeId === node.node_id }"
              :aria-pressed="selectedNodeId === node.node_id"
              @click="selectNode(node)"
            >
              <span class="node-title">{{ nodeLabel(node) }}</span>
              <code v-if="node.resource_id">{{ node.resource_id }}</code>
              <small v-if="node.content_desc">{{ node.content_desc }}</small>
              <small>{{ node.class_name }} · {{ node.displayed ? t('appAutomation.nativeInspector.displayed') : t('appAutomation.nativeInspector.hidden') }} · {{ node.enabled ? t('appAutomation.nativeInspector.enabled') : t('appAutomation.nativeInspector.disabled') }}</small>
            </button>
          </div>
        </div>
      </div>

      <div class="locator-editor">
        <div class="locator-heading">
          <h4>{{ t('appAutomation.nativeInspector.locator') }}</h4>
          <label class="container-toggle">
            <input v-model="selectingContainer" type="checkbox" />
            {{ t('appAutomation.nativeInspector.selectAsContainer') }}
          </label>
        </div>
        <div class="locator-fields">
          <label class="context-field">
            <span>{{ selectingContainer ? t('appAutomation.nativeInspector.containerStrategy') : t('appAutomation.nativeInspector.strategy') }}</span>
            <select v-model="activeLocator.strategy" :disabled="busy">
              <option value="id">id</option>
              <option value="accessibility_id">accessibility_id</option>
            </select>
          </label>
          <label class="context-field locator-value">
            <span>{{ selectingContainer ? t('appAutomation.nativeInspector.containerValue') : t('appAutomation.nativeInspector.value') }}</span>
            <input v-model.trim="activeLocator.value" maxlength="512" :disabled="busy" />
          </label>
        </div>
        <div class="container-fields">
          <label class="container-toggle">
            <input v-model="containerEnabled" type="checkbox" :disabled="busy" />
            {{ t('appAutomation.nativeInspector.useContainer') }}
          </label>
          <template v-if="containerEnabled">
            <label class="context-field">
              <span>{{ t('appAutomation.nativeInspector.containerStrategy') }}</span>
              <select v-model="form.container.strategy" :disabled="busy">
                <option value="id">id</option>
                <option value="accessibility_id">accessibility_id</option>
              </select>
            </label>
            <label class="context-field locator-value">
              <span>{{ t('appAutomation.nativeInspector.containerValue') }}</span>
              <input v-model.trim="form.container.value" maxlength="512" :disabled="busy" />
            </label>
          </template>
        </div>
        <label class="context-field name-field">
          <span>{{ t('appAutomation.nativeInspector.elementName') }}</span>
          <input v-model.trim="form.name" maxlength="200" :disabled="busy" />
        </label>
        <div class="locator-actions">
          <button type="button" :disabled="!canVerifyLocator || busy" @click="verifyLocator">
            {{ t('appAutomation.nativeInspector.verifyLocator') }}
          </button>
          <button type="button" :disabled="!canSaveElement || busy" @click="saveElement">
            {{ isEditingInitial ? t('appAutomation.nativeInspector.updateElement') : t('appAutomation.nativeInspector.saveElement') }}
          </button>
        </div>
        <div v-if="verification" class="verification-result" :class="verificationClass" role="status" aria-live="polite">
          <strong>{{ verificationLabel }}</strong>
          <span v-if="verification.count === 1 && verification.bounds">
            {{ boundsLabel(verification.bounds) }}
            · {{ verification.displayed ? t('appAutomation.nativeInspector.displayed') : t('appAutomation.nativeInspector.hidden') }}
            · {{ verification.enabled ? t('appAutomation.nativeInspector.enabled') : t('appAutomation.nativeInspector.disabled') }}
          </span>
        </div>
      </div>

      <div class="saved-elements">
        <label class="context-field saved-element-picker">
          <span>{{ t('appAutomation.nativeInspector.savedElements') }}</span>
          <select v-model="selectedSavedElementId" :disabled="busy">
            <option :value="null">{{ t('appAutomation.nativeInspector.selectSavedElement') }}</option>
            <option v-for="item in compatibleElements" :key="item.id" :value="item.id">
              {{ item.name }} · {{ item.config.strategy }}: {{ item.config.value }}
            </option>
          </select>
        </label>
        <button v-if="allowStepBinding" type="button" :disabled="!selectedSavedElement || busy" @click="useSavedElement">
          {{ t('appAutomation.nativeInspector.useInStep') }}
        </button>
        <button type="button" :disabled="!selectedSavedElement || !hasActiveSession || busy" @click="verifySavedElement">
          {{ t('appAutomation.nativeInspector.verifySaved') }}
        </button>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { useUserStore } from '@/stores/user'
import {
  acquireRecorderLease,
  closeAppiumSession,
  createAppElement,
  getAppElementList,
  getAppiumOperation,
  getPackageList,
  heartbeatDevice,
  releaseRecorderLease,
  runAppiumSessionOperation,
  startAppiumSession,
  updateAppElement,
} from '@/api/app-automation'
import { getAllAppDevices } from '@/api/app-device-options'

const props = defineProps({
  projectId: { type: [Number, String], default: null },
  projectOptions: { type: Array, default: () => [] },
  projectName: { type: String, default: '' },
  projectEnabled: { type: Boolean, default: null },
  fixedProject: { type: Boolean, default: false },
  appPackageId: { type: [Number, String], default: null },
  appPackageLocked: { type: Boolean, default: false },
  initialElement: { type: Object, default: null },
  bindingId: { type: [Number, String], default: null },
  allowStepBinding: { type: Boolean, default: false },
})
const emit = defineEmits(['saved', 'use-element'])
const { t } = useI18n()
const userStore = useUserStore()

const sameId = (left, right) => left !== null && left !== undefined && right !== null && right !== undefined
  && String(left) === String(right)
const currentUserId = () => userStore.user?.id ?? userStore.user?.pk ?? null
const currentSessionGeneration = () => typeof userStore.getSessionGeneration === 'function'
  ? userStore.getSessionGeneration()
  : null
const unpack = response => {
  let value = response?.data ?? response
  for (let index = 0; index < 3; index += 1) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) break
    if (value.data && typeof value.data === 'object' && !Array.isArray(value.data)) {
      value = value.data
      continue
    }
    break
  }
  return value
}
const requestId = () => {
  if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID()
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, char => {
    const value = Math.floor(Math.random() * 16)
    return (char === 'x' ? value : (value & 0x3) | 0x8).toString(16)
  })
}

const selectedProjectId = ref(props.projectId ?? null)
const selectedPackageId = ref(props.appPackageId ?? null)
const selectedDeviceId = ref(null)
const packageOptions = ref([])
const devices = ref([])
const nativeElements = ref([])
const selectedSavedElementId = ref(null)
const form = ref({ name: '', strategy: 'id', value: '', container: { strategy: 'id', value: '' } })
const containerEnabled = ref(false)
const selectingContainer = ref(false)
const nodes = ref([])
const selectedNodeId = ref(null)
const screenshotUrl = ref('')
const screenImage = ref(null)
const imageSize = ref({ width: 0, height: 0 })
const verification = ref(null)
const statusKey = ref('appAutomation.nativeInspector.status.idle')
const errorKey = ref('')
const errorParams = ref({})
const busy = ref(false)
const contextEpoch = ref(0)
const ownedSession = ref(null)
const disposed = ref(false)
let requestGeneration = 0
let taskQueue = Promise.resolve()

const selectedProject = computed(() => props.projectOptions.find(item => sameId(item.id, selectedProjectId.value)) || null)
const projectId = computed(() => selectedProjectId.value)
const projectEnabled = computed(() => typeof props.projectEnabled === 'boolean'
  ? props.projectEnabled
  : selectedProject.value?.appium_enabled === true)
const projectName = computed(() => props.projectName || selectedProject.value?.name || '')
const selectedPackage = computed(() => packageOptions.value.find(item => sameId(item.id, selectedPackageId.value)) || null)
const selectedDevice = computed(() => devices.value.find(item => sameId(item.id, selectedDeviceId.value)) || null)
const selectedNode = computed(() => nodes.value.find(item => item.node_id === selectedNodeId.value) || null)
const compatibleElements = computed(() => nativeElements.value.filter(item =>
  selectedPackage.value?.package_name && item.config?.package_name === selectedPackage.value.package_name))
const selectedSavedElement = computed(() => compatibleElements.value.find(item => sameId(item.id, selectedSavedElementId.value)) || null)
const isEditingInitial = computed(() => Boolean(props.initialElement?.id))
const activeLocator = computed(() => selectingContainer.value ? form.value.container : form.value)
const hasActiveSession = computed(() => Boolean(ownedSession.value?.sessionId && ownedSession.value.status === 'active'))
const canStart = computed(() => Boolean(projectId.value && projectEnabled.value && selectedPackageId.value
  && selectedDeviceId.value && selectedDevice.value && isDeviceUsable(selectedDevice.value)
  && currentUserId() !== null && userStore.accessToken))
const canVerifyLocator = computed(() => hasActiveSession.value && Boolean(form.value.strategy && form.value.value.trim())
  && (!containerEnabled.value || Boolean(form.value.container.strategy && form.value.container.value.trim())))
const canSaveElement = computed(() => Boolean(projectId.value && projectEnabled.value && selectedPackage.value?.package_name
  && form.value.name.trim() && form.value.strategy && form.value.value.trim()
  && (!containerEnabled.value || Boolean(form.value.container.strategy && form.value.container.value.trim()))))
const verificationClass = computed(() => verification.value?.count === 1
  ? 'verification-unique'
  : verification.value?.ambiguous_container || (verification.value?.count ?? 0) > 1
    ? 'verification-ambiguous' : 'verification-missing')
const verificationLabel = computed(() => {
  if (verification.value?.ambiguous_container) return t('appAutomation.nativeInspector.verification.ambiguousContainer')
  if (verification.value?.count === 0) return t('appAutomation.nativeInspector.verification.none')
  if (verification.value?.count === 1) return t('appAutomation.nativeInspector.verification.one')
  return t('appAutomation.nativeInspector.verification.many', { count: verification.value?.count ?? 0 })
})
const selectedBoundsStyle = computed(() => {
  const bounds = selectedNode.value ? parseBounds(selectedNode.value.bounds) : null
  if (!bounds || !imageSize.value.width || !imageSize.value.height) return null
  return {
    left: `${bounds.x / imageSize.value.width * 100}%`,
    top: `${bounds.y / imageSize.value.height * 100}%`,
    width: `${bounds.width / imageSize.value.width * 100}%`,
    height: `${bounds.height / imageSize.value.height * 100}%`,
  }
})

function isDeviceUsable(device) {
  return Boolean(device && ['available', 'online'].includes(device.status)
    && !device.locked_by && !device.maintenance_mode && !device.lease_requires_token && !device.has_pending_action)
}

function deviceStateLabel(device) {
  if (!device) return t('appAutomation.nativeInspector.noDevice')
  const state = device.maintenance_mode ? 'maintenance'
    : device.locked_by ? 'locked'
      : device.status === 'offline' ? 'offline'
        : isDeviceUsable(device) ? 'available' : 'unavailable'
  return t(`appAutomation.nativeInspector.deviceStates.${state}`)
}

function captureIdentity() {
  return {
    userId: currentUserId(),
    sessionGeneration: currentSessionGeneration(),
    projectId: selectedProjectId.value,
    packageId: selectedPackageId.value,
    deviceId: selectedDeviceId.value,
    epoch: contextEpoch.value,
  }
}

function isAccountCurrent(identity) {
  return sameId(identity?.userId, currentUserId())
    && identity?.sessionGeneration === currentSessionGeneration()
}

function isContextCurrent(identity) {
  return !disposed.value && identity?.epoch === contextEpoch.value
    && isAccountCurrent(identity)
    && sameId(identity.projectId, selectedProjectId.value)
    && sameId(identity.packageId, selectedPackageId.value)
    && (identity.deviceId === selectedDeviceId.value || sameId(identity.deviceId, selectedDeviceId.value))
}

function enqueue(task) {
  const result = taskQueue.then(task, task)
  taskQueue = result.catch(() => {})
  return result
}

function clearSnapshot() {
  screenshotUrl.value = ''
  imageSize.value = { width: 0, height: 0 }
  nodes.value = []
  selectedNodeId.value = null
  verification.value = null
}

function parseBounds(value) {
  if (value && typeof value === 'object') {
    const { x, y, width, height } = value
    return [x, y, width, height].every(Number.isFinite) ? { x, y, width, height } : null
  }
  const match = typeof value === 'string' && value.match(/^\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]$/)
  if (!match) return null
  const [, x, y, right, bottom] = match.map(Number)
  return { x, y, width: right - x, height: bottom - y }
}

function operationEnvelope(response) {
  const value = unpack(response)
  if (!value || typeof value.operation_id !== 'string' || !value.operation_id) {
    throw new Error('invalid_operation_response')
  }
  return value
}

async function pollOperation(operationId) {
  const deadline = Date.now() + 60000
  while (Date.now() < deadline) {
    const response = await getAppiumOperation(operationId)
    const body = unpack(response) || {}
    const status = String(body.status || '').toLowerCase()
    if (['succeeded', 'success', 'completed', 'failed', 'unknown'].includes(status)) {
      if (!['succeeded', 'success', 'completed'].includes(status)) {
        const error = new Error(body.error || `operation_${status}`)
        error.operationStatus = status
        error.recoveryRequired = body.recovery_required === true || status === 'unknown'
        throw error
      }
      return body.result ?? body
    }
    await new Promise(resolve => setTimeout(resolve, 400))
  }
  const error = new Error('operation_poll_timeout')
  error.recoveryRequired = true
  throw error
}

async function submitOperation(target, action, payload = {}) {
  if (!target?.sessionId || !target.lease?.leaseToken) throw new Error('session_unavailable')
  const accepted = operationEnvelope(await runAppiumSessionOperation(target.sessionId, {
    expected_user_id: target.identity.userId,
    lease_token: target.lease.leaseToken,
    idempotency_key: requestId(),
    action,
    ...payload,
  }))
  return pollOperation(accepted.operation_id)
}

async function getAcceptedStart(deviceId, body) {
  const start = () => startAppiumSession(deviceId, body)
  try {
    return unpack(await start())
  } catch (firstError) {
    try {
      return unpack(await start())
    } catch {
      firstError.recoveryRequired = true
      throw firstError
    }
  }
}

async function heartbeat(target) {
  const canRenew = () => target.status === 'active' && isAccountCurrent(target.identity) && ownedSession.value === target
  if (!canRenew()) return
  try {
    await heartbeatDevice(target.identity.deviceId, {
      lease_token: target.lease.leaseToken,
      lease_seconds: target.lease.leaseSeconds,
    })
  } catch {
    if (!canRenew()) return
    target.recoveryRequired = true
    target.status = 'unknown'
    if (isContextCurrent(target.identity)) {
      statusKey.value = 'appAutomation.nativeInspector.status.leaseLost'
      errorKey.value = 'appAutomation.nativeInspector.errors.leaseLost'
    }
  }
}

function startHeartbeat(target) {
  const seconds = Math.max(30, Math.min(90, Number(target.lease.leaseSeconds) || 90))
  target.heartbeatTimer = setInterval(() => { void heartbeat(target) }, Math.max(10000, Math.floor(seconds * 1000 / 3)))
}

function stopHeartbeat(target) {
  if (target?.heartbeatTimer !== undefined) {
    clearInterval(target.heartbeatTimer)
    target.heartbeatTimer = undefined
  }
}

function hasClosedResult(result) {
  return result?.success === true && result.status === 'closed'
}

async function releaseLease(target) {
  if (target.leaseReleased) return true
  if (!isAccountCurrent(target.identity)) return false
  try {
    await releaseRecorderLease(target.identity.deviceId, {
      expected_user_id: target.identity.userId,
      lease_token: target.lease.leaseToken,
    })
  } catch (error) {
    if (target.status !== 'closed' || !target.closeOperationId
        || ![403, 404].includes(error?.response?.status) || !isAccountCurrent(target.identity)) throw error
    const receipt = unpack(await getAppiumOperation(target.closeOperationId))
    if (!hasClosedResult(receipt?.result) || receipt.result.lease_ended !== true) throw error
  }
  target.leaseReleased = true
  return true
}

async function closeOwnedSession(target) {
  if (!target || ownedSession.value !== target) return false
  if (!isAccountCurrent(target.identity)) {
    target.recoveryRequired = true
    if (!disposed.value) {
      statusKey.value = 'appAutomation.nativeInspector.status.accountChanged'
      errorKey.value = 'appAutomation.nativeInspector.errors.accountChanged'
    }
    return false
  }

  stopHeartbeat(target)
  if (target.sessionId && ['active', 'closing'].includes(target.status)) {
    target.status = 'closing'
    target.closeRequestKey ??= requestId()
    if (isContextCurrent(target.identity)) {
      statusKey.value = 'appAutomation.nativeInspector.status.closing'
      errorKey.value = ''
    }
    try {
      if (!target.closeOperationId) {
        const accepted = operationEnvelope(await closeAppiumSession(target.sessionId, {
          expected_user_id: target.identity.userId,
          lease_token: target.lease.leaseToken,
          idempotency_key: target.closeRequestKey,
        }))
        target.closeOperationId = accepted.operation_id
      }
      const result = await pollOperation(target.closeOperationId)
      if (!hasClosedResult(result)) throw new Error('close_not_confirmed')
      target.status = 'closed'
      target.recoveryRequired = false
      target.leaseReleased = result.lease_ended === true
    } catch (error) {
      let status = 'closePending'
      let errorName = ''
      if (error?.operationStatus === 'unknown' || (error?.operationStatus === 'failed' && error.recoveryRequired)) {
        target.status = 'unknown'
        target.recoveryRequired = true
        status = 'cleanupRequired'
        errorName = 'cleanupUnconfirmed'
      } else if (error?.operationStatus === 'failed') {
        target.status = 'active'
        target.closeRequestKey = null
        target.closeOperationId = null
        status = 'active'
        errorName = 'operationFailed'
        if (isContextCurrent(target.identity)) startHeartbeat(target)
      }
      if (isContextCurrent(target.identity)) {
        statusKey.value = `appAutomation.nativeInspector.status.${status}`
        errorKey.value = errorName ? `appAutomation.nativeInspector.errors.${errorName}` : ''
      }
      return false
    }
  } else if (target.sessionId && !['closed', 'failed'].includes(target.status)) {
    target.recoveryRequired = true
    if (isContextCurrent(target.identity)) {
      statusKey.value = 'appAutomation.nativeInspector.status.cleanupRequired'
      errorKey.value = 'appAutomation.nativeInspector.errors.cleanupUnconfirmed'
    }
    return false
  }

  try {
    if (!await releaseLease(target)) return false
  } catch {
    if (isContextCurrent(target.identity)) {
      statusKey.value = 'appAutomation.nativeInspector.status.releasePending'
      errorKey.value = 'appAutomation.nativeInspector.errors.releaseFailed'
    }
    return false
  }

  if (ownedSession.value === target) ownedSession.value = null
  if (isContextCurrent(target.identity)) statusKey.value = 'appAutomation.nativeInspector.status.closed'
  return true
}

async function releaseUnstartedLease(target) {
  if (!target || target.sessionId || !isAccountCurrent(target.identity)) return false
  try {
    await releaseLease(target)
    if (ownedSession.value === target) ownedSession.value = null
    return true
  } catch {
    target.recoveryRequired = true
    if (isContextCurrent(target.identity)) {
      statusKey.value = 'appAutomation.nativeInspector.status.cleanupRequired'
      errorKey.value = 'appAutomation.nativeInspector.errors.releaseFailed'
    }
    return false
  }
}

async function performStart(identity) {
  if (!isContextCurrent(identity) || !canStart.value) return false
  busy.value = true
  errorKey.value = ''
  statusKey.value = 'appAutomation.nativeInspector.status.acquiringLease'
  const deviceId = identity.deviceId
  let target = null
  try {
    const leaseBody = unpack(await acquireRecorderLease(deviceId, { expected_user_id: identity.userId })) || {}
    const device = leaseBody.device
    const lease = {
      leaseToken: typeof leaseBody.lease_token === 'string' ? leaseBody.lease_token : '',
      leaseSeconds: leaseBody.lease_seconds,
    }
    const verified = Boolean(lease.leaseToken)
      && Number.isInteger(lease.leaseSeconds) && lease.leaseSeconds >= 30 && lease.leaseSeconds <= 90
      && sameId(device?.locked_by, identity.userId)
      && device?.status === 'locked' && device?.lease_requires_token === true
    if (!verified) {
      if (lease.leaseToken) {
        ownedSession.value = { identity, lease, sessionId: null, status: 'starting' }
        target = ownedSession.value
        if (isAccountCurrent(identity)) await releaseUnstartedLease(target)
      }
      throw new Error('lease_unverified')
    }
    ownedSession.value = { identity, lease, sessionId: null, status: 'starting' }
    target = ownedSession.value
    if (!isAccountCurrent(identity)) throw new Error('account_changed')

    statusKey.value = 'appAutomation.nativeInspector.status.starting'
    const startBody = await getAcceptedStart(deviceId, {
      expected_user_id: identity.userId,
      lease_token: lease.leaseToken,
      project_id: identity.projectId,
      app_package_id: identity.packageId,
      idempotency_key: requestId(),
    })
    if (!startBody?.session_id || !startBody?.operation_id) {
      target.status = 'unknown'
      target.recoveryRequired = true
      throw new Error('start_not_confirmed')
    }
    target.sessionId = String(startBody.session_id)
    target.startOperationId = String(startBody.operation_id)
    const result = await pollOperation(target.startOperationId)
    if (result?.success !== true || result.status !== 'active') {
      target.status = result?.status === 'failed' && result?.recovery_required !== true ? 'failed' : 'unknown'
      target.recoveryRequired = target.status !== 'failed'
      if (target.status === 'failed') await closeOwnedSession(target)
      throw new Error(result?.error || 'start_failed')
    }
    target.status = 'active'
    if (!isContextCurrent(identity)) {
      await closeOwnedSession(target)
      return false
    }
    startHeartbeat(target)
    statusKey.value = 'appAutomation.nativeInspector.status.active'
    await refreshSnapshotNow(target, identity)
    return true
  } catch (error) {
    if (target && !target.sessionId && target.status !== 'unknown' && isAccountCurrent(identity)) {
      await releaseUnstartedLease(target)
    }
    if (target?.sessionId && error.operationStatus === 'failed' && error.recoveryRequired === false) {
      target.status = 'failed'
      await closeOwnedSession(target)
    }
    if (target?.sessionId && target.status !== 'failed' && !target.recoveryRequired) {
      target.status = 'unknown'
      target.recoveryRequired = true
    }
    if (isContextCurrent(identity)) {
      statusKey.value = target?.recoveryRequired
        ? 'appAutomation.nativeInspector.status.cleanupRequired'
        : 'appAutomation.nativeInspector.status.error'
      errorKey.value = target?.recoveryRequired
        ? 'appAutomation.nativeInspector.errors.cleanupUnconfirmed'
        : mapError(error)
    }
    return false
  } finally {
    busy.value = false
  }
}

function mapError(error) {
  const status = Number.isInteger(error?.response?.status) ? error.response.status : undefined
  if (status === 403) return 'appAutomation.nativeInspector.errors.leaseLost'
  if (status === 409) return 'appAutomation.nativeInspector.errors.deviceBusy'
  if (status === 503) return 'appAutomation.nativeInspector.errors.serviceUnavailable'
  if (error?.message === 'lease_unverified') return 'appAutomation.nativeInspector.errors.leaseUnverified'
  if (error?.message === 'account_changed') return 'appAutomation.nativeInspector.errors.accountChanged'
  if (error?.message === 'start_not_confirmed') return 'appAutomation.nativeInspector.errors.cleanupUnconfirmed'
  return 'appAutomation.nativeInspector.errors.operationFailed'
}

function startSession() {
  if (busy.value || ownedSession.value || !canStart.value) return Promise.resolve(false)
  const identity = captureIdentity()
  return enqueue(() => performStart(identity))
}

async function refreshSnapshotNow(target, identity) {
  if (!isContextCurrent(identity) || ownedSession.value !== target || target.status !== 'active') return false
  busy.value = true
  statusKey.value = 'appAutomation.nativeInspector.status.loadingSnapshot'
  errorKey.value = ''
  try {
    const treeResult = await submitOperation(target, 'tree')
    if (!isContextCurrent(identity) || ownedSession.value !== target) return false
    let nextScreenshot = treeResult?.screenshot || ''
    if (!nextScreenshot) {
      const screenshotResult = await submitOperation(target, 'screenshot')
      nextScreenshot = screenshotResult?.screenshot || screenshotResult?.content || ''
    }
    if (!isContextCurrent(identity) || ownedSession.value !== target) return false
    if (typeof nextScreenshot !== 'string' || !nextScreenshot.startsWith('data:image/')) {
      throw new Error('screenshot_unavailable')
    }
    const nextNodes = Array.isArray(treeResult?.nodes) ? treeResult.nodes : []
    screenshotUrl.value = nextScreenshot
    nodes.value = nextNodes.filter(node => node && Number.isSafeInteger(node.node_id))
    selectedNodeId.value = null
    imageSize.value = { width: 0, height: 0 }
    statusKey.value = 'appAutomation.nativeInspector.status.active'
    return true
  } catch (error) {
    if (isContextCurrent(identity)) {
      statusKey.value = 'appAutomation.nativeInspector.status.error'
      errorKey.value = error?.recoveryRequired
        ? 'appAutomation.nativeInspector.errors.cleanupUnconfirmed'
        : 'appAutomation.nativeInspector.errors.operationFailed'
    }
    return false
  } finally {
    busy.value = false
  }
}

function refreshSnapshot() {
  const target = ownedSession.value
  if (!target || target.status !== 'active' || busy.value) return Promise.resolve(false)
  const identity = target.identity
  return enqueue(() => refreshSnapshotNow(target, identity))
}

function verifyLocator() {
  if (!canVerifyLocator.value || busy.value) return Promise.resolve(false)
  const target = ownedSession.value
  const identity = target.identity
  const locator = {
    package_name: selectedPackage.value.package_name,
    strategy: form.value.strategy,
    value: form.value.value.trim(),
  }
  if (containerEnabled.value) {
    locator.container = {
      strategy: form.value.container.strategy,
      value: form.value.container.value.trim(),
    }
  }
  return enqueue(async () => {
    busy.value = true
    verification.value = null
    errorKey.value = ''
    statusKey.value = 'appAutomation.nativeInspector.status.verifying'
    try {
      const result = await submitOperation(target, 'locate', { locator })
      if (!isContextCurrent(identity)) return false
      verification.value = normalizeVerification(result)
      statusKey.value = 'appAutomation.nativeInspector.status.verified'
      return true
    } catch (error) {
      if (isContextCurrent(identity)) {
        statusKey.value = 'appAutomation.nativeInspector.status.error'
        errorKey.value = error?.recoveryRequired
          ? 'appAutomation.nativeInspector.errors.cleanupUnconfirmed'
          : 'appAutomation.nativeInspector.errors.operationFailed'
      }
      return false
    } finally {
      busy.value = false
    }
  })
}

function normalizeVerification(result) {
  const count = Number.isSafeInteger(result?.count)
    ? Math.max(0, result.count)
    : result?.found === true ? 1 : 0
  return {
    count,
    ambiguous_container: result?.ambiguous_container === true,
    bounds: result?.bounds && typeof result.bounds === 'object' ? result.bounds : null,
    displayed: result?.displayed === true,
    enabled: result?.enabled === true,
  }
}

async function verifySavedElement() {
  const item = selectedSavedElement.value
  const target = ownedSession.value
  if (!item || !target || target.status !== 'active' || busy.value) return false
  const identity = target.identity
  return enqueue(async () => {
    busy.value = true
    verification.value = null
    errorKey.value = ''
    statusKey.value = 'appAutomation.nativeInspector.status.verifying'
    try {
      const result = await submitOperation(target, 'locate', { element_id: item.id })
      if (!isContextCurrent(identity)) return false
      verification.value = normalizeVerification(result)
      statusKey.value = 'appAutomation.nativeInspector.status.verified'
      return true
    } catch {
      if (isContextCurrent(identity)) {
        statusKey.value = 'appAutomation.nativeInspector.status.error'
        errorKey.value = 'appAutomation.nativeInspector.errors.operationFailed'
      }
      return false
    } finally {
      busy.value = false
    }
  })
}

function buildElementConfig() {
  const config = {
    package_name: selectedPackage.value.package_name,
    strategy: form.value.strategy,
    value: form.value.value.trim(),
  }
  if (containerEnabled.value) {
    config.container = {
      strategy: form.value.container.strategy,
      value: form.value.container.value.trim(),
    }
  }
  return config
}

function saveElement() {
  if (!canSaveElement.value || busy.value) return Promise.resolve(false)
  const identity = captureIdentity()
  const input = {
    name: form.value.name.trim(),
    project: identity.projectId,
    element_type: 'native',
    config: buildElementConfig(),
  }
  const targetElement = props.initialElement
  const bindingId = props.bindingId
  return enqueue(async () => {
    busy.value = true
    errorKey.value = ''
    statusKey.value = 'appAutomation.nativeInspector.status.saving'
    try {
      const response = targetElement?.id
        ? await updateAppElement(targetElement.id, input)
        : await createAppElement(input)
      const saved = unpack(response)
      const element = saved?.id ? saved : { ...input, ...(targetElement || {}), id: saved?.id }
      if (!element.id) throw new Error('element_save_unconfirmed')
      if (!isContextCurrent(identity)) return false
      await loadNativeElements(identity.projectId, identity)
      if (!isContextCurrent(identity)) return false
      selectedSavedElementId.value = element.id
      statusKey.value = 'appAutomation.nativeInspector.status.saved'
      emit('saved', { element, bindingId })
      if (hasActiveSession.value) {
        const result = await submitOperation(ownedSession.value, 'locate', { element_id: element.id })
        if (isContextCurrent(identity)) {
          verification.value = normalizeVerification(result)
          statusKey.value = 'appAutomation.nativeInspector.status.verified'
        }
      }
      return true
    } catch {
      if (isContextCurrent(identity)) {
        statusKey.value = 'appAutomation.nativeInspector.status.error'
        errorKey.value = 'appAutomation.nativeInspector.errors.saveFailed'
      }
      return false
    } finally {
      busy.value = false
    }
  })
}

function useSavedElement() {
  const element = selectedSavedElement.value
  if (!element || !props.allowStepBinding) return false
  emit('use-element', { element, bindingId: props.bindingId })
  return true
}

function locatorForNode(node) {
  if (node.resource_id?.trim()) return { strategy: 'id', value: node.resource_id.trim() }
  if (node.content_desc?.trim()) return { strategy: 'accessibility_id', value: node.content_desc.trim() }
  return null
}

function selectNode(node) {
  selectedNodeId.value = node.node_id
  const locator = locatorForNode(node)
  if (!locator) {
    errorKey.value = 'appAutomation.nativeInspector.errors.nodeHasNoLocator'
    verification.value = null
    return false
  }
  errorKey.value = ''
  if (selectingContainer.value) {
    form.value.container = locator
    containerEnabled.value = true
    selectingContainer.value = false
  } else {
    form.value.strategy = locator.strategy
    form.value.value = locator.value
    verification.value = null
  }
  return true
}

function nodeLabel(node) {
  return node.text || node.content_desc || node.resource_id || node.class_name || `#${node.node_id}`
}

function nodeSummary(node) {
  const locator = locatorForNode(node)
  return locator ? `${locator.strategy}: ${locator.value}` : nodeLabel(node)
}

function selectNodeFromScreen(event) {
  if (!screenImage.value || !imageSize.value.width || !imageSize.value.height) return
  const rect = screenImage.value.getBoundingClientRect()
  if (!rect.width || !rect.height) return
  const x = (event.clientX - rect.left) / rect.width * imageSize.value.width
  const y = (event.clientY - rect.top) / rect.height * imageSize.value.height
  const matches = nodes.value.map(node => ({ node, bounds: parseBounds(node.bounds) }))
    .filter(item => item.bounds && x >= item.bounds.x && x <= item.bounds.x + item.bounds.width
      && y >= item.bounds.y && y <= item.bounds.y + item.bounds.height)
    .sort((left, right) => left.bounds.width * left.bounds.height - right.bounds.width * right.bounds.height)
  if (matches[0]) selectNode(matches[0].node)
}

function updateImageSize() {
  imageSize.value = {
    width: screenImage.value?.naturalWidth || 0,
    height: screenImage.value?.naturalHeight || 0,
  }
}

function boundsLabel(bounds) {
  if (!bounds || typeof bounds !== 'object') return ''
  return `${bounds.x ?? '?'} / ${bounds.y ?? '?'} · ${bounds.width ?? '?'} × ${bounds.height ?? '?'}`
}

function selectSavedElement(id) {
  const item = compatibleElements.value.find(element => sameId(element.id, id))
  if (!item) return
  form.value = {
    name: item.name || '',
    strategy: item.config.strategy || 'id',
    value: item.config.value || '',
    container: { ...(item.config.container || { strategy: 'id', value: '' }) },
  }
  containerEnabled.value = Boolean(item.config.container)
  verification.value = null
}

async function loadNativeElements(project, identity = null) {
  if (!project) {
    nativeElements.value = []
    selectedSavedElementId.value = null
    return
  }
  const generation = ++requestGeneration
  try {
    const response = await getAppElementList({ project, element_type: 'native', page: 1, page_size: 100 })
    const body = unpack(response)
    if (generation !== requestGeneration || !sameId(selectedProjectId.value, project)
      || (identity && !isAccountCurrent(identity))) return
    nativeElements.value = (Array.isArray(body) ? body : Array.isArray(body?.results) ? body.results : [])
      .filter(item => item?.element_type === 'native' && item?.config && typeof item.config === 'object')
    if (!nativeElements.value.some(item => sameId(item.id, selectedSavedElementId.value))) selectedSavedElementId.value = null
  } catch {
    if (generation === requestGeneration) nativeElements.value = []
  }
}

async function loadDevices(identity) {
  try {
    const list = await getAllAppDevices()
    if (isAccountCurrent(identity)) devices.value = Array.isArray(list) ? list : []
  } catch {
    if (isAccountCurrent(identity)) {
      devices.value = []
      errorKey.value = 'appAutomation.nativeInspector.errors.deviceListFailed'
    }
  }
}

async function loadPackages(identity) {
  try {
    const response = await getPackageList({ page: 1, page_size: 100 })
    const body = unpack(response)
    if (!isAccountCurrent(identity)) return
    packageOptions.value = Array.isArray(body) ? body : Array.isArray(body?.results) ? body.results : []
    const initialPackageName = props.initialElement?.config?.package_name
    const initialPackage = packageOptions.value.find(item => item.package_name === initialPackageName)
    if (props.appPackageId !== null && props.appPackageId !== undefined) selectedPackageId.value = props.appPackageId
    else if (initialPackage) selectedPackageId.value = initialPackage.id
    else if (packageOptions.value.length === 1) selectedPackageId.value = packageOptions.value[0].id
  } catch {
    if (isAccountCurrent(identity)) {
      packageOptions.value = []
      errorKey.value = 'appAutomation.nativeInspector.errors.applicationListFailed'
    }
  }
}

async function loadSnapshotLists() {
  const identity = captureIdentity()
  await Promise.all([loadDevices(identity), loadPackages(identity)])
  if (isContextCurrent(identity)) await loadNativeElements(identity.projectId, identity)
}

function closeSession() {
  const target = ownedSession.value
  if (!target || busy.value) return Promise.resolve(false)
  return enqueue(async () => {
    busy.value = true
    try {
      return target.sessionId ? await closeOwnedSession(target) : await releaseUnstartedLease(target)
    } finally {
      busy.value = false
    }
  })
}

function cleanupContextChange() {
  const target = ownedSession.value
  clearSnapshot()
  verification.value = null
  if (!target) return
  if (!isAccountCurrent(target.identity)) {
    target.recoveryRequired = true
    target.status = 'unknown'
    stopHeartbeat(target)
    statusKey.value = 'appAutomation.nativeInspector.status.accountChanged'
    errorKey.value = 'appAutomation.nativeInspector.errors.accountChanged'
    return
  }
  void enqueue(async () => {
    busy.value = true
    try {
      if (target.sessionId) await closeOwnedSession(target)
      else await releaseUnstartedLease(target)
    } finally {
      busy.value = false
    }
  })
}

watch(() => props.projectId, value => {
  if (props.fixedProject && !sameId(selectedProjectId.value, value)) selectedProjectId.value = value ?? null
})
watch(() => props.appPackageId, value => {
  if (props.appPackageLocked) selectedPackageId.value = value ?? null
})
watch(() => props.initialElement, value => {
  form.value = {
    name: value?.name || '',
    strategy: value?.config?.strategy || 'id',
    value: value?.config?.value || '',
    container: { ...(value?.config?.container || { strategy: 'id', value: '' }) },
  }
  containerEnabled.value = Boolean(value?.config?.container)
  selectedSavedElementId.value = value?.id ?? null
}, { immediate: true })
watch(() => [selectedProjectId.value, selectedPackageId.value, selectedDeviceId.value, currentUserId(), currentSessionGeneration()], () => {
  contextEpoch.value += 1
  clearSnapshot()
  errorKey.value = ''
  verification.value = null
  void loadNativeElements(selectedProjectId.value)
  cleanupContextChange()
})
watch(() => form.value.strategy + '\u0000' + form.value.value + '\u0000' + JSON.stringify(form.value.container) + '\u0000' + containerEnabled.value, () => {
  verification.value = null
})
onMounted(() => { void loadSnapshotLists() })
onBeforeUnmount(() => {
  disposed.value = true
  contextEpoch.value += 1
  const target = ownedSession.value
  if (!target) return
  if (!isAccountCurrent(target.identity)) {
    stopHeartbeat(target)
    return
  }
  void enqueue(async () => {
    if (target.sessionId) await closeOwnedSession(target)
    else await releaseUnstartedLease(target)
  })
})
</script>

<style scoped>
.native-inspector {
  display: grid;
  min-width: 0;
  gap: 12px;
  color: #26363b;
}

.inspector-context,
.inspector-actions,
.locator-fields,
.container-fields,
.locator-actions,
.saved-elements {
  display: flex;
  align-items: end;
  flex-wrap: wrap;
  gap: 10px;
}

.context-field {
  display: grid;
  min-width: 180px;
  flex: 1 1 190px;
  gap: 5px;
  color: #405158;
  font-size: 12px;
}

.context-field input,
.context-field select {
  box-sizing: border-box;
  width: 100%;
  min-height: 34px;
  padding: 6px 8px;
  border: 1px solid #c6d1d4;
  border-radius: 4px;
  background: #fff;
  color: #26363b;
  font: inherit;
  font-size: 14px;
}

.context-field input:focus,
.context-field select:focus {
  border-color: #24756d;
  outline: 2px solid #d8eee9;
}

.context-value,
.device-state {
  display: grid;
  min-width: 120px;
  gap: 5px;
  font-size: 12px;
}

.context-value strong,
.device-state strong {
  font-size: 14px;
}

.native-inspector button {
  min-height: 34px;
  padding: 6px 10px;
  border: 1px solid #bfcdd0;
  border-radius: 4px;
  background: #fff;
  color: #25363c;
  font: inherit;
  cursor: pointer;
}

.native-inspector button:hover:not(:disabled) {
  border-color: #24756d;
  background: #f0f8f6;
}

.native-inspector button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.inspector-actions {
  align-items: center;
  padding-block: 8px;
  border-block: 1px solid #dce4e5;
}

.danger-action {
  border-color: #d3a39a !important;
  color: #853f34 !important;
}

.session-status {
  color: #516168;
}

.inspector-error,
.inspector-warning {
  margin: 0;
  padding: 8px 10px;
  border-left: 3px solid #bd5146;
  background: #fff5f2;
  color: #76352d;
}

.inspector-warning {
  border-color: #bb8938;
  background: #fff9ed;
  color: #684b1c;
}

.inspector-workspace {
  display: grid;
  gap: 14px;
}

.snapshot-layout {
  display: grid;
  grid-template-columns: minmax(250px, 1fr) minmax(280px, 1fr);
  gap: 14px;
  align-items: start;
}

.screen-panel,
.tree-panel,
.locator-editor {
  display: grid;
  min-width: 0;
  gap: 8px;
}

.screen-panel h4,
.tree-panel h4,
.locator-editor h4 {
  margin: 0;
  font-size: 15px;
}

.screen-wrap {
  position: relative;
  display: inline-grid;
  justify-self: start;
  max-width: 100%;
  max-height: 62vh;
  overflow: auto;
  border: 1px solid #c5d1d4;
  background: #f4f7f7;
  cursor: crosshair;
}

.screen-wrap img {
  display: block;
  width: auto;
  max-width: min(100%, 420px);
  max-height: 62vh;
  object-fit: contain;
}

.selected-bounds {
  position: absolute;
  border: 2px solid #d4513a;
  background: #d4513a24;
  pointer-events: none;
}

.empty-screen,
.empty-tree,
.selected-node-label {
  margin: 0;
  color: #596970;
  font-size: 13px;
}

.node-list {
  display: grid;
  max-height: 62vh;
  overflow: auto;
  border: 1px solid #d2dcde;
}

.tree-node {
  display: grid;
  justify-items: start;
  gap: 3px;
  border: 0 !important;
  border-bottom: 1px solid #e3e9ea !important;
  border-radius: 0 !important;
  text-align: left;
  overflow-wrap: anywhere;
}

.tree-node.selected {
  border-left: 3px solid #24756d !important;
  background: #e9f5f2;
}

.tree-node code,
.tree-node small {
  color: #506168;
  white-space: normal;
  overflow-wrap: anywhere;
}

.node-title {
  font-weight: 600;
}

.locator-editor {
  padding-top: 12px;
  border-top: 1px solid #dce4e5;
}

.locator-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}

.container-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #405158;
  font-size: 13px;
}

.container-fields {
  align-items: end;
  padding: 8px;
  border-left: 2px solid #3f9184;
  background: #f2f8f6;
}

.name-field {
  max-width: 460px;
}

.verification-result {
  display: grid;
  gap: 4px;
  padding: 8px 10px;
  border-left: 3px solid #358060;
  background: #eff8f3;
  color: #24533d;
}

.verification-missing {
  border-color: #bd5146;
  background: #fff5f2;
  color: #76352d;
}

.verification-ambiguous {
  border-color: #bb8938;
  background: #fff9ed;
  color: #684b1c;
}

.saved-elements {
  padding-top: 12px;
  border-top: 1px solid #dce4e5;
}

.saved-element-picker {
  min-width: 260px;
}

@media (max-width: 760px) {
  .snapshot-layout {
    grid-template-columns: minmax(0, 1fr);
  }

  .inspector-actions > button,
  .locator-actions > button,
  .saved-elements > button {
    flex: 1 1 130px;
  }
}
</style>
