import request from '../utils/api'

const PAGE_SIZE = 100
const MAX_ITEMS = 1000
const MAX_PAGES = Math.ceil(MAX_ITEMS / PAGE_SIZE)

async function getAllPages(url, project) {
  const rows = []
  let expectedCount
  for (let page = 1; page <= MAX_PAGES; page += 1) {
    const response = await request({
      url, method: 'get', params: { project, page, page_size: PAGE_SIZE },
    })
    const body = response?.data ?? response
    if (Array.isArray(body)) {
      if (body.length > MAX_ITEMS) throw new Error('Scene data page limit exceeded')
      return body
    }
    if (!Array.isArray(body?.results)) {
      if (Array.isArray(body?.data) && body.count === undefined && body.next === undefined) {
        if (body.data.length > MAX_ITEMS) throw new Error('Scene data page limit exceeded')
        return body.data
      }
      throw new Error('Invalid scene data page')
    }

    const pageRows = body.results
    if (!Number.isSafeInteger(body.count) || body.count < 0 || body.count > MAX_ITEMS ||
        !Object.prototype.hasOwnProperty.call(body, 'next') ||
        (body.next !== null && typeof body.next !== 'string') || pageRows.length > PAGE_SIZE) {
      throw new Error('Invalid scene data pagination metadata')
    }
    if (expectedCount === undefined) expectedCount = body.count
    else if (body.count !== expectedCount) throw new Error('Scene data count changed during pagination')
    if (pageRows.length + rows.length > MAX_ITEMS) throw new Error('Scene data page limit exceeded')
    rows.push(...pageRows)
    if (body.count < rows.length) throw new Error('Invalid scene data count')

    const hasNext = body.next !== null && body.next !== ''
    if (hasNext) {
      if (page === MAX_PAGES || rows.length >= MAX_ITEMS) throw new Error('Scene data page limit exceeded')
      continue
    }
    if (body.count !== rows.length) throw new Error('Incomplete scene data pages')
    return rows
  }
  throw new Error('Scene data page limit exceeded')
}

export function getSceneDatasets(project) {
  return request({ url: '/app-automation/scene-datasets/', method: 'get', params: { project } })
}

export function getAllSceneDatasets(project) {
  return getAllPages('/app-automation/scene-datasets/', project)
}

export function uploadSceneDataset(data) {
  return request({
    url: '/app-automation/scene-datasets/', method: 'post', data, timeout: 30000,
    headers: { 'Content-Type': undefined },
  })
}

export function getProjectComponents(project) {
  return request({ url: '/app-automation/project-components/', method: 'get', params: { project } })
}

export function getAllProjectComponents(project) {
  return getAllPages('/app-automation/project-components/', project)
}

export function createProjectComponent(data) {
  return request({ url: '/app-automation/project-components/', method: 'post', data })
}

export function executeSceneDataset(caseId, data) {
  return request({ url: `/app-automation/test-cases/${caseId}/execute-dataset/`, method: 'post', data })
}
