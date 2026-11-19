export const CASE_ADOPTION_BATCH_SIZE = 200

export function chunkCaseIds(caseIds, chunkSize = CASE_ADOPTION_BATCH_SIZE) {
  const normalizedIds = [...new Set(
    (Array.isArray(caseIds) ? caseIds : [])
      .filter(caseId => typeof caseId === 'string' && caseId.trim())
      .map(caseId => caseId.trim())
  )]
  const size = Number.isInteger(chunkSize) && chunkSize > 0 ? chunkSize : CASE_ADOPTION_BATCH_SIZE
  const chunks = []
  for (let offset = 0; offset < normalizedIds.length; offset += size) {
    chunks.push(normalizedIds.slice(offset, offset + size))
  }
  return chunks
}

export function serializeCaseTable(headers, rows) {
  if (!Array.isArray(headers) || headers.length === 0) return ''
  const serializeCell = value => String(value ?? '').replace(/\r?\n/g, '<br>').replace(/\|/g, '\\|')
  const lines = [
    headers.map(serializeCell).join(' | '),
    headers.map(() => '---').join(' | ')
  ]
  for (const row of Array.isArray(rows) ? rows : []) {
    lines.push((Array.isArray(row) ? row : []).map(serializeCell).join(' | '))
  }
  return `${lines.join('\n')}\n`
}

export function normalizeCanonicalCases(cases) {
  if (!Array.isArray(cases)) return []

  return cases
    .filter(testCase => testCase && typeof testCase.case_id === 'string' && testCase.case_id.trim())
    .map(testCase => ({
      caseId: testCase.case_id,
      scenario: testCase.title,
      priority: testCase.priority || 'P2',
      precondition: testCase.preconditions || '',
      steps: Array.isArray(testCase.steps) ? testCase.steps.join('\n') : '',
      expected: testCase.expected_result || ''
    }))
}

export function buildCaseAdoptionPayload(caseIds, projectId) {
  const payload = {
    case_ids: [...new Set(
      (Array.isArray(caseIds) ? caseIds : [])
        .filter(caseId => typeof caseId === 'string' && caseId.trim())
    )]
  }
  if (projectId !== undefined && projectId !== null && projectId !== '') {
    payload.project_id = projectId
  }
  return payload
}
