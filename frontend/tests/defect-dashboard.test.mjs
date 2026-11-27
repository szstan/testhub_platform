import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { afterEach, test } from 'node:test'
import { baseParse } from '@vue/compiler-dom'
import { parse as parseSfc } from '@vue/compiler-sfc'
import { nextTick } from 'vue'
import { deferred, loadSfcSetup, plain } from './helpers/sfc-setup.mjs'

const fixtureSummary = {
  total: 23,
  closed: 5,
  resolved: 2,
  overdue: 1,
  status_distribution: [{ status: 'new', count: 16 }, { status: 'resolved', count: 2 }, { status: 'closed', count: 5 }],
  version_distribution: [
    { version_id: 123, version__name: 'v1.0', label: 'v1.0（项目甲 · #123）', count: 13 },
    { version_id: 124, version__name: 'v1.0', label: 'v1.0（项目乙 · #124）', count: 7 },
    { version__name: 'v0.9', count: 2 },
    { version__name: null, count: 1 }
  ],
  module_distribution: [...'abcdefghz'].map(module => ({ module, count: module === 'z' ? 15 : 1 })),
  assignee_distribution: [...'abcdefghz'].map(assignee => ({ assignee__username: assignee, count: assignee === 'z' ? 14 : 1 }))
    .concat({ assignee__username: null, count: 1 })
}
const fixtureTrend = [{ date: '2026-09-01', created: 2, closed: 1 }]
const instances = []
afterEach(() => instances.splice(0).forEach(instance => instance.dispose()))

async function setup({
  summary = async () => ({ data: fixtureSummary }),
  trend = async () => ({ data: fixtureTrend })
} = {}) {
  const charts = []
  const trendParams = []
  const instance = await loadSfcSetup('src/views/defects/DefectDashboard.vue', {
    'vue-router': { useRouter: () => ({}) },
    '@element-plus/icons-vue': { Plus: {}, Refresh: {} },
    'echarts/core': {
      use() {},
      init(element) {
        const chart = {
          element,
          options: [],
          disposed: false,
          setOption(option) { this.options.push(plain(option)) },
          resize() {},
          dispose() { this.disposed = true }
        }
        charts.push(chart)
        return chart
      }
    },
    'echarts/charts': { BarChart: {}, LineChart: {}, PieChart: {} },
    'echarts/components': { GridComponent: {}, LegendComponent: {}, TooltipComponent: {} },
    'echarts/renderers': { CanvasRenderer: {} },
    '@/api/defects': {
      getDefectSummary: summary,
      getDefectTrend(params) {
        trendParams.push(plain(params))
        return trend()
      }
    },
    './options': { getOptionLabel: (options, key) => key, severityOptions: [], statusOptions: [] }
  }, { window: { addEventListener() {}, removeEventListener() {} } })
  instances.push(instance)
  return { ...instance, charts, trendParams }
}

async function setupReport({ summary = fixtureSummary, trend = fixtureTrend } = {}) {
  const instance = await loadSfcSetup('src/views/defects/DefectReport.vue', {
    '@element-plus/icons-vue': { Download: {}, Refresh: {} },
    'echarts/core': { use() {}, init() { return { setOption() {}, resize() {}, dispose() {} } } },
    'echarts/charts': { BarChart: {}, LineChart: {}, PieChart: {} },
    'echarts/components': { GridComponent: {}, LegendComponent: {}, TooltipComponent: {} },
    'echarts/renderers': { CanvasRenderer: {} },
    dayjs: { default: () => ({ format: () => '20260901000000' }) },
    '@/api/defects': {
      getDefectSummary: async () => ({ data: summary }),
      getDefectTrend: async () => ({ data: trend }),
      exportDefectReport: async () => ({ data: new Uint8Array() })
    },
    './options': { getOptionLabel: (options, key) => key, priorityOptions: [], severityOptions: [], statusOptions: [] }
  }, { window: { addEventListener() {}, removeEventListener() {} } })
  instances.push(instance)
  return instance
}

const flushRequests = () => new Promise(resolve => setImmediate(resolve))

