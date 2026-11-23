<template>
  <section class="device-recorder" :aria-label="t('appAutomation.recorder.title')">
    <div class="recorder-toolbar">
      <button
        type="button"
        class="record-toggle"
        :aria-pressed="recordingMode"
        :disabled="interactionBusy || starting || (!recordingMode && !canStart)"
        @click="toggleRecording"
      >
        {{ t(recordingMode ? 'appAutomation.recorder.stop' : 'appAutomation.recorder.start') }}
      </button>
      <button
        type="button"
        class="refresh-button"
        :disabled="loadingScreenshot || interactionBusy || !canViewScreenshot || deviceId === null || deviceId === undefined"
        :aria-label="t('appAutomation.recorder.refresh')"
        @click="refreshScreenshot"
      >
        {{ t('appAutomation.recorder.refresh') }}
      </button>
      <fieldset class="mode-picker">
        <legend>{{ t('appAutomation.recorder.mode') }}</legend>
        <button
          v-for="mode in modes"
          :key="mode"
          type="button"
          :aria-pressed="selectedMode === mode"
          @click="setMode(mode)"
        >
          {{ t(`appAutomation.recorder.modes.${mode}`) }}
        </button>
      </fieldset>
    </div>

    <p class="recorder-status" role="status" aria-live="polite">
      {{ t(statusKey) }}
    </p>

    <div class="screen-stage">
      <p v-if="!screenshotUrl" class="empty-state">
        {{ t('appAutomation.recorder.empty') }}
      </p>
      <button
        v-else
        type="button"
        class="screen-target"
        :class="{ 'screen-target-active': canInteract }"
        :aria-label="t('appAutomation.recorder.screen')"
        :aria-disabled="!canInteract"
        :disabled="!canInteract"
        @pointerdown="handlePointerDown"
        @pointermove="handlePointerMove"
        @pointerup="handlePointerUp"
        @pointercancel="cancelPointer"
        @keydown="handleImageKeydown"
        @contextmenu.prevent
      >
        <img
          ref="imageRef"
          :src="screenshotUrl"
          alt=""
          draggable="false"
          @load="handleImageLoad"
        />
      </button>
    </div>

    <div v-if="selectedMode === 'key'" class="key-controls" role="group" :aria-label="t('appAutomation.recorder.keys')">
      <button
        v-for="key in androidKeys"
        :key="key"
        type="button"
        :disabled="!canRecord"
        @click="recordKey(key)"
      >
        {{ t(`appAutomation.recorder.keyNames.${key}`) }}
      </button>
    </div>

    <div class="steps-heading">
      <h3>{{ t('appAutomation.recorder.stagedSteps') }}</h3>
      <button type="button" :disabled="steps.length === 0" @click="clearSteps">
        {{ t('appAutomation.recorder.clear') }}
      </button>
    </div>
    <ol class="staged-steps" :aria-label="t('appAutomation.recorder.stagedSteps')">
      <li v-for="(step, index) in steps" :key="index">
        <span>{{ t(`appAutomation.recorder.actions.${step.action}`) }}</span>
        <code v-if="step.action === 'tap'">
          {{ step.x_ratio.toFixed(3) }}, {{ step.y_ratio.toFixed(3) }}
        </code>
        <code v-else-if="step.action === 'swipe'">
          {{ step.start.x_ratio.toFixed(3) }}, {{ step.start.y_ratio.toFixed(3) }}
          -> {{ step.end.x_ratio.toFixed(3) }}, {{ step.end.y_ratio.toFixed(3) }}
        </code>
        <span v-else>{{ t(`appAutomation.recorder.keyNames.${step.key}`) }}</span>
      </li>
    </ol>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps({
  deviceId: { type: [Number, String], default: null },
  recordingEnabled: { type: Boolean, default: false },
  canStart: { type: Boolean, default: false },
  starting: { type: Boolean, default: false },
  canViewScreenshot: { type: Boolean, default: false },
  getScreenshot: { type: Function, required: true },
  sendInteraction: { type: Function, required: true },
})

const emit = defineEmits(['update:steps', 'step-staged', 'recording-change', 'error'])
const { t } = useI18n()
const modes = ['tap', 'swipe', 'key']
const androidKeys = ['back', 'home', 'enter']
const screenshotUrl = ref('')
const imageRef = ref(null)
const imageSize = ref({ width: 0, height: 0 })
const steps = ref([])
const selectedMode = ref('tap')
const recordingMode = computed(() => props.recordingEnabled)
const loadingScreenshot = ref(false)
const interactionBusy = ref(false)
const activePointer = ref(null)
const actionGeneration = ref(0)
const statusKey = ref('appAutomation.recorder.status.idle')
const canRecord = computed(() => recordingMode.value && !interactionBusy.value && !loadingScreenshot.value)
const canInteract = computed(() => canRecord.value && imageSize.value.width > 0)

const unwrap = response => response?.data ?? response

const screenshotContent = response => {
  const body = unwrap(response)
  if (body?.success === false) return ''
  return body?.data?.content || body?.content || (typeof body === 'string' ? body : '')
}

