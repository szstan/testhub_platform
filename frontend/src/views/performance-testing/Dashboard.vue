<template>
  <section class="perf-dashboard">
    <header class="dashboard-header">
      <h1>{{ t('performanceTesting.dashboard.title') }}</h1>
      <div class="dashboard-filters">
        <el-select
          v-model="projectId"
          class="project-filter"
          :aria-label="t('performanceTesting.common.project')"
        >
          <el-option :label="t('performanceTesting.common.allProjects')" :value="0" />
          <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
        </el-select>
        <el-select
          v-model="days"
          class="range-filter"
          :aria-label="t('performanceTesting.dashboard.dateRange')"
        >
          <el-option v-for="option in [7, 30, 90]" :key="option" :value="option" :label="rangeLabel(option)" />
        </el-select>
        <el-button :icon="Refresh" :loading="loading" @click="refresh">
          {{ t('performanceTesting.common.refresh') }}
        </el-button>
      </div>
    </header>

    <el-alert
      v-if="projectsFailed"
      type="warning"
      :title="t('performanceTesting.dashboard.projectsFailed')"
      :closable="false"
      show-icon
    />

    <div class="dashboard-content" :aria-busy="loading">
      <div v-if="loading" class="loading-card" role="status">
        <span class="visually-hidden">{{ t('performanceTesting.common.loading') }}</span>
        <el-skeleton :rows="5" animated aria-hidden="true" />
      </div>
      <div v-else-if="loadFailed" class="loading-card load-error">
        <el-alert type="error" :title="t('performanceTesting.dashboard.loadFailed')" :closable="false" show-icon />
        <el-button @click="refresh">{{ t('performanceTesting.common.retry') }}</el-button>
      </div>
      <template v-else-if="data">
        <dl class="summary-grid">
          <div v-for="card in statCards" :key="card.key" class="summary-card" :data-metric="card.key">
            <dt>{{ card.label }}</dt>
            <dd>{{ card.value }}</dd>
            <dd class="summary-note">{{ card.note }}</dd>
          </div>
        </dl>

        <el-card shadow="never" class="recent-card">
          <template #header>
            <div class="panel-header">
              <div class="panel-title">
                <h2>{{ t('performanceTesting.dashboard.recentTitle') }}</h2>
                <span v-if="data.time_zone" class="time-zone">
                  {{ t('performanceTesting.dashboard.timeZone', { zone: data.time_zone }) }}
                </span>
              </div>
              <router-link class="text-link" :to="executionListRoute">
                {{ t('performanceTesting.dashboard.viewAll') }}
              </router-link>
            </div>
          </template>
          <el-empty v-if="!recent.length" :image-size="56" :description="t('performanceTesting.dashboard.trendEmpty')" />
          <template v-else>
            <div class="execution-grid execution-header" aria-hidden="true">
              <span>{{ t('performanceTesting.common.scenario') }}</span>
              <span>{{ t('performanceTesting.common.createdAt') }}</span>
              <span>{{ t('performanceTesting.common.status') }}</span>
              <span>{{ t('performanceTesting.execution.sla') }}</span>
              <span>{{ t('performanceTesting.metric.tps') }}</span>
              <span>{{ t('performanceTesting.dashboard.p95WithUnit') }}</span>
              <span>{{ t('performanceTesting.common.actions') }}</span>
            </div>
            <ul class="execution-list" :aria-label="t('performanceTesting.dashboard.recentTitle')">
              <li v-for="row in recent" :key="row.id" class="execution-grid execution-row">
                <div class="execution-name">
                  <strong>{{ row.scenario_name }}</strong>
                  <span class="execution-meta">
                    {{ row.execution_no }}<template v-if="!projectId && row.project_name"> · {{ row.project_name }}</template>
                  </span>
                </div>
                <div class="execution-cell">
                  <span class="field-label">{{ t('performanceTesting.common.createdAt') }}</span>
                  <time :datetime="row.created_at">{{ formatDashboardTime(row.created_at) }}</time>
                </div>
                <div class="execution-cell">
                  <span class="field-label">{{ t('performanceTesting.common.status') }}</span>
                  <el-tag size="small" effect="plain" :type="statusTagType(row.status)">
                    {{ t('performanceTesting.status.' + row.status) }}
                  </el-tag>
                </div>
                <div class="execution-cell">
                  <span class="field-label">{{ t('performanceTesting.execution.sla') }}</span>
                  <el-tag size="small" effect="plain" :type="slaTagType(row.sla_result)">
                    {{ t('performanceTesting.sla.' + (row.sla_result || 'NOT_EVALUATED')) }}
                  </el-tag>
                </div>
                <div class="execution-cell number-cell">
                  <span class="field-label">{{ t('performanceTesting.metric.tps') }}</span>
                  {{ formatNumber(row.tps) }}
                </div>
                <div class="execution-cell number-cell">
                  <span class="field-label">{{ t('performanceTesting.dashboard.p95WithUnit') }}</span>
                  {{ formatNumber(row.p95_rt) }}
                </div>
                <router-link class="text-link execution-action" :to="executionRoute(row)">
                  {{ t(isActive(row) ? 'performanceTesting.execution.monitor' : 'performanceTesting.execution.viewReport') }}
                  <span class="visually-hidden"> — {{ row.execution_no }}</span>
                </router-link>
              </li>
            </ul>
          </template>
        </el-card>

        <div class="charts-grid">
          <el-card shadow="never">
            <template #header><h2>{{ t('performanceTesting.dashboard.trendTitle') }}</h2></template>
            <div v-if="hasTrend" ref="trendElement" class="trend-chart" />
            <el-empty v-else :image-size="56" :description="t('performanceTesting.dashboard.trendEmpty')" />
          </el-card>
          <el-card shadow="never">
            <template #header><h2>{{ t('performanceTesting.dashboard.slaDistTitle') }}</h2></template>
            <div v-if="finishedCount" class="sla-chart-panel">
              <div ref="slaElement" class="sla-chart" aria-hidden="true" />
              <dl class="sla-counts">
                <div v-for="bucket in slaBuckets" :key="bucket.key">
                  <dt><span :style="{ backgroundColor: bucket.color }" />{{ bucket.label }}</dt>
                  <dd>{{ formatNumber(bucket.value) }}</dd>
                </div>
              </dl>
            </div>
            <el-empty v-else :image-size="56" :description="t('performanceTesting.dashboard.slaEmpty')" />
          </el-card>
        </div>

        <el-card shadow="never" class="slow-card">
          <template #header><h2>{{ t('performanceTesting.dashboard.slowestTitle') }}</h2></template>
          <template v-if="data.slowest?.items.length">
            <p class="slow-source">
              {{ t('performanceTesting.dashboard.slowestSource') }}
              <router-link class="text-link" :to="executionRoute(data.slowest.execution)">
                {{ data.slowest.execution.scenario_name }} · {{ data.slowest.execution.execution_no }}
              </router-link>
            </p>
            <ol class="slow-list">
              <li v-for="(item, index) in data.slowest.items" :key="index">
                <span class="slow-name">
                  <span v-if="item.method" class="slow-method">{{ item.method }}</span>
                  {{ item.step_name }}
                </span>
                <span class="slow-track" aria-hidden="true">
                  <span :style="{ width: `${item.p95_rt / data.slowest.items[0].p95_rt * 100}%` }" />
                </span>
                <strong class="number-cell">{{ formatNumber(item.p95_rt) }} ms</strong>
              </li>
            </ol>
          </template>
          <el-empty v-else :image-size="56" :description="t('performanceTesting.dashboard.slowestEmpty')" />
        </el-card>
      </template>
    </div>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch, watchPostEffect } from 'vue'
