const reportPath = record => {
  if (!record?.id || !record.report_access_token) return ''
  return `/api/app-automation/executions/${record.id}/report/${encodeURIComponent(record.report_access_token)}/`
}

/** Open the tab during the click, then fetch a fresh authorized report token. */
export async function openAppExecutionReport(executionOrResolver, { fetchExecution, openWindow } = {}) {
  const open = openWindow || ((...args) => window.open(...args))
  let popup
  try {
    popup = open('about:blank', '_blank')
  } catch {
    return { opened: false, reason: 'blocked' }
  }
  if (!popup) return { opened: false, reason: 'blocked' }

  try {
    popup.opener = null
    const resolved = typeof executionOrResolver === 'function'
      ? await executionOrResolver()
      : executionOrResolver
    const id = resolved && typeof resolved === 'object' ? resolved.id : resolved
    if (!id || typeof fetchExecution !== 'function') {
      popup.close()
      return { opened: false, reason: 'unavailable' }
    }
    const response = await fetchExecution(id)
    const record = response?.data?.data ?? response?.data
    const url = String(record?.id) === String(id) ? reportPath(record) : ''
    if (!url) {
      popup.close()
      return { opened: false, reason: 'unavailable' }
    }
    popup.location.replace(url)
    return { opened: true }
  } catch {
    popup.close()
    return { opened: false, reason: 'failed' }
  }
}
