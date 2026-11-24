import { getDeviceList } from './app-automation'

const PAGE_SIZE = 100
const MAX_PAGES = 100
const MAX_DEVICES = PAGE_SIZE * MAX_PAGES

const unwrapPage = response => {
  let body = response?.data ?? response
  if (body?.success === false) throw new Error('Device list request failed')
  if (body && !Array.isArray(body) && body.results === undefined && body.data !== undefined) body = body.data
  return body
}

const validateRows = (items, seen) => {
  if (!Array.isArray(items)) throw new Error('Invalid device list')
  for (const item of items) {
    if (!item || typeof item !== 'object' || item.id === null || item.id === undefined) {
      throw new Error('Invalid device identity')
    }
    const id = String(item.id)
    if (!/^[1-9][0-9]*$/.test(id) || seen.has(id)) throw new Error('Device pages changed; refresh the list')
    seen.add(id)
  }
}

// Never follow the server's next URL: requests stay on the fixed API route.
// Stable ordering and an authenticated roster fingerprint prevent balanced
// deletion/insertion from silently shifting the next page's device identities.
export async function getAllAppDevices(params = {}) {
  const rows = []
  const seen = new Set()
  let expectedCount
  let expectedRoster
  for (let page = 1; page <= MAX_PAGES; page += 1) {
    const query = { ...params, consistent: 1, ordering: 'id', page, page_size: PAGE_SIZE }
    if (expectedRoster === undefined) delete query.expected_roster
    else query.expected_roster = expectedRoster
    const body = unwrapPage(await getDeviceList(query))
    if (Array.isArray(body)) {
      if (page !== 1 || body.length > MAX_DEVICES) throw new Error('Device list exceeds its bounded limit')
      validateRows(body, seen)
      return body
    }
    if (!body || !Array.isArray(body.results) || !Number.isSafeInteger(body.count)
        || body.count < 0 || body.count > MAX_DEVICES || body.results.length > PAGE_SIZE
        || !Object.prototype.hasOwnProperty.call(body, 'next')
        || (body.next !== null && typeof body.next !== 'string')
        || typeof body.roster_version !== 'string' || !/^[0-9a-f]{64}$/.test(body.roster_version)) {
      throw new Error('Invalid device pagination')
    }
    if (expectedCount === undefined) expectedCount = body.count
    if (expectedRoster === undefined) expectedRoster = body.roster_version
    if (body.count !== expectedCount || body.roster_version !== expectedRoster) {
      throw new Error('Device pages changed; refresh the list')
    }
    validateRows(body.results, seen)
    rows.push(...body.results)
    if (rows.length > expectedCount) throw new Error('Invalid device pagination count')
    const hasNext = body.next !== null && body.next !== ''
    if (!hasNext) {
      if (rows.length !== expectedCount) throw new Error('Incomplete device pages')
      return rows
    }
    if (!body.results.length || rows.length >= expectedCount || page === MAX_PAGES) {
      throw new Error('Incomplete device pages')
    }
  }
  throw new Error('Device list exceeds its bounded limit')
}
