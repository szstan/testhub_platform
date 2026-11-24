<template>
  <section class="step-diagnostics" :aria-label="t('appAutomation.stepDiagnostics.title')">
    <div class="step-diagnostics-heading">
      <strong>{{ t('appAutomation.stepDiagnostics.title') }}</strong>
      <el-button type="primary" size="small" :loading="submitting" :disabled="!canRun" @click="runStep">
        {{ t('appAutomation.stepDiagnostics.run') }}
      </el-button>
    </div>
    <p v-if="reasonKey" class="hint">{{ t(reasonKey) }}</p>
    <p v-else class="hint">{{ t('appAutomation.stepDiagnostics.ready', { step: stepName || String(stepIndex + 1) }) }}</p>
    <el-button v-if="lastExecutionId" link type="primary" @click="openExecutions">
      {{ t('appAutomation.stepDiagnostics.viewResult') }}
    </el-button>
  </section>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { getAppDevice } from '@/api/app-automation'
import { runSavedSceneStep } from '@/api/app-step-diagnostics'
import { getAppExecutionErrorMessage, prepareAppDeviceForExecution } from '@/utils/app-automation-device-execution.mjs'

const props = defineProps({
  caseId: { type: [Number, String], default: null },
  stepIndex: { type: Number, default: null },
  stepName: { type: String, default: '' },
  nested: { type: Boolean, default: false },
  dirty: { type: Boolean, default: false },
  recording: { type: Boolean, default: false },
  deviceId: { type: [Number, String], default: null },
})
const { t } = useI18n()
const router = useRouter()
const userStore = useUserStore()
const submitting = ref(false)
const lastExecution = ref(null)
const currentUserId = () => userStore.user?.id ?? userStore.user?.pk ?? null
const currentSessionGeneration = () => userStore.getSessionGeneration?.()
const sameIdentifier = (left, right) => (
  left === null || left === undefined || right === null || right === undefined
    ? left === right
    : String(left) === String(right)
)
const sessionMatches = snapshot => sameIdentifier(currentUserId(), snapshot.userId)
  && currentSessionGeneration() === snapshot.sessionGeneration
const editorContextMatches = snapshot => sameIdentifier(props.caseId, snapshot.caseId)
  && sameIdentifier(props.deviceId, snapshot.deviceId)
  && props.stepIndex === snapshot.stepIndex
  && props.dirty === snapshot.dirty
  && props.recording === snapshot.recording
  && props.nested === snapshot.nested
const lastExecutionId = computed(() => {
  const execution = lastExecution.value
  return execution && sessionMatches(execution) ? execution.id : null
})
const reasonKey = computed(() => {
  if (!props.caseId || props.dirty) return 'appAutomation.stepDiagnostics.saveFirst'
  if (props.recording) return 'appAutomation.stepDiagnostics.stopRecording'
  if (props.nested || props.stepIndex === null) return 'appAutomation.stepDiagnostics.selectRootStep'
  if (!props.deviceId) return 'appAutomation.stepDiagnostics.selectDevice'
  return ''
})
const canRun = computed(() => !reasonKey.value && !submitting.value)

const runStep = async () => {
  if (!canRun.value) return false
  const snapshot = {
    caseId: props.caseId,
    stepIndex: props.stepIndex,
    deviceId: props.deviceId,
    dirty: props.dirty,
    recording: props.recording,
    nested: props.nested,
    userId: currentUserId(),
    sessionGeneration: currentSessionGeneration(),
  }
  if (!snapshot.userId) {
    ElMessage.error(t('appAutomation.stepDiagnostics.accountChanged'))
    return false
  }
  submitting.value = true
  try {
    const device = await prepareAppDeviceForExecution({
      selectedId: snapshot.deviceId, userId: snapshot.userId, sessionGeneration: snapshot.sessionGeneration,
      getCurrentUserId: currentUserId,
      getCurrentSessionGeneration: currentSessionGeneration,
      getDevice: getAppDevice, t,
    })
    if (!sessionMatches(snapshot)) throw new Error('diagnostic account changed')
    if (!editorContextMatches(snapshot)) return false
    const response = await runSavedSceneStep(snapshot.caseId, {
      device_id: device.device_id, auto_lock: true, expected_user_id: snapshot.userId,
      step_index: snapshot.stepIndex,
    })
    const data = response?.data ?? response
    if (data?.success === false) throw new Error('diagnostic rejected')
    if (!sessionMatches(snapshot)) {
      ElMessage.error(t('appAutomation.stepDiagnostics.accountChanged'))
      return false
    }
    const executionId = data?.execution?.id
      ?? data?.data?.execution_id
      ?? data?.execution_id
      ?? data?.data?.execution?.id
      ?? null
    if (executionId !== null && executionId !== undefined) {
      lastExecution.value = {
        id: executionId,
        userId: snapshot.userId,
        sessionGeneration: snapshot.sessionGeneration,
      }
    }
    ElMessage.success(t(editorContextMatches(snapshot)
      ? 'appAutomation.stepDiagnostics.submitted'
      : 'appAutomation.stepDiagnostics.submittedAfterContextChange'))
    return true
  } catch (error) {
    const message = sessionMatches(snapshot)
      ? getAppExecutionErrorMessage(error, t('appAutomation.stepDiagnostics.failed'))
      : t('appAutomation.stepDiagnostics.accountChanged')
    ElMessage.error(message)
    return false
  } finally {
    submitting.value = false
  }
}

const openExecutions = () => router.push({ path: '/app-automation/executions' })
</script>

<style scoped>
.step-diagnostics { padding: 12px 0; }
.step-diagnostics-heading { display: flex; align-items: center; gap: 12px; }
.hint { color: var(--el-text-color-secondary); font-size: 13px; margin: 8px 0; }
</style>
