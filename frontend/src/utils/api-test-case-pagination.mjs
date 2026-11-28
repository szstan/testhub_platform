export async function fetchAllApiPages(get, url, params = {}, options = {}) {
  const pageSize = options.pageSize || 100
  const maxPages = options.maxPages || 1000
  const rows = new Map()

  for (let page = 1; page <= maxPages; page += 1) {
    const response = await get(url, { params: { ...params, page, page_size: pageSize } })
    const data = response?.data ?? response
    if (Array.isArray(data)) return data
    if (!data || !Array.isArray(data.results)) throw new Error('Invalid paginated response')

    const before = rows.size
    for (const item of data.results) {
      const key = item?.id ?? `${page}:${rows.size}`
      rows.set(key, item)
    }
    const expectedCount = Number(data.count)
    const needsMore = Boolean(data.next) || (Number.isFinite(expectedCount) && rows.size < expectedCount)
    if (!needsMore) return [...rows.values()]
    if (!data.results.length || rows.size === before) throw new Error('Pagination did not advance')
  }

  throw new Error('Pagination limit reached')
}
