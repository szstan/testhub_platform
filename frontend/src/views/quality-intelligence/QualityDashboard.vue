<template>
  <div class="quality-page quality-dashboard">
    <div class="quality-header">
      <div>
        <p class="eyebrow">{{ t('qualityIntelligence.dashboard.eyebrow') }}</p>
        <h1>{{ t('qualityIntelligence.dashboard.title') }}</h1>
        <p class="subtitle">{{ t('qualityIntelligence.dashboard.subtitle') }}</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="loadDashboard">
        {{ t('qualityIntelligence.actions.refresh') }}
      </el-button>
    </div>

    <el-card shadow="never" class="filter-panel">
      <el-form :inline="true" :model="filters" @submit.prevent>
        <el-form-item :label="t('qualityIntelligence.filters.source')">
          <el-select v-model="filters.source_type" clearable :placeholder="t('qualityIntelligence.filters.allSources')" @change="loadDashboard">
            <el-option v-for="source in sourceOptions" :key="source" :label="source" :value="source" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('qualityIntelligence.filters.dateRange')">
          <el-date-picker
            v-model="dateRange"
            type="daterange"
            value-format="YYYY-MM-DD"
            :start-placeholder="t('qualityIntelligence.filters.startDate')"
            :end-placeholder="t('qualityIntelligence.filters.endDate')"
            @change="loadDashboard"
          />
        </el-form-item>
        <el-button text @click="clearFilters">{{ t('qualityIntelligence.actions.clear') }}</el-button>
      </el-form>
    </el-card>

    <el-alert v-if="error" class="state-alert" type="error" :title="error" show-icon :closable="false" />

    <el-skeleton v-if="loading && !summary.run_count" :rows="4" animated />
    <template v-else>
      <el-row :gutter="16" class="metric-grid">
        <el-col v-for="metric in metrics" :key="metric.key" :xs="12" :sm="12" :md="6" :lg="3">
          <el-card shadow="never" class="metric-card" :class="`metric-${metric.tone}`">
            <div class="metric-value">{{ metric.value }}</div>
            <div class="metric-label">{{ metric.label }}</div>
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="16" class="dashboard-grid">
        <el-col :xs="24" :lg="15">
          <el-card shadow="never" class="section-card chart-card">
            <template #header>
              <div class="section-heading">
                <span>{{ t('qualityIntelligence.dashboard.trendTitle') }}</span>
                <el-tag size="small" effect="plain">{{ t('qualityIntelligence.labels.deterministic') }}</el-tag>
              </div>
            </template>
            <div v-if="trendPoints.length" ref="trendChartRef" class="chart" />
            <el-empty v-else :description="t('qualityIntelligence.states.noTrend')" />
          </el-card>
        </el-col>
        <el-col :xs="24" :lg="9">
          <el-card shadow="never" class="section-card">
            <template #header>{{ t('qualityIntelligence.dashboard.sourcesTitle') }}</template>
            <div v-if="sourceBreakdown.length" class="source-list">
              <div v-for="source in sourceBreakdown" :key="source.name" class="source-row">
                <div class="source-name">
                  <span class="source-dot" :class="`source-${source.name.toLowerCase()}`" />
                  <span>{{ source.name }}</span>
                </div>
                <strong>{{ source.count }}</strong>
              </div>
            </div>
            <el-empty v-else :description="t('qualityIntelligence.states.noData')" />
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="16" class="dashboard-grid">
        <el-col :xs="24" :lg="12">
          <el-card shadow="never" class="section-card">
            <template #header>{{ t('qualityIntelligence.dashboard.qualityTitle') }}</template>
            <div class="status-list">
              <div v-for="status in qualityStatuses" :key="status.key" class="status-row">
                <span>{{ status.label }}</span>
                <el-progress :percentage="status.percentage" :color="status.color" :show-text="false" />
                <strong>{{ status.value }}</strong>
              </div>
            </div>
          </el-card>
        </el-col>
        <el-col :xs="24" :lg="12">
          <el-card shadow="never" class="section-card">
            <template #header>{{ t('qualityIntelligence.dashboard.governanceTitle') }}</template>
            <div class="governance-summary">
              <div>
                <span>{{ t('qualityIntelligence.dashboard.flakyProfiles') }}</span>
                <strong>{{ flakyProfiles }}</strong>
              </div>
              <div>
                <span>{{ t('qualityIntelligence.dashboard.activeQuarantines') }}</span>
                <strong>{{ activeQuarantines }}</strong>
              </div>
              <el-button type="primary" plain @click="router.push('/quality-intelligence/profiles')">
                {{ t('qualityIntelligence.actions.openProfiles') }}
              </el-button>
            </div>
          </el-card>
        </el-col>
      </el-row>
    </template>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { Refresh } from '@element-plus/icons-vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { getQualityProfiles, getQualityQuarantines, getQualitySummary, getQualityTrend } from '@/api/quality-intelligence'

