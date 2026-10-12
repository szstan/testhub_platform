<template>
  <div class="perf-dashboard">
    <div class="dash-header">
      <div class="dash-title">
        <el-icon><Odometer /></el-icon>
        <span>{{ t('performanceTesting.dashboard.title') }}</span>
      </div>
      <div class="dash-actions">
        <el-select v-model="projectId" :placeholder="t('performanceTesting.common.allProjects')" clearable style="width: 180px" @change="loadAll">
          <el-option v-for="p in projects" :key="p.id" :label="p.name" :value="p.id" />
        </el-select>
        <el-select v-model="range" style="width: 120px" @change="computeTrend">
          <el-option :label="t('performanceTesting.dashboard.range7')" :value="7" />
          <el-option :label="t('performanceTesting.dashboard.range30')" :value="30" />
          <el-option :label="t('performanceTesting.dashboard.range90')" :value="90" />
        </el-select>
        <el-button type="primary" :icon="Refresh" :loading="loading" @click="loadAll">
          {{ t('performanceTesting.common.refresh') }}
        </el-button>
      </div>
    </div>

    <!-- 概览卡片 -->
    <el-row :gutter="16" class="stat-row">
      <el-col :span="4" v-for="c in statCards" :key="c.key">
        <el-card shadow="hover" class="stat-card" :class="c.cls">
          <div class="sc-val">{{ c.value }}</div>
          <div class="sc-label">{{ c.label }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" class="mid-row">
      <el-col :span="12">
        <el-card shadow="hover" class="chart-card">
          <template #header>{{ t('performanceTesting.dashboard.trendTitle') }}</template>
          <div ref="trendRef" class="chart-box"></div>
          <el-empty v-if="!hasTrend" :description="t('performanceTesting.dashboard.trendEmpty')" />
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="hover" class="chart-card">
          <template #header>{{ t('performanceTesting.dashboard.slaDistTitle') }}</template>
          <div ref="slaRef" class="chart-box"></div>
          <el-empty v-if="!hasSla" :description="t('performanceTesting.dashboard.trendEmpty')" />
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" class="mid-row">
      <el-col :span="12">
        <el-card shadow="hover" class="chart-card">
          <template #header>{{ t('performanceTesting.dashboard.slowestTitle') }}</template>
          <div ref="slowRef" class="chart-box"></div>
          <el-empty v-if="!hasSlow" :description="t('performanceTesting.dashboard.slowestEmpty')" />
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="hover" class="chart-card">
          <template #header>{{ t('performanceTesting.dashboard.recentTitle') }}</template>
          <el-table :data="recent" size="small" @row-click="openExecution" style="cursor: pointer">
            <el-table-column :label="t('performanceTesting.execution.executionNo')" prop="execution_no" min-width="150" />
            <el-table-column :label="t('performanceTesting.scenario.title')" prop="scenario_name" min-width="120" show-overflow-tooltip />
            <el-table-column :label="t('performanceTesting.common.status')" width="100">
              <template #default="{ row }"><el-tag size="small" :type="statusTagType(row.status)">{{ t('performanceTesting.status.' + row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column :label="t('performanceTesting.metric.tps')" width="90" align="right">
              <template #default="{ row }">{{ fmt(row.tps) }}</template>
            </el-table-column>
            <el-table-column :label="t('performanceTesting.metric.p95Rt')" width="100" align="right">
              <template #default="{ row }">{{ fmt(row.p95_rt) }}</template>
            </el-table-column>
            <el-table-column :label="t('performanceTesting.execution.sla')" width="90">
              <template #default="{ row }"><el-tag size="small" :type="slaTagType(row.sla_result)">{{ t('performanceTesting.sla.' + (row.sla_result || 'NOT_EVALUATED')) }}</el-tag></template>
            </el-table-column>
          </el-table>
          <el-empty v-if="recent.length === 0" :description="t('performanceTesting.dashboard.trendEmpty')" />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import * as echarts from 'echarts'
import { Odometer, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { getPerfProjects, getPerfDashboard, getPerfRequestStats } from '@/api/performance-testing'
import { statusTagType, slaTagType } from './shared'

const { t } = useI18n()
const router = useRouter()
const loading = ref(false)
const projects = ref([])
const projectId = ref(null)
const range = ref(30)
const data = ref(null)
const recent = computed(() => data.value?.recent || [])
const running = computed(() => data.value?.running || [])

const statCards = computed(() => {
  const d = data.value || {}
  return [
    { key: 'total', cls: 'total', label: t('performanceTesting.dashboard.scenarioTotal'), value: d.total_executions ?? 0 },
    { key: 'exec', cls: 'exec', label: t('performanceTesting.dashboard.executionTotal'), value: d.completed + d.failed },
    { key: 'sla', cls: 'sla', label: t('performanceTesting.dashboard.slaPassRate'), value: (d.sla_pass_rate ?? 0) + '%' },
    { key: 'avg', cls: 'avg', label: t('performanceTesting.dashboard.avgTps'), value: fmt(d.avg_tps) },
    { key: 'run', cls: 'run', label: t('performanceTesting.dashboard.runningNow'), value: running.value.length }
  ]
})

const trendRef = ref(null)
const slaRef = ref(null)
const slowRef = ref(null)
let trendChart = null, slaChart = null, slowChart = null

const hasTrend = ref(false)
const hasSla = ref(false)
const hasSlow = ref(false)
const slowItems = ref([])

function fmt(v) {
  if (v === undefined || v === null || v === '') return '-'
  return typeof v === 'number' ? (Math.round(v * 100) / 100) : v
}

async function loadProjects() {
  try {
    const res = await getPerfProjects({ page_size: 200 })
    projects.value = res.data.results || res.data || []
  } catch (e) { /* ignore */ }
}

async function loadDashboard() {
  loading.value = true
  try {
    const params = {}
    if (projectId.value) params.project = projectId.value
    const res = await getPerfDashboard(params)
    data.value = res.data
    await nextTick()
    computeTrend()
    computeSla()
    await loadSlow()
  } catch (e) {
    ElMessage.error('加载看板失败')
  } finally {
    loading.value = false
  }
}

function computeTrend() {
  const list = recent.value.slice()
  const buckets = {}
  for (const e of list) {
    const d = (e.created_at || '').slice(0, 10)
    if (d) buckets[d] = (buckets[d] || 0) + 1
  }
  const keys = Object.keys(buckets).sort().slice(-range.value)
  hasTrend.value = keys.length > 0
  if (!trendChart) trendChart = echarts.init(trendRef.value)
  trendChart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: '3%', right: '4%', bottom: '8%', top: '12%', containLabel: true },
    xAxis: { type: 'category', data: keys, axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', minInterval: 1 },
    series: [{ type: 'line', smooth: true, data: keys.map(k => buckets[k]), itemStyle: { color: '#1890ff' }, areaStyle: { opacity: 0.15, color: '#1890ff' } }]
  })
}

