import { normalizeUiId } from './ui-project-context.mjs'

const REQUEST_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const ENGINES = new Set(['playwright', 'selenium'])
const BROWSERS = new Set(['chrome', 'chromium', 'firefox', 'edge', 'safari', 'webkit'])

export function newUiSubmissionId() {
  if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID()
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 15) | 64
  bytes[8] = (bytes[8] & 63) | 128
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export function uiPendingRunKey(kind, userId, projectId, resourceId) {
  const ids = [userId, projectId, resourceId].map(normalizeUiId)
  if (!['case', 'suite'].includes(kind) || ids.some(id => !id)) return ''
  return `testhub:ui-run:${kind}:${ids.join(':')}`
}

function safeRecord(record) {
  if (!record || !REQUEST_ID.test(record.client_request_id || '')) return null
  const result = { client_request_id: record.client_request_id }
  for (const key of ['execution_id', 'rerun_of']) {
    const id = normalizeUiId(record[key])
    if (id) result[key] = id
  }
  if (['case', 'suite'].includes(record.rerun_kind)) result.rerun_kind = record.rerun_kind
  if (ENGINES.has(record.engine)) result.engine = record.engine
  if (BROWSERS.has(record.browser)) result.browser = record.browser
  if (typeof record.headless === 'boolean') result.headless = record.headless
  return result
}

export function readUiPendingRun(key, storage) {
  if (!key) return null
  try { return safeRecord(JSON.parse((storage ?? globalThis.sessionStorage)?.getItem(key) || 'null')) } catch { return null }
}

export function writeUiPendingRun(key, record, storage) {
  const safe = safeRecord(record)
  if (!key || !safe) return
  try { (storage ?? globalThis.sessionStorage)?.setItem(key, JSON.stringify(safe)) } catch { /* Keep the in-memory identity. */ }
}

export function clearUiPendingRun(key, storage) {
  if (!key) return
  try { (storage ?? globalThis.sessionStorage)?.removeItem(key) } catch { /* Storage is optional. */ }
}

export function normalizeUiRunStatus(value) {
  const status = String(value || '').toLowerCase()
  return { success: 'passed', failure: 'failed', cancelled: 'aborted', canceled: 'aborted' }[status] || status
}

export function isUiSubmissionRejected(record) {
  return record?.accepted === false && ['preflight', 'dispatch', 'permission'].includes(record.failure_stage)
}

export function isUiRunActive(value) {
  return ['pending', 'running'].includes(normalizeUiRunStatus(value))
}