import { useI18n } from 'vue-i18n'
import { Refresh } from '@element-plus/icons-vue'
import { init, use } from 'echarts/core'
import { LineChart, PieChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { getPerfDashboard, getPerfProjects } from '@/api/performance-testing'
import { slaTagType, statusTagType } from './shared'

use([LineChart, PieChart, GridComponent, TooltipComponent, CanvasRenderer])

const { t, locale } = useI18n()
const projectId = ref(0)
const days = ref(30)
const projects = ref([])
const projectsFailed = ref(false)
const loading = ref(false)
const loadFailed = ref(false)
const data = ref(null)
let dashboardRequest

const numberFormat = computed(() => new Intl.NumberFormat(locale.value, { maximumFractionDigits: 2 }))
const recent = computed(() => data.value?.recent || [])
const dailyExecutions = computed(() => data.value?.daily_executions || [])
const hasTrend = computed(() => dailyExecutions.value.some(({ count }) => count > 0))
const trendElement = ref(null)
const slaElement = ref(null)
const SLA_COLORS = { PASSED: '#19875a', FAILED: '#d64c59', NOT_EVALUATED: '#a1a9b5' }
const ACTIVE_STATUSES = ['PENDING', 'PREPARING', 'RUNNING', 'STOPPING']

const slaBuckets = computed(() => Object.entries(SLA_COLORS).map(([key, color]) => ({
  key,
  color,
  label: t(`performanceTesting.sla.${key}`),
  value: data.value?.sla_counts?.[key] ?? 0,
})))
const finishedCount = computed(() => slaBuckets.value.reduce((total, bucket) => total + bucket.value, 0))
const evaluatedCount = computed(() => (data.value?.sla_counts?.PASSED ?? 0) + (data.value?.sla_counts?.FAILED ?? 0))

const statCards = computed(() => {
  const result = data.value || {}
  return [
    {
      key: 'scenarios', label: t('performanceTesting.dashboard.scenarioTotal'),
      value: formatNumber(result.total_scenarios), note: t('performanceTesting.dashboard.scenarioScope'),
    },
    {
      key: 'executions', label: t('performanceTesting.dashboard.executionTotal'),
      value: formatNumber(result.total_executions), note: rangeLabel(days.value),
    },
    {
      key: 'sla', label: t('performanceTesting.dashboard.slaPassRate'),
      value: result.sla_pass_rate == null
        ? t('performanceTesting.sla.NOT_EVALUATED')
        : `${formatNumber(result.sla_pass_rate)}%`,
      note: t('performanceTesting.dashboard.slaCoverage', {
        evaluated: formatNumber(evaluatedCount.value), total: formatNumber(finishedCount.value),
      }),
    },
    {
      key: 'tps', label: t('performanceTesting.dashboard.avgTps'),
      value: formatNumber(result.avg_tps), note: t('performanceTesting.dashboard.tpsScope'),
    },
    {
      key: 'running', label: t('performanceTesting.dashboard.runningNow'),
      value: formatNumber(result.running_count), note: t('performanceTesting.dashboard.activeScope'),
    },
  ]
})

const executionListRoute = computed(() => ({
  name: 'PerfExecutions',
  query: projectId.value ? { project: projectId.value } : {},
}))

function formatNumber(value) {
  if (value == null || value === '' || !Number.isFinite(Number(value))) return '—'
  return numberFormat.value.format(Number(value))
}

function rangeLabel(value) {
  return t(`performanceTesting.dashboard.range${value}`)
}

function formatDashboardTime(iso) {
  // The API serializes timestamps in the same timezone as the daily buckets.
  return iso ? iso.slice(0, 19).replace('T', ' ') : '—'
}

function isActive(execution) {
  return ACTIVE_STATUSES.includes(execution.status)
}

function executionRoute(execution) {
  return { name: isActive(execution) ? 'PerfExecutionMonitor' : 'PerfExecutionReport', params: { id: execution.id } }
}

async function loadProjects() {
  projectsFailed.value = false
  try {
    const { data: result } = await getPerfProjects({ page_size: 0 })
    projects.value = result.results || result
  } catch {
    projectsFailed.value = true
  }
}

async function loadDashboard() {
  dashboardRequest?.abort()
  const request = new AbortController()
  dashboardRequest = request
  data.value = null
  loadFailed.value = false
  loading.value = true
  const params = { days: days.value }
  if (projectId.value) params.project = projectId.value
  try {
    const { data: result } = await getPerfDashboard(params, request.signal)
    if (!request.signal.aborted) data.value = result
  } catch {
    if (!request.signal.aborted) loadFailed.value = true
  } finally {
    if (!request.signal.aborted) loading.value = false
  }
}

function refresh() {
  if (projectsFailed.value) loadProjects()
  loadDashboard()
}

function renderChart(element, getOptions) {
  watchPostEffect((onCleanup) => {
    if (!element.value) return
    const chart = init(element.value)
    const observer = new ResizeObserver(() => chart.resize())
    onCleanup(() => {
      observer.disconnect()
      chart.dispose()
    })
    chart.setOption(getOptions())
    observer.observe(element.value)
  })
}

renderChart(trendElement, () => ({
  tooltip: { trigger: 'axis', confine: true },
  grid: { left: 12, right: 18, top: 20, bottom: 12, containLabel: true },
  xAxis: {
    type: 'category', boundaryGap: false,
    data: dailyExecutions.value.map(({ date }) => date),
    axisLabel: { hideOverlap: true, formatter: value => value.slice(5) },
  },
  yAxis: { type: 'value', minInterval: 1 },
  series: [{
    name: t('performanceTesting.dashboard.executionTotal'), type: 'line',
    data: dailyExecutions.value.map(({ count }) => count),
    showSymbol: false, itemStyle: { color: '#3575df' }, areaStyle: { opacity: 0.12 },
  }],
}))

renderChart(slaElement, () => ({
  tooltip: { trigger: 'item', confine: true },
  series: [{
    type: 'pie', radius: ['54%', '77%'], label: { show: false },
    data: slaBuckets.value.map(({ label, value, color }) => ({ name: label, value, itemStyle: { color } })),
  }],
}))

watch([projectId, days], loadDashboard, { immediate: true })
onMounted(loadProjects)
onBeforeUnmount(() => dashboardRequest?.abort())
</script>

<style scoped>
.perf-dashboard {
  container: perf-dashboard / inline-size;
  min-width: 0;
  padding: 16px;
  color: var(--el-text-color-primary);
}

.dashboard-header,
.panel-header,
.dashboard-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px 16px;
}

