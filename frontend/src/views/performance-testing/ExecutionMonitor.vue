<template>
  <div class="perf-monitor" v-loading="loading">
    <!-- ---------- 顶栏 ---------- -->
    <div class="monitor-head">
      <div class="head-left">
        <el-button link :icon="ArrowLeft" @click="goBack">{{ t('performanceTesting.common.back') }}</el-button>
        <el-divider direction="vertical" />
        <span class="exec-no">{{ execution.execution_no || '-' }}</span>
        <span class="scenario-name">{{ execution.scenario_name }}</span>
        <el-tag :type="statusTagType(execution.status)" size="small">
          {{ execution.status ? t('performanceTesting.status.' + execution.status) : '-' }}
        </el-tag>
        <el-tag
          v-if="execution.sla_result && execution.sla_result !== 'NOT_EVALUATED'"
          :type="slaTagType(execution.sla_result)"
          size="small"
          effect="plain"
        >
          SLA {{ t('performanceTesting.sla.' + execution.sla_result) }}
        </el-tag>
      </div>
      <div class="head-right">
        <span class="conn-badge" :class="channel">
          <i class="dot" />{{ channelText }}
        </span>
        <el-button v-if="isActive" type="danger" plain :icon="VideoPause" @click="handleStop">
          {{ t('performanceTesting.execution.stop') }}
        </el-button>
        <el-button v-else type="primary" :icon="Document" @click="goReport">
          {{ t('performanceTesting.monitor.goReport') }}
        </el-button>
      </div>
    </div>

    <!-- ---------- 进度条 ---------- -->
    <el-card shadow="never" class="progress-card">
      <div class="progress-row">
        <el-progress
          :percentage="Math.min(100, Math.round(progress))"
          :status="progressStatus"
          :stroke-width="14"
          class="main-progress"
        />
        <div class="progress-meta">
          <span>{{ t('performanceTesting.monitor.elapsed') }}: <b>{{ formatDuration(elapsed) }}</b></span>
          <span>{{ t('performanceTesting.monitor.planned') }}: <b>{{ formatDuration(plannedDuration) }}</b></span>
          <span>{{ t('performanceTesting.metric.activeUsers') }}: <b>{{ latest.active_users ?? '-' }}</b></span>
        </div>
      </div>
      <el-alert
        v-if="execution.error_message"
        type="error"
        :closable="false"
        show-icon
        :title="execution.error_message"
        class="err-alert"
      />
      <el-alert
        v-else-if="abortNotice"
        type="warning"
        :closable="false"
        show-icon
        :title="abortNotice"
        class="err-alert"
      />
    </el-card>

    <!-- ---------- 指标卡 ---------- -->
    <MetricCards :items="metricItems" :cols="6" class="metric-row" />

    <!-- ---------- 实时曲线 ---------- -->
    <el-card shadow="never" class="chart-card">
      <template #header>
        <div class="card-head">
          <span>{{ t('performanceTesting.monitor.chartTitle') }}</span>
          <span class="card-tip">{{ t('performanceTesting.monitor.chartTip') }}</span>
        </div>
      </template>
      <RealtimeChart ref="chartRef" :height="320" />
    </el-card>

    <el-row :gutter="12">
      <!-- ---------- SLA 阈值实时对照 ---------- -->
      <el-col :span="10">
        <el-card shadow="never" class="sla-card">
          <template #header>{{ t('performanceTesting.sla.title') }}</template>
          <el-table v-if="slaRows.length" :data="slaRows" size="small">
            <el-table-column prop="label" :label="t('performanceTesting.metric.tps')" min-width="140">
              <template #header>{{ t('performanceTesting.sla.threshold') }}</template>
              <template #default="{ row }">{{ row.label }}</template>
            </el-table-column>
            <el-table-column :label="t('performanceTesting.sla.threshold')" width="90" align="right">
              <template #default="{ row }">{{ row.threshold }}</template>
            </el-table-column>
            <el-table-column :label="t('performanceTesting.sla.actual')" width="90" align="right">
              <template #default="{ row }">
                <span :class="{ 'bad-val': !row.passed }">{{ row.actual }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('performanceTesting.sla.result')" width="80" align="center">
              <template #default="{ row }">
                <el-icon :class="row.passed ? 'ok' : 'bad'">
                  <component :is="row.passed ? CircleCheck : CircleClose" />
                </el-icon>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-else :description="t('performanceTesting.sla.notEvaluatedTip')" :image-size="60" />
        </el-card>
      </el-col>

      <!-- ---------- 执行日志 ---------- -->
      <el-col :span="14">
        <el-card shadow="never" class="log-card">
          <template #header>
            <div class="card-head">
              <span>{{ t('performanceTesting.monitor.logTitle') }}</span>
              <el-button link :icon="Refresh" @click="fetchLog">
                {{ t('performanceTesting.common.refresh') }}
              </el-button>
            </div>
          </template>
          <pre v-if="runLog" ref="logRef" class="log-pre">{{ runLog }}</pre>
          <el-empty v-else :description="t('performanceTesting.monitor.logEmpty')" :image-size="60" />
        </el-card>
      </el-col>
    </el-row>

    <!-- ---------- 接口明细（结束后） ---------- -->
    <el-card v-if="!isActive && requestStats.length" shadow="never" class="stat-card">
      <template #header>{{ t('performanceTesting.monitor.stepDetailTitle') }}</template>
      <el-table :data="requestStats" size="small" border>
        <el-table-column prop="step_name" :label="t('performanceTesting.editor.stepName')" min-width="160" />
        <el-table-column prop="method" label="Method" width="80" />
        <el-table-column prop="total" :label="t('performanceTesting.metric.totalRequests')" width="100" align="right" />
        <el-table-column prop="failed" :label="t('performanceTesting.metric.failedRequests')" width="90" align="right" />
        <el-table-column :label="t('performanceTesting.metric.errorRate')" width="100" align="right">
          <template #default="{ row }">
            <span :class="{ 'bad-val': row.error_rate > 0 }">{{ row.error_rate }}%</span>
          </template>
        </el-table-column>
        <el-table-column prop="tps" :label="t('performanceTesting.metric.tps')" width="90" align="right" />
        <el-table-column prop="avg_rt" :label="t('performanceTesting.metric.avgRt')" width="110" align="right" />
        <el-table-column prop="p95_rt" :label="t('performanceTesting.metric.p95Rt')" width="110" align="right" />
        <el-table-column prop="max_rt" :label="t('performanceTesting.metric.maxRt')" width="110" align="right" />
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowLeft, VideoPause, Document, Refresh, CircleCheck, CircleClose
} from '@element-plus/icons-vue'