echarts.use([LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

const { t } = useI18n()
const router = useRouter()
const loading = ref(false)
const error = ref('')
const summary = ref({})
const trendPoints = ref([])
const profiles = ref([])
const quarantines = ref([])
const dateRange = ref([])
const filters = ref({ source_type: '' })
const trendChartRef = ref(null)
let trendChart = null

const sourceOptions = ['API', 'UI', 'APP', 'PERF']
const rowsFrom = (payload) => payload?.results || payload?.items || (Array.isArray(payload) ? payload : [])
const numberOf = value => Number(value || 0)

const requestParams = computed(() => {
  const params = {}
  if (filters.value.source_type) params.source_type = filters.value.source_type
  if (dateRange.value?.[0]) params.created_after = dateRange.value[0]
  if (dateRange.value?.[1]) params.created_before = dateRange.value[1]
  return params
})

const metrics = computed(() => [
  { key: 'runs', label: t('qualityIntelligence.dashboard.runs'), value: numberOf(summary.value.run_count), tone: 'blue' },
  { key: 'items', label: t('qualityIntelligence.dashboard.items'), value: numberOf(summary.value.item_count), tone: 'teal' },
  { key: 'failures', label: t('qualityIntelligence.dashboard.failures'), value: numberOf(summary.value.failure_count), tone: 'red' },
  { key: 'partial', label: t('qualityIntelligence.dashboard.partial'), value: numberOf(summary.value.runs_by_quality_status?.PARTIAL), tone: 'amber' }
])

const sourceBreakdown = computed(() => Object.entries(summary.value.runs_by_source || {}).map(([name, count]) => ({ name, count })))
const flakyProfiles = computed(() => profiles.value.filter(profile => profile.reliability_status === 'FLAKY').length)
const activeQuarantines = computed(() => quarantines.value.filter(item => item.status === 'ACTIVE').length)
const qualityStatuses = computed(() => {
  const values = summary.value.runs_by_quality_status || {}
  const total = Math.max(Object.values(values).reduce((sum, value) => sum + numberOf(value), 0), 1)
  return [
    { key: 'INGESTED', label: t('qualityIntelligence.status.INGESTED'), value: numberOf(values.INGESTED), color: '#17b26a' },
    { key: 'PARTIAL', label: t('qualityIntelligence.status.PARTIAL'), value: numberOf(values.PARTIAL), color: '#f79009' },
    { key: 'FAILED', label: t('qualityIntelligence.status.FAILED'), value: numberOf(values.FAILED), color: '#f04438' },
    { key: 'SOURCE_MISSING', label: t('qualityIntelligence.status.SOURCE_MISSING'), value: numberOf(values.SOURCE_MISSING), color: '#667085' }
  ].map(item => ({ ...item, percentage: Math.round(item.value / total * 100) }))
})

const loadDashboard = async () => {
  loading.value = true
  error.value = ''
  try {
    const [summaryResponse, trendResponse, profileResponse, quarantineResponse] = await Promise.all([
      getQualitySummary(requestParams.value),
      getQualityTrend(requestParams.value),
      getQualityProfiles({ page_size: 200, ...requestParams.value }),
      getQualityQuarantines({ page_size: 200, status: 'ACTIVE' })
    ])
    summary.value = summaryResponse.data || {}
    trendPoints.value = trendResponse.data?.points || rowsFrom(trendResponse.data)
    profiles.value = rowsFrom(profileResponse.data)
    quarantines.value = rowsFrom(quarantineResponse.data)
    await nextTick()
    renderTrend()
  } catch (requestError) {
    error.value = requestError?.response?.data?.detail || t('qualityIntelligence.errors.loadDashboard')
  } finally {
    loading.value = false
  }
}

const clearFilters = () => {
  filters.value.source_type = ''
  dateRange.value = []
  loadDashboard()
}

const renderTrend = () => {
  if (!trendChartRef.value) return
  if (!trendChart) trendChart = echarts.init(trendChartRef.value)
  trendChart.setOption({
    color: ['#2f6bff', '#f04438'],
    tooltip: { trigger: 'axis' },
    legend: { top: 0, right: 0 },
    grid: { left: 16, right: 20, top: 34, bottom: 20, containLabel: true },
    xAxis: { type: 'category', data: trendPoints.value.map(point => point.date), boundaryGap: false },
    yAxis: { type: 'value', min: 0, splitLine: { lineStyle: { color: '#eaecf0' } } },
    series: [
      { name: t('qualityIntelligence.dashboard.passedItems'), type: 'line', smooth: true, data: trendPoints.value.map(point => point.passed_items || 0) },
      { name: t('qualityIntelligence.dashboard.failedItems'), type: 'line', smooth: true, data: trendPoints.value.map(point => point.failed_items || 0) }
    ]
  })
}

const handleResize = () => trendChart?.resize()

onMounted(() => {
  window.addEventListener('resize', handleResize)
  loadDashboard()
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  trendChart?.dispose()
  trendChart = null
})
</script>

<style scoped>
.quality-page { color: #101828; }
.quality-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 18px; }
.quality-header h1 { margin: 0; font-size: 24px; line-height: 1.25; font-weight: 650; }
.eyebrow { margin: 0 0 6px; color: #2f6bff; font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.subtitle { margin: 7px 0 0; color: #667085; }
.filter-panel, .section-card, .metric-card { border-color: #eaecf0; border-radius: 8px; }
.filter-panel { margin-bottom: 16px; }
.filter-panel :deep(.el-form-item) { margin-bottom: 0; }
.state-alert { margin-bottom: 16px; }
.metric-grid { margin-bottom: 16px; }
.metric-card { min-height: 108px; }
.metric-value { font-size: 30px; line-height: 1; font-weight: 700; }
.metric-label { margin-top: 13px; color: #667085; font-size: 13px; }
.metric-blue { border-top: 3px solid #2f6bff; }
.metric-teal { border-top: 3px solid #12b3a8; }
.metric-red { border-top: 3px solid #f04438; }
.metric-amber { border-top: 3px solid #f79009; }
.dashboard-grid { margin-bottom: 16px; }
.section-card { height: 100%; }
.chart-card { min-height: 330px; }
.chart { height: 260px; width: 100%; }
.section-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; font-weight: 650; }
.source-list, .status-list { display: grid; gap: 14px; }
.source-row, .status-row { display: grid; align-items: center; gap: 12px; }
.source-row { grid-template-columns: 1fr auto; padding: 10px 0; border-bottom: 1px solid #f2f4f7; }
.source-row:last-child { border-bottom: 0; }
.source-name { display: flex; align-items: center; gap: 8px; }
.source-dot { width: 9px; height: 9px; border-radius: 50%; background: #98a2b3; }
.source-api { background: #2f6bff; } .source-ui { background: #12b3a8; } .source-app { background: #7a5af8; } .source-perf { background: #f79009; }
.status-row { grid-template-columns: 112px 1fr 32px; }
.status-row strong, .source-row strong { font-variant-numeric: tabular-nums; text-align: right; }
.governance-summary { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: center; gap: 16px; min-height: 112px; }
.governance-summary div { display: grid; gap: 6px; }
.governance-summary span { color: #667085; font-size: 13px; }
.governance-summary strong { font-size: 28px; }
.governance-summary .el-button { grid-column: 1 / -1; justify-self: start; }
@media (max-width: 768px) {
  .quality-header { align-items: stretch; flex-direction: column; }
  .quality-header .el-button { align-self: flex-start; }
  .filter-panel :deep(.el-form) { display: grid; gap: 10px; }
  .filter-panel :deep(.el-form-item) { margin-right: 0; }
  .filter-panel :deep(.el-select), .filter-panel :deep(.el-date-editor) { width: 100%; }
}
</style>