const refreshScreenshot = async () => {
  if (!props.canViewScreenshot || loadingScreenshot.value || interactionBusy.value || props.deviceId === null || props.deviceId === undefined) return false
  const generation = actionGeneration.value
  const deviceId = props.deviceId
  loadingScreenshot.value = true
  statusKey.value = 'appAutomation.recorder.status.loading'
  try {
    const response = await props.getScreenshot(deviceId)
    const content = screenshotContent(response)
    if (typeof content !== 'string' || !content) throw new Error('empty screenshot')
    if (generation !== actionGeneration.value || !props.canViewScreenshot || String(props.deviceId) !== String(deviceId)) return false
    screenshotUrl.value = content
    imageSize.value = { width: 0, height: 0 }
    statusKey.value = recordingMode.value
      ? 'appAutomation.recorder.status.recording'
      : 'appAutomation.recorder.status.idle'
    return true
  } catch (error) {
    if (generation === actionGeneration.value) {
      statusKey.value = 'appAutomation.recorder.status.error'
      const status = Number.isInteger(error?.response?.status) ? error.response.status : undefined
      emit('error', { operation: 'screenshot', ...(status ? { status } : {}) })
    }
    return false
  } finally {
    loadingScreenshot.value = false
  }
}

const handleImageLoad = () => {
  if (!imageRef.value) return
  imageSize.value = {
    width: imageRef.value.naturalWidth || 0,
    height: imageRef.value.naturalHeight || 0,
  }
}

const pointFromEvent = event => {
  const image = imageRef.value
  if (!image || !imageSize.value.width || !imageSize.value.height) return null
  const rect = image.getBoundingClientRect()
  if (!rect.width || !rect.height) return null
  const xRatio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width))
  const yRatio = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height))
  const x = Math.min(imageSize.value.width - 1, Math.floor(xRatio * imageSize.value.width))
  const y = Math.min(imageSize.value.height - 1, Math.floor(yRatio * imageSize.value.height))
  return {
    x,
    y,
    x_ratio: x / imageSize.value.width,
    y_ratio: y / imageSize.value.height,
  }
}

const imageMetadata = () => ({
  coordinate_space: 'image_ratio',
  image_width: imageSize.value.width,
  image_height: imageSize.value.height,
})

const stageSuccessfulInteraction = async (command, step) => {
  if (!canRecord.value) return false
  const generation = actionGeneration.value
  const deviceId = props.deviceId
  interactionBusy.value = true
  try {
    const result = await props.sendInteraction(deviceId, command)
    if (unwrap(result)?.success === false) throw new Error('interaction rejected')
    if (generation !== actionGeneration.value || !props.recordingEnabled || String(props.deviceId) !== String(deviceId)) return false
    const stagedStep = { ...step, index: steps.value.length + 1 }
    steps.value = [...steps.value, stagedStep]
    emit('update:steps', steps.value.slice())
    emit('step-staged', stagedStep)
    statusKey.value = 'appAutomation.recorder.status.recording'
    return true
  } catch (error) {
    if (generation === actionGeneration.value) {
      statusKey.value = 'appAutomation.recorder.status.error'
      const status = Number.isInteger(error?.response?.status) ? error.response.status : undefined
      emit('error', { operation: 'interaction', ...(status ? { status } : {}) })
    }
    return false
  } finally {
    interactionBusy.value = false
  }
}

const handlePointerDown = event => {
  if (!canInteract.value || !['tap', 'swipe'].includes(selectedMode.value)) return
  const start = pointFromEvent(event)
  if (!start) return
  activePointer.value = { pointerId: event.pointerId, start, end: start, mode: selectedMode.value }
  event.currentTarget?.setPointerCapture?.(event.pointerId)
  event.preventDefault?.()
}

const handlePointerMove = event => {
  if (!activePointer.value || activePointer.value.pointerId !== event.pointerId) return
  const end = pointFromEvent(event)
  if (end) activePointer.value = { ...activePointer.value, end }
}

const handlePointerUp = async event => {
  const gesture = activePointer.value
  if (!gesture || gesture.pointerId !== event.pointerId) return false
  const end = pointFromEvent(event) || gesture.end
  activePointer.value = null

  if (gesture.mode === 'tap') {
    const metadata = imageMetadata()
    return stageSuccessfulInteraction(
      { action: 'tap', x: gesture.start.x, y: gesture.start.y },
      {
        action: 'tap',
        x_ratio: gesture.start.x_ratio,
        y_ratio: gesture.start.y_ratio,
        ...metadata,
      },
    )
  }

  if (gesture.start.x === end.x && gesture.start.y === end.y) return false
  const durationMs = 300
  const metadata = imageMetadata()
  return stageSuccessfulInteraction(
    {
      action: 'swipe',
      x1: gesture.start.x,
      y1: gesture.start.y,
      x2: end.x,
      y2: end.y,
      duration_ms: durationMs,
    },
    {
      action: 'swipe',
      start: { x_ratio: gesture.start.x_ratio, y_ratio: gesture.start.y_ratio },
      end: { x_ratio: end.x_ratio, y_ratio: end.y_ratio },
      duration_ms: durationMs,
      ...metadata,
    },
  )
}

