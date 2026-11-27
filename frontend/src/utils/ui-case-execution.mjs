import { normalizeUiId } from './ui-project-context.mjs'

export function readCaseExecutionLog(value) {
  if (!value) return { steps: [], text: '' }
  if (typeof value === 'string') {
    try {
      return readCaseExecutionLog(JSON.parse(value))
    } catch {
      return { steps: [], text: value }
    }
  }
  const steps = Array.isArray(value) ? value : value.steps
  return { steps: Array.isArray(steps) ? steps.filter(step => step && typeof step === 'object') : [], text: '' }
}

export function caseExecutionFailure(execution, log) {
  if (!['failed', 'error'].includes(execution?.status)) return null
  const firstStep = log.steps.find(step => step.success === false || step.status === 'failed' || step.status === 'error')
  const stepNumber = normalizeUiId(firstStep?.step_number) || null
  const message = [firstStep?.error, execution.error_message, log.text]
    .find(text => typeof text === 'string' && text.trim()) || ''
  const screenshots = Array.isArray(execution.screenshots) ? execution.screenshots : []
  let screenshotIndex = stepNumber
    ? screenshots.findIndex(item => item?.url && normalizeUiId(item.step_number) === stepNumber)
    : -1
  if (screenshotIndex < 0) screenshotIndex = screenshots.findIndex(item => item?.url)
  return { stepNumber, message, screenshotIndex }
}
