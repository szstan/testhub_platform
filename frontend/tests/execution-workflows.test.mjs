import assert from 'node:assert/strict'
import { after, beforeEach, test } from 'node:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const bundleDir = await mkdtemp(join(frontend, 'node_modules', '.execution-api-test-'))
await build({
  absWorkingDir: frontend,
  stdin: {
    contents: `export * as executions from './src/api/executions.js';
      export * as contracts from './src/api/contracts.js';
      export { calls } from '@/utils/api';
      export { default as enExecution } from './src/locales/lang/en/execution.js';
      export { default as zhExecution } from './src/locales/lang/zh-cn/execution.js';
      export { default as enNav } from './src/locales/lang/en/nav.js';
      export { default as zhNav } from './src/locales/lang/zh-cn/nav.js';`,
    resolveDir: frontend,
  },
  bundle: true,
  platform: 'node',
  format: 'esm',
  outfile: join(bundleDir, 'workflows.mjs'),
  plugins: [{
    name: 'recording-api',
    setup(builder) {
      builder.onResolve({ filter: /^@\/utils\/api$/ }, () => ({ path: 'recording-api', namespace: 'workflow-test' }))
      builder.onLoad({ filter: /.*/, namespace: 'workflow-test' }, () => ({
        contents: `
          export const calls = [];
          const record = (method) => (url, dataOrConfig, config) => {
            calls.push({ method, url, data: method === 'get' || method === 'delete' ? undefined : dataOrConfig,
              params: method === 'get' ? dataOrConfig?.params : undefined });
            return Promise.resolve({ data: {} });
          };
          export default { get: record('get'), post: record('post'), patch: record('patch'), delete: record('delete') };
        `,
        loader: 'js',
      }))
    },
  }],
})
const { executions, contracts, calls, enExecution, zhExecution, enNav, zhNav } = await import(pathToFileURL(join(bundleDir, 'workflows.mjs')))
after(() => rm(bundleDir, { recursive: true, force: true }))
beforeEach(() => calls.splice(0))

test('OpenAPI contract wrappers cover CRUD, diff, coverage, preview, materialization and execution', async () => {
  await contracts.listOpenApiContractSpecs({ page: 2 })
  await contracts.getOpenApiContractSpec(12)
  await contracts.createOpenApiContractSpec({ project: 4, name: 'orders', version: 'v2', document: { paths: {} } })
  await contracts.updateOpenApiContractSpec(12, { version: 'v3' })
  await contracts.deleteOpenApiContractSpec(12)
  await contracts.diffOpenApiContractSpecs(12, 8)
  await contracts.getOpenApiContractCoverage(12)
  await contracts.listGeneratedOpenApiContractCases(12)
  await contracts.materializeOpenApiContractCases(12, { kinds: ['positive'], limit: 10 })
  await contracts.executeOpenApiContractSpec(12, { job_id: 22, headers: {} })

  assert.deepEqual(calls, [
    { method: 'get', url: '/api-testing/openapi-contracts/', data: undefined, params: { page: 2 } },
    { method: 'get', url: '/api-testing/openapi-contracts/12/', data: undefined, params: undefined },
    { method: 'post', url: '/api-testing/openapi-contracts/', data: { project: 4, name: 'orders', version: 'v2', document: { paths: {} } }, params: undefined },
    { method: 'patch', url: '/api-testing/openapi-contracts/12/', data: { version: 'v3' }, params: undefined },
    { method: 'delete', url: '/api-testing/openapi-contracts/12/', data: undefined, params: undefined },
    { method: 'post', url: '/api-testing/openapi-contracts/12/diff/', data: { baseline_id: 8 }, params: undefined },
    { method: 'get', url: '/api-testing/openapi-contracts/12/coverage/', data: undefined, params: undefined },
    { method: 'get', url: '/api-testing/openapi-contracts/12/generate-cases/', data: undefined, params: undefined },
    { method: 'post', url: '/api-testing/openapi-contracts/12/materialize-cases/', data: { kinds: ['positive'], limit: 10 }, params: undefined },
    { method: 'post', url: '/api-testing/openapi-contracts/12/execute/', data: { job_id: 22, headers: {} }, params: undefined },
  ])
})

test('execution governance and matrix wrappers use the scoped CRUD and audit endpoints', async () => {
  await executions.listExecutionRetryPolicies({ project: 3 })
  await executions.createExecutionRetryPolicy({ project: 3, max_attempts: 3 })
  await executions.updateExecutionRetryPolicy(5, { enabled: false })
  await executions.deleteExecutionRetryPolicy(5)
  await executions.listExecutionNotificationSubscriptions({ project: 3 })
  await executions.createExecutionNotificationSubscription({ project: 3, channel: 6, event_type: 'gate_failed' })
  await executions.updateExecutionNotificationSubscription(7, { enabled: false })
  await executions.listExecutionNotificationEvents({ status: 'failed' })
  await executions.listExecutionAISuggestions({ status: 'proposed' })
  await executions.approveExecutionAISuggestion(9, 'reviewed')
  await executions.rejectExecutionAISuggestion(10, 'not applicable')
  await executions.listExecutionAISuggestionAudits({ suggestion: 9 })
  await executions.listExecutionGateEvaluations({ job: 22 })
  await executions.listExecutionDataMatrices({ dataset: 4 })
  await executions.createExecutionDataMatrix({ dataset: 4, name: 'smoke', columns: [], rows: [] })
  await executions.updateExecutionDataMatrix(11, { enabled: false })
  await executions.deleteExecutionDataMatrix(11)
  await executions.listExecutionProjects()
  await executions.listExecutionNotificationChannels({ page_size: 100 })

  assert.deepEqual(calls.map(({ method, url }) => [method, url]), [
    ['get', '/executions/retry-policies/'],
    ['post', '/executions/retry-policies/'],
    ['patch', '/executions/retry-policies/5/'],
    ['delete', '/executions/retry-policies/5/'],
    ['get', '/executions/notification-subscriptions/'],
    ['post', '/executions/notification-subscriptions/'],
    ['patch', '/executions/notification-subscriptions/7/'],
    ['get', '/executions/notification-events/'],
    ['get', '/executions/ai-suggestions/'],
    ['post', '/executions/ai-suggestions/9/approve/'],
    ['post', '/executions/ai-suggestions/10/reject/'],
    ['get', '/executions/ai-suggestion-audits/'],
    ['get', '/executions/quality-gate-evaluations/'],
    ['get', '/executions/data-matrices/'],
    ['post', '/executions/data-matrices/'],
    ['patch', '/executions/data-matrices/11/'],
    ['delete', '/executions/data-matrices/11/'],
    ['get', '/projects/all/'],
    ['get', '/monitor/channels/'],
  ])
})