const cancelPointer = event => {
  if (activePointer.value?.pointerId === event.pointerId) activePointer.value = null
}

const recordKey = key => {
  if (!canRecord.value || selectedMode.value !== 'key' || !androidKeys.includes(key)) return false
  return stageSuccessfulInteraction(
    { action: 'key', key },
    { action: 'key', key },
  )
}

const handleImageKeydown = event => {
  if (!canInteract.value || selectedMode.value !== 'tap' || !['Enter', ' '].includes(event.key)) return
  event.preventDefault()
  const x = Math.floor(imageSize.value.width / 2)
  const y = Math.floor(imageSize.value.height / 2)
  stageSuccessfulInteraction(
    { action: 'tap', x, y },
    {
      action: 'tap',
      x_ratio: x / imageSize.value.width,
      y_ratio: y / imageSize.value.height,
      ...imageMetadata(),
    },
  )
}

const toggleRecording = () => {
  if (interactionBusy.value || props.starting || (!recordingMode.value && !props.canStart)) return
  activePointer.value = null
  emit('recording-change', !recordingMode.value)
}

const setMode = mode => {
  if (modes.includes(mode)) {
    selectedMode.value = mode
    activePointer.value = null
  }
}

const clearSteps = () => {
  steps.value = []
  emit('update:steps', [])
}

watch(() => props.deviceId, () => {
  actionGeneration.value += 1
  screenshotUrl.value = ''
  imageSize.value = { width: 0, height: 0 }
  activePointer.value = null
  steps.value = []
  statusKey.value = 'appAutomation.recorder.status.idle'
  emit('update:steps', [])
})

watch(() => props.recordingEnabled, enabled => {
  actionGeneration.value += 1
  activePointer.value = null
  statusKey.value = enabled
    ? 'appAutomation.recorder.status.recording'
    : 'appAutomation.recorder.status.idle'
})

watch(() => props.canViewScreenshot, canView => {
  if (canView) return
  actionGeneration.value += 1
  screenshotUrl.value = ''
  imageSize.value = { width: 0, height: 0 }
  activePointer.value = null
})
</script>

<style scoped>
.device-recorder {
  display: grid;
  gap: 12px;
  min-width: 0;
  color: #24313a;
}

.recorder-toolbar,
.mode-picker,
.key-controls,
.steps-heading {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.device-recorder button {
  min-height: 36px;
  padding: 6px 12px;
  border: 1px solid #c8d1d6;
  border-radius: 4px;
  background: #fff;
  color: #24313a;
  font: inherit;
  cursor: pointer;
}

.device-recorder button:hover:not(:disabled) {
  border-color: #317b75;
  background: #f2faf8;
}

.device-recorder button:focus-visible,
.screen-target:focus-visible {
  outline: 3px solid #83c9bd;
  outline-offset: 2px;
}

.device-recorder button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.record-toggle[aria-pressed='true'] {
  border-color: #b45b40;
  color: #8f3d2e;
}

.mode-picker {
  min-width: 0;
  margin: 0;
  padding: 4px 8px 8px;
  border: 1px solid #d7dfe2;
  border-radius: 4px;
}

.mode-picker legend {
  padding: 0 4px;
  color: #52616a;
  font-size: 0.85rem;
}

.mode-picker button[aria-pressed='true'] {
  border-color: #317b75;
  background: #e9f5f2;
}

.recorder-status {
  min-height: 1.4em;
  margin: 0;
  color: #52616a;
}

.screen-stage {
  display: grid;
  min-height: 160px;
  max-height: 68vh;
  place-items: center;
  overflow: auto;
  border: 1px solid #c8d1d6;
  border-radius: 4px;
  background: #22282b;
}

.empty-state {
  margin: 16px;
  color: #e2e8eb;
  text-align: center;
}

.screen-target {
  display: block;
  max-width: 100%;
  margin: 0 auto;
  padding: 0 !important;
  overflow: hidden;
  border: 0 !important;
  border-radius: 0 !important;
  background: transparent !important;
  line-height: 0;
  touch-action: none;
}

.screen-target img {
  display: block;
  width: auto;
  max-width: 100%;
  max-height: 68vh;
  height: auto;
  user-select: none;
}

.screen-target-active {
  cursor: crosshair !important;
}

.key-controls {
  padding-block: 2px;
}

.steps-heading {
  justify-content: space-between;
}

.steps-heading h3 {
  margin: 0;
  font-size: 1rem;
  font-weight: 600;
}

.staged-steps {
  display: grid;
  gap: 6px;
  max-height: 220px;
  margin: 0;
  padding-inline-start: 28px;
  overflow: auto;
}

.staged-steps li {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}

.staged-steps code {
  overflow-wrap: anywhere;
  color: #41525a;
}

@media (max-width: 640px) {
  .recorder-toolbar {
    align-items: stretch;
  }

  .mode-picker {
    flex: 1 1 100%;
  }

  .screen-stage {
    max-height: 58vh;
  }

  .screen-target img {
    max-height: 58vh;
  }
}
</style>
