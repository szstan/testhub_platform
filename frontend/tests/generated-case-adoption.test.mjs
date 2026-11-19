import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildCaseAdoptionPayload,
  chunkCaseIds,
  normalizeCanonicalCases,
  serializeCaseTable
} from '../src/utils/generated-case-adoption.mjs'

test('maps canonical API cases without deriving IDs from Markdown or titles', () => {
  assert.deepEqual(normalizeCanonicalCases([
    {
      case_id: 'AI-009',
      title: 'Login succeeds',
      priority: 'P1',
      preconditions: 'A user exists.',
      steps: ['Enter credentials.', 'Submit.'],
      expected_result: 'The dashboard appears.'
    },
    { title: 'Missing canonical ID' }
  ]), [{
    caseId: 'AI-009',
    scenario: 'Login succeeds',
    priority: 'P1',
    precondition: 'A user exists.',
    steps: 'Enter credentials.\nSubmit.',
    expected: 'The dashboard appears.'
  }])
})

test('splits selected IDs into stable batches at the API limit', () => {
  const ids = Array.from({ length: 405 }, (_, index) => `AI-${index + 1}`)
  const chunks = chunkCaseIds([...ids, ids[0], '', '  '])

  assert.deepEqual(chunks.map(chunk => chunk.length), [200, 200, 5])
  assert.deepEqual(chunks.flat(), ids)
})

test('serializes edited cases as a parseable Markdown table', () => {
  const table = serializeCaseTable(
    ['用例ID', '测试目标', '前置条件', '测试步骤', '预期结果', '优先级'],
    [['AI-001', 'Login | succeeds', 'A user exists.', 'Enter credentials.\nSubmit.', 'Dashboard appears.', 'P1']]
  )

  assert.equal(table, [
    '用例ID | 测试目标 | 前置条件 | 测试步骤 | 预期结果 | 优先级',
    '--- | --- | --- | --- | --- | ---',
    'AI-001 | Login \\| succeeds | A user exists. | Enter credentials.<br>Submit. | Dashboard appears. | P1',
    ''
  ].join('\n'))
})