test('workflow SFCs compile and expose loading, empty, error and permission states', async () => {
  const files = [
    'src/views/api-testing/OpenApiContracts.vue',
    'src/views/executions/ExecutionGovernance.vue',
    'src/views/executions/DatasetsPlugins.vue',
  ]
  for (const relativePath of files) {
    const filename = join(frontend, relativePath)
    const source = await readFile(filename, 'utf8')
    const { descriptor, errors } = parse(source, { filename })
    assert.deepEqual(errors, [], `${relativePath} parses`)
    assert.ok(descriptor.scriptSetup, `${relativePath} has a script setup block`)
    compileScript(descriptor, { id: relativePath })
    const template = compileTemplate({ source: descriptor.template.content, filename, id: relativePath })
    assert.deepEqual(template.errors, [], `${relativePath} template compiles`)
    assert.match(source, /v-loading=/, `${relativePath} indicates loading state`)
    assert.match(source, /el-empty/, `${relativePath} exposes an empty state`)
    assert.match(source, /permissionDenied/, `${relativePath} tracks permission failures`)
    if (relativePath.endsWith('OpenApiContracts.vue')) {
      assert.match(source, /ensureCaseKindsSelected\(caseKinds\.value\)/, 'materialization rejects an empty case-kind selection')
      assert.match(source, /ensureCaseKindsSelected\(executeForm\.value\.kinds\)/, 'execution rejects an empty case-kind selection')
    }
  }
})

test('workflow labels exist in English and Chinese locale dictionaries', async () => {
  const files = [
    'src/views/api-testing/OpenApiContracts.vue',
    'src/views/executions/ExecutionGovernance.vue',
    'src/views/executions/DatasetsPlugins.vue',
  ]
  const references = new Set()
  for (const relativePath of files) {
    const source = await readFile(join(frontend, relativePath), 'utf8')
    for (const [, key] of source.matchAll(/[\x27\x22\x60](execution\.(?:contracts|governance|datasets)\.[^\x27\x22\x60]+)[\x27\x22\x60]/g)) references.add(key)
  }
  const variants = { kinds: ['positive', 'negative', 'security'], statuses: ['pending', 'dispatching', 'sent', 'failed'], suggestionStatuses: ['proposed', 'approved', 'rejected', 'expired'], notificationTypes: ['gate_failed', 'integration_failed', 'failure_reopened', 'quarantine_expiring'], stages: ['dispatch', 'result', 'release', 'webhook'] }
  const lookup = (locale, dottedPath) => dottedPath.split('.').reduce((value, part) => value?.[part], locale)
  for (const reference of references) {
    const match = reference.match(/^(execution\.(?:contracts|governance|datasets)\.\w+)\.\$\{[^}]+\}$/)
    const paths = match ? variants[match[1].split('.').at(-1)]?.map(value => `${match[1]}.${value}`) : [reference]
    assert.ok(paths, `known dynamic translation group: ${reference}`)
    for (const path of paths) {
      const localePath = path.slice('execution.'.length)
      assert.notEqual(lookup(enExecution, localePath), undefined, `English translation: ${path}`)
      assert.notEqual(lookup(zhExecution, localePath), undefined, `Chinese translation: ${path}`)
    }
  }
  assert.ok(enNav.menu.openapiContracts)
  assert.ok(zhNav.menu.openapiContracts)
  assert.ok(enNav.menu.executionGovernance)
  assert.ok(zhNav.menu.executionGovernance)
})

test('workflow pages are registered and discoverable without duplicating Tests-as-Code navigation', async () => {
  const router = await readFile(join(frontend, 'src/router/index.js'), 'utf8')
  const layout = await readFile(join(frontend, 'src/layout/index.vue'), 'utf8')
  const testcaseList = await readFile(join(frontend, 'src/views/testcases/TestCaseList.vue'), 'utf8')
  assert.match(router, /name: 'OpenApiContracts'/)
  assert.match(router, /name: 'ExecutionGovernance'/)
  assert.match(layout, /index="\/api-testing\/openapi-contracts"/)
  assert.match(layout, /index="\/ai-generation\/governance"/)
  assert.match(testcaseList, /TestCaseSync/)
  assert.equal((layout.match(/index="\/ai-generation\/testcases\/sync"/g) || []).length, 0)
})