import MetricCards from './components/MetricCards.vue'
import RealtimeChart from './components/RealtimeChart.vue'
import { statusTagType, slaTagType, formatDuration, apiError } from './shared'
import {
  getPerfExecution, getPerfRealtime, stopPerfExecution, getEngineStatus,
  getPerfRunLog, getPerfRequestStats, getPerfScenario
} from '@/api/performance-testing'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()

const ACTIVE_STATUSES = ['PENDING', 'PREPARING', 'RUNNING', 'STOPPING']
const executionId = Number(route.params.id)

const loading = ref(false)
const execution = reactive({
  execution_no: '', scenario_name: '', status: '', sla_result: '',
  error_message: '', load_snapshot: {}, sla_detail: [], summary: {}, duration: 0
})
const progress = ref(0)
const latest = ref({})
const runLog = ref('')
const requestStats = ref([])
const abortNotice = ref('')
const slaThresholds = ref({})
const chartRef = ref(null)
const logRef = ref(null)

// 'ws' | 'polling' | 'closed'
const channel = ref('polling')
let socket = null
let pollTimer = null
let logTimer = null
let wsRetry = 0
let lastTs = -1
let finished = false
let wsGotMessage = false
let wsSilenceTimer = null

const isActive = computed(() => ACTIVE_STATUSES.includes(execution.status))
const plannedDuration = computed(() => {
  // build_snapshot 会把算好的总时长写进 _planned_duration，比自己按模型再算一遍可靠
  const snap = execution.load_snapshot || {}
  return Number(snap._planned_duration || snap.duration || 0)
})
const elapsed = computed(() => {
  if (execution.duration) return Math.round(execution.duration)
  return Math.round(latest.value.ts_offset || 0)
})
const progressStatus = computed(() => {
  if (execution.status === 'COMPLETED') return 'success'
  if (['FAILED', 'TIMEOUT'].includes(execution.status)) return 'exception'
  if (execution.status === 'STOPPED') return 'warning'
  return ''
})
const channelText = computed(() => {
  if (channel.value === 'ws') return t('performanceTesting.monitor.connWs')
  if (channel.value === 'polling') return t('performanceTesting.monitor.connPolling')
  return t('performanceTesting.monitor.connClosed')
})

