<template>
  <div class="perf-report" v-loading="loading">
    <!-- ---------- 顶栏 ---------- -->
    <div class="report-head">
      <div class="head-left">
        <el-button link :icon="ArrowLeft" @click="goBack">{{ t('performanceTesting.common.back') }}</el-button>
        <el-divider direction="vertical" />
        <span class="exec-no">{{ execution.execution_no }}</span>
        <span class="scenario-name">{{ execution.scenario_name }}</span>
        <el-tag :type="statusTagType(execution.status)" size="small">
          {{ execution.status ? t('performanceTesting.status.' + execution.status) : '-' }}
        </el-tag>
        <el-tag :type="slaTagType(execution.sla_result)" size="small" effect="plain">
          SLA {{ execution.sla_result ? t('performanceTesting.sla.' + execution.sla_result) : '-' }}
        </el-tag>
        <el-tag
          v-if="execution.verdict && execution.verdict !== 'NOT_EVALUATED'"
          :type="execution.verdict === 'PASSED' ? 'success' : 'danger'"
          size="small"
        >
          {{ execution.verdict === 'PASSED' ? '验收通过' : '验收未通过' }}
        </el-tag>
      </div>
      <div class="head-right">
        <el-button :icon="Refresh" :loading="regenerating" @click="handleRegenerate">
          {{ t('performanceTesting.report.regenerate') }}
        </el-button>
        <el-button :icon="Link" :disabled="!execution.report_url" @click="openHtml">
          {{ t('performanceTesting.report.exportHtml') }}
        </el-button>
        <el-button :icon="Download" :disabled="!execution.has_raw_detail" @click="downloadRaw">
          {{ t('performanceTesting.report.downloadRaw') }}
        </el-button>
        <el-button :icon="Share" :disabled="!execution.report_url" @click="openShare">
          {{ t('performanceTesting.share.button') }}
        </el-button>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="report-tabs" @tab-change="onTabChange">
      <!-- ================= 概览 ================= -->
      <el-tab-pane :label="t('performanceTesting.report.tabOverview')" name="overview">
        <MetricCards :items="overviewCards" :cols="6" class="metric-row" />

        <el-row :gutter="12">
          <el-col :span="12">
            <el-card shadow="never">
              <template #header>{{ t('performanceTesting.sla.title') }}</template>
              <el-table v-if="(execution.sla_detail || []).length" :data="execution.sla_detail" size="small">
                <el-table-column prop="label" :label="t('performanceTesting.sla.result')" min-width="150" />
                <el-table-column prop="threshold" :label="t('performanceTesting.sla.threshold')" width="100" align="right" />
                <el-table-column :label="t('performanceTesting.sla.actual')" width="100" align="right">
                  <template #default="{ row }">
                    <span :class="{ 'bad-val': !row.passed }">{{ row.actual }}</span>
                  </template>
                </el-table-column>
                <el-table-column width="70" align="center">
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

          <el-col :span="12">
            <el-card shadow="never">
              <template #header>{{ t('performanceTesting.report.baselineCompare') }}</template>
              <template v-if="baseline && baseline.has_baseline">
                <div class="baseline-meta">
                  {{ t('performanceTesting.comparison.baseline') }}: {{ baseline.baseline_execution_no }}
                  <el-tag
                    :type="baseline.degraded ? 'danger' : 'success'"
                    size="small"
                    effect="plain"
                  >
                    {{ baseline.degraded
                      ? t('performanceTesting.report.worse')
                      : t('performanceTesting.report.better') }}
                  </el-tag>
                </div>
                <el-table :data="baseline.items" size="small">
                  <el-table-column prop="label" :label="t('performanceTesting.sla.result')" min-width="130" />
                  <el-table-column prop="baseline" :label="t('performanceTesting.comparison.baseline')" width="90" align="right" />
                  <el-table-column prop="current" :label="t('performanceTesting.sla.actual')" width="90" align="right" />
                  <el-table-column :label="t('performanceTesting.comparison.diff')" width="110" align="right">
                    <template #default="{ row }">
                      <span :class="row.degraded ? 'bad-val' : 'good-val'">
                        {{ row.change_pct > 0 ? '+' : '' }}{{ row.change_pct }}%
                      </span>
                    </template>
                  </el-table-column>
                </el-table>
              </template>
              <el-empty v-else :description="t('performanceTesting.report.noBaseline')" :image-size="60" />
            </el-card>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- ================= 时序图 ================= -->
      <el-tab-pane :label="t('performanceTesting.report.tabTimeline')" name="timeline">
        <el-card shadow="never" class="chart-card">
          <template #header>{{ t('performanceTesting.report.chartTps') }} / {{ t('performanceTesting.report.chartUsers') }}</template>
          <div ref="tpsChartRef" class="chart-box" />
        </el-card>
        <el-card shadow="never" class="chart-card">
          <template #header>{{ t('performanceTesting.report.chartRt') }}</template>
          <div ref="rtChartRef" class="chart-box" />
        </el-card>
        <el-card shadow="never" class="chart-card">
          <template #header>{{ t('performanceTesting.report.chartError') }}</template>
          <div ref="errChartRef" class="chart-box chart-sm" />
        </el-card>
      </el-tab-pane>

      <!-- ================= 接口明细 ================= -->
      <el-tab-pane :label="t('performanceTesting.report.tabRequests')" name="requests">
        <el-card shadow="never">
          <el-table :data="requestStats" size="small" border>
            <el-table-column prop="step_name" :label="t('performanceTesting.editor.stepName')" min-width="180" fixed />
            <el-table-column prop="method" label="Method" width="80" />
            <el-table-column prop="total" :label="t('performanceTesting.metric.totalRequests')" width="100" align="right" />
            <el-table-column prop="success" :label="t('performanceTesting.metric.successRequests')" width="90" align="right" />
            <el-table-column prop="failed" :label="t('performanceTesting.metric.failedRequests')" width="90" align="right" />
            <el-table-column :label="t('performanceTesting.metric.errorRate')" width="100" align="right">
              <template #default="{ row }">
                <span :class="{ 'bad-val': row.error_rate > 0 }">{{ row.error_rate }}%</span>
              </template>
            </el-table-column>
            <el-table-column prop="tps" :label="t('performanceTesting.metric.tps')" width="90" align="right" />
            <el-table-column prop="avg_rt" :label="t('performanceTesting.metric.avgRt')" width="100" align="right" />
            <el-table-column prop="min_rt" :label="t('performanceTesting.metric.minRt')" width="100" align="right" />
            <el-table-column prop="p90_rt" :label="t('performanceTesting.metric.p90Rt')" width="90" align="right" />
            <el-table-column prop="p95_rt" :label="t('performanceTesting.metric.p95Rt')" width="100" align="right" />
            <el-table-column prop="p99_rt" :label="t('performanceTesting.metric.p99Rt')" width="90" align="right" />
            <el-table-column prop="max_rt" :label="t('performanceTesting.metric.maxRt')" width="100" align="right" />
            <el-table-column :label="t('performanceTesting.metric.sentBytes')" width="100" align="right">
              <template #default="{ row }">{{ formatBytes(row.sent_bytes) }}</template>
            </el-table-column>
            <el-table-column :label="t('performanceTesting.metric.recvBytes')" width="100" align="right">
              <template #default="{ row }">{{ formatBytes(row.recv_bytes) }}</template>
            </el-table-column>
            <template #empty>
              <el-empty :description="t('performanceTesting.common.empty')" />
            </template>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ================= 错误分析 ================= -->
      <el-tab-pane :label="t('performanceTesting.report.tabErrors')" name="errors">
        <el-card shadow="never">
          <template #header>{{ t('performanceTesting.report.errorTop') }}</template>
          <el-table v-if="errorRows.length" :data="errorRows" size="small" border>
            <el-table-column prop="step_name" :label="t('performanceTesting.report.errorStep')" width="180" />
            <el-table-column prop="type" :label="t('performanceTesting.report.errorType')" width="180" />
            <el-table-column prop="message" :label="t('performanceTesting.report.errorMessage')" min-width="320" show-overflow-tooltip />
            <el-table-column prop="count" :label="t('performanceTesting.report.errorCount')" width="110" align="right" />
          </el-table>
          <el-empty v-else :description="t('performanceTesting.report.noErrors')" :image-size="80" />
        </el-card>
      </el-tab-pane>

      <!-- ================= 执行配置 ================= -->
      <el-tab-pane :label="t('performanceTesting.report.tabConfig')" name="config">
        <el-alert
          type="info"
          :closable="false"
          show-icon
          :title="t('performanceTesting.report.snapshotTip')"
          class="snapshot-tip"
        />
        <el-row :gutter="12">
          <el-col :span="10">
            <el-card shadow="never">
              <template #header>{{ t('performanceTesting.report.loadSnapshot') }}</template>
              <el-descriptions :column="1" border size="small">
                <el-descriptions-item :label="t('performanceTesting.loadModel.label')">
                  {{ loadModelLabel }}
                </el-descriptions-item>
                <el-descriptions-item
                  v-for="(v, k) in displayLoadSnapshot"
                  :key="k"
                  :label="k"
                >
                  {{ typeof v === 'object' ? JSON.stringify(v) : v }}
                </el-descriptions-item>
              </el-descriptions>
              <el-descriptions :column="1" border size="small" class="mt12">
                <el-descriptions-item :label="t('performanceTesting.execution.executedBy')">
                  {{ execution.executed_by?.username || '-' }}
                </el-descriptions-item>
                <el-descriptions-item :label="t('performanceTesting.execution.triggerType')">
                  {{ execution.trigger_type ? t('performanceTesting.trigger.' + execution.trigger_type) : '-' }}
                </el-descriptions-item>
                <el-descriptions-item :label="t('performanceTesting.execution.startTime')">
                  {{ formatTime(execution.start_time) }}
                </el-descriptions-item>
                <el-descriptions-item :label="t('performanceTesting.execution.endTime')">
                  {{ formatTime(execution.end_time) }}
                </el-descriptions-item>
                <el-descriptions-item :label="t('performanceTesting.metric.duration')">
                  {{ execution.duration ? formatDuration(execution.duration) : '-' }}
                </el-descriptions-item>
                <el-descriptions-item label="Worker">{{ execution.worker_host || '-' }}</el-descriptions-item>
              </el-descriptions>
            </el-card>
          </el-col>
          <el-col :span="14">
            <el-card shadow="never">
              <template #header>{{ t('performanceTesting.report.stepsSnapshot') }}</template>
              <el-table :data="execution.steps_snapshot || []" size="small" border>
                <el-table-column type="index" width="50" />
                <el-table-column prop="name" :label="t('performanceTesting.editor.stepName')" min-width="150" />
                <el-table-column prop="method" label="Method" width="80" />
                <el-table-column prop="url" :label="t('performanceTesting.editor.url')" min-width="220" show-overflow-tooltip />
                <el-table-column :label="t('performanceTesting.editor.stepSetup')" width="80" align="center">
                  <template #default="{ row }">
                    <el-tag v-if="row.is_setup" size="small" type="warning" effect="plain">
                      {{ t('performanceTesting.common.yes') }}
                    </el-tag>
                    <span v-else>-</span>
                  </template>
                </el-table-column>
                <el-table-column :label="t('performanceTesting.editor.stepEnabled')" width="80" align="center">
                  <template #default="{ row }">
                    {{ row.enabled ? t('performanceTesting.common.yes') : t('performanceTesting.common.no') }}
                  </template>
                </el-table-column>
              </el-table>
            </el-card>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- AI 分析 Tab（懒加载，点击才请求） -->
      <el-tab-pane :label="t('performanceTesting.report.tabAiAnalysis')" name="ai-analysis">
        <div class="ai-panel">
          <!-- 未开始：引导卡片 -->
          <div v-if="!aiAnalysis.loaded && !aiAnalysis.loading && !aiAnalysis.text && !aiAnalysis.error" class="ai-hero">
            <div class="ai-hero-icon">
              <el-icon :size="30"><MagicStick /></el-icon>
            </div>
            <h3 class="ai-hero-title">{{ t('performanceTesting.report.tabAiAnalysis') }}</h3>
            <p class="ai-hero-desc">{{ t('performanceTesting.report.aiDesc') }}</p>
            <div class="ai-hero-features">
              <span class="ai-feature">{{ t('performanceTesting.report.aiFeatureBottleneck') }}</span>
              <span class="ai-feature">{{ t('performanceTesting.report.aiFeatureSuggestion') }}</span>
              <span class="ai-feature">{{ t('performanceTesting.report.aiFeatureRisk') }}</span>
            </div>
            <el-button type="primary" round size="large" class="ai-start-btn" @click="loadAiAnalysis">
              <el-icon><MagicStick /></el-icon>
              {{ t('performanceTesting.report.aiStart') }}
            </el-button>
          </div>

          <!-- 分析中 -->
          <div v-if="aiAnalysis.loading && !aiAnalysis.text" class="ai-streaming-hint">
            <el-icon class="is-loading"><Loading /></el-icon>
            <span>{{ t('performanceTesting.report.aiStreaming') }}</span>
            <span class="ai-dots"><i>.</i><i>.</i><i>.</i></span>
          </div>

          <!-- 分析内容（Markdown 渲染） -->
          <div v-if="aiAnalysis.text" class="ai-card">
            <div class="ai-card-head">
              <div class="ai-card-title">
                <span class="ai-badge">
                  <el-icon><MagicStick /></el-icon>
                  {{ t('performanceTesting.report.aiResultTitle') }}
                </span>
                <span v-if="aiAnalysis.loading" class="ai-streaming-tag">
                  <el-icon class="is-loading"><Loading /></el-icon>
                  {{ t('performanceTesting.report.aiGenerating') }}
                </span>
              </div>
              <div class="ai-card-actions">
                <el-button link size="small" :icon="DocumentCopy" :disabled="aiAnalysis.loading" @click="copyAiText">
                  {{ t('performanceTesting.report.aiCopy') }}
                </el-button>
                <el-button link size="small" type="primary" :icon="Refresh" :disabled="aiAnalysis.loading" @click="loadAiAnalysis">
                  {{ t('performanceTesting.report.aiReanalyze') }}
                </el-button>
              </div>
            </div>
            <div class="ai-card-body markdown-body" v-html="aiRenderedHtml"></div>
          </div>

          <div v-if="aiAnalysis.error" class="ai-error">
            <el-alert :title="aiAnalysis.error" type="error" show-icon :closable="false" />
            <el-button class="ai-retry-btn" type="primary" plain size="small" @click="loadAiAnalysis">
              {{ t('performanceTesting.common.retry') }}
            </el-button>
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- ---------- 分享直链对话框 ---------- -->
    <el-dialog
      v-model="shareVisible"
      :title="t('performanceTesting.share.title')"
      width="560px"
      append-to-body
      @closed="onShareClosed"
    >
      <el-alert
        :title="t('performanceTesting.share.tip')"
        type="warning"
        :closable="false"
        show-icon
        class="share-tip"
      />
      <el-form label-width="96px" class="share-form">
        <el-form-item :label="t('performanceTesting.share.expiry')">
          <el-radio-group v-model="shareExpiry">
            <el-radio-button :value="0">{{ t('performanceTesting.share.never') }}</el-radio-button>
            <el-radio-button :value="1">{{ t('performanceTesting.share.day1') }}</el-radio-button>
            <el-radio-button :value="7">{{ t('performanceTesting.share.day7') }}</el-radio-button>
            <el-radio-button :value="30">{{ t('performanceTesting.share.day30') }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
      </el-form>

      <template v-if="shareUrl">
        <div class="share-link-block">
          <div class="share-label">{{ t('performanceTesting.share.reportLink') }}</div>
          <el-input :model-value="shareUrl" readonly>
            <template #append>
              <el-button :icon="DocumentCopy" @click="copyText(shareUrl)">
                {{ t('performanceTesting.share.copy') }}
              </el-button>
            </template>
          </el-input>
          <div class="share-label">{{ t('performanceTesting.share.rawLink') }}</div>
          <el-input :model-value="shareRawUrl" readonly>
            <template #append>
              <el-button :icon="DocumentCopy" @click="copyText(shareRawUrl)">
                {{ t('performanceTesting.share.copy') }}
              </el-button>
            </template>
          </el-input>
          <div v-if="shareExpiresAt" class="share-exp">
            {{ t('performanceTesting.share.expiresAt') }}: {{ formatTime(shareExpiresAt) }}
          </div>
        </div>
        <div class="share-actions">
          <el-button type="danger" plain :icon="CircleClose" @click="handleRevoke">
            {{ t('performanceTesting.share.revoke') }}
          </el-button>
        </div>
      </template>
      <template v-else>
        <div class="share-actions">
          <el-button type="primary" :loading="shareGenerating" :icon="Share" @click="handleGenerate">
            {{ t('performanceTesting.share.generate') }}
          </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { ArrowLeft, Refresh, Link, Download, Share, DocumentCopy, CircleCheck, CircleClose, MagicStick, Loading } from '@element-plus/icons-vue'
import * as echarts from 'echarts'
import { marked } from 'marked'

import MetricCards from './components/MetricCards.vue'
import { statusTagType, slaTagType, formatTime, formatDuration, apiError } from './shared'
import request from '@/utils/api'
import { useUserStore } from '@/stores/user'
import {
  getPerfExecution, getPerfSamples, getPerfRequestStats, generatePerfReport,
  compareWithBaseline, generatePerfShareLink, revokePerfShareLink
} from '@/api/performance-testing'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()

const executionId = Number(route.params.id)
const loading = ref(false)
const regenerating = ref(false)
const activeTab = ref('overview')

const execution = reactive({
  execution_no: '', scenario_name: '', status: '', sla_result: '', summary: {},
  sla_detail: [], load_snapshot: {}, steps_snapshot: [], report_url: '',
  has_raw_detail: false, executed_by: null,
  verdict: 'NOT_EVALUATED', verdict_details: []
})

// AI 分析状态（懒加载，点击 Tab 才请求）
const aiAnalysis = reactive({
  loaded: false,
  loading: false,
  text: '',
  error: ''
})
const aiAbortCtrl = ref(null)
let aiIdleTimer = null

// AI 分析输出为 markdown（marked 默认转义原始 HTML，防 XSS）；
// 流式输出时对未闭合语法标记做临时清理，避免打字机过程中出现裸符号
const aiRenderedHtml = computed(() => {
  const text = aiAnalysis.text || ''
  if (!text) return ''
  let src = text
  if (aiAnalysis.loading) {
    const boldCount = (src.match(/\*\*/g) || []).length
    if (boldCount % 2 === 1) src += '**'
    if ((src.match(/^>/gm) || []).length && !/\n\s*$/.test(src)) src += '\n'
  }
  let html = marked.parse(src)
  if (aiAnalysis.loading) {
    html += '<span class="ai-streaming-cursor"></span>'
  }
  return html
})

function copyAiText() {
  navigator.clipboard?.writeText(aiAnalysis.text).then(
    () => ElMessage.success(t('performanceTesting.share.copied')),
    () => ElMessage.warning(t('performanceTesting.share.copyFailed'))
  )
}
const samples = ref([])
const requestStats = ref([])
const baseline = ref(null)

const tpsChartRef = ref(null)
const rtChartRef = ref(null)
const errChartRef = ref(null)
let tpsChart = null
let rtChart = null
let errChart = null

function num(v, digits = 1) {
  return v == null || v === '' ? '-' : Number(v).toFixed(digits)
}

function formatBytes(bytes) {
  const b = Number(bytes) || 0
  if (b < 1024) return `${b} B`
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`
  if (b < 1024 * 1024 * 1024) return `${(b / 1024 / 1024).toFixed(1)} MB`
  return `${(b / 1024 / 1024 / 1024).toFixed(2)} GB`
}

const overviewCards = computed(() => {
  const s = execution.summary || {}
  const errRate = Number(s.error_rate || 0)
  return [
    { label: t('performanceTesting.metric.totalRequests'), value: s.total_requests ?? '-', status: 'normal' },
    { label: t('performanceTesting.metric.tps'), value: num(s.tps), status: 'normal' },
    { label: t('performanceTesting.metric.peakTps'), value: num(s.peak_tps), status: 'normal' },
    { label: t('performanceTesting.metric.avgRt'), value: num(s.avg_rt), unit: 'ms', status: 'normal' },
    { label: t('performanceTesting.metric.p95Rt'), value: num(s.p95_rt), unit: 'ms', status: 'normal' },
    {
      label: t('performanceTesting.metric.errorRate'),
      value: num(errRate, 2),
      unit: '%',
      status: errRate > 1 ? 'bad' : (errRate > 0 ? 'warn' : 'good')
    }
  ]
})

const loadModelLabel = computed(() => {
  const model = (execution.load_snapshot || {}).model
  return model ? t('performanceTesting.loadModel.' + model) : '-'
})

const displayLoadSnapshot = computed(() => {
  const snap = { ...(execution.load_snapshot || {}) }
  delete snap.model
  // 内部字段不展示给用户
  delete snap._planned_duration
  Object.keys(snap).forEach(k => {
    if (snap[k] === 0 || snap[k] === '' || snap[k] == null) delete snap[k]
  })
  return snap
})

// 错误明细：从各步骤的 error_detail 聚合，按出现次数倒序取 TOP10
const errorRows = computed(() => {
  const rows = []
  requestStats.value.forEach(stat => {
    ;(stat.error_detail || []).forEach(item => {
      rows.push({
        step_name: stat.step_name,
        type: item.type || item.error_type || 'Unknown',
        message: item.message || item.error || '',
        count: item.count || 1
      })
    })
  })
  return rows.sort((a, b) => b.count - a.count).slice(0, 10)
})

// ------------------------------------------------------------------ //
// 图表
// ------------------------------------------------------------------ //
const AXIS_BASE = {
  grid: { left: 50, right: 50, top: 40, bottom: 40 },
  tooltip: { trigger: 'axis' },
  legend: { top: 6 }
}

function xAxisData() {
  return samples.value.map(s => `${Math.round(s.ts_offset)}s`)
}

function renderCharts() {
  const x = xAxisData()

  if (tpsChartRef.value) {
    tpsChart = tpsChart || echarts.init(tpsChartRef.value)
    tpsChart.setOption({
      ...AXIS_BASE,
      xAxis: { type: 'category', data: x, boundaryGap: false },
      yAxis: [
        { type: 'value', name: 'TPS' },
        { type: 'value', name: t('performanceTesting.metric.activeUsers') }
      ],
      series: [
        {
          name: 'TPS',
          type: 'line',
          smooth: true,
          showSymbol: false,
          data: samples.value.map(s => s.tps),
          itemStyle: { color: '#1890ff' },
          areaStyle: { opacity: 0.12 }
        },
        {
          name: t('performanceTesting.metric.activeUsers'),
          type: 'line',
          yAxisIndex: 1,
          step: 'end',
          showSymbol: false,
          data: samples.value.map(s => s.active_users),
          itemStyle: { color: '#722ed1' },
          lineStyle: { type: 'dashed' }
        }
      ]
    })
  }

  if (rtChartRef.value) {
    rtChart = rtChart || echarts.init(rtChartRef.value)
    rtChart.setOption({
      ...AXIS_BASE,
      xAxis: { type: 'category', data: x, boundaryGap: false },
      yAxis: { type: 'value', name: 'ms' },
      series: [
        { name: 'avg', type: 'line', smooth: true, showSymbol: false, data: samples.value.map(s => s.avg_rt), itemStyle: { color: '#52c41a' } },
        { name: 'P90', type: 'line', smooth: true, showSymbol: false, data: samples.value.map(s => s.p90_rt), itemStyle: { color: '#faad14' } },
        { name: 'P95', type: 'line', smooth: true, showSymbol: false, data: samples.value.map(s => s.p95_rt), itemStyle: { color: '#fa8c16' } },
        { name: 'P99', type: 'line', smooth: true, showSymbol: false, data: samples.value.map(s => s.p99_rt), itemStyle: { color: '#f5222d' } }
      ]
    })
  }

  if (errChartRef.value) {
    errChart = errChart || echarts.init(errChartRef.value)
    errChart.setOption({
      ...AXIS_BASE,
      legend: { show: false },
      grid: { left: 50, right: 50, top: 20, bottom: 40 },
      xAxis: { type: 'category', data: x, boundaryGap: false },
      yAxis: { type: 'value', name: '%', max: (v) => Math.max(1, Math.ceil(v.max)) },
      series: [{
        name: t('performanceTesting.metric.errorRate'),
        type: 'line',
        step: 'end',
        showSymbol: false,
        data: samples.value.map(s => s.error_rate),
        itemStyle: { color: '#f5222d' },
        areaStyle: { opacity: 0.15 }
      }]
    })
  }
}

function resizeCharts() {
  tpsChart?.resize()
  rtChart?.resize()
  errChart?.resize()
}

async function onTabChange(name) {
  if (name === 'timeline') {
    await nextTick()
    renderCharts()
    resizeCharts()
  }
  if (name === 'ai-analysis' && !aiAnalysis.loaded && !aiAnalysis.loading) {
    loadAiAnalysis()
  }
}

// AI 分析：fetch + ReadableStream 消费 SSE（可携带 JWT 认证头），打字机效果
async function loadAiAnalysis() {
  aiAnalysis.loading = true
  aiAnalysis.text = ''
  aiAnalysis.error = ''

  const userStore = useUserStore()
  const ctrl = new AbortController()
  aiAbortCtrl.value = ctrl

  // 空闲超时：每收到一个数据块重置，流式输出期间不会误杀
  const resetIdleTimer = () => {
    clearTimeout(aiIdleTimer)
    aiIdleTimer = setTimeout(() => {
      if (aiAnalysis.loading) {
        aiAnalysis.error = t('performanceTesting.report.aiTimeout')
        aiAnalysis.loading = false
        ctrl.abort()
      }
    }, 60000)
  }
  const finish = () => {
    clearTimeout(aiIdleTimer)
    aiAnalysis.loaded = true
    aiAnalysis.loading = false
  }
  const fail = (msg) => {
    clearTimeout(aiIdleTimer)
    if (aiAnalysis.loading) {
      aiAnalysis.error = msg || t('performanceTesting.report.aiFailed')
      aiAnalysis.loading = false
    }
  }

  resetIdleTimer()
  try {
    const resp = await fetch(`/api/perf-testing/executions/${executionId}/ai-analysis/`, {
      method: 'GET',
      signal: ctrl.signal,
      credentials: 'include',
      headers: {
        Accept: 'text/event-stream, application/json',
        ...(userStore.accessToken ? { Authorization: `Bearer ${userStore.accessToken}` } : {})
      }
    })

    if (!resp.ok) {
      let detail = ''
      try {
        const body = await resp.json()
        detail = body.detail || body.error || ''
      } catch (e) { /* 响应体非 JSON，忽略 */ }
      if (resp.status === 401) fail(t('performanceTesting.report.aiAuthExpired'))
      else if (resp.status === 403) fail(t('performanceTesting.report.aiForbidden'))
      else fail(detail || t('performanceTesting.report.aiHttpFailed', { status: resp.status }))
      return
    }

    const contentType = resp.headers.get('Content-Type') || ''
    // Redis 缓存命中 → 一次性 JSON 返回（< 100ms）
    if (contentType.includes('application/json')) {
      const data = await resp.json()
      if (data.error) {
        fail(data.error)
      } else {
        aiAnalysis.text = data.analysis || ''
        finish()
      }
      return
    }

    // SSE 流式：按 "\n\n" 切事件，逐行解析 data: 前缀
    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      resetIdleTimer()
      buffer += decoder.decode(value, { stream: true })

      let sep
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const rawEvent = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        for (const line of rawEvent.split('\n')) {
          if (!line.startsWith('data: ')) continue
          try {
            const data = JSON.parse(line.slice(6))
            if (data.chunk) aiAnalysis.text += data.chunk
            if (data.error) { fail(data.error); return }
            if (data.done) { finish(); return }
          } catch (e) { /* 单行解析失败忽略，等待后续数据 */ }
        }
      }
    }
    // 流正常结束但未收到 done 事件（兜底视为完成）
    if (aiAnalysis.loading) finish()
  } catch (e) {
    if (e.name !== 'AbortError') fail()
  }
}

// ------------------------------------------------------------------ //
// 数据加载
// ------------------------------------------------------------------ //
async function loadAll() {
  loading.value = true
  try {
    const [detailRes, sampleRes, statsRes] = await Promise.all([
      getPerfExecution(executionId),
      getPerfSamples(executionId, 1000).catch(() => ({ data: { samples: [] } })),
      getPerfRequestStats(executionId).catch(() => ({ data: [] }))
    ])
    Object.assign(execution, detailRes.data)
    samples.value = sampleRes.data?.samples || []
    requestStats.value = statsRes.data || []
    try {
      const cmp = await compareWithBaseline({ execution_id: executionId })
      baseline.value = cmp.data
    } catch (e) {
      baseline.value = { has_baseline: false }
    }
    if (!execution.report_url && !['PENDING', 'PREPARING', 'RUNNING', 'STOPPING'].includes(execution.status)) {
      ElMessage.info(t('performanceTesting.report.notReady'))
    }
  } catch (e) {
    ElMessage.error(apiError(e, t('performanceTesting.common.empty')))
  } finally {
    loading.value = false
  }
}

async function handleRegenerate() {
  regenerating.value = true
  try {
    const res = await generatePerfReport(executionId)
    execution.report_url = res.data?.report_url
    ElMessage.success(t('performanceTesting.report.regenerated'))
  } catch (e) {
    ElMessage.error(apiError(e, t('performanceTesting.report.regenerate')))
  } finally {
    regenerating.value = false
  }
}

// 报告接口需鉴权，不能用 window.open 直链（浏览器新窗口不带 token，会 401）。
// 改为用带 token 的 axios 拉取：HTML 注入新窗口，原始数据用 blob 触发下载。
function openHtml() {
  const w = window.open('', '_blank')
  if (!w) {
    ElMessage.error(t('performanceTesting.report.popupBlocked'))
    return
  }
  w.document.title = t('performanceTesting.report.title')
  request({ url: `/perf-testing/executions/${executionId}/report/`, responseType: 'text' })
    .then(res => {
      w.document.open()
      w.document.write(typeof res.data === 'string' ? res.data : '')
      w.document.close()
    })
    .catch(() => {
      w.close()
      ElMessage.error(t('performanceTesting.report.openFailed'))
    })
}
function downloadRaw() {
  request({ url: `/perf-testing/executions/${executionId}/download-raw/`, responseType: 'blob' })
    .then(res => {
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url
      a.download = `${execution.execution_no || 'perf'}_raw.csv.gz`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    })
    .catch(() => {
      ElMessage.error(t('performanceTesting.report.downloadFailed'))
    })
}
function goBack() {
  router.push('/performance-testing/executions')
}

// ---------- 分享直链 ----------
const shareVisible = ref(false)
const shareGenerating = ref(false)
const shareExpiry = ref(7)
const shareUrl = ref('')
const shareRawUrl = ref('')
const shareExpiresAt = ref('')
const shareToken = ref('')

function openShare() {
  // 打开即按当前 expiry 预生成一条分享直链
  shareVisible.value = true
  handleGenerate()
}
async function handleGenerate() {
  shareGenerating.value = true
  try {
    const res = await generatePerfShareLink(
      executionId, shareExpiry.value > 0 ? shareExpiry.value : null)
    shareToken.value = res.data?.token || ''
    shareUrl.value = res.data?.share_url || ''
    shareRawUrl.value = res.data?.raw_url || ''
    shareExpiresAt.value = res.data?.expires_at || ''
    ElMessage.success(t('performanceTesting.share.generated'))
  } catch (e) {
    ElMessage.error(apiError(e, t('performanceTesting.share.generate')))
  } finally {
    shareGenerating.value = false
  }
}
async function handleRevoke() {
  try {
    await revokePerfShareLink(executionId)
    shareUrl.value = ''
    shareRawUrl.value = ''
    shareExpiresAt.value = ''
    shareToken.value = ''
    ElMessage.success(t('performanceTesting.share.revoked'))
  } catch (e) {
    ElMessage.error(apiError(e, t('performanceTesting.share.revoke')))
  }
}
function copyText(text) {
  navigator.clipboard?.writeText(text).then(
    () => ElMessage.success(t('performanceTesting.share.copied')),
    () => ElMessage.warning(t('performanceTesting.share.copyFailed'))
  )
}
function onShareClosed() {
  shareUrl.value = ''
  shareRawUrl.value = ''
  shareExpiresAt.value = ''
  shareToken.value = ''
}

onMounted(async () => {
  await loadAll()
  window.addEventListener('resize', resizeCharts)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeCharts)
  ;[tpsChart, rtChart, errChart].forEach(c => c?.dispose())
  tpsChart = rtChart = errChart = null
  // 离开页面中断 AI 分析 SSE 连接，避免后台悬挂
  clearTimeout(aiIdleTimer)
  aiAbortCtrl.value?.abort()
})
</script>

<style lang="scss" scoped>
.perf-report { padding: 16px; }

.report-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  margin-bottom: 12px;
  background: #fff;
  border-radius: 6px;

  .head-left { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .head-right { display: flex; gap: 8px; }
  .exec-no { font-weight: 600; color: #303133; font-family: Menlo, Consolas, monospace; }
  .scenario-name { color: #909399; font-size: 13px; }
}

.report-tabs {
  background: #fff;
  border-radius: 6px;
  padding: 0 14px 14px;
  :deep(.el-tabs__content) { padding-top: 12px; }
}

.metric-row { margin-bottom: 12px; }
.chart-card { margin-bottom: 12px; }
.chart-box { height: 300px; }
.chart-sm { height: 200px; }
.snapshot-tip { margin-bottom: 12px; }
.mt12 { margin-top: 12px; }
.baseline-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  font-size: 13px;
  color: #909399;
}
.ok { color: #52c41a; }
.bad { color: #f5222d; }
.bad-val { color: #f5222d; font-weight: 600; }
.good-val { color: #52c41a; font-weight: 600; }

/* AI 分析 Tab */
.ai-panel {
  min-height: 320px;
  padding: 4px;
}

/* 未开始：引导卡片 */
.ai-hero {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 56px 24px;
  border: 1px dashed #dcdfe6;
  border-radius: 10px;
  background:
    radial-gradient(600px 200px at 50% -40px, rgba(64, 158, 255, 0.08), transparent),
    #fff;

  .ai-hero-icon {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 64px;
    height: 64px;
    border-radius: 18px;
    color: #fff;
    background: linear-gradient(135deg, #409eff 0%, #7c4dff 100%);
    box-shadow: 0 8px 20px rgba(64, 158, 255, 0.35);
    margin-bottom: 18px;
  }
  .ai-hero-title {
    margin: 0 0 8px;
    font-size: 18px;
    font-weight: 600;
    color: #303133;
  }
  .ai-hero-desc {
    margin: 0 0 18px;
    max-width: 460px;
    font-size: 13px;
    line-height: 1.7;
    color: #909399;
  }
  .ai-hero-features {
    display: flex;
    gap: 10px;
    margin-bottom: 24px;
    flex-wrap: wrap;
    justify-content: center;
  }
  .ai-feature {
    padding: 4px 12px;
    font-size: 12px;
    color: #409eff;
    background: rgba(64, 158, 255, 0.08);
    border: 1px solid rgba(64, 158, 255, 0.25);
    border-radius: 999px;
  }
  .ai-start-btn {
    padding: 11px 28px;
    background: linear-gradient(135deg, #409eff 0%, #7c4dff 100%);
    border: none;
    box-shadow: 0 6px 16px rgba(64, 158, 255, 0.35);
    &:hover { opacity: 0.92; }
  }
}

/* 分析中（尚无文本） */
.ai-streaming-hint {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 80px 0;
  font-size: 14px;
  color: #409eff;

  .ai-dots i {
    font-style: normal;
    animation: ai-dot 1.2s infinite;
    &:nth-child(2) { animation-delay: 0.2s; }
    &:nth-child(3) { animation-delay: 0.4s; }
  }
}
@keyframes ai-dot {
  0%, 60%, 100% { opacity: 0.2; }
  30% { opacity: 1; }
}

/* 分析结果卡片 */
.ai-card {
  border: 1px solid #ebeef5;
  border-radius: 10px;
  overflow: hidden;
  background: #fff;

  .ai-card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 16px;
    background: linear-gradient(90deg, rgba(64, 158, 255, 0.08), rgba(124, 77, 255, 0.06));
    border-bottom: 1px solid #ebeef5;
  }
  .ai-card-title {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .ai-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    font-weight: 600;
    background: linear-gradient(135deg, #409eff, #7c4dff);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
  }
  .ai-streaming-tag {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 12px;
    color: #409eff;
  }
  .ai-card-actions { display: flex; gap: 4px; }
  .ai-card-body { padding: 20px 24px; }
}

/* 流式输出光标 */
:deep(.ai-streaming-cursor) {
  display: inline-block;
  width: 8px;
  height: 15px;
  margin-left: 3px;
  vertical-align: text-bottom;
  background: #409eff;
  border-radius: 1px;
  animation: ai-blink 1s steps(1) infinite;
}
@keyframes ai-blink {
  0%, 50% { opacity: 1; }
  51%, 100% { opacity: 0; }
}

/* 失败提示 */
.ai-error {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 12px;
  .el-alert { flex: 1; }
}

/* Markdown 正文样式 */
.markdown-body {
  font-size: 14px;
  line-height: 1.8;
  color: #303133;
  word-break: break-word;

  :deep(h1), :deep(h2), :deep(h3), :deep(h4) {
    color: #1f2d3d;
    margin: 18px 0 10px;
    line-height: 1.4;
    &:first-child { margin-top: 0; }
  }
  :deep(h1) { font-size: 18px; }
  :deep(h2) {
    font-size: 16px;
    padding-left: 10px;
    border-left: 3px solid #409eff;
  }
  :deep(h3) { font-size: 15px; }
  :deep(p) { margin: 8px 0; }
  :deep(ul), :deep(ol) { margin: 8px 0; padding-left: 22px; }
  :deep(li) { margin: 4px 0; }
  :deep(li::marker) { color: #409eff; }
  :deep(strong) { color: #1f2d3d; }
  :deep(blockquote) {
    margin: 12px 0;
    padding: 8px 16px;
    border-left: 4px solid #409eff;
    border-radius: 0 6px 6px 0;
    background: #f5f7fa;
    color: #606266;
  }
  :deep(code) {
    padding: 2px 6px;
    font-size: 13px;
    font-family: Consolas, Monaco, Menlo, monospace;
    color: #c7254e;
    background: #f5f7fa;
    border-radius: 4px;
  }
  :deep(pre) {
    margin: 12px 0;
    padding: 14px 16px;
    overflow-x: auto;
    background: #282c34;
    border-radius: 6px;
    code { padding: 0; color: #abb2bf; background: transparent; }
  }
  :deep(table) {
    width: 100%;
    margin: 12px 0;
    border-collapse: collapse;
    th, td { padding: 8px 12px; border: 1px solid #ebeef5; text-align: left; }
    th { background: #f5f7fa; font-weight: 600; }
    tr:nth-child(even) { background: #fafafa; }
  }
  :deep(hr) { margin: 18px 0; border: none; border-top: 1px solid #ebeef5; }
  :deep(a) { color: #409eff; text-decoration: none; }
}
</style>
