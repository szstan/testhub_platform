export function normalizeUiId(value) {
  if (typeof value === 'string' && !/^\d+$/.test(value)) return ''
  if (typeof value !== 'number' && typeof value !== 'string') return ''
  const id = Number(value)
  return Number.isSafeInteger(id) && id > 0 ? id : ''
}

export function selectUiProject(projects, requested, saved) {
  const hasProject = id => Boolean(id && projects.some(project => project.id === id))
  if (requested !== undefined) {
    const id = normalizeUiId(requested)
    return hasProject(id) ? { id, invalid: false } : { id: '', invalid: true }
  }
  const remembered = normalizeUiId(saved)
  if (hasProject(remembered)) return { id: remembered, invalid: false }
  return { id: projects.length === 1 ? projects[0].id : '', invalid: false }
}

export function uiExecutionLocation(name, project, executionId) {
  return { name, query: { project: String(project), executionId: String(executionId) } }
}

const workbenchRoutes = new Set(['UiDashboard', 'UiProjects', 'UiTestCases', 'UiSuites', 'UiExecutions', 'UiReports', 'UiElementsEnhanced'])

export function uiWorkbenchReturnLocation(name, params) {
  if (!workbenchRoutes.has(name)) return null
  const query = {}
  for (const key of ['project', 'executionId', 'caseId']) {
    const id = normalizeUiId(params[key])
    if (id) query[key] = String(id)
  }
  if ((name === 'UiProjects' && params.setup === 'project') || (name === 'UiTestCases' && params.setup === 'case')) {
    query.setup = params.setup
  }
  return { name, query }
}

export async function loadUiProjectOptions(requestPage) {
  const projects = new Map()
  const pageSize = 100
  for (let page = 1; ; page += 1) {
    const { data } = await requestPage({ page, page_size: pageSize })
    const rows = Array.isArray(data) ? data : data?.results
    if (!Array.isArray(rows)) throw new Error('Invalid UI project response')
    const previousSize = projects.size
    for (const row of rows) {
      const id = normalizeUiId(row?.id)
      if (id) projects.set(id, { ...row, id })
    }
    if (!data.next) return [...projects.values()]
    if (projects.size === previousSize) throw new Error('UI project pagination did not advance')
  }
}