function num(v, digits = 1) {
  return v == null || v === '' ? '-' : Number(v).toFixed(digits)
}

const metricItems = computed(() => {
  const s = latest.value || {}
  const sum = execution.summary || {}
  const errRate = s.error_rate ?? sum.error_rate
  return [
    { label: t('performanceTesting.metric.tps'), value: num(s.tps ?? sum.tps), status: 'normal' },
    { label: t('performanceTesting.metric.avgRt'), value: num(s.avg_rt ?? sum.avg_rt), unit: 'ms', status: 'normal' },
    { label: t('performanceTesting.metric.p95Rt'), value: num(s.p95_rt ?? sum.p95_rt), unit: 'ms', status: 'normal' },
    {
      label: t('performanceTesting.metric.errorRate'),
      value: num(errRate, 2),
      unit: '%',
      status: Number(errRate || 0) > 1 ? 'bad' : (Number(errRate || 0) > 0 ? 'warn' : 'good')
    },
    {
      label: t('performanceTesting.metric.totalRequests'),
      value: s.total_requests ?? sum.total_requests ?? '-',
      status: 'normal'
    },
    { label: t('performanceTesting.metric.cpu'), value: num(s.cpu_percent), unit: '%', status: 'normal' }
  ]
})

const slaRows = computed(() => {
  const detail = execution.sla_detail || []
  if (detail.length) {
    return detail.map(d => ({
      label: d.label, threshold: d.threshold, actual: d.actual, passed: d.passed
    }))
  }
  // 运行中还没有终局判定（sla_detail 在压测结束才回写），
  // 这里用场景阈值 + 当前采样做实时对照，让用户压到一半就能看出要不要停
  const thresholds = slaThresholds.value
  if (!thresholds || !Object.keys(thresholds).length) return []
  const map = {
    avg_response_time: ['avg_rt', 'max', t('performanceTesting.metric.avgRt')],
    p90_response_time: ['p90_rt', 'max', t('performanceTesting.metric.p90Rt')],
    p95_response_time: ['p95_rt', 'max', t('performanceTesting.metric.p95Rt')],
    p99_response_time: ['p99_rt', 'max', t('performanceTesting.metric.p99Rt')],
    error_rate: ['error_rate', 'max', t('performanceTesting.metric.errorRate')],
    min_tps: ['tps', 'min', t('performanceTesting.metric.tps')]
  }
  const s = latest.value || {}
  return Object.entries(thresholds).map(([key, threshold]) => {
    const meta = map[key]
    if (!meta) return null
    const [field, direction, label] = meta
    const actual = Number(s[field] ?? (execution.summary || {})[field] ?? 0)
    return {
      label,
      threshold,
      actual: actual.toFixed(2),
      passed: direction === 'max' ? actual <= Number(threshold) : actual >= Number(threshold)
    }
  }).filter(Boolean)
})

// ------------------------------------------------------------------ //
// 数据加载
// ------------------------------------------------------------------ //
function applyExecution(data) {
  Object.assign(execution, data)
  progress.value = data.progress || 0
  if (data.summary && Object.keys(data.summary).length) {
    latest.value = { ...latest.value, ...data.summary }
  }
}

async function fetchExecution() {
  const res = await getPerfExecution(executionId)
  applyExecution(res.data)
  return res.data
}

async function fetchSlaThresholds(scenarioId) {
  if (!scenarioId) return
  try {
    const { data: scenario } = await getPerfScenario(scenarioId)
    slaThresholds.value = (scenario.sla_config || {}).enabled
      ? (scenario.sla_config.thresholds || {})
      : {}
  } catch (e) {
    slaThresholds.value = {}
  }
}

async function fetchLog() {
  try {
    const res = await getPerfRunLog(executionId, { lines: 400 })
    runLog.value = res.data?.content || ''
    await nextTick()
    if (logRef.value) logRef.value.scrollTop = logRef.value.scrollHeight
  } catch (e) {
    /* 日志文件可能尚未创建，静默 */
  }
}