test('the dashboard ranks modules and assignees by count, and switches the chart to version data', async () => {
  const { state, mount, charts } = await setup()
  await mount()
  state.distributionChartRef.value = { id: 'distribution' }
  state.assigneeChartRef.value = { id: 'assignee' }
  await nextTick()

  const distribution = charts.find(chart => chart.element.id === 'distribution')
  const assignee = charts.find(chart => chart.element.id === 'assignee')
  assert.equal(distribution.options.at(-1).yAxis.data[0], 'z')
  assert.equal(distribution.options.at(-1).yAxis.data.length, 8)
  assert.equal(distribution.options.at(-1).series[0].data[0], 15)
  assert.equal(assignee.options.at(-1).yAxis.data[0], 'z')
  assert.equal(assignee.options.at(-1).yAxis.data.length, 8)
  assert.equal(assignee.options.at(-1).series[0].data[0], 14)

  state.distributionDimension.value = 'version'
  await nextTick()
  assert.deepEqual(distribution.options.at(-1).yAxis.data, [
    'v1.0（项目甲 · #123）',
    'v1.0（项目乙 · #124）',
    'v0.9',
    '未设置版本'
  ])
  assert.deepEqual(distribution.options.at(-1).series[0].data, [13, 7, 2, 1])
  assert.equal(charts.length, 2)

  state.distributionChartRef.value = null
  await nextTick()
  assert.equal(distribution.disposed, true)
})

test('the initial summary failure leaves list navigation in the page header', async () => {
  const { state, mount } = await setup({ summary: async () => { throw new Error('Summary unavailable') } })
  await mount()
  assert.equal(state.summaryPanel.data, null)

  const source = await readFile(new URL('../src/views/defects/DefectDashboard.vue', import.meta.url), 'utf8')
  const { descriptor, errors } = parseSfc(source)
  assert.deepEqual(errors, [])
  const template = baseParse(descriptor.template.content)
  const matches = []
  const visit = (node, ancestors = []) => {
    if (node.type === 1) {
      const nextAncestors = [...ancestors, node]
      if (node.tag === 'el-button' && node.children.some(child => child.type === 2 && child.content.trim() === '查看列表')) {
        matches.push({ node, ancestors })
      }
      node.children.forEach(child => visit(child, nextAncestors))
    } else if (node.children) {
      node.children.forEach(child => visit(child, ancestors))
    }
  }
  visit(template)

  assert.equal(matches.length, 1)
  const { node, ancestors } = matches[0]
  const parent = ancestors.at(-1)
  assert.equal(parent.tag, 'div')
  assert.ok(parent.props.some(prop => prop.type === 6 && prop.name === 'class' && prop.value?.content === 'header-actions'))
  assert.ok(ancestors.some(element => element.tag === 'div' && element.props.some(
    prop => prop.type === 6 && prop.name === 'class' && prop.value?.content === 'page-header'
  )))
  assert.ok(!ancestors.some(element => element.props.some(
    prop => prop.type === 7 && prop.name === 'if' && prop.exp?.content === 'summaryPanel.data'
  )))
  assert.ok(node.props.some(prop => prop.type === 7 && prop.name === 'on' && prop.arg?.content === 'click' && prop.exp?.content === "router.push('/defects/list')"))
})

test('the report keeps duplicate version rows, their labels and counts, and legacy names', async () => {
  const { state } = await setupReport()
  await state.loadReport()

  assert.deepEqual(plain(state.versionRows.value).map(({ label, value }) => [label, value]), [
    ['v1.0（项目甲 · #123）', 13],
    ['v1.0（项目乙 · #124）', 7],
    ['v0.9', 2],
    ['未设置', 1]
  ])
})

