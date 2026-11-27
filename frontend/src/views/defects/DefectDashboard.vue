<template>
  <div class="page-container defect-dashboard">
    <div class="page-header">
      <h1 class="page-title">缺陷看板</h1>
      <div class="header-actions">
        <el-button text type="primary" @click="router.push('/defects/list')">查看列表</el-button>
        <el-button @click="loadDashboard" :loading="loading">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button type="primary" @click="router.push('/defects/create')">
          <el-icon><Plus /></el-icon>
          新建缺陷
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="summaryNotice"
      class="panel-notice"
      :title="summaryNotice"
      type="warning"
      :closable="false"
      show-icon
    />
    <el-row :gutter="16" class="metric-grid" :aria-busy="summaryPanel.loading">
      <el-col v-for="metric in metrics" :key="metric.key" :xs="12" :sm="12" :md="8" :lg="4">
        <el-card shadow="hover" class="metric-card" :class="`metric-${metric.key}`">
          <div class="metric-value">{{ summaryPanel.data ? metric.value : '—' }}</div>
          <div class="metric-label" :title="metric.hint">{{ metric.label }}</div>
        </el-card>
      </el-col>
    </el-row>

    <div class="charts-container">
      <template v-if="summaryPanel.data">
        <div class="chart-row">
          <el-card shadow="never">
            <template #header>状态分布</template>
            <div v-if="statusDistribution.length" ref="statusChartRef" class="chart-body"></div>
            <el-empty v-else description="暂无状态数据" />
          </el-card>

          <el-card shadow="never">
            <template #header>严重级别分布</template>
            <div v-if="severityDistribution.length" ref="severityChartRef" class="chart-body"></div>
            <el-empty v-else description="暂无严重级别数据" />
          </el-card>
        </div>

        <div class="chart-row">
          <el-card shadow="never">
            <template #header>
              <div class="card-header">
                <span>版本与模块分布</span>
                <el-radio-group v-model="distributionDimension" size="small" aria-label="分布维度">
                  <el-radio-button value="module">模块</el-radio-button>
                  <el-radio-button value="version">版本</el-radio-button>
                </el-radio-group>
              </div>
            </template>
            <div v-if="dimensionRows.length" ref="distributionChartRef" class="chart-body"></div>
            <el-empty v-else :description="distributionDimension === 'version' ? '暂无版本数据' : '暂无模块数据'" />
          </el-card>

          <el-card shadow="never">
            <template #header>
              <div class="card-header">
                <span>处理人缺陷分布</span>
                <span class="subtle-note">按缺陷数排名</span>
              </div>
            </template>
            <div v-if="assigneeDistribution.length" ref="assigneeChartRef" class="chart-body"></div>
            <el-empty v-else description="暂无处理人数据" />
          </el-card>
        </div>
      </template>
      <el-skeleton v-else-if="summaryPanel.loading" :rows="4" animated />

      <el-card shadow="never" :aria-busy="trendPanel.loading">
        <template #header>
          <div class="card-header">
            <div class="card-heading">
              <span>缺陷趋势</span>
              <span class="subtle-note">近 {{ trendDays }} 天</span>
            </div>
            <el-button text type="primary" @click="router.push('/defects/reports')">查看报表</el-button>
          </div>
        </template>
        <el-alert v-if="trendNotice" :title="trendNotice" type="warning" :closable="false" show-icon />
        <div v-if="trendRows.length" ref="trendChartRef" class="chart-body trend-body"></div>
        <el-empty v-else-if="trendPanel.data" description="暂无趋势数据" />
        <el-skeleton v-else-if="trendPanel.loading" :rows="4" animated />
      </el-card>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Plus, Refresh } from '@element-plus/icons-vue'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { getDefectSummary, getDefectTrend } from '@/api/defects'
import { getOptionLabel, severityOptions, statusOptions } from './options'

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  CanvasRenderer,
])

const router = useRouter()
const trendDays = 14
const distributionLimit = 8
const summaryPanel = reactive({ data: null, loading: false, failed: false })
const trendPanel = reactive({ data: null, loading: false, failed: false })
const loading = computed(() => summaryPanel.loading || trendPanel.loading)

const panelNotice = (panel, label) => {
  if (!panel.failed) return ''
  return panel.data === null
    ? `${label}加载失败，请刷新重试`
    : `${label}刷新失败，当前显示上次成功加载的数据`
}
const summaryNotice = computed(() => panelNotice(summaryPanel, '缺陷汇总'))
const trendNotice = computed(() => panelNotice(trendPanel, '缺陷趋势'))
const summary = computed(() => summaryPanel.data ?? {})
const trendRows = computed(() => trendPanel.data ?? [])

