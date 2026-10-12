<template>
  <div ref="chartRef" class="realtime-chart" :style="{ height: height + 'px' }"></div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import * as echarts from 'echarts'

const props = defineProps({
  height: { type: Number, default: 320 },
  maxPoints: { type: Number, default: 300 }
})

const chartRef = ref(null)
let chart = null
const times = []
const tps = []
const avgRt = []
const p95Rt = []
const errorRate = []
const activeUsers = []

function ensureChart() {
  if (!chartRef.value) return
  if (!chart) chart = echarts.init(chartRef.value)
  chart.setOption({
    tooltip: { trigger: 'axis' },
    legend: {
      data: ['TPS', '平均RT', 'P95', '错误率', '并发'],
      bottom: 0,
      textStyle: { fontSize: 11 }
    },
    grid: { left: '3%', right: '4%', bottom: '14%', top: '8%', containLabel: true },
    xAxis: { type: 'category', boundaryGap: false, data: times, axisLabel: { fontSize: 10 } },
    yAxis: [
      { type: 'value', name: 'TPS', axisLabel: { fontSize: 10 }, splitLine: { lineStyle: { color: '#f0f0f0' } } },
      { type: 'value', name: 'RT(ms)', position: 'right', axisLabel: { fontSize: 10 } },
      { type: 'value', name: 'err%', show: false, max: 100 }
    ],
    series: [
      { name: 'TPS', type: 'line', smooth: true, showSymbol: false, data: tps, itemStyle: { color: '#1890ff' } },
      { name: '平均RT', type: 'line', smooth: true, showSymbol: false, yAxisIndex: 1, data: avgRt, itemStyle: { color: '#52c41a' } },
      { name: 'P95', type: 'line', smooth: true, showSymbol: false, yAxisIndex: 1, data: p95Rt, lineStyle: { type: 'dashed' }, itemStyle: { color: '#722ed1' } },
      { name: '错误率', type: 'line', smooth: true, showSymbol: false, yAxisIndex: 2, data: errorRate, itemStyle: { color: '#ff4d4f' }, areaStyle: { opacity: 0.12 } },
      { name: '并发', type: 'line', smooth: true, showSymbol: false, data: activeUsers, itemStyle: { color: '#faad14' } }
    ]
  })
}

function push(sample) {
  if (!sample) return
  times.push(formatOffset(sample.ts_offset))
  tps.push(round(sample.tps))
  avgRt.push(round(sample.avg_rt))
  p95Rt.push(round(sample.p95_rt))
  errorRate.push(round(sample.error_rate))
  activeUsers.push(sample.active_users || 0)
  if (times.length > props.maxPoints) {
    times.shift(); tps.shift(); avgRt.shift(); p95Rt.shift(); errorRate.shift(); activeUsers.shift()
  }
  if (!chart) ensureChart()
  if (chart) {
    chart.setOption({
      xAxis: { data: times },
      series: [
        { data: tps }, { data: avgRt }, { data: p95Rt }, { data: errorRate }, { data: activeUsers }
      ]
    })
  }
}

function reset() {
  times.length = 0; tps.length = 0; avgRt.length = 0; p95Rt.length = 0
  errorRate.length = 0; activeUsers.length = 0
  if (chart) chart.clear()
}

function formatOffset(sec) {
  if (sec === undefined || sec === null) return ''
  const s = Number(sec)
  const m = Math.floor(s / 60)
  const r = s % 60
  return m > 0 ? `${m}m${String(r).padStart(2, '0')}s` : `${r}s`
}
function round(v) {
  return v === undefined || v === null ? 0 : Math.round(Number(v) * 100) / 100
}
function resize() { if (chart) chart.resize() }

onMounted(() => { nextTick(ensureChart); window.addEventListener('resize', resize) })
onUnmounted(() => {
  window.removeEventListener('resize', resize)
  if (chart) { chart.dispose(); chart = null }
})

defineExpose({ push, reset })
</script>

<style lang="scss" scoped>
.realtime-chart {
  width: 100%;
}
</style>