.dashboard-header { margin-bottom: 16px; }
.dashboard-header h1 { margin: 0; font-size: 21px; font-weight: 650; }
.dashboard-filters { justify-content: flex-start; gap: 8px; }
.project-filter { width: 180px; max-width: 100%; }
.range-filter { width: 136px; max-width: 100%; }

.dashboard-content { display: grid; gap: 16px; }
.el-alert + .dashboard-content { margin-top: 16px; }
.loading-card { padding: 24px; border-radius: 8px; background: var(--el-bg-color); }
.load-error { display: grid; justify-items: start; gap: 12px; }

.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 148px), 1fr));
  gap: 12px;
  margin: 0;
}

.summary-card {
  min-width: 0;
  padding: 13px 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
}

.summary-card dt { font-size: 13px; color: var(--el-text-color-regular); }
.summary-card dd { margin: 3px 0 0; font-size: 24px; font-weight: 650; font-variant-numeric: tabular-nums; }
.summary-card .summary-note { font-size: 12px; font-weight: 400; color: var(--el-text-color-regular); }

h2 { margin: 0; font-size: 15px; font-weight: 650; }
.panel-title { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; }
.time-zone { color: var(--el-text-color-regular); font-size: 12px; }
.text-link { color: var(--el-color-primary); text-underline-offset: 3px; text-decoration: none; }
.text-link:hover, .text-link:focus-visible { text-decoration: underline; }