async function fetchRequestStats() {
  try {
    const res = await getPerfRequestStats(executionId)
    requestStats.value = res.data || []
  } catch (e) {
    requestStats.value = []
  }
}

function pushSample(sample) {
  if (!sample) return
  if (sample.ts_offset != null) lastTs = Math.max(lastTs, sample.ts_offset)
  latest.value = { ...latest.value, ...sample }
  chartRef.value?.push(sample)
  if (plannedDuration.value > 0 && sample.ts_offset != null) {
    progress.value = Math.min(100, (sample.ts_offset / plannedDuration.value) * 100)
  }
}

// ------------------------------------------------------------------ //
// WebSocket
// ------------------------------------------------------------------ //
function connectWs() {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const url = `${proto}://${window.location.host}/ws/perf-testing/executions/${executionId}/`
  try {
    socket = new WebSocket(url)
  } catch (e) {
    startPolling()
    return
  }

  // 看门狗：WS 若能连上但一直收不到任何数据（典型如 Redis/channels 实际不可用，
  // executor 推送被熔断，WS 显示"已连接"却不推送），则主动降级到轮询。
  function startWsWatchdog() {
    if (wsSilenceTimer) clearTimeout(wsSilenceTimer)
    wsSilenceTimer = setTimeout(() => {
      wsSilenceTimer = null
      if (finished || channel.value !== 'ws') return
      if (wsGotMessage) {
        // 最近有过数据，重置计时继续观察
        wsGotMessage = false
        startWsWatchdog()
      } else {
        // 6 秒一条都没收到 → 视为假连接，关掉让 onclose 走降级轮询
        try { socket && socket.close() } catch (e) { /* ignore */ }
      }
    }, 6000)
  }

  socket.onopen = () => {
    wsRetry = 0
    wsGotMessage = false
    channel.value = 'ws'
    stopPolling()
    startWsWatchdog()
  }

  socket.onmessage = (event) => {
    wsGotMessage = true
    startWsWatchdog()
    let data
    try {
      data = JSON.parse(event.data)
    } catch (e) {
      return
    }
    if (data.status) execution.status = data.status
    if (data.progress != null) progress.value = data.progress
    if (data.sla_result) execution.sla_result = data.sla_result
    if (data.summary && Object.keys(data.summary).length) {
      execution.summary = data.summary
      latest.value = { ...latest.value, ...data.summary }
    }
    if (data.sample) pushSample(data.sample)
    if (data.message && data.status === 'STOPPING') abortNotice.value = data.message
    if (!ACTIVE_STATUSES.includes(data.status || execution.status)) onFinished()
  }

  socket.onerror = () => {
    // 出错时不立刻放弃：先重试两次，再降级轮询
    if (socket) {
      try { socket.close() } catch (e) { /* ignore */ }
    }
  }

  socket.onclose = () => {
    if (wsSilenceTimer) { clearTimeout(wsSilenceTimer); wsSilenceTimer = null }
    socket = null
    if (finished) {
      channel.value = 'closed'
      return
    }
    wsRetry += 1
    if (wsRetry <= 2) {
      setTimeout(() => { if (!finished) connectWs() }, wsRetry * 1000)
    } else {
      startPolling()
    }
  }
}

