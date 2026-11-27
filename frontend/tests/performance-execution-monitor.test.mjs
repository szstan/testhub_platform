import assert from 'node:assert/strict'
import { test } from 'node:test'
import { deferred, loadSfcSetup } from './helpers/sfc-setup.mjs'

const monitorPath = 'src/views/performance-testing/ExecutionMonitor.vue'

async function setupFinishedMonitor(execution, getPerfRealtime) {
  const logRequests = []
  const view = await loadSfcSetup(monitorPath, {
    'vue-router': {
      useRoute: () => ({ params: { id: '12' }, hash: '#execution-log' }),
      useRouter: () => ({}),
    },
    '@element-plus/icons-vue': {
      ArrowLeft: {}, VideoPause: {}, Document: {}, Refresh: {}, CircleCheck: {}, CircleClose: {},
    },
    './components/MetricCards.vue': { default: {} },
    './components/RealtimeChart.vue': { default: {} },
    './shared': {
      statusTagType: () => 'danger', slaTagType: () => 'info',
      formatDuration: value => String(value), apiError: error => error.message,
    },
    '@/api/performance-testing': {
      getPerfExecution: async () => ({ data: execution }),
      getPerfRealtime,
      getPerfRunLog: async id => {
        logRequests.push(id)
        return { data: { content: 'Worker startup failed in test fixture' } }
      },
      getPerfRequestStats: async () => ({ data: [] }),
      getEngineStatus: async () => assert.fail('Finished executions do not open a live channel'),
      stopPerfExecution: async () => {},
      getPerfScenario: async () => ({}),
    },
    '@/utils/websocket': { buildWebSocketUrl: () => '', issueWebSocketTicket: async () => '' },
  })
  return { ...view, logRequests }
}

test('a failed execution still loads and focuses its log when historical samples fail', async () => {
  const focusCalls = []
  const view = await setupFinishedMonitor(
    { status: 'FAILED', error_message: 'Worker startup failed' },
    async () => { throw new Error('Historical samples unavailable') },
  )
  try {
    view.state.logPanel.value = {
      scrollIntoView: () => focusCalls.push('scroll'),
      focus: () => focusCalls.push('focus'),
    }
    await view.mount()

    assert.deepEqual(view.logRequests, [12])
    assert.equal(view.state.runLog.value, 'Worker startup failed in test fixture')
    assert.equal(view.state.execution.error_message, 'Worker startup failed')
    assert.equal(view.state.channel.value, 'closed')
    assert.equal(view.state.loading.value, false)
    assert.deepEqual(focusCalls, ['scroll', 'focus'])
  } finally {
    view.dispose()
  }
})

test('late historical samples update the chart without rolling back terminal progress', { timeout: 3000 }, async () => {
  const replay = deferred()
  const chartSample = deferred()
  const view = await setupFinishedMonitor(
    { status: 'STOPPED', load_snapshot: { duration: 300 } },
    () => replay.promise,
  )
  try {
    view.state.chartRef.value = { push: chartSample.resolve }
    await view.mount()
    assert.equal(view.state.progress.value, 100)
    assert.equal(view.state.loading.value, false)

    const sample = { ts_offset: 60, tps: 15 }
    replay.resolve({ data: { samples: [sample] } })
    assert.deepEqual(await chartSample.promise, sample)
    assert.equal(view.state.latest.value.ts_offset, 60)
    assert.equal(view.state.progress.value, 100)
  } finally {
    view.dispose()
  }
})