const numberOf = value => Number(value || 0)
const firstDefined = (...values) => values.find(value => value !== undefined && value !== null && value !== '')
const summaryNumber = (...keys) => numberOf(firstDefined(...keys.map(key => summary.value[key])))
const totalCount = computed(() => summaryNumber('total', 'total_count'))
const closedCount = computed(() => summaryNumber('closed', 'closed_count'))
const openCount = computed(() => {
  const explicitOpen = firstDefined(summary.value.open, summary.value.open_count)
  return explicitOpen !== undefined ? numberOf(explicitOpen) : Math.max(totalCount.value - closedCount.value, 0)
})

const metrics = computed(() => [
  { key: 'total', label: '总缺陷', value: totalCount.value },
  { key: 'open', label: '未关闭', value: openCount.value, hint: '包含仍处于待回归、回归通过或已驳回等状态的缺陷' },
  { key: 'resolved', label: '待回归', value: summaryNumber('resolved', 'resolved_count') },
  { key: 'closed', label: '已关闭', value: closedCount.value },
  { key: 'severe', label: '高风险', value: summaryNumber('severe', 'critical', 'critical_count') },
  { key: 'overdue', label: '逾期未关闭', value: summaryNumber('overdue', 'overdue_count'), hint: '已超过期望修复时间，且仍未关闭' }
])

const normalizeDistribution = (data, options, fallbackLabel = '未设置') => {
  if (!data) return []
  const rows = Array.isArray(data)
    ? data
    : Object.entries(data).map(([key, value]) => ({ key, value }))

  return rows
    .map(item => {
      const key = item.key ?? item.status ?? item.severity ?? item.version__name ?? item.module ?? item.assignee__username ?? item.name
      const hasVersionIdentity = item.version__name !== undefined || item.version_id !== undefined
      const rawLabel = hasVersionIdentity
        ? firstDefined(item.label, item.version__name, item.key)
        : item.module ?? item.assignee__username ?? item.name ?? key
      return {
        label: options ? getOptionLabel(options, key) : String(rawLabel || fallbackLabel),
        value: numberOf(item.count ?? item.value)
      }
    })
    .filter(item => item.label && item.value > 0)
}

const rankedDistribution = (data, fallbackLabel) => normalizeDistribution(data, null, fallbackLabel)
  .sort((left, right) => right.value - left.value || left.label.localeCompare(right.label, 'zh-CN'))
  .slice(0, distributionLimit)

const statusDistribution = computed(() => normalizeDistribution(
  summary.value.status_distribution || summary.value.by_status || summary.value.status || summary.value.status_counts,
  statusOptions
))
const severityDistribution = computed(() => normalizeDistribution(
  summary.value.severity_distribution || summary.value.by_severity || summary.value.severity || summary.value.severity_counts,
  severityOptions
))
const distributionDimension = ref('module')
const dimensionRows = computed(() => distributionDimension.value === 'version'
  ? rankedDistribution(summary.value.version_distribution, '未设置版本')
  : rankedDistribution(summary.value.module_distribution, '未设置模块'))
const assigneeDistribution = computed(() => rankedDistribution(summary.value.assignee_distribution, '未指派'))

const normalizeTrendRows = (data) => {
  const rows = Array.isArray(data) ? data : (data?.results ?? data?.items)
  if (!Array.isArray(rows)) throw new Error('Invalid defect trend response')
  return rows.map(item => ({
    date: item.date ?? item.day ?? item.created_date ?? '-',
    created: numberOf(item.created ?? item.new ?? item.created_count),
    closed: numberOf(item.closed ?? item.closed_count)
  }))
}

const valueAxis = {
  type: 'value',
  minInterval: 1,
  splitLine: { lineStyle: { color: '#f2f4f7' } }
}
const chartPalette = ['#2F6BFF', '#17B26A', '#FF9F43', '#F04438', '#7A5AF8', '#12B3A8', '#8E6CFF', '#FDB022']

const statusOption = computed(() => ({
  color: chartPalette,
  tooltip: { trigger: 'item' },
  legend: { bottom: 0, left: 'center' },
  series: [{
    type: 'pie',
    radius: ['45%', '72%'],
    center: ['50%', '44%'],
    itemStyle: { borderRadius: 10, borderColor: '#fff', borderWidth: 2 },
    label: { formatter: '{b}\n{c}' },
    data: statusDistribution.value.map(item => ({ value: item.value, name: item.label }))
  }]
}))