.execution-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 140px 84px 90px 64px 82px 96px;
  align-items: center;
  gap: 10px;
  padding: 9px 0;
}

.execution-header { color: var(--el-text-color-regular); font-size: 12px; }
.execution-list { list-style: none; margin: 0; padding: 0; }
.execution-row { border-top: 1px solid var(--el-border-color-lighter); font-size: 13px; }
.execution-name { display: grid; min-width: 0; gap: 3px; overflow-wrap: anywhere; }
.execution-meta { color: var(--el-text-color-regular); font-size: 12px; }
.execution-cell { min-width: 0; }
.execution-cell time { font-variant-numeric: tabular-nums; }
.execution-action { justify-self: start; line-height: 1.5; font-weight: 550; }
.number-cell { font-variant-numeric: tabular-nums; }

.field-label, .visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  overflow: hidden;
  white-space: nowrap;
  clip-path: inset(50%);
}

.charts-grid { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 16px; }
.trend-chart { height: 200px; }
.sla-chart-panel { display: flex; flex-wrap: wrap; align-items: center; justify-content: center; column-gap: 16px; }
.sla-chart { width: 150px; max-width: 100%; height: 150px; }
.sla-counts { flex: 1 1 126px; display: grid; gap: 9px; margin: 0; font-size: 13px; }
.sla-counts div { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.sla-counts dt { display: flex; align-items: center; gap: 8px; }
.sla-counts dt span { width: 8px; height: 8px; flex: 0 0 8px; border-radius: 50%; }
.sla-counts dd { margin: 0; font-weight: 600; }

.slow-source { margin: 0 0 12px; font-size: 13px; color: var(--el-text-color-regular); overflow-wrap: anywhere; }
.slow-list { display: grid; gap: 12px; list-style: none; margin: 0; padding: 0; }
.slow-list li { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 3fr) max-content; align-items: center; gap: 12px; font-size: 13px; }
.slow-name { overflow-wrap: anywhere; }
.slow-method { margin-right: 4px; color: var(--el-text-color-secondary); font-size: 12px; }
.slow-track { height: 6px; border-radius: 6px; background: var(--el-fill-color-light); }
.slow-track span { display: block; height: 100%; border-radius: inherit; background: var(--el-color-primary); }

@container perf-dashboard (max-width: 820px) {
  .execution-header { display: none; }
  .execution-row { grid-template-columns: repeat(auto-fit, minmax(min(100%, 103px), 1fr)); gap: 8px 12px; padding: 12px 0; }
  .execution-row:first-child { border-top: 0; padding-top: 0; }
  .execution-name, .execution-action { grid-column: 1 / -1; }
  .field-label { position: static; display: block; width: auto; height: auto; margin-bottom: 3px; white-space: normal; clip-path: none; color: var(--el-text-color-regular); font-size: 11px; }
  .charts-grid { grid-template-columns: minmax(0, 1fr); }
}

@container perf-dashboard (max-width: 480px) {
  .project-filter { width: 100%; }
  .slow-list li { grid-template-columns: minmax(0, 1fr) max-content; }
  .slow-track { display: none; }
}
</style>