test('the report ranks version IDs by defect count before limiting the chart to eight rows', async () => {
  const earlierVersions = Array.from({ length: 7 }, (_, index) => ({
    version_id: index + 1, version__name: `v0.${index + 1}`, count: 1
  }))
  const duplicateNameRows = [
    { version_id: 8, version__name: 'v1.0', label: 'v1.0（项目甲 · #8）', count: 2 },
    { version_id: 9, version__name: 'v1.0', label: 'v1.0（项目乙 · #9）', count: 31 }
  ]
  const { state } = await setupReport({
    summary: { ...fixtureSummary, version_distribution: [...earlierVersions, ...duplicateNameRows] }
  })
  await state.loadReport()

  const ranked = plain(state.versionRows.value)
  assert.equal(ranked.length, 8)
  assert.deepEqual(ranked.slice(0, 2).map(({ label, value }) => [label, value]), [
    ['v1.0（项目乙 · #9）', 31],
    ['v1.0（项目甲 · #8）', 2]
  ])
})

test('dashboard module labels remain sourced from modules, not version labels', async () => {
  const summary = {
    ...fixtureSummary,
    module_distribution: [{ module: 'Payments', label: 'Not a module name', count: 3 }]
  }
  const { state } = await setup({ summary: async () => ({ data: summary }) })
  await state.loadDashboard()
  state.distributionDimension.value = 'module'
  assert.deepEqual(plain(state.dimensionRows.value), [{ label: 'Payments', value: 3 }])
})

test('summary renders immediately and stays available when the slower trend request fails', async () => {
  const trend = deferred()
  const { state, mount, trendParams } = await setup({ trend: () => trend.promise })
  const initial = mount()
  await flushRequests()

  assert.equal(state.summaryPanel.data.total, 23)
  assert.equal(state.summaryPanel.loading, false)
  assert.equal(state.trendPanel.loading, true)
  assert.equal(state.summaryNotice.value, '')
  assert.deepEqual(trendParams, [{ days: 14 }])
  const metrics = plain(state.metrics.value).map(({ label, value }) => [label, value])
  assert.ok(metrics.some(row => row[0] === '未关闭' && row[1] === 18))
  assert.ok(metrics.some(row => row[0] === '逾期未关闭' && row[1] === 1))

  trend.reject(new Error('Trend unavailable'))
  await initial
  assert.equal(state.loading.value, false)
  assert.equal(state.summaryPanel.data.total, 23)
  assert.equal(state.trendPanel.data, null)
  assert.match(state.trendNotice.value, /加载失败/)
})

test('a successful trend is displayed even when the summary request fails', async () => {
  const { state, mount } = await setup({ summary: async () => { throw new Error('Summary unavailable') } })
  await mount()
  assert.equal(state.summaryPanel.data, null)
  assert.match(state.summaryNotice.value, /加载失败/)
  assert.equal(state.trendNotice.value, '')
  assert.deepEqual(plain(state.trendRows.value), fixtureTrend)
})

test('a failed refresh labels retained data as stale and a later success clears the warning', async () => {
  let summaryResult = fixtureSummary
  let trendResult = fixtureTrend
  const { state, mount } = await setup({
    summary: async () => {
      if (!summaryResult) throw new Error('Summary unavailable')
      return { data: summaryResult }
    },
    trend: async () => {
      if (!trendResult) throw new Error('Trend unavailable')
      return { data: trendResult }
    }
  })
  await mount()

  summaryResult = null
  trendResult = null
  await state.loadDashboard()
  assert.equal(state.summaryPanel.data.total, 23)
  assert.deepEqual(plain(state.trendRows.value), fixtureTrend)
  assert.match(state.summaryNotice.value, /上次成功加载/)
  assert.match(state.trendNotice.value, /上次成功加载/)

  summaryResult = { total: 0, closed: 0 }
  trendResult = []
  await state.loadDashboard()
  assert.equal(state.summaryNotice.value, '')
  assert.equal(state.trendNotice.value, '')
  assert.equal(state.summaryPanel.data.total, 0)
  assert.deepEqual(plain(state.trendPanel.data), [])
})

test('unexpected successful responses do not become all-zero metrics or an empty trend', async () => {
  const { state, mount } = await setup({
    summary: async () => ({ data: '<html>Unexpected document</html>' }),
    trend: async () => ({ data: {} })
  })
  await mount()
  assert.equal(state.summaryPanel.data, null)
  assert.equal(state.trendPanel.data, null)
  assert.match(state.summaryNotice.value, /加载失败/)
  assert.match(state.trendNotice.value, /加载失败/)
})