const barOption = (rows, color, withBackground = false) => ({
  color: [color],
  tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
  grid: { top: 16, left: 20, right: 20, bottom: 20, containLabel: true },
  xAxis: valueAxis,
  yAxis: {
    type: 'category',
    inverse: true,
    data: rows.map(item => item.label),
    axisTick: { show: false },
    axisLabel: { width: 112, overflow: 'truncate' }
  },
  series: [{
    type: 'bar',
    barMaxWidth: 18,
    data: rows.map(item => item.value),
    itemStyle: { borderRadius: 999 },
    showBackground: withBackground,
    backgroundStyle: { color: '#f2f4f7', borderRadius: 999 }
  }]
})
const severityOption = computed(() => barOption(severityDistribution.value, '#F04438', true))
const distributionOption = computed(() => barOption(dimensionRows.value, '#2F6BFF'))
const assigneeOption = computed(() => barOption(assigneeDistribution.value, '#12B3A8'))

const trendOption = computed(() => ({
  color: ['#2F6BFF', '#17B26A'],
  tooltip: { trigger: 'axis' },
  legend: { top: 0, right: 0 },
  grid: { top: 40, left: 20, right: 20, bottom: 24, containLabel: true },
  xAxis: { type: 'category', data: trendRows.value.map(item => item.date), boundaryGap: false },
  yAxis: valueAxis,
  series: [
    {
      name: '新增',
      type: 'line',
      symbolSize: 8,
      areaStyle: { opacity: 0.12 },
      data: trendRows.value.map(item => item.created)
    },
    {
      name: '关闭次数',
      type: 'line',
      symbolSize: 8,
      areaStyle: { opacity: 0.08 },
      data: trendRows.value.map(item => item.closed)
    }
  ]
}))

const bindChart = (target, option) => {
  let chart = null
  watch([target, option], ([element, config], [previousElement]) => {
    if (element !== previousElement) {
      chart?.dispose()
      chart = element ? echarts.init(element) : null
    }
    chart?.setOption(config)
  }, { flush: 'post' })
  onBeforeUnmount(() => chart?.dispose())
  return () => chart?.resize()
}

const statusChartRef = ref(null)
const severityChartRef = ref(null)
const distributionChartRef = ref(null)
const assigneeChartRef = ref(null)
const trendChartRef = ref(null)
const resizeCharts = [
  bindChart(statusChartRef, statusOption),
  bindChart(severityChartRef, severityOption),
  bindChart(distributionChartRef, distributionOption),
  bindChart(assigneeChartRef, assigneeOption),
  bindChart(trendChartRef, trendOption)
]
const handleResize = () => resizeCharts.forEach(resize => resize())

const loadSummary = async () => {
  const { data } = await getDefectSummary()
  if (firstDefined(data?.total, data?.total_count) === undefined) throw new Error('Invalid defect summary response')
  return data
}
const loadTrend = async () => {
  const { data } = await getDefectTrend({ days: trendDays })
  return normalizeTrendRows(data)
}

const loadPanel = async (panel, loadData) => {
  panel.loading = true
  try {
    panel.data = await loadData()
    panel.failed = false
  } catch {
    panel.failed = true
  } finally {
    panel.loading = false
  }
}

const loadDashboard = async () => {
  if (loading.value) return
  await Promise.all([
    loadPanel(summaryPanel, loadSummary),
    loadPanel(trendPanel, loadTrend)
  ])
}

onMounted(() => {
  window.addEventListener('resize', handleResize)
  return loadDashboard()
})
onBeforeUnmount(() => window.removeEventListener('resize', handleResize))
</script>

<style lang="scss" scoped>
.header-actions {
  display: flex;
  gap: 10px;
}

.panel-notice {
  margin-bottom: 16px;
}

.metric-grid {
  margin-bottom: 16px;
}

.metric-card {
  margin-bottom: 16px;
  overflow: hidden;
  position: relative;

  &::before {
    content: '';
    position: absolute;
    inset: 0 auto 0 0;
    width: 4px;
    background: #2f6bff;
  }

  .metric-value {
    font-size: 28px;
    font-weight: 700;
    color: #1d2939;
    line-height: 1.1;
  }

  .metric-label {
    margin-top: 8px;
    color: #667085;
  }
}

.metric-open::before { background: #f79009; }
.metric-resolved::before { background: #7a5af8; }
.metric-closed::before { background: #17b26a; }
.metric-severe::before { background: #f04438; }
.metric-overdue::before { background: #12b3a8; }

.charts-container {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.chart-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.card-header,
.card-heading {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 12px;
}

.card-header {
  justify-content: space-between;
}

.subtle-note {
  color: #667085;
  font-size: 12px;
}

.chart-body {
  width: 100%;
  height: 280px;
}

.trend-body {
  height: 320px;
}

@media (max-width: 991px) {
  .chart-row {
    grid-template-columns: 1fr;
  }
}
</style>