// ------------------------------------------------------------------ //
// 轮询降级
// ------------------------------------------------------------------ //
function startPolling() {
  if (pollTimer || finished) return
  channel.value = 'polling'
  pollOnce()
  pollTimer = setInterval(pollOnce, 2000)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function pollOnce() {
  try {
    const { data } = await getPerfRealtime(executionId, lastTs)
    execution.status = data.status
    progress.value = data.progress || progress.value
    execution.sla_result = data.sla_result
    execution.error_message = data.error_message || ''
    if (data.summary && Object.keys(data.summary).length) {
      execution.summary = data.summary
      latest.value = { ...latest.value, ...data.summary }
    }
    ;(data.samples || []).forEach(pushSample)
    if (!ACTIVE_STATUSES.includes(data.status)) onFinished()
  } catch (e) {
    /* 网络抖动不打断轮询 */
  }
}

// ------------------------------------------------------------------ //
// 收尾
// ------------------------------------------------------------------ //
async function onFinished() {
  if (finished) return
  finished = true
  stopPolling()
  if (logTimer) { clearInterval(logTimer); logTimer = null }
  if (socket) {
    try { socket.close() } catch (e) { /* ignore */ }
    socket = null
  }
  channel.value = 'closed'
  await fetchExecution()
  await Promise.all([fetchLog(), fetchRequestStats()])
  if (execution.sla_result === 'FAILED') {
    ElMessage.warning(t('performanceTesting.monitor.finished'))
  } else {
    ElMessage.success(t('performanceTesting.monitor.finished'))
  }
}

async function handleStop() {
  try {
    await ElMessageBox.confirm(
      t('performanceTesting.execution.stopConfirm'),
      t('performanceTesting.execution.stop'),
      { type: 'warning' }
    )
  } catch (e) {
    return
  }
  try {
    await stopPerfExecution(executionId)
    ElMessage.success(t('performanceTesting.execution.stopped'))
  } catch (e) {
    ElMessage.error(apiError(e, t('performanceTesting.execution.stop')))
  }
}

function goBack() {
  router.push('/performance-testing/executions')
}
function goReport() {
  router.push(`/performance-testing/executions/${executionId}`)
}

// ------------------------------------------------------------------ //
// 生命周期
// ------------------------------------------------------------------ //
onMounted(async () => {
  loading.value = true
  try {
    const data = await fetchExecution()
    if (!ACTIVE_STATUSES.includes(data.status)) {
      // 已结束：只做一次历史回放，不建实时通道
      finished = true
      channel.value = 'closed'
      const { data: replay } = await getPerfRealtime(executionId, -1)
      ;(replay.samples || []).forEach(pushSample)
      progress.value = 100
      await Promise.all([fetchLog(), fetchRequestStats()])
      return
    }
    // 先把已有采样点补齐，避免中途进来只看到后半段曲线
    const { data: history } = await getPerfRealtime(executionId, -1)
    ;(history.samples || []).forEach(pushSample)
    fetchSlaThresholds(data.scenario)

    let wsOk = false
    try {
      const { data: status } = await getEngineStatus()
      wsOk = !!status.websocket
    } catch (e) {
      wsOk = false
    }
    if (wsOk) connectWs()
    else startPolling()

    fetchLog()
    logTimer = setInterval(fetchLog, 8000)
  } finally {
    loading.value = false
  }
})

onBeforeUnmount(() => {
  finished = true
  stopPolling()
  if (logTimer) clearInterval(logTimer)
  if (socket) {
    try { socket.close() } catch (e) { /* ignore */ }
    socket = null
  }
})
</script>

<style lang="scss" scoped>
.perf-monitor { padding: 16px; }

.monitor-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  margin-bottom: 12px;
  background: #fff;
  border-radius: 6px;

  .head-left { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .head-right { display: flex; align-items: center; gap: 12px; }
  .exec-no { font-weight: 600; color: #303133; font-family: Menlo, Consolas, monospace; }
  .scenario-name { color: #909399; font-size: 13px; }
}

.conn-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #909399;
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #c0c4cc; }
  &.ws .dot { background: #52c41a; box-shadow: 0 0 0 3px rgba(82, 196, 26, 0.15); }
  &.polling .dot { background: #faad14; box-shadow: 0 0 0 3px rgba(250, 173, 20, 0.15); }
  &.closed .dot { background: #c0c4cc; }
}

.progress-card {
  margin-bottom: 12px;
  :deep(.el-card__body) { padding: 14px; }
  .progress-row { display: flex; align-items: center; gap: 20px; }
  .main-progress { flex: 1; }
  .progress-meta {
    display: flex;
    gap: 18px;
    font-size: 13px;
    color: #909399;
    white-space: nowrap;
    b { color: #303133; }
  }
  .err-alert { margin-top: 10px; }
}

.metric-row { margin-bottom: 12px; }
.chart-card { margin-bottom: 12px; }
.card-head { display: flex; align-items: center; justify-content: space-between; }
.card-tip { font-size: 12px; color: #c0c4cc; font-weight: 400; }

.sla-card, .log-card { height: 320px; :deep(.el-card__body) { padding: 8px 12px; overflow: auto; height: 250px; } }
.ok { color: #52c41a; }
.bad { color: #f5222d; }
.bad-val { color: #f5222d; font-weight: 600; }

.log-pre {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: #606266;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 240px;
  overflow: auto;
}

.stat-card { margin-top: 12px; }
</style>