function computeSla() {
  const counts = { PASSED: 0, FAILED: 0, NOT_EVALUATED: 0 }
  for (const e of recent.value) {
    const r = e.sla_result || 'NOT_EVALUATED'
    if (r === 'PASSED') counts.PASSED++
    else if (r === 'FAILED') counts.FAILED++
    else counts.NOT_EVALUATED++
  }
  hasSla.value = recent.value.length > 0
  if (!slaChart) slaChart = echarts.init(slaRef.value)
  slaChart.setOption({
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['40%', '65%'],
      data: [
        { name: t('performanceTesting.sla.PASSED'), value: counts.PASSED, itemStyle: { color: '#52c41a' } },
        { name: t('performanceTesting.sla.FAILED'), value: counts.FAILED, itemStyle: { color: '#ff4d4f' } },
        { name: t('performanceTesting.sla.NOT_EVALUATED'), value: counts.NOT_EVALUATED, itemStyle: { color: '#bfbfbf' } }
      ]
    }]
  })
}

async function loadSlow() {
  const target = recent.value.find(e => e.sla_result) // 取最近一条有结论的
  slowItems.value = []
  hasSlow.value = false
  if (!target) return
  try {
    const res = await getPerfRequestStats(target.id)
    const stats = (res.data || []).filter(s => s.p95_rt > 0).sort((a, b) => b.p95_rt - a.p95_rt).slice(0, 5)
    slowItems.value = stats.map(s => ({ name: s.step_name, p95: Math.round(s.p95_rt) }))
    hasSlow.value = slowItems.value.length > 0
    if (!slowChart) slowChart = echarts.init(slowRef.value)
    slowChart.setOption({
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      grid: { left: '3%', right: '8%', bottom: '3%', top: '6%', containLabel: true },
      xAxis: { type: 'value', name: 'ms' },
      yAxis: { type: 'category', data: slowItems.value.map(s => s.name).reverse(), axisLabel: { fontSize: 11 } },
      series: [{ type: 'bar', data: slowItems.value.map(s => s.p95).reverse(), itemStyle: { color: '#1890ff' }, barWidth: '55%' }]
    })
  } catch (e) { /* ignore */ }
}

function openExecution(row) {
  router.push(`/performance-testing/executions/${row.id}/monitor`)
}

function resize() { [trendChart, slaChart, slowChart].forEach(c => c && c.resize()) }
function disposeAll() {
  [trendChart, slaChart, slowChart].forEach(c => { if (c) { c.dispose(); c = null } })
}

async function loadAll() { await loadDashboard() }
onMounted(() => { loadProjects(); loadDashboard(); window.addEventListener('resize', resize) })
onUnmounted(() => { window.removeEventListener('resize', resize); disposeAll() })
</script>

<style lang="scss" scoped>
.perf-dashboard { padding: 16px; }
.dash-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
.dash-title { display: flex; align-items: center; font-size: 20px; font-weight: 600; color: #1f2d3d;
  .el-icon { margin-right: 8px; color: #1890ff; font-size: 24px; } }
.dash-actions { display: flex; align-items: center; gap: 10px; }
.stat-row { margin-bottom: 16px; }
.stat-card { border: none; border-radius: 10px; position: relative; overflow: hidden; text-align: center;
  &::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; background: #1890ff; }
  &.total::before { background: #1890ff; } &.exec::before { background: #13c2c2; }
  &.sla::before { background: #52c41a; } &.avg::before { background: #722ed1; } &.run::before { background: #faad14; }
  .sc-val { font-size: 26px; font-weight: 700; color: #1890ff; } &.exec .sc-val { color: #13c2c2; } &.sla .sc-val { color: #52c41a; } &.avg .sc-val { color: #722ed1; } &.run .sc-val { color: #faad14; }
  .sc-label { font-size: 13px; color: #8c8c8c; margin-top: 4px; }
}
.chart-card { margin-bottom: 16px; }
.chart-box { height: 260px; width: 100%; }
.mid-row { margin-bottom: 0; }
</style>
