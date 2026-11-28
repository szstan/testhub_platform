import { normalizeUiId } from './ui-project-context.mjs'
import { clearUiPendingRun, isUiRunActive, isUiSubmissionRejected, newUiSubmissionId, normalizeUiRunStatus, readUiPendingRun, uiPendingRunKey, writeUiPendingRun } from './ui-execution-submission.mjs'

const terminal = new Set(['passed', 'failed', 'error', 'aborted'])
const message = error => error?.response?.data?.message || error?.response?.data?.error || error?.response?.data?.detail || error?.message || ''
const refusalResult = data => ({ ...data, success: false, status: normalizeUiRunStatus(data.status) || 'error',
  logs: data.logs || data.message || data.error || data.detail || '',
  errors: data.errors?.length ? data.errors : [{ message: data.message || data.error || data.detail || '' }] })

export function createUiCaseRunController(options) {
  const wrap = options.reactive || (value => value)
  const runs = wrap(new Map())
  let version = 0
  let disposed = false
  const key = (project, caseId) => JSON.stringify([String(options.actorId?.() || ''), String(project), normalizeUiId(caseId)])
  const storage = () => { try { return options.storage?.() } catch { return undefined } }
  const current = run => !disposed && run.version === version && run.actor === options.actorId?.() && run.project === options.projectId() && runs.get(run.key) === run
  const stopTimer = run => {
    if (run.timer !== null) options.clearTimer?.(run.timer)
    run.timer = null
  }
  const persist = run => writeUiPendingRun(run.storageKey, { ...run.options, client_request_id: run.requestId, execution_id: run.executionId }, storage())
  const clearPersisted = run => {
    const saved = readUiPendingRun(run.storageKey, storage())
    if (!saved || saved.client_request_id === run.requestId) clearUiPendingRun(run.storageKey, storage())
  }
  const finish = (run, result) => {
    if (!current(run)) return
    stopTimer(run)
    clearPersisted(run)
    options.onResult(run.project, run.caseId, result)
    runs.delete(run.key)
  }
  const makeRun = (caseId, config, saved = null) => {
    const project = options.projectId()
    const actor = options.actorId?.()
    const run = wrap({ key: key(project, caseId), project, caseId: normalizeUiId(caseId), actor, version,
      storageKey: uiPendingRunKey('case', actor, project, caseId),
      requestId: saved?.client_request_id || newUiSubmissionId(),
      executionId: normalizeUiId(saved?.execution_id), options: { ...config, ...saved },
      phase: 'submitting', status: 'pending', error: '', timer: null, io: false, cancelling: false })
    delete run.options.execution_id
    delete run.options.client_request_id
    runs.set(run.key, run)
    persist(run)
    return run
  }
  const schedule = run => {
    if (!current(run)) return
    stopTimer(run)
    if (options.hidden?.()) { run.phase = 'paused'; return }
    run.timer = options.schedule(() => poll(run), 3000)
  }
  const applyRecord = (run, record) => {
    const project = normalizeUiId(record?.project?.id ?? record?.project)
    const caseId = normalizeUiId(record?.test_case?.id ?? record?.test_case)
    if (project !== run.project || normalizeUiId(record?.id) !== run.executionId || (caseId && caseId !== run.caseId)) {
      finish(run, { success: false, status: 'error', failure_stage: 'access', logs: 'The execution is unavailable for this project or case.', errors: [] })
      return
    }
    run.status = normalizeUiRunStatus(record.status)
    if (terminal.has(run.status)) {
      finish(run, { ...record, success: run.status === 'passed', status: run.status, execution_id: record.id,
        logs: record.execution_logs || '', errors: record.error_message ? [{ message: record.error_message }] : [] })
    } else if (isUiRunActive(run.status)) {
      run.phase = 'polling'
      run.error = ''
      schedule(run)
    } else {
      run.phase = 'paused'
      run.error = 'The execution service returned an unknown status.'
    }
  }
  async function poll(run) {
    if (!current(run) || run.io || !run.executionId) return
    stopTimer(run)
    if (options.hidden?.()) { run.phase = 'paused'; return }
    run.io = true
    try {
      const { data } = await options.detail(run.executionId)
      if (current(run)) applyRecord(run, data)
    } catch (error) {
      if (!current(run)) return
      if ([403, 404].includes(error.response?.status)) {
        finish(run, { success: false, status: 'error', failure_stage: 'access', logs: message(error), errors: [{ message: message(error) }] })
      } else {
        run.phase = 'paused'
        run.error = message(error)
      }
    } finally { if (current(run)) run.io = false }
  }
  async function lookup(run, resubmit = false) {
    if (!current(run) || run.io) return
    const saved = readUiPendingRun(run.storageKey, storage())
    if (saved?.client_request_id === run.requestId && saved.execution_id) run.executionId = saved.execution_id
    if (run.executionId) return poll(run)
    run.io = true
    run.phase = 'recovering'
    try {
      const { data } = await options.lookup(run.caseId, { client_request_id: run.requestId })
      if (!current(run)) return
      const id = normalizeUiId(data?.execution_id)
      if (!id) throw new Error('The submission identity could not be recovered.')
      run.executionId = id
      persist(run)
      run.io = false
      await poll(run)
    } catch (error) {
      if (!current(run)) return
      run.io = false
      if (resubmit && error.response?.status === 404) return submit(run)
      run.phase = 'unknown'
      run.error = message(error)
    } finally { if (current(run)) run.io = false }
  }
  async function submit(run) {
    if (!current(run) || run.io) return
    run.io = true
    run.phase = 'submitting'
    persist(run)
    try {
      const { data } = await options.submit(run.caseId, { ...run.options, project_id: run.project, client_request_id: run.requestId })
      const id = normalizeUiId(data?.execution_id)
      if (id) {
        run.executionId = id
        const saved = readUiPendingRun(run.storageKey, storage())
        // A response after navigation may enrich only its own still-pending submission.
        if (current(run) || saved?.client_request_id === run.requestId) persist(run)
      }
      if (!current(run)) return
      if (isUiSubmissionRejected(data)) {
        finish(run, refusalResult(data))
      } else if (data?.accepted === true || isUiRunActive(data?.status) || (id && typeof data?.success !== 'boolean')) {
        if (!id) throw new Error('The execution service did not return an execution ID.')
        run.io = false
        await poll(run)
      } else if (typeof data?.success === 'boolean') {
        finish(run, data)
      } else throw new Error('The submission result is unknown.')
    } catch (error) {
      if (!current(run)) return
      const data = error.response?.data || {}
      const id = normalizeUiId(data.execution_id)
      run.io = false
      if (isUiSubmissionRejected(data)) {
        finish(run, refusalResult(data))
      } else if (id) {
        run.executionId = id
        persist(run)
        await poll(run)
      } else if (error.response && error.response.status < 500) {
        finish(run, { ...data, success: false, failure_stage: data.failure_stage || (error.response.status === 403 ? 'permission' : 'preflight'),
          logs: data.logs || message(error), errors: data.errors?.length ? data.errors : [{ message: message(error) }] })
      } else {
        run.phase = 'unknown'
        run.error = message(error)
        await lookup(run)
      }
    } finally { if (current(run)) run.io = false }
  }
  function currentRun(caseId) {
    const run = runs.get(key(options.projectId(), caseId))
    return run && current(run) ? run : null
  }
  function pendingRecord(caseId) {
    const saved = readUiPendingRun(uiPendingRunKey('case', options.actorId?.(), options.projectId(), caseId), storage())
    if (saved) return saved
    const old = runs.get(key(options.projectId(), caseId))
    if (old && old.actor === options.actorId?.() && old.project === options.projectId()) {
      return { ...old.options, client_request_id: old.requestId, execution_id: old.executionId }
    }
    return null
  }
  async function restore(caseId) {
    if (!normalizeUiId(caseId) || !normalizeUiId(options.projectId())) return
    const existing = currentRun(caseId)
    if (existing) return existing
    const saved = pendingRecord(caseId)
    if (!saved) return
    const run = makeRun(caseId, {}, saved)
    await lookup(run)
    return run
  }
  async function start(caseId, config) {
    if (disposed || currentRun(caseId) || !normalizeUiId(caseId) || !normalizeUiId(options.projectId())) return
    const saved = pendingRecord(caseId)
    const run = makeRun(caseId, config, saved)
    if (saved) await lookup(run)
    else await submit(run)
    return run
  }
  async function cancel(caseId) {
    const run = currentRun(caseId)
    if (!run?.executionId || run.cancelling) return
    run.cancelling = true
    try {
      await options.abort(run.executionId)
      if (current(run)) await poll(run)
    } catch (error) {
      if (current(run)) { run.phase = 'paused'; run.error = message(error) }
    } finally { if (current(run)) run.cancelling = false }
  }
  const resetScope = () => { version += 1; for (const run of runs.values()) stopTimer(run) }
  return { runs, key, currentRun, start, restore, cancel,
    resume(caseId) { const run = currentRun(caseId); return run ? lookup(run) : restore(caseId) },
    retry(caseId) { const run = currentRun(caseId); return run ? lookup(run, true) : restore(caseId) },
    visible() { for (const run of runs.values()) if (current(run)) { if (options.hidden?.()) { stopTimer(run); run.phase = 'paused' } else void lookup(run) } },
    resetScope,
    dispose() { resetScope(); disposed = true; runs.clear() },
  }
}
